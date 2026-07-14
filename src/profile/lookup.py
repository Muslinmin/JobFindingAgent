"""Lookups — back the agent's reference resolution (agent_v2.md §7)."""

from __future__ import annotations

from difflib import SequenceMatcher

from profile.schema import Profile, ProfileItem, Skill


def get_item(p: Profile, item_id: str) -> ProfileItem | None:
    """Exact lookup by id. For constructing ops, not for resolving phrases."""
    return next((i for i in p.items if i.id == item_id), None)


def get_skill(p: Profile, skill_id: str) -> Skill | None:
    """Exact lookup by id."""
    return next((s for s in p.skills if s.id == skill_id), None)


def resolve_surface(p: Profile, surface: str) -> Skill | None:
    """Map ANY surface spelling ('RESTful API', 'REST') to its one Skill.

    This is what collapses the ATS variants: three spellings, one competence.
    Used by the tailorer to decide which spelling a job description is asking
    for, and by the text guard to check a surfaced term against the item's
    permission list.
    """
    lowered = surface.strip().lower()
    return next(
        (s for s in p.skills if any(x.lower() == lowered for x in s.surfaces())),
        None,
    )


def _score(needle: str, hay: str) -> float:
    n, h = needle.strip().lower(), hay.strip().lower()
    if not h:
        return 0.0
    if n == h:
        return 1.0
    if n in h:
        return 0.9
    return SequenceMatcher(None, n, h).ratio()


def find_skills(p: Profile, phrase: str, threshold: float = 0.6) -> list[Skill]:
    """Fuzzy lookup for reference resolution. Matches label AND aliases.

    Returns a LIST, never a single best guess. agent_v2.md §7 requires
    *salient = exactly one candidate*; zero or many means CLARIFY, NEVER ASSUME
    (no recency-pick, no position-pick). Collapsing to one guess here would
    silently destroy that rule, so the 0/1/N decision is handed to the LLM with
    every candidate intact.
    """
    scored = [(max(_score(phrase, x) for x in s.surfaces()), s) for s in p.skills]
    return [s for sc, s in sorted(scored, key=lambda t: -t[0]) if sc >= threshold]


def find_items(p: Profile, phrase: str, threshold: float = 0.6) -> list[ProfileItem]:
    """Fuzzy lookup over item titles and organizations. Same 0/1/N contract."""
    scored = [
        (max(_score(phrase, i.title), _score(phrase, i.organization or "")), i)
        for i in p.items
    ]
    return [i for sc, i in sorted(scored, key=lambda t: -t[0]) if sc >= threshold]


def items_demonstrating(p: Profile, skill_id: str) -> list[ProfileItem]:
    """Which items may legitimately surface this skill."""
    return [i for i in p.items if skill_id in i.demonstrated_skills]
