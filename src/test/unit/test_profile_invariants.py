import pytest

from profile.invariants import ProfileInvariantError, orphan_skills, validate_invariants
from profile.schema import Profile, ProfileItem, Skill


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


def test_duplicate_item_id_across_experiences_and_projects_raises():
    p = _profile(
        projects=[ProfileItem(id="exp_1", title="Dup", bullets=["x"])],
    )
    with pytest.raises(ProfileInvariantError):
        validate_invariants(p)


def test_demonstrated_skill_absent_from_superset_raises():
    p = _profile(
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Did a thing."],
                demonstrated_skills=["not_a_real_skill"],
            )
        ],
    )
    with pytest.raises(ProfileInvariantError):
        validate_invariants(p)


def test_orphan_skill_does_not_raise_and_is_surfaced():
    p = _profile(
        skills=[Skill(id="python", label="Python"), Skill(id="rust", label="Rust")],
    )
    validate_invariants(p)  # does not raise
    orphans = orphan_skills(p)
    assert [s.id for s in orphans] == ["rust"]


def test_duplicate_skill_ids_raise():
    p = _profile(
        skills=[Skill(id="python", label="Python"), Skill(id="python", label="Python (alt)")],
    )
    with pytest.raises(ProfileInvariantError):
        validate_invariants(p)


def test_two_skills_claiming_same_surface_raises():
    p = _profile(
        skills=[
            Skill(id="rest_api", label="REST API", aliases=["REST"]),
            Skill(id="rest_something_else", label="REST"),
        ],
        experiences=[
            ProfileItem(id="exp_1", title="Engineer", bullets=["x"], demonstrated_skills=[])
        ],
    )
    with pytest.raises(ProfileInvariantError):
        validate_invariants(p)


def test_unknown_demonstrated_skill_id_raises():
    p = _profile(
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1", title="Engineer", bullets=["x"], demonstrated_skills=["ghost_skill"]
            )
        ],
    )
    with pytest.raises(ProfileInvariantError):
        validate_invariants(p)
