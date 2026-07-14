"""Structural invariants — enforced at LOAD, not at use.

Tailoring's guards assume every ref_id resolves and every demonstrated skill
exists. Checking that HERE collapses an ambiguity: a guard violation at
tailor time then means exactly one thing — the LLM misbehaved.
"""

from __future__ import annotations

from profile.schema import Profile, Skill


class ProfileInvariantError(ValueError):
    """A structural guarantee the rest of the system relies on has been broken."""


def validate_invariants(p: Profile) -> None:
    # 1. Item ids unique across experiences AND projects (one flat ref_id namespace)
    ids = [i.id for i in p.items]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ProfileInvariantError(f"Duplicate item ids: {sorted(dupes)}")

    # 2. Skill ids unique
    sids = [s.id for s in p.skills]
    if dupes := {s for s in sids if sids.count(s) > 1}:
        raise ProfileInvariantError(f"Duplicate skill ids: {sorted(dupes)}")

    # 3. No surface string claimed by two skills. Otherwise resolve_surface() is
    #    non-deterministic and the tailorer cannot know which competence a JD
    #    keyword refers to.
    seen: dict[str, str] = {}
    for s in p.skills:
        for surface in s.surfaces():
            key = surface.strip().lower()
            if key in seen and seen[key] != s.id:
                raise ProfileInvariantError(
                    f"Surface '{surface}' claimed by both '{seen[key]}' and '{s.id}'"
                )
            seen[key] = s.id

    # 4. Every demonstrated skill id resolves. A dangling id would cause the
    #    tailoring text guard to reject legitimate output.
    known = set(sids)
    for item in p.items:
        if unknown := [s for s in item.demonstrated_skills if s not in known]:
            raise ProfileInvariantError(
                f"Item '{item.id}' names unknown skill ids: {unknown}"
            )


def orphan_skills(p: Profile) -> list[Skill]:
    """Skills no item demonstrates.

    LEGAL, and deliberately NOT an invariant: such a skill may still be listed
    on a CV, it simply can never be woven into a bullet. In practice an orphan
    usually means AN ITEM IS MISSING from the profile, not that the skill is
    false. Surfaced for human review, never raised.
    """
    demonstrated = {s for i in p.items for s in i.demonstrated_skills}
    return [s for s in p.skills if s.id not in demonstrated]
