from pydantic import Field

from profile.projections import IDENTITY_CHAR_BUDGET, index_view, profile_summary, profile_to_text
from profile.schema import IDENTITY, Education, Profile, ProfileItem, Skill, tier


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        phone="+65 9000 0000",
        location="Singapore",
        candidate_status="Fresh graduate",
        summary_seed="A seed paragraph that should never leak into the agent's context.",
        skills=[Skill(id="python", label="Python", aliases=["Py"])],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Backend Engineer",
                organization="Acme",
                bullets=["A frozen factual bullet."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
        target_tracks=["backend engineering"],
    )
    base.update(overrides)
    return Profile(**base)


def test_profile_summary_includes_identity_verbatim():
    p = _profile()
    summary = profile_summary(p)
    assert "Jane Candidate" in summary
    assert "Singapore" in summary
    assert "Fresh graduate" in summary


def test_profile_summary_drops_body_and_render_tiers():
    p = _profile()
    summary = profile_summary(p)
    assert "seed paragraph" not in summary
    assert "frozen factual bullet" not in summary
    assert "jane@example.com" not in summary
    assert "+65 9000 0000" not in summary


def test_profile_summary_projects_index_collections_to_labels():
    p = _profile()
    summary = profile_summary(p)
    assert "Backend Engineer" in summary  # experience title, index tier
    assert "Python" in summary  # skill label, index tier
    assert "backend engineering" in summary  # target_tracks, index tier


def test_profile_summary_carries_index_ids_alongside_labels():
    """agent_v2.md §5: index tier projects to {id, label}, not label alone —
    reference resolution (§7) resolves a phrase to a skill_id, which the
    agent can only cite if the projection showed it."""
    p = _profile()
    summary = profile_summary(p)
    assert "python: Python" in summary
    assert "exp_1: Backend Engineer" in summary


def test_profile_summary_renders_id_less_index_items_as_bare_labels():
    # target_tracks are plain strings — no id to carry.
    p = _profile()
    summary = profile_summary(p)
    assert "target_tracks: backend engineering" in summary


def test_profile_summary_applies_char_budget_backstop_to_identity_scalars():
    oversized = "x" * (IDENTITY_CHAR_BUDGET + 1)
    p = _profile(candidate_status=oversized)
    summary = profile_summary(p)
    assert oversized not in summary


def test_profile_summary_has_no_hardcoded_field_list():
    """A new IDENTITY-tier field on the schema must appear in the projection
    without profile_summary being edited — it is a mechanical tier filter."""

    class ExtendedProfile(Profile):
        favorite_language: str = Field("Python", json_schema_extra=tier(IDENTITY))

    p = ExtendedProfile(name="Jane Candidate", email="jane@example.com")
    assert "favorite_language: Python" in profile_summary(p)


def test_profile_to_text_includes_target_tracks_and_excludes_contact_details():
    p = _profile()
    text = profile_to_text(p)
    assert "backend engineering" in text
    assert "jane@example.com" not in text
    assert "+65 9000 0000" not in text


def test_profile_to_text_includes_bullets_and_skills():
    p = _profile()
    text = profile_to_text(p)
    assert "A frozen factual bullet." in text
    assert "Python" in text
    assert "Py" in text  # alias surfaces too


def test_profile_summary_projects_education_to_its_own_identity_fields():
    p = _profile(
        education=[
            Education(
                id="edu_1",
                institution="Example Institute",
                degree="BEng Robotics",
                graduation_date="Jun 2026",
                date_range="2022 - 2026",  # render tier — must be dropped
                notes="First Class Honours",  # render tier — must be dropped
            )
        ],
    )
    summary = profile_summary(p)
    assert "Example Institute" in summary
    assert "BEng Robotics" in summary
    assert "Jun 2026" in summary
    assert "2022 - 2026" not in summary
    assert "First Class Honours" not in summary


def test_index_view_drops_aliases_and_bodies():
    p = _profile()
    view = index_view(p)
    assert view.skills[0].id == "python"
    assert view.skills[0].label == "Python"
    assert view.experiences[0].id == "exp_1"
    assert view.experiences[0].label == "Backend Engineer"
    assert view.target_tracks == ["backend engineering"]
