from profile.lookup import (
    find_items,
    find_skills,
    get_item,
    get_skill,
    items_demonstrating,
    resolve_surface,
)
from profile.schema import Profile, ProfileItem, Skill


def _profile() -> Profile:
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[
            Skill(id="rest_api", label="REST API", aliases=["RESTful API", "REST"]),
            Skill(id="python", label="Python"),
        ],
        experiences=[
            ProfileItem(
                id="exp_backend",
                title="Backend Engineer",
                organization="Acme Robotics",
                bullets=["Exposed REST endpoints with Python."],
                demonstrated_skills=["rest_api", "python"],
            ),
        ],
        projects=[
            ProfileItem(
                id="proj_backend",
                title="Backend Capstone",
                bullets=["Built a REST service."],
                demonstrated_skills=["rest_api"],
            ),
        ],
    )


def test_get_item_exact_match():
    p = _profile()
    assert get_item(p, "exp_backend").title == "Backend Engineer"
    assert get_item(p, "missing") is None


def test_get_skill_exact_match():
    p = _profile()
    assert get_skill(p, "python").label == "Python"
    assert get_skill(p, "missing") is None


def test_resolve_surface_matches_label_and_aliases():
    p = _profile()
    assert resolve_surface(p, "RESTful API").id == "rest_api"
    assert resolve_surface(p, "REST").id == "rest_api"
    assert resolve_surface(p, "REST API").id == "rest_api"
    assert resolve_surface(p, "unknown surface") is None


def test_find_items_returns_all_candidates_above_threshold_not_a_best_guess():
    p = _profile()
    matches = find_items(p, "backend", threshold=0.3)
    assert {i.id for i in matches} == {"exp_backend", "proj_backend"}


def test_find_items_returns_empty_list_below_threshold():
    p = _profile()
    assert find_items(p, "completely unrelated phrase xyz", threshold=0.9) == []


def test_find_skills_matches_alias():
    p = _profile()
    matches = find_skills(p, "RESTful API", threshold=0.9)
    assert [s.id for s in matches] == ["rest_api"]


def test_items_demonstrating_returns_only_tagged_items():
    p = _profile()
    ids = {i.id for i in items_demonstrating(p, "python")}
    assert ids == {"exp_backend"}
