import json
from pathlib import Path
from unittest.mock import patch

import pytest

from profile.schema import Profile, ProfileItem, Skill
from tailoring.render import RenderError
from tailoring.schema import TailoredSelection
from tailoring.tailor import ArtifactResult, TailoringError, save_debug_artifact, tailor

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


def _profile() -> Profile:
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python"), Skill(id="fastapi", label="FastAPI")],
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


def _selection_json(bullets: list[str]) -> str:
    return json.dumps(
        {
            "summary": "A concise summary.",
            "experience_order": ["exp_1"],
            "experiences": [{"ref_id": "exp_1", "bullets": bullets}],
            "project_order": [],
            "projects": [],
            "skill_order": ["python"],
        }
    )


# TC-ORCH-01 — clean run returns an ArtifactResult naming a PDF that exists.
@pytest.mark.asyncio
async def test_tailor_returns_artifact_result_on_clean_run(tmp_path):
    profile = _profile()
    llm = FakeLLM(_selection_json(["Built a thing with Python."]))
    fake_pdf = tmp_path / "cv.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailoring.tailor.render", return_value=fake_pdf) as mock_render:
        result = await tailor(
            "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
        )

    mock_render.assert_called_once()
    assert isinstance(result, ArtifactResult)
    assert result.path == fake_pdf
    assert result.path.exists()


# TC-ORCH-02 — malformed JSON -> schema_invalid, render never reached, no file written.
@pytest.mark.asyncio
async def test_tailor_raises_schema_invalid_on_malformed_json(tmp_path):
    profile = _profile()
    llm = FakeLLM("not valid json {")

    with patch("tailoring.tailor.render") as mock_render:
        with pytest.raises(TailoringError) as exc_info:
            await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "schema_invalid"
    mock_render.assert_not_called()
    assert list(tmp_path.iterdir()) == []


# TC-ORCH-02b — a ref_id the profile doesn't have is also schema_invalid.
@pytest.mark.asyncio
async def test_tailor_raises_schema_invalid_on_bad_ref_id(tmp_path):
    profile = _profile()
    bad_json = json.dumps(
        {
            "summary": "s",
            "experience_order": ["exp_ghost"],
            "experiences": [{"ref_id": "exp_ghost", "bullets": ["x"]}],
            "project_order": [],
            "projects": [],
            "skill_order": [],
        }
    )
    llm = FakeLLM(bad_json)

    with patch("tailoring.tailor.render") as mock_render:
        with pytest.raises(TailoringError) as exc_info:
            await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "schema_invalid"
    mock_render.assert_not_called()


# TC-ORCH-03 — the real skill-subset guard fires: an unauthorized skill surfaced.
@pytest.mark.asyncio
async def test_tailor_raises_guard_violation_on_unauthorized_skill(tmp_path):
    profile = _profile()
    # exp_1 only demonstrates "python" — surfacing "FastAPI" is unauthorized.
    llm = FakeLLM(_selection_json(["Built a thing with FastAPI."]))

    with patch("tailoring.tailor.render") as mock_render:
        with pytest.raises(TailoringError) as exc_info:
            await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "guard_violation"
    assert any("fastapi" in v.lower() for v in exc_info.value.violations)
    mock_render.assert_not_called()
    assert list(tmp_path.iterdir()) == []


# TC-ORCH-04 — the real no-new-specifics guard fires: a new numeral.
@pytest.mark.asyncio
async def test_tailor_raises_guard_violation_on_new_numeral(tmp_path):
    profile = _profile()
    llm = FakeLLM(_selection_json(["Led a team of 5 to build a thing."]))

    with patch("tailoring.tailor.render") as mock_render:
        with pytest.raises(TailoringError) as exc_info:
            await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "guard_violation"
    assert any("5" in v for v in exc_info.value.violations)
    mock_render.assert_not_called()


# TC-ORCH-05 — a compile failure raises TailoringError, no partial result.
@pytest.mark.asyncio
async def test_tailor_raises_render_failed_when_compile_fails(tmp_path):
    profile = _profile()
    llm = FakeLLM(_selection_json(["Built a thing with Python."]))

    with patch("tailoring.tailor.render", side_effect=RenderError("tectonic blew up")):
        with pytest.raises(TailoringError) as exc_info:
            await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "render_failed"


# TC-ORCH-06 — save_debug_artifact writes a debug/ folder when enabled.
def test_save_debug_artifact_writes_when_enabled(tmp_path):
    path = save_debug_artifact("tex source", tmp_path, enabled=True)
    assert path == tmp_path / "debug" / "cv.tex"
    assert path.read_text() == "tex source"


# TC-ORCH-07 — save_debug_artifact does nothing when disabled (the default).
def test_save_debug_artifact_no_op_when_disabled(tmp_path):
    result = save_debug_artifact("tex source", tmp_path, enabled=False)
    assert result is None
    assert not (tmp_path / "debug").exists()


@pytest.mark.asyncio
async def test_tailor_writes_debug_artifact_when_requested(tmp_path):
    profile = _profile()
    llm = FakeLLM(_selection_json(["Built a thing with Python."]))
    fake_pdf = tmp_path / "cv.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailoring.tailor.render", return_value=fake_pdf):
        await tailor(
            "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path,
            save_debug_artifacts=True,
        )

    assert (tmp_path / "debug" / "cv.tex").exists()
