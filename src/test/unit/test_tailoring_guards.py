from unittest.mock import patch

from profile.schema import ProfileItem, Skill
from tailoring.guards import check_no_new_specifics, check_skill_subset
from tailoring.schema import TailoredItem

_SKILLS = [Skill(id="python", label="Python"), Skill(id="fastapi", label="FastAPI")]


def _item(demonstrated_skills):
    return ProfileItem(
        id="exp_1", title="Engineer", bullets=["x"], demonstrated_skills=demonstrated_skills
    )


# TC-GUARD-01 — detected skill is authorised for this item.
def test_detected_skill_within_demonstrated_skills_passes():
    with patch("tailoring.guards._get_skill_detector", return_value=lambda t, s: {"python"}):
        violations = check_skill_subset(
            TailoredItem(ref_id="exp_1", bullets=["Built it with Python."]),
            _item(["python", "fastapi"]),
            _SKILLS,
        )
    assert violations == []


# TC-GUARD-02 — detected skill absent from demonstrated_skills is flagged.
def test_detected_skill_outside_demonstrated_skills_is_flagged():
    with patch("tailoring.guards._get_skill_detector", return_value=lambda t, s: {"leadership"}):
        violations = check_skill_subset(
            TailoredItem(ref_id="exp_1", bullets=["Led the effort."]),
            _item(["python", "fastapi"]),
            _SKILLS,
        )
    assert violations == ["leadership"]


# TC-GUARD-03 — no skills detected, nothing to check.
def test_no_skills_detected_passes():
    with patch("tailoring.guards._get_skill_detector", return_value=lambda t, s: set()):
        violations = check_skill_subset(
            TailoredItem(ref_id="exp_1", bullets=["Did some general work."]),
            _item(["python", "fastapi"]),
            _SKILLS,
        )
    assert violations == []


# TC-GUARD-04 — synonym/tense change only, no new numeral or named entity.
def test_synonym_and_tense_change_passes():
    violations = check_no_new_specifics(
        "Developed backend services using PostgreSQL",
        "Built backend services with Postgres",
    )
    assert violations == []


# TC-GUARD-05 — a new numeral absent from the source is flagged.
def test_new_numeral_is_flagged():
    violations = check_no_new_specifics(
        "Led a team of 5 to build backend services",
        "Built backend services",
    )
    assert any("5" in v for v in violations)


# TC-GUARD-06 — numeral reused but its context changed, altering the meaning.
def test_numeral_reused_in_new_context_is_flagged():
    violations = check_no_new_specifics(
        "Managed a team of 5 clients",
        "Managed a team of 5 engineers",
    )
    assert violations != []


# TC-GUARD-07 — a new named entity absent from the source is flagged.
def test_new_named_entity_is_flagged():
    violations = check_no_new_specifics(
        "Worked on backend services using AWS",
        "Worked on backend services",
    )
    assert any("AWS" in v for v in violations)
