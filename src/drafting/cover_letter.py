"""Cover letter drafting — prose plus guards (agent_v2.md §7).

Specced in `architecture_v2.md` (`cover-letter-generator` feeds
`draft_cover_letter`) and never built until now: JD + profile → 250–400
words, persisted as a plain-text `cover_letter` artifact. Like `tailoring/`
this layer touches no database — the caller registers the artifact — and
like `tailoring/` it loads its domain-knowledge prompt itself, so the
playbook text is paid for only on a cover-letter call and never on every
chat turn.

**Why the guard is weaker here than on the CV, and how much.** The CV path
has real teeth: `check_skill_subset` diffs each tailored bullet against
that item's authored `demonstrated_skills`, so a fabricated *attribution*
is caught structurally. A cover letter is free prose over the whole
profile — there are no `ref_id`s, so there is nothing to diff per item, and
the per-item guard simply does not apply. What survives is
`check_no_new_specifics`: new numerals and new named entities.

That guard is only worth anything because of what `_source_text` does and
does not include. It includes the profile and the job's **company and role**
— the letter legitimately names its employer — and it deliberately excludes
the **JD body**. That exclusion is the whole mechanism: a technology named
in the posting but absent from the profile ("we use Kubernetes") shows up
in the letter as a capitalised token with no source, and gets flagged. Feed
the JD in as source text and that check goes silent exactly when it matters
most, because everything the posting mentions becomes "known".

A known limit, stated rather than glossed: this catches fabricated
specifics, not fabricated *relationships*. "Led the team on project X" when
X was not a leadership role uses only words the profile contains, and no
check here will see it. The prompt forbids it; nothing enforces it. The CV
path's `demonstrated_skills` is what closes that hole, and a cover letter
has no equivalent to close it with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from loguru import logger

from drafting.followup import DraftLLM
from profile.schema import BODY, INDEX, Profile, field_tier
from tailoring.guards import check_no_new_specifics

_DOMAIN_KNOWLEDGE_PATH = Path(__file__).parent / "prompts" / "cover_letter.md"

# Three attempts total, matching tailoring/tailor.py. A letter is one cheap
# completion, and the alternative to retrying is refusing a request the
# model very likely gets right on the next pass.
MAX_GUARD_RETRIES: Final[int] = 2


class DraftingError(Exception):
    """The one exception this layer raises, mirroring `TailoringError`.
    `reason` is a stable string the caller branches on — 'llm_call_failed'
    or 'guard_violation' — and `violations` is populated only for the
    latter."""

    def __init__(
        self, reason: str, *, violations: list[str] | None = None, message: str | None = None
    ):
        self.reason = reason
        self.violations = violations or []
        super().__init__(message or reason)


def _content_for_prompt(profile: Profile) -> dict:
    """BODY + INDEX tier fields only — the same structural exclusion
    `tailoring/prompt.py` applies. The LLM writing the letter has no need
    for the candidate's phone number or links, so it never sees them."""
    cls = type(profile)
    return {
        name: getattr(profile, name)
        for name in cls.model_fields
        if field_tier(cls, name) in (BODY, INDEX)
    }


def _source_text(profile: Profile, company: str, role: str) -> str:
    """Everything the letter is permitted to assert, as one blob for the
    no-new-specifics diff.

    The employer's name and the role title are in here because the letter
    must be able to say them. The JD body is *not* — see this module's
    docstring; excluding it is what lets the guard catch a skill lifted
    from the posting that the candidate does not have.

    Identity fields the letter legitimately mentions (the candidate's own
    name, their school) are included even though the LLM never receives
    them in the prompt: the guard's job is to decide what may appear in the
    output, which is a wider set than what goes into the input.
    """
    parts: list[str] = [company, role, profile.name]

    if profile.summary_seed:
        parts.append(profile.summary_seed)
    parts.extend(profile.target_tracks)

    for education in profile.education:
        parts.extend(p for p in (education.institution, education.degree) if p)

    for item in profile.items:
        parts.extend(p for p in (item.title, item.organization) if p)
        parts.extend(item.bullets)

    for skill in profile.skills:
        parts.extend(skill.surfaces())

    return " ".join(parts)


def assemble_cover_letter_prompt(
    jd: str,
    profile: Profile,
    company: str,
    role: str,
    *,
    note: str | None = None,
    previous_violations: list[str] | None = None,
) -> str:
    content = _content_for_prompt(profile)
    profile_json = json.dumps(
        {
            "summary_seed": content.get("summary_seed"),
            "target_tracks": content.get("target_tracks", []),
            "experiences": [item.model_dump() for item in content.get("experiences", [])],
            "projects": [item.model_dump() for item in content.get("projects", [])],
            "skills": [skill.model_dump() for skill in content.get("skills", [])],
        },
        indent=2,
    )

    prompt = (
        f"{_DOMAIN_KNOWLEDGE_PATH.read_text()}\n\n"
        f"# The role\n{role} at {company}\n\n"
        f"# Job description\n{jd}\n\n"
        f"# Candidate profile (the only thing you may claim from)\n{profile_json}\n"
    )

    if note:
        prompt += (
            "\n# What the candidate asked for\n"
            "Follow this, but do not invent any fact it does not state:\n"
            f"{note}\n"
        )

    if previous_violations:
        listed = "\n".join(f"- {v}" for v in previous_violations)
        prompt += (
            "\n# Previous attempt flagged by the truthfulness checker\n"
            "Your last draft was rejected for the following issue(s):\n"
            f"{listed}\n\n"
            "Note: this checker can produce false positives — it does not know "
            "the job posting's own wording. Use your judgment: fix anything that "
            "genuinely is not supported by the profile, and leave accurate text "
            "alone.\n"
        )

    return prompt


async def draft_cover_letter(
    jd: str,
    profile: Profile,
    company: str,
    role: str,
    *,
    llm: DraftLLM,
    note: str | None = None,
) -> str:
    """Draft, check, retry up to `MAX_GUARD_RETRIES`, return the text.

    Raises `DraftingError('llm_call_failed')` if the client breaks, and
    `DraftingError('guard_violation')` carrying the surviving violations if
    the last attempt still asserts something the profile does not support.
    Returning a letter that failed the check is not an option — the whole
    point of the check is that the user should not have to fact-check their
    own cover letter.
    """
    source = _source_text(profile, company, role)
    previous_violations: list[str] | None = None

    for attempt in range(MAX_GUARD_RETRIES + 1):
        prompt = assemble_cover_letter_prompt(
            jd, profile, company, role, note=note, previous_violations=previous_violations
        )
        try:
            letter = await llm.complete(prompt)
        except Exception as e:
            raise DraftingError("llm_call_failed", message=str(e)) from e

        violations = check_no_new_specifics(letter, source)
        if not violations:
            return letter.strip()

        if attempt < MAX_GUARD_RETRIES:
            logger.warning(
                f"cover letter: guard violation(s) on attempt {attempt + 1}/"
                f"{MAX_GUARD_RETRIES + 1} for {role} at {company}: {violations}"
            )
            previous_violations = violations
            continue

        logger.error(f"cover letter: guard violation(s) for {role} at {company}: {violations}")
        raise DraftingError("guard_violation", violations=violations)
