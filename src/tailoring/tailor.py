"""Tailoring layer — public entry point (tailoring_build.md WP4).

`tailor()` is the ONLY supported entry point into this layer: LLM call →
schema validation → truthfulness guards → render, single pass, no retry
(tailoring.md §5). It performs no database read or write of any kind —
registering the artifact and advancing the job's state are the caller's job
(the "Caller contract" in tailoring_build.md). That boundary is what lets
this whole layer be tested with a mock LLM and no backend at all.

Every failure mode collapses to one exception, `TailoringError`, carrying a
`reason` the caller can branch on and, for guard failures, the list of
violations that tripped it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ValidationError

from profile.schema import Profile
from tailoring.guards import check_no_new_specifics, check_skill_subset
from tailoring.prompt import LLMTailor, call_llm_tailor
from tailoring.render import RenderError, _render_tex_source, render
from tailoring.schema import TailoredSelection


class ArtifactResult(BaseModel):
    """Names the file `tailor()` produced. Deliberately its own tiny model,
    not `app.models.job.ArtifactCreate` — this layer doesn't import from
    `app` at all, so the caller is the one place that translates this into
    whatever the backend's artifact-registration call expects."""

    kind: str = "cv_pdf"
    path: Path


class TailoringError(Exception):
    """The one exception this layer raises. `reason` is a stable string a
    caller can branch on: 'llm_call_failed', 'schema_invalid',
    'guard_violation', or 'render_failed'. `violations` is populated only
    for 'guard_violation'."""

    def __init__(
        self, reason: str, *, violations: list[str] | None = None, message: str | None = None
    ):
        self.reason = reason
        self.violations = violations or []
        super().__init__(message or reason)


def save_debug_artifact(tex_source: str, output_dir: str | Path, enabled: bool) -> Path | None:
    """Writes the intermediate `.tex` into a `debug/` folder beside the
    output when `enabled` — local-iteration aid, off by default (§6)."""
    if not enabled:
        return None
    debug_dir = Path(output_dir) / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / "cv.tex"
    debug_path.write_text(tex_source)
    return debug_path


def _collect_guard_violations(selection: TailoredSelection, profile: Profile) -> list[str]:
    """Runs both guards over every tailored item. `selection` was validated
    against `profile` in `call_llm_tailor`, so every `ref_id` here is
    guaranteed to resolve — a guard violation can only mean the LLM's TEXT
    misbehaved, never that a reference is dangling."""
    violations: list[str] = []
    source_by_id = {item.id: item for item in profile.items}
    skills_by_id = {skill.id: skill for skill in profile.skills}

    for tailored_item in (*selection.experiences, *selection.projects):
        source_item = source_by_id[tailored_item.ref_id]

        for skill_id in check_skill_subset(tailored_item, source_item, profile.skills):
            violations.append(f"{tailored_item.ref_id}: unauthorized skill '{skill_id}'")

        # Surfacing an AUTHORIZED skill's name is the whole point of §3, even
        # when that exact word isn't in the source bullet text — so the
        # no-new-specifics diff must treat authorized skill surfaces as
        # already "known", or it would flag every legitimate skill mention
        # as a fabricated named entity.
        authorized_surfaces = [
            surface
            for skill_id in source_item.demonstrated_skills
            for surface in skills_by_id[skill_id].surfaces()
        ]
        tailored_text = " ".join(tailored_item.bullets)
        source_text = " ".join([*source_item.bullets, *authorized_surfaces])
        for specific in check_no_new_specifics(tailored_text, source_text):
            violations.append(f"{tailored_item.ref_id}: {specific}")

    return violations


async def tailor(
    job_description: str,
    profile: Profile,
    *,
    llm: LLMTailor,
    template_path: str | Path,
    output_dir: str | Path,
    save_debug_artifacts: bool = False,
) -> ArtifactResult:
    output_dir = Path(output_dir)
    template_path = Path(template_path)

    try:
        selection = await call_llm_tailor(job_description, profile, llm)
    except (json.JSONDecodeError, ValidationError) as e:
        raise TailoringError("schema_invalid", message=str(e)) from e
    except Exception as e:
        # The LLM call itself blew up (rate limit, network, ...) rather
        # than returning something we could parse or validate.
        raise TailoringError("llm_call_failed", message=str(e)) from e

    violations = _collect_guard_violations(selection, profile)
    if violations:
        logger.critical(f"Tailoring guard violation(s) for jd={job_description[:80]!r}: {violations}")
        raise TailoringError("guard_violation", violations=violations)

    if save_debug_artifacts:
        tex_source = _render_tex_source(selection, profile, template_path=template_path)
        save_debug_artifact(tex_source, output_dir, enabled=True)

    try:
        # render() shells out to tectonic via a BLOCKING subprocess call —
        # left on the event loop's own thread, a multi-second PDF compile
        # would stall every other coroutine in the process (FastAPI
        # requests, the scheduler, the Telegram bot's polling). to_thread
        # offloads it to a worker thread instead; Python's default executor
        # already bounds how many run concurrently, so no queue of our own
        # is needed here — how many jobs to tailor at once is a scheduler
        # policy decision, not this layer's to make.
        pdf_path = await asyncio.to_thread(
            render, selection, profile, template_path=template_path, output_dir=output_dir
        )
    except RenderError as e:
        raise TailoringError("render_failed", message=str(e)) from e

    return ArtifactResult(kind="cv_pdf", path=pdf_path)
