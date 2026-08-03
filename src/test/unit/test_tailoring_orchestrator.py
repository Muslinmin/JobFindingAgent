import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger as loguru_logger

from profile.schema import Profile, ProfileItem, Skill
from tailoring.render import RenderError
from tailoring.schema import TailoredSelection
from tailoring.tailor import (
    MAX_GUARD_RETRIES,
    ArtifactResult,
    TailoringError,
    save_debug_artifact,
    tailor,
)

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


class SequenceFakeLLM:
    """Returns a different response on each successive call — used to drive
    the guard-retry loop through a scripted sequence of attempts."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses[self.calls]
        self.calls += 1
        return response


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


# --- Retry-loop fixtures -----------------------------------------------
# The TC-RETRY-* tests below drive `_tailor_with_guard_retries` through a
# scripted sequence of LLM attempts. These fixtures isolate that setup
# (profile, a patched `render`, and a factory for scripted LLM responses)
# so each test body only states the scenario — which attempts violate —
# not the plumbing.


@pytest.fixture
def profile() -> Profile:
    return _profile()


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


@pytest.fixture
def mock_render(fake_pdf: Path):
    """Patches `render` so a successful attempt resolves to `fake_pdf`
    without touching `tectonic`. Yields the mock so tests can assert on
    call count even when the retry loop never reaches it."""
    with patch("tailoring.tailor.render", return_value=fake_pdf) as mock:
        yield mock


@pytest.fixture
def make_sequence_llm():
    """Factory fixture: build a `SequenceFakeLLM` from a list of raw LLM
    response strings, one per successive `complete()` call — attempt N of
    the retry loop receives `responses[N]`."""

    def _make(responses: list[str]) -> SequenceFakeLLM:
        return SequenceFakeLLM(responses)

    return _make


class _PropagateHandler(logging.Handler):
    """`tailor.py` logs the retry loop via loguru (`logger.warning` per
    retryable attempt, `logger.critical` on exhaustion), but loguru does not
    feed the stdlib `logging` module by default — so pytest's `caplog`
    can't see it out of the box. This forwards each loguru record into
    stdlib logging so `caplog` captures the SAME log calls the production
    code actually makes (recipe from loguru's own docs)."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger(record.name).handle(record)


@pytest.fixture
def caplog(caplog):
    """Overrides pytest's built-in `caplog` to also capture loguru output
    for the duration of the test — request `caplog` in a test as usual."""
    handler_id = loguru_logger.add(_PropagateHandler(), format="{message}")
    caplog.set_level(logging.WARNING)
    yield caplog
    loguru_logger.remove(handler_id)


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


def _print_retry_trace(caplog: pytest.LogCaptureFixture) -> None:
    """Prints the retry loop's actual log records (visible with `pytest -s`)
    so a human can see the attempt-by-attempt trace, not just the pass/fail
    assertion."""
    print()
    for record in caplog.records:
        print(f"  [{record.levelname}] {record.message}")


# TC-RETRY-01 — first attempt violates, second is clean -> tailor() succeeds;
# call_llm_tailor invoked exactly twice; second prompt carries the first
# attempt's violations; tailor.py logs exactly one WARNING (the retryable
# miss) and no CRITICAL (it never exhausts the budget).
@pytest.mark.asyncio
async def test_tailor_retries_once_and_succeeds_on_clean_second_attempt(
    profile, mock_render, make_sequence_llm, tmp_path, caplog
):
    # exp_1 only demonstrates "python" -> first attempt's "FastAPI" is unauthorized.
    llm = make_sequence_llm(
        [
            _selection_json(["Built a thing with FastAPI."]),
            _selection_json(["Built a thing with Python."]),
        ]
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )
    _print_retry_trace(caplog)

    assert isinstance(result, ArtifactResult)
    assert llm.calls == 2
    assert "unauthorized skill" in llm.prompts[1].lower()
    mock_render.assert_called_once()

    # Assert against tailor.py's ACTUAL loguru output, not just the call
    # count — this is the evidence the retry loop itself fired, via the
    # same logging the production code emits.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(warnings) == 1
    assert f"attempt 1/{MAX_GUARD_RETRIES + 1}" in warnings[0].message.lower()
    assert "unauthorized skill 'fastapi'" in warnings[0].message.lower()
    assert criticals == []


# TC-RETRY-02 — every attempt violates -> tailor() raises guard_violation;
# call_llm_tailor invoked exactly 1 + MAX_GUARD_RETRIES times; tailor.py
# logs one WARNING per retryable miss (MAX_GUARD_RETRIES of them) followed
# by exactly one CRITICAL on the terminal failure.
@pytest.mark.asyncio
async def test_tailor_raises_guard_violation_after_exhausting_retries(
    profile, mock_render, make_sequence_llm, tmp_path, caplog
):
    bad_response = _selection_json(["Built a thing with FastAPI."])
    llm = make_sequence_llm([bad_response] * (MAX_GUARD_RETRIES + 1))

    with pytest.raises(TailoringError) as exc_info:
        await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)
    _print_retry_trace(caplog)

    assert exc_info.value.reason == "guard_violation"
    assert llm.calls == MAX_GUARD_RETRIES + 1
    mock_render.assert_not_called()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(warnings) == MAX_GUARD_RETRIES
    assert [f"attempt {i + 1}/{MAX_GUARD_RETRIES + 1}" in w.message.lower() for i, w in enumerate(warnings)] == [
        True
    ] * MAX_GUARD_RETRIES
    assert len(criticals) == 1
    assert "unauthorized skill 'fastapi'" in criticals[0].message.lower()


# TC-RETRY-03 — first attempt is clean -> tailor() succeeds; call_llm_tailor
# invoked exactly once (no regression on the common, no-violation path);
# tailor.py logs nothing at WARNING/CRITICAL — the retry loop never engaged.
@pytest.mark.asyncio
async def test_tailor_succeeds_on_first_attempt_without_retrying(
    profile, mock_render, make_sequence_llm, tmp_path, caplog
):
    llm = make_sequence_llm([_selection_json(["Built a thing with Python."])])

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )
    _print_retry_trace(caplog)

    assert isinstance(result, ArtifactResult)
    assert llm.calls == 1
    mock_render.assert_called_once()
    assert caplog.records == []


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
