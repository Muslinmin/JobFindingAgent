from profile.advisory import advise
from profile.schema import (
    AddItem,
    AddTargetTrack,
    EditBullets,
    Profile,
    ProfileItem,
    Skill,
)


def _profile(**overrides) -> Profile:
    base = dict(
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
    )
    base.update(overrides)
    return Profile(**base)


def test_add_target_track_always_returns_an_advisory_and_never_blocks():
    p = _profile()
    op = AddTargetTrack(op="add_target_track", track="robotics QA")
    advisory = advise(op, p)
    assert advisory is not None
    assert "robotics QA" in advisory


def test_add_target_track_advisory_names_the_scoring_consequence():
    p = _profile()
    op = AddTargetTrack(op="add_target_track", track="backend engineering")
    advisory = advise(op, p)
    assert "scor" in advisory.lower()


def test_add_item_with_no_demonstrated_skills_returns_advisory():
    p = _profile()
    op = AddItem(
        op="add_item",
        kind="project",
        item=ProfileItem(id="proj_new", title="New Project", bullets=["Built it."]),
    )
    advisory = advise(op, p)
    assert advisory is not None
    assert "New Project" in advisory


def test_add_item_with_demonstrated_skills_returns_no_advisory():
    p = _profile()
    op = AddItem(
        op="add_item",
        kind="project",
        item=ProfileItem(
            id="proj_new",
            title="New Project",
            bullets=["Built it."],
            demonstrated_skills=["python"],
        ),
    )
    assert advise(op, p) is None


def test_ops_without_advisory_return_none():
    p = _profile()
    op = EditBullets(op="edit_bullets", item_id="exp_1", bullets=["Rewritten bullet."])
    assert advise(op, p) is None
