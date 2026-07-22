"""Query regen job (scheduling_v2.md WP-S6).

Weekly (Monday, before the daily scrape) and on-demand — the same callable
backs the agent's `regenerate_queries` tool (architecture_v2.md § 9). Loads
`profile.json` via `load_profile(profile_path)` (raises on missing/invalid,
per profile/loader.py's contract — never silently proceeds on a broken
profile), makes exactly one LLM call, and writes `search_queries.json` with
backup-on-change.

Consistent with the rest of the scheduler package (scheduler/jobs/scrape.py,
tailor.py): no function in this module carries a bare `Profile` instance —
`_assemble_query_regen_prompt` takes `profile_path` and calls `load_profile`
itself, the same "path in, load at the point of use" shape used everywhere
else. This job in particular may run months apart from the last
scrape/tailor run, so there is no "current" instance to reuse even if one
were threaded in from `SchedulerDeps` — loading fresh every call is simply
the only sensible option.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from loguru import logger

from app.config import Settings
from profile.loader import load_profile


class QueryRegenLLM(Protocol):
    """Text-in, text-out — same narrow shape as tailoring.prompt.LLMTailor
    and scheduler.jobs.follow_up.DraftLLM. `TaskLLMClient` satisfies all
    three structurally; there is deliberately no shared base Protocol so
    each module states only what it needs."""

    async def complete(self, prompt: str) -> str: ...


def _assemble_query_regen_prompt(profile_path: Path) -> str:
    """`load_profile(profile_path)` -> profile (skills + target_tracks, per
    architecture_v2.md § 1) -> prompt asking the LLM for a JSON list of
    search query strings."""
    profile = load_profile(profile_path)
    skills = ", ".join(skill.label for skill in profile.skills)
    tracks = ", ".join(profile.target_tracks)
    return (
        "Generate a JSON array of 5-10 concise job search query strings for "
        "this candidate, suitable for searching job portals (e.g. "
        '"backend engineer python"). Return ONLY a JSON array of strings — '
        "no markdown, no other text.\n\n"
        f"Skills: {skills}\n"
        f"Target tracks: {tracks}\n"
    )


def _write_with_backup(path: Path, content: str) -> bool:
    """Write `content` to `path` only if it differs from the existing file
    (or the file doesn't exist yet). Backs the old file up to
    `<path>.bak` before overwriting. Returns True if a write happened,
    False if the content was unchanged (idempotent no-op)."""
    path = Path(path)
    if path.exists():
        existing = path.read_text()
        if existing == content:
            return False
        path.with_suffix(path.suffix + ".bak").write_text(existing)
    path.write_text(content)
    return True


async def run_query_regen(
    llm: QueryRegenLLM,
    settings: Settings,
    profile_path: Path,
    queries_path: Path,
) -> None:
    """`_assemble_query_regen_prompt(profile_path)` raises `FileNotFoundError`
    if `profile.json` is missing — caught here, logged as a warning, and
    returned from without calling the LLM or touching `queries_path`.
    Otherwise: one LLM call -> `_write_with_backup`. Also the callable the
    agent's `regenerate_queries` tool imports directly (architecture_v2.md
    § 9's tools table)."""
    try:
        prompt = _assemble_query_regen_prompt(profile_path)
    except FileNotFoundError:
        logger.warning(f"query_regen: profile not found at {profile_path}, skipping")
        return

    try:
        raw = await llm.complete(prompt)
        queries = json.loads(raw)
        if not isinstance(queries, list):
            raise ValueError(f"query_regen: LLM did not return a JSON list, got {type(queries)}")
        content = json.dumps(queries, indent=2)
        written = _write_with_backup(queries_path, content)
        if written:
            logger.info(f"query_regen: wrote {len(queries)} queries to {queries_path}")
        else:
            logger.info("query_regen: queries unchanged, no write")
    except Exception:
        logger.exception("query_regen: failed to regenerate queries")
