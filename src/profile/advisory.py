"""Advisory messages for profile mutations — never blocking (Requirement 8).

`target_tracks` is intent, not history: it is explicitly allowed to outrun
the evidence, so this module names the downstream consequence and lets the
human decide. It never gates the write. It makes no LLM or embedding call
(out of scope for this layer — see .agent/profile.md "What the layer is NOT"),
so it cannot judge whether a track is *semantically* supported by a skill; it
can only report the profile's evidence coverage mechanically.
"""

from __future__ import annotations

from profile.lookup import items_demonstrating
from profile.schema import AddItem, AddTargetTrack, Profile, ProfileOp


def advise(op: ProfileOp, p: Profile) -> str | None:
    if isinstance(op, AddTargetTrack):
        return _advise_add_target_track(op, p)
    if isinstance(op, AddItem):
        return _advise_add_item(op)
    return None


def _advise_add_target_track(op: AddTargetTrack, p: Profile) -> str:
    with_evidence = [s for s in p.skills if items_demonstrating(p, s.id)]
    return (
        f"Adding '{op.track}' as a target track. The scorer builds one vector "
        f"from the whole profile text, so jobs found on this track are scored "
        f"against ALL of it, not just this track. The profile currently has "
        f"{len(with_evidence)}/{len(p.skills)} skill(s) backed by evidence "
        f"(tagged on at least one item). If this track's relevant skills are "
        f"thin or untagged, expect low scores that may not clear the gate. "
        f"Add anyway?"
    )


def _advise_add_item(op: AddItem) -> str | None:
    if op.item.demonstrated_skills:
        return None
    return (
        f"'{op.item.title}' has no demonstrated_skills tagged. Tailoring can "
        f"still rephrase its bullets, but cannot surface any skill on this item "
        f"until it is tagged. Add anyway?"
    )
