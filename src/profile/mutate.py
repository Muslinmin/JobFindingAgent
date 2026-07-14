"""The typed mutator — the SOLE writer of profile.json.

No other code path is permitted to write that file (Requirement 6). Every
write goes through a `ProfileOp`, so list semantics (append vs. replace) are
unambiguous at the type level rather than by convention.

Write sequencing: unconfirmed calls compute a diff and advisory and touch
nothing on disk; only a confirmed call backs up and writes
(.agent/profile.md "Write sequencing — profile first, queries second"). This
module does not regenerate search_queries.json itself — coupling a pure file
operation to the LLM layer would mean a profile edit could fail because an
LLM call is down. It only reports `queries_stale` so the caller knows to.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from profile.advisory import advise
from profile.invariants import ProfileInvariantError, validate_invariants
from profile.schema import (
    AddItem,
    AddSkill,
    AddTargetTrack,
    EditBullets,
    Profile,
    ProfileOp,
    QUERY_AFFECTING_OPS,
    RemoveSkill,
    RemoveTargetTrack,
    TagSkill,
)

_backup_counter = 0


class UpdateResult(BaseModel):
    ok: bool
    changed: bool
    pending_confirmation: bool = False
    diff: str | None = None
    advisory: str | None = None
    summary: str | None = None
    queries_stale: bool = False


def update_profile(op: ProfileOp, confirmed: bool, path: str | Path) -> UpdateResult:
    path = Path(path)
    current = Profile.model_validate(json.loads(path.read_text()))

    try:
        # An op that would break an invariant is rejected BEFORE the file is
        # touched — e.g. add_skill naming a demonstrated_by item id that
        # doesn't exist, or tag_skill/edit_bullets naming an unknown item_id.
        updated = _apply(op, current)
        validate_invariants(updated)
    except ProfileInvariantError as e:
        return UpdateResult(ok=False, changed=False, summary=str(e))

    if not confirmed:
        return UpdateResult(
            ok=True,
            changed=False,
            pending_confirmation=True,
            diff=_diff(current, updated),
            advisory=advise(op, updated),
        )

    _backup(path, current)
    path.write_text(json.dumps(updated.model_dump(mode="json"), indent=2))

    return UpdateResult(
        ok=True,
        changed=True,
        summary=f"Applied '{op.op}'",
        queries_stale=op.op in QUERY_AFFECTING_OPS,
    )


def _apply(op: ProfileOp, p: Profile) -> Profile:
    """Every operation is a full-field replacement at the point of write — the
    diff is what the human approves, and a diff of a full replacement is
    unambiguous to read (unlike an append-semantics diff, which hides what is
    being appended to)."""
    new = p.model_copy(deep=True)

    if isinstance(op, AddTargetTrack):
        if op.track not in new.target_tracks:
            new.target_tracks = [*new.target_tracks, op.track]

    elif isinstance(op, RemoveTargetTrack):
        new.target_tracks = [t for t in new.target_tracks if t != op.track]

    elif isinstance(op, AddSkill):
        new.skills = [*new.skills, op.skill]
        for item_id in op.demonstrated_by:
            item = next((i for i in new.items if i.id == item_id), None)
            if item is None:
                raise ProfileInvariantError(
                    f"add_skill: no item with id '{item_id}' to tag"
                )
            if op.skill.id not in item.demonstrated_skills:
                item.demonstrated_skills.append(op.skill.id)

    elif isinstance(op, RemoveSkill):
        # CASCADES: stripped from the superset AND from every item's
        # demonstrated_skills. Not cascading would break invariant 4 on the
        # next load.
        new.skills = [s for s in new.skills if s.id != op.skill_id]
        for item in new.items:
            item.demonstrated_skills = [
                s for s in item.demonstrated_skills if s != op.skill_id
            ]

    elif isinstance(op, AddItem):
        if op.kind == "experience":
            new.experiences = [*new.experiences, op.item]
        else:
            new.projects = [*new.projects, op.item]

    elif isinstance(op, EditBullets):
        item = next((i for i in new.items if i.id == op.item_id), None)
        if item is None:
            raise ProfileInvariantError(f"edit_bullets: no item with id '{op.item_id}'")
        item.bullets = op.bullets

    elif isinstance(op, TagSkill):
        item = next((i for i in new.items if i.id == op.item_id), None)
        if item is None:
            raise ProfileInvariantError(f"tag_skill: no item with id '{op.item_id}'")
        for sid in op.skill_ids:
            if sid not in item.demonstrated_skills:
                item.demonstrated_skills.append(sid)

    return new


def _diff(before: Profile, after: Profile) -> str:
    before_json = json.dumps(before.model_dump(mode="json"), indent=2, sort_keys=True)
    after_json = json.dumps(after.model_dump(mode="json"), indent=2, sort_keys=True)
    return "".join(
        difflib.unified_diff(
            before_json.splitlines(keepends=True),
            after_json.splitlines(keepends=True),
            fromfile="current",
            tofile="updated",
        )
    )


def _backup(path: Path, profile: Profile) -> None:
    global _backup_counter
    backup_dir = path.parent / "profiles" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")
    seq = _backup_counter % 1000
    _backup_counter += 1
    dest = backup_dir / f"profile_{timestamp}_{seq}.json"
    dest.write_text(json.dumps(profile.model_dump(mode="json"), indent=2))
