import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from profile.schema import Education, Profile, ProfileItem

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "profile_template.json"

# Fields that are identifiers, not content — deliberately untagged.
_UNTAGGED = {"id"}


def _tagged_fields(model_cls) -> dict[str, object]:
    return {
        name: info.json_schema_extra
        for name, info in model_cls.model_fields.items()
        if name not in _UNTAGGED
    }


@pytest.mark.parametrize("model_cls", [Profile, ProfileItem, Education])
def test_every_field_carries_a_tier_tag(model_cls):
    for name, extra in _tagged_fields(model_cls).items():
        assert isinstance(extra, dict) and "tier" in extra, (
            f"{model_cls.__name__}.{name} has no tier tag"
        )


def test_profile_template_validates_against_schema():
    data = json.loads(TEMPLATE_PATH.read_text())
    Profile.model_validate(data)  # raises on failure


def test_unknown_top_level_field_raises():
    data = json.loads(TEMPLATE_PATH.read_text())
    data["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_unknown_field_on_nested_item_raises():
    data = json.loads(TEMPLATE_PATH.read_text())
    data["experiences"][0]["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
