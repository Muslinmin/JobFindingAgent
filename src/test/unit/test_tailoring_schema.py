import pytest
from pydantic import ValidationError

from profile.schema import Profile, ProfileItem, Skill
from tailoring.schema import TailoredItem, TailoredSelection


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python"), Skill(id="fastapi", label="FastAPI")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Built a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[
            ProfileItem(
                id="proj_1",
                title="Side project",
                bullets=["Shipped a thing."],
                demonstrated_skills=["python", "fastapi"],
            )
        ],
    )
    base.update(overrides)
    return Profile(**base)


def _selection(**overrides) -> dict:
    base = dict(
        summary="A concise summary.",
        experience_order=["exp_1"],
        experiences=[{"ref_id": "exp_1", "bullets": ["Built a thing."]}],
        project_order=["proj_1"],
        projects=[{"ref_id": "proj_1", "bullets": ["Shipped a thing."]}],
        skill_order=["python"],
    )
    base.update(overrides)
    return base


# TC-SCHEMA-05 — valid selection, no identity fields on the model at all.
def test_valid_selection_constructs_with_no_identity_fields():
    selection = TailoredSelection(**_selection())
    identity_like = {"name", "email", "phone", "location", "links"}
    assert not identity_like & set(TailoredSelection.model_fields)
    assert selection.summary == "A concise summary."


# TC-SCHEMA-06 — a ref_id absent from the Profile raises when checked against it.
def test_unresolvable_ref_id_raises_against_profile():
    p = _profile()
    data = _selection(experiences=[{"ref_id": "not_a_real_id", "bullets": ["x"]}])
    with pytest.raises(ValidationError):
        TailoredSelection.model_validate(data, context={"profile": p})


# TC-SCHEMA-07 — a ref_id that does resolve passes.
def test_resolvable_ref_id_passes_against_profile():
    p = _profile()
    selection = TailoredSelection.model_validate(_selection(), context={"profile": p})
    assert selection.experiences[0].ref_id == "exp_1"


# TC-SCHEMA-08 — skill_order entry absent from Profile.skills raises.
def test_unknown_skill_in_skill_order_raises():
    p = _profile()
    data = _selection(skill_order=["python", "not_a_real_skill"])
    with pytest.raises(ValidationError):
        TailoredSelection.model_validate(data, context={"profile": p})


# TC-SCHEMA-09 — an order entry with no corresponding TailoredItem raises.
def test_order_entry_without_matching_item_raises():
    p = _profile()
    data = _selection(experience_order=["exp_1", "exp_missing"])
    with pytest.raises(ValidationError):
        TailoredSelection.model_validate(data, context={"profile": p})


def test_bare_construction_without_profile_context_skips_cross_checks():
    # No Profile in context — shape-only construction, used by schema-only tests.
    TailoredSelection(**_selection(experiences=[{"ref_id": "anything", "bullets": ["x"]}]))


def test_extra_field_on_tailored_item_raises():
    with pytest.raises(ValidationError):
        TailoredItem(ref_id="exp_1", bullets=["x"], unexpected="surprise")
