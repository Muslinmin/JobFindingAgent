from profile.schema import ProfileItem, Skill
from tailoring.guards import check_skill_subset
from tailoring.schema import TailoredItem

_SKILLS = [
    Skill(id="python", label="Python"),
    Skill(id="fastapi", label="FastAPI", aliases=["Fast API"]),
    Skill(id="leadership", label="Leadership"),
]


# TC-GUARD-08 — the real (unpatched) surface-match detector.
def test_real_detector_flags_unauthorized_surfaced_skill():
    source_item = ProfileItem(
        id="exp_1", title="Engineer", bullets=["x"], demonstrated_skills=["python"]
    )
    tailored_item = TailoredItem(
        ref_id="exp_1",
        bullets=["Led the team, building the service in Python with Fast API."],
    )

    violations = check_skill_subset(tailored_item, source_item, _SKILLS)

    # "leadership" surfaced by "Led" is NOT a surface of any skill here, so
    # only the genuinely detected-but-unauthorized surface ("fastapi") fires.
    assert violations == ["fastapi"]


def test_real_detector_passes_when_all_surfaced_skills_are_authorized():
    source_item = ProfileItem(
        id="exp_1",
        title="Engineer",
        bullets=["x"],
        demonstrated_skills=["python", "fastapi"],
    )
    tailored_item = TailoredItem(
        ref_id="exp_1",
        bullets=["Built the service in Python with Fast API."],
    )

    assert check_skill_subset(tailored_item, source_item, _SKILLS) == []
