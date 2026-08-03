import json

import pytest
from pydantic import ValidationError

from profile.schema import Profile, ProfileItem, Skill
from tailoring.prompt import assemble_prompt, call_llm_tailor
from tailoring.schema import TailoredSelection


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        phone="+65 9000 0000",
        links=["https://github.com/jane"],
        skills=[Skill(id="python", label="Python"), Skill(id="fastapi", label="FastAPI")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Built a thing with Python."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[
            ProfileItem(
                id="proj_1",
                title="Side project",
                bullets=["Shipped a thing with FastAPI."],
                demonstrated_skills=["python", "fastapi"],
            )
        ],
    )
    base.update(overrides)
    return Profile(**base)


def _valid_selection_json() -> str:
    return json.dumps(
        {
            "summary": "A concise summary.",
            "experience_order": ["exp_1"],
            "experiences": [{"ref_id": "exp_1", "bullets": ["Built a thing with Python."]}],
            "project_order": ["proj_1"],
            "projects": [{"ref_id": "proj_1", "bullets": ["Shipped a thing with FastAPI."]}],
            "skill_order": ["python", "fastapi"],
        }
    )


# TC-PROMPT-01 — JD text present in the prompt.
def test_assemble_prompt_contains_jd_text():
    prompt = assemble_prompt("Looking for a backend engineer.", _profile())
    assert "Looking for a backend engineer." in prompt


# TC-PROMPT-02 — all profile content fields present.
def test_assemble_prompt_contains_profile_content():
    prompt = assemble_prompt("jd", _profile())
    assert "Built a thing with Python." in prompt
    assert "Shipped a thing with FastAPI." in prompt
    assert "FastAPI" in prompt
    assert '"demonstrated_skills"' in prompt


# TC-PROMPT-03 — identity fields absent from the prompt.
def test_assemble_prompt_excludes_identity_fields():
    prompt = assemble_prompt("jd", _profile())
    assert "jane@example.com" not in prompt
    assert "+65 9000 0000" not in prompt
    assert "https://github.com/jane" not in prompt
    assert "Jane Candidate" not in prompt


# TC-PROMPT-04 — domain-knowledge content present.
def test_assemble_prompt_contains_domain_knowledge():
    prompt = assemble_prompt("jd", _profile())
    assert "resume-tailor" in prompt
    assert "resume-ats-optimizer" in prompt
    assert "resume-section-builder" in prompt


# TC-PROMPT-05 — valid LLM output returns a TailoredSelection.
@pytest.mark.asyncio
async def test_call_llm_tailor_returns_tailored_selection_on_valid_output():
    result = await call_llm_tailor("jd", _profile(), FakeLLM(_valid_selection_json()))
    assert isinstance(result, TailoredSelection)
    assert result.experiences[0].ref_id == "exp_1"


# TC-PROMPT-06 — malformed JSON raises, no partial data returned.
@pytest.mark.asyncio
async def test_call_llm_tailor_raises_on_malformed_json():
    with pytest.raises(json.JSONDecodeError):
        await call_llm_tailor("jd", _profile(), FakeLLM("not valid json {"))


# TC-PROMPT-08 — no previous_violations -> prompt is byte-identical to the default.
def test_assemble_prompt_without_violations_matches_default():
    assert assemble_prompt("jd", _profile()) == assemble_prompt(
        "jd", _profile(), previous_violations=None
    )


# TC-PROMPT-09 — previous_violations appends a block naming each issue.
def test_assemble_prompt_with_violations_appends_feedback_block():
    prompt = assemble_prompt(
        "jd", _profile(), previous_violations=["exp_1: unauthorized skill 'leadership'"]
    )
    assert "exp_1: unauthorized skill 'leadership'" in prompt
    assert "false positives" in prompt.lower()
    # Still contains everything the base prompt has.
    assert "jd" in prompt


# TC-PROMPT-07 — a ref_id not present in the profile raises ValidationError.
@pytest.mark.asyncio
async def test_call_llm_tailor_raises_on_unresolvable_ref_id():
    bad_json = json.dumps(
        {
            "summary": "A concise summary.",
            "experience_order": ["exp_ghost"],
            "experiences": [{"ref_id": "exp_ghost", "bullets": ["Fabricated."]}],
            "project_order": [],
            "projects": [],
            "skill_order": [],
        }
    )
    with pytest.raises(ValidationError):
        await call_llm_tailor("jd", _profile(), FakeLLM(bad_json))
