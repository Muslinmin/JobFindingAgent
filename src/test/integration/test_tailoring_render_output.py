"""Not part of the TC-* catalog. A manual-inspection aid: runs the real
`tailor()` pipeline (real renderer + tectonic, fake LLM so no API key is
needed) against `profile_template.json` and leaves the PDF at a fixed,
git-ignored path instead of pytest's ephemeral tmp_path, so a human can
open it and actually look at the CV.
"""

import json
import shutil
from pathlib import Path

import pytest

from profile.schema import Profile
from tailoring.tailor import tailor

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"
PROFILE_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "profile_template.json"
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "tailoring_output"

skip_if_no_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed"
)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


@pytest.mark.live
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_render_a_real_pdf_for_manual_inspection():
    profile = Profile.model_validate(json.loads(PROFILE_TEMPLATE_PATH.read_text()))

    jd = (
        "We're hiring an Example Engineer with hands-on Example Framework "
        "experience to design, build, and ship example subsystems end to end."
    )
    selection_json = json.dumps(
        {
            "summary": (
                "Example engineer with hands-on Example Framework delivery "
                "experience, from specification through to working hardware."
            ),
            "experience_order": ["exp_example_role", "exp_example_intern"],
            "experiences": [
                {
                    "ref_id": "exp_example_role",
                    "bullets": [
                        "Built an example subsystem using Example Framework, "
                        "delivering it end to end and validating it against real hardware.",
                        "Maintained example documentation and integration test records "
                        "throughout the engagement.",
                    ],
                },
                {
                    "ref_id": "exp_example_intern",
                    "bullets": ["Implemented example interface components using Example Tool."],
                },
            ],
            "project_order": ["proj_example_major", "proj_example_minor"],
            "projects": [
                {
                    "ref_id": "proj_example_major",
                    "bullets": [
                        "Designed and delivered an example pipeline, taking it from "
                        "specification through to a working demonstration on physical hardware.",
                        "Exposed the pipeline through an example service interface so "
                        "other systems could invoke it.",
                    ],
                },
                {
                    "ref_id": "proj_example_minor",
                    "bullets": ["Implemented an example algorithm from scratch and analysed its trade-offs."],
                },
            ],
            "skill_order": ["example_language", "example_framework", "example_tool", "example_concept"],
        }
    )

    result = await tailor(
        jd,
        profile,
        llm=FakeLLM(selection_json),
        template_path=TEMPLATE_PATH,
        output_dir=OUTPUT_DIR,
        save_debug_artifacts=True,
    )

    assert result.path.exists()
    assert result.path.stat().st_size > 0
    print(f"\nCV written to: {result.path}")
