import json
from pathlib import Path

import pytest

from profile.schema import Profile
from tailoring.prompt import call_llm_tailor
from tailoring.schema import TailoredSelection

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "profile_template.json"


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


# TC-PROMPT-08 — realistic Profile + JD fixture, output passes schema
# validation end-to-end.
@pytest.mark.asyncio
async def test_call_llm_tailor_end_to_end_with_realistic_fixture():
    profile = Profile.model_validate(json.loads(TEMPLATE_PATH.read_text()))

    jd = (
        "We're hiring an Example Engineer with hands-on Example Framework "
        "experience to build and ship example subsystems."
    )
    selection_json = json.dumps(
        {
            "summary": "Example engineer with hands-on Example Framework delivery experience.",
            "experience_order": ["exp_example_role", "exp_example_intern"],
            "experiences": [
                {
                    "ref_id": "exp_example_role",
                    "bullets": [
                        "Built an example subsystem using Example Framework, "
                        "delivering it end to end and validating it against real hardware."
                    ],
                },
                {
                    "ref_id": "exp_example_intern",
                    "bullets": ["Implemented example interface components using Example Tool."],
                },
            ],
            "project_order": ["proj_example_major"],
            "projects": [
                {
                    "ref_id": "proj_example_major",
                    "bullets": [
                        "Designed and delivered an example pipeline, taking it from "
                        "specification through to a working demonstration on physical hardware."
                    ],
                }
            ],
            "skill_order": ["example_language", "example_framework"],
        }
    )

    result = await call_llm_tailor(jd, profile, FakeLLM(selection_json))

    assert isinstance(result, TailoredSelection)
    assert result.experience_order == ["exp_example_role", "exp_example_intern"]
    assert result.skill_order == ["example_language", "example_framework"]
