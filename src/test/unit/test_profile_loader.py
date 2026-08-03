import json
from pathlib import Path

import pytest

from profile.invariants import ProfileInvariantError
from profile.loader import load_profile

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "profile_template.json"


def test_load_profile_returns_a_profile_for_the_template():
    profile = load_profile(TEMPLATE_PATH)
    assert profile.name
    assert profile.experiences


def test_load_profile_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "does_not_exist.json")


def test_load_profile_raises_on_invariant_violation(tmp_path):
    data = json.loads(TEMPLATE_PATH.read_text())
    # Duplicate an experience id into projects to break invariant 1.
    data["projects"].append(data["experiences"][0])
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))

    with pytest.raises(ProfileInvariantError):
        load_profile(path)
