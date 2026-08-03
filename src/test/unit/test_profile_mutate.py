import json

import pytest

from profile.mutate import update_profile
from profile.schema import (
    AddSkill,
    AddTargetTrack,
    EditBullets,
    Profile,
    ProfileItem,
    RemoveSkill,
    Skill,
)


def _seed_profile(tmp_path):
    profile = Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Did a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
        target_tracks=["robotics"],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile.model_dump(mode="json"), indent=2))
    return path


def test_add_target_track_appends_and_existing_tracks_survive(tmp_path):
    path = _seed_profile(tmp_path)
    op = AddTargetTrack(op="add_target_track", track="mechatronics")
    update_profile(op, confirmed=True, path=path)

    written = Profile.model_validate(json.loads(path.read_text()))
    assert written.target_tracks == ["robotics", "mechatronics"]


def test_remove_skill_cascades_to_every_items_demonstrated_skills(tmp_path):
    path = _seed_profile(tmp_path)
    op = RemoveSkill(op="remove_skill", skill_id="python")
    update_profile(op, confirmed=True, path=path)

    written = Profile.model_validate(json.loads(path.read_text()))
    assert written.skills == []
    assert written.experiences[0].demonstrated_skills == []


def test_edit_bullets_replaces_in_full(tmp_path):
    path = _seed_profile(tmp_path)
    op = EditBullets(op="edit_bullets", item_id="exp_1", bullets=["Rewritten bullet."])
    update_profile(op, confirmed=True, path=path)

    written = Profile.model_validate(json.loads(path.read_text()))
    assert written.experiences[0].bullets == ["Rewritten bullet."]


def test_unconfirmed_op_writes_nothing_to_disk(tmp_path):
    path = _seed_profile(tmp_path)
    before = path.read_text()
    op = AddTargetTrack(op="add_target_track", track="mechatronics")

    result = update_profile(op, confirmed=False, path=path)

    assert result.pending_confirmation is True
    assert result.changed is False
    assert path.read_text() == before


def test_confirmed_op_backs_up_before_overwrite(tmp_path):
    path = _seed_profile(tmp_path)
    op = AddTargetTrack(op="add_target_track", track="mechatronics")

    update_profile(op, confirmed=True, path=path)

    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 1
    backed_up = Profile.model_validate(json.loads(backups[0].read_text()))
    assert backed_up.target_tracks == ["robotics"]  # pre-write state


@pytest.mark.parametrize(
    "op, expected_stale",
    [
        (AddTargetTrack(op="add_target_track", track="mechatronics"), True),
        (RemoveSkill(op="remove_skill", skill_id="python"), True),
        (EditBullets(op="edit_bullets", item_id="exp_1", bullets=["New."]), False),
    ],
)
def test_queries_stale_flag_matches_query_affecting_ops(tmp_path, op, expected_stale):
    path = _seed_profile(tmp_path)
    result = update_profile(op, confirmed=True, path=path)
    assert result.queries_stale is expected_stale


def test_advisory_returned_for_add_target_track_and_write_still_succeeds(tmp_path):
    path = _seed_profile(tmp_path)
    op = AddTargetTrack(op="add_target_track", track="unsupported track")

    unconfirmed = update_profile(op, confirmed=False, path=path)
    assert unconfirmed.advisory is not None

    confirmed = update_profile(op, confirmed=True, path=path)
    assert confirmed.ok is True
    assert confirmed.changed is True


def test_op_that_would_break_an_invariant_is_rejected_before_write(tmp_path):
    path = _seed_profile(tmp_path)
    before = path.read_text()
    op = AddSkill(op="add_skill", skill=Skill(id="rust", label="Rust"), demonstrated_by=["no_such_item"])

    result = update_profile(op, confirmed=True, path=path)

    assert result.ok is False
    assert path.read_text() == before  # nothing written
    assert list((tmp_path / "profiles" / "backups").glob("*")) == []


def test_add_skill_with_existing_id_raises_invariant(tmp_path):
    path = _seed_profile(tmp_path)
    op = AddSkill(op="add_skill", skill=Skill(id="python", label="Python (dup)"))

    result = update_profile(op, confirmed=True, path=path)

    assert result.ok is False
