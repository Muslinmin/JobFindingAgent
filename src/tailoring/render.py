"""Tailoring layer — deterministic renderer (tailoring_build.md WP5).

`build_render_model` merges identity(profile) + resolved(selection) into one
dict; `render` feeds it through a Jinja2 `.tex` template (custom delimiters
so Jinja's own `{{ }}` / `{% %}` never collide with LaTeX braces — the
template owns ALL LaTeX syntax) and compiles it with `tectonic`. Every
content string is passed through `latex_escape` before it reaches the
template, so the LLM can never inject layout through a bullet or a summary
(tailoring.md §6, invariant "LaTeX equivalent of invariant 2").

Internally split into `_render_tex_source` (pure templating) and
`_compile_tex` (the tectonic subprocess) so unit tests can exercise the
`.tex` output deterministically by patching `_compile_tex`, without needing
a real LaTeX toolchain installed — only TC-RENDER-08/09 touch the real
binary.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import jinja2

from profile.schema import IDENTITY, Profile, ProfileItem, RENDER, field_tier
from tailoring.schema import TailoredItem, TailoredSelection

_LATEX_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_LATEX_JINJA_OPTIONS = dict(
    block_start_string=r"\BLOCK{",
    block_end_string="}",
    variable_start_string=r"\VAR{",
    variable_end_string="}",
    comment_start_string=r"\#{",
    comment_end_string="}",
    # Deliberately no line_statement_prefix/line_comment_prefix: a plain
    # LaTeX comment (`%...`) is completely ordinary in this template, and
    # any prefix built on `%` collides with one sooner or later (a divider
    # comment like `%%%%%%` already did). \BLOCK{}/\VAR{} are the only
    # Jinja syntax this template ever needs.
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)


class RenderError(RuntimeError):
    """The tectonic compile step failed, timed out, or produced no PDF."""


def latex_escape(text: str) -> str:
    """Escape every LaTeX special char in a single pass over `text`, so a
    replacement's own backslash/brace is never re-escaped on a later
    iteration (which sequential str.replace calls would do)."""
    if not text:
        return ""
    return "".join(_LATEX_ESCAPE_MAP.get(ch, ch) for ch in text)


def _escape_strings(value):
    if isinstance(value, str):
        return latex_escape(value)
    if isinstance(value, list):
        return [_escape_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _escape_strings(v) for k, v in value.items()}
    return value


_BOLD_MARKER_RE = re.compile(r"\*\*(.+?)\*\*")


def _apply_bold_markers(text: str) -> str:
    """Convert the LLM's `**term**` emphasis convention into `\\textbf{}`,
    escaping each segment (plain and bold) independently so the escape pass
    and the markup conversion can never interfere with each other. This is
    the ONLY path that ever emits `\\textbf` — the LLM emits `**term**`,
    never LaTeX, preserving invariant 2 (tailoring.md §6)."""
    if not text:
        return ""
    parts: list[str] = []
    last_end = 0
    for match in _BOLD_MARKER_RE.finditer(text):
        parts.append(latex_escape(text[last_end:match.start()]))
        parts.append(r"\textbf{" + latex_escape(match.group(1)) + "}")
        last_end = match.end()
    parts.append(latex_escape(text[last_end:]))
    return "".join(parts)


def _display_link(url: str) -> str:
    """'https://www.github.com/x/' -> 'github.com/x' — the href target
    keeps the full URL; only the on-page text is shortened."""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def _resolved_item(ref_id: str, source_by_id: dict, tailored_by_ref: dict) -> dict:
    source: ProfileItem = source_by_id[ref_id]
    tailored: TailoredItem = tailored_by_ref[ref_id]
    return {
        "title": source.title,
        "organization": source.organization,
        "date_range": source.date_range,
        "bullets": tailored.bullets,
    }


def build_render_model(selection: TailoredSelection, profile: Profile) -> dict:
    """identity(profile) + resolved(selection), merged into one dict.

    Identity fields are copied verbatim from `profile` (byte-identical —
    invariant 4) except `links`, which gains a computed `display` string
    alongside the untouched `url`; nothing here escapes or reformats
    content. Escaping happens only later, as a separate pass right before
    templating.
    """
    cls = type(profile)
    identity = {
        name: getattr(profile, name)
        for name in cls.model_fields
        if field_tier(cls, name) in (IDENTITY, RENDER)
    }
    identity["education"] = [edu.model_dump() for edu in profile.education]
    identity["links"] = [{"url": link, "display": _display_link(link)} for link in profile.links]

    source_by_id = {item.id: item for item in profile.items}
    skills_by_id = {skill.id: skill for skill in profile.skills}
    experiences_by_ref = {item.ref_id: item for item in selection.experiences}
    projects_by_ref = {item.ref_id: item for item in selection.projects}

    # Group selected skills by their PROFILE-level category (a display
    # concern, authored on the Skill itself), preserving the LLM's
    # skill_order both within each group and across which group appears
    # first — the LLM still selects by id; this only changes how the
    # selection is laid out.
    grouped: dict[str, list[str]] = {}
    for skill_id in selection.skill_order:
        skill = skills_by_id.get(skill_id)
        if skill is None:
            continue
        grouped.setdefault(skill.category or "Other", []).append(skill.label)

    return {
        **identity,
        "summary": selection.summary,
        "experiences": [
            _resolved_item(ref_id, source_by_id, experiences_by_ref)
            for ref_id in selection.experience_order
        ],
        "projects": [
            _resolved_item(ref_id, source_by_id, projects_by_ref)
            for ref_id in selection.project_order
        ],
        "skill_categories": [
            {"label": label, "skills": skills} for label, skills in grouped.items()
        ],
    }


def _render_tex_source(selection: TailoredSelection, profile: Profile, *, template_path: Path) -> str:
    model = build_render_model(selection, profile)
    escaped = _escape_strings(model)

    # Bold-marker parsing is the one place that deviates from plain
    # escaping — it only applies to the LLM's own free text (summary and
    # bullets), never to profile-sourced fields (titles, orgs, dates,
    # identity, skill labels), which the generic pass above already covers.
    if model.get("summary"):
        escaped["summary"] = _apply_bold_markers(model["summary"])
    for section in ("experiences", "projects"):
        for escaped_item, raw_item in zip(escaped[section], model[section]):
            escaped_item["bullets"] = [_apply_bold_markers(b) for b in raw_item["bullets"]]

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_path.parent), **_LATEX_JINJA_OPTIONS
    )
    template = env.get_template(template_path.name)
    return template.render(**escaped)


def _compile_tex(tex_path: Path, output_dir: Path, *, timeout: int = 60) -> Path:
    try:
        subprocess.run(
            ["tectonic", "--outdir", str(output_dir), str(tex_path)],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RenderError("tectonic is not installed / not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"tectonic timed out after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        raise RenderError(f"tectonic failed (exit {e.returncode}): {stderr}") from e

    pdf_path = output_dir / f"{tex_path.stem}.pdf"
    if not pdf_path.exists():
        raise RenderError(f"tectonic exited 0 but produced no PDF at {pdf_path}")
    return pdf_path


def render(
    selection: TailoredSelection,
    profile: Profile,
    *,
    template_path: str | Path,
    output_dir: str | Path,
) -> Path:
    """Write the tailored CV's `.tex` source and compile it to a PDF.
    Returns the PDF's path; raises `RenderError` on any compile failure."""
    template_path = Path(template_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tex_source = _render_tex_source(selection, profile, template_path=template_path)
    tex_path = output_dir / "cv.tex"
    tex_path.write_text(tex_source)

    return _compile_tex(tex_path, output_dir)
