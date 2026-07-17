import json
import shutil
from pathlib import Path

import pytest

from profile.schema import Profile, ProfileItem, Skill
from tailoring.tailor import ArtifactResult, tailor

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"

skip_if_no_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed"
)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


def _profile() -> Profile:
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Built a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
    )


# TC-ORCH-08 — end-to-end with a mock LLM and the real renderer; no DB touch
# anywhere in the layer (nothing here imports app.* at all — the boundary
# is enforced at the module level, not just by not calling anything).
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_tailor_end_to_end_with_real_renderer(tmp_path):
    profile = _profile()
    llm = FakeLLM(
        json.dumps(
            {
                "summary": "A concise summary.",
                "experience_order": ["exp_1"],
                "experiences": [{"ref_id": "exp_1", "bullets": ["Built a thing with Python."]}],
                "project_order": [],
                "projects": [],
                "skill_order": ["python"],
            }
        )
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )

    assert isinstance(result, ArtifactResult)
    assert result.path.exists()
    assert result.path.stat().st_size > 0
