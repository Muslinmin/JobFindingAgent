import shutil
from pathlib import Path

import pytest

from profile.schema import Education, Profile, ProfileItem, Skill
from tailoring.render import RenderError, _compile_tex, render
from tailoring.schema import TailoredSelection

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"

skip_if_no_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed"
)


def _profile() -> Profile:
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        education=[Education(id="edu_1", institution="Test University", degree="BEng")],
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1", title="Engineer", bullets=["Built a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
    )


def _selection(profile: Profile) -> TailoredSelection:
    return TailoredSelection.model_validate(
        {
            "summary": "A concise summary.",
            "experience_order": ["exp_1"],
            "experiences": [{"ref_id": "exp_1", "bullets": ["Built a thing with Python."]}],
            "project_order": [],
            "projects": [],
            "skill_order": ["python"],
        },
        context={"profile": profile},
    )


# TC-RENDER-08 — the real tectonic compile step.
@skip_if_no_tectonic
def test_tectonic_compiles_a_valid_tex_file(tmp_path):
    tex_path = tmp_path / "cv.tex"
    tex_path.write_text(
        "\\documentclass{article}\n\\begin{document}\nhello\n\\end{document}\n"
    )
    pdf_path = _compile_tex(tex_path, tmp_path)
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


@skip_if_no_tectonic
def test_tectonic_raises_render_error_on_bad_input(tmp_path):
    tex_path = tmp_path / "cv.tex"
    tex_path.write_text("\\documentclass{article}\n\\begin{document}\n\\undefinedcommand\n")
    with pytest.raises(RenderError):
        _compile_tex(tex_path, tmp_path, timeout=30)


# TC-RENDER-09 [live] — render() end-to-end, including the tectonic compile.
@pytest.mark.live
@skip_if_no_tectonic
def test_render_end_to_end_produces_a_non_empty_pdf(tmp_path):
    profile = _profile()
    selection = _selection(profile)

    pdf_path = render(selection, profile, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
