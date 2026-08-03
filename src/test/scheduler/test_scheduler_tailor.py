"""WP-S5 — tailor job tests.

Mocks `tailoring.tailor.tailor` (the tailoring layer's one entry point),
the injected JobService facade, and the NotificationTelegramClient. Batch
selection (score DESC, posted_at DESC, id ASC and the LIMIT itself) is the
facade/repository's responsibility (`top_scored_for_tailoring`); these
tests verify the job calls it with `settings.tailor_batch_size` and
processes whatever it returns, isolating one record's failure from the
rest of the batch.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import ApplicationStatus, ArtifactKind
from app.models.job import Job
from scheduler.jobs.tailor import _tailor_one, run_tailor
from tailoring.tailor import ArtifactResult, TailoringError


def _job(job_id: int = 1, company: str = "Acme", role: str = "Engineer", score: int = 8000) -> Job:
    return Job(
        id=job_id,
        fingerprint=f"fp{job_id}",
        company=company,
        role=role,
        description="JD text",
        url=f"https://example.com/{job_id}",
        posted_at=None,
        metadata=None,
        status=ApplicationStatus.SCORED,
        score=score,
        status_changed_at="2026-01-01T00:00:00+00:00",
        follow_up_count=0,
        last_follow_up_at=None,
        follow_up_nudge_at=None,
        seen_count=1,
        last_seen_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def pdf_path(tmp_path) -> Path:
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 fake pdf bytes")
    return p


def _service() -> AsyncMock:
    return AsyncMock()


class _Settings:
    tailor_batch_size = 10


def _patch_common(pdf_path, tailor_side_effect=None, tailor_return=None):
    patches = [
        patch("scheduler.jobs.tailor.load_profile", return_value=MagicMock()),
    ]
    tailor_mock = AsyncMock()
    if tailor_side_effect is not None:
        tailor_mock.side_effect = tailor_side_effect
    else:
        tailor_mock.return_value = tailor_return or ArtifactResult(kind="cv_pdf", path=pdf_path)
    patches.append(patch("scheduler.jobs.tailor.tailor", tailor_mock))
    return patches, tailor_mock


# ── _tailor_one ───────────────────────────────────────────────────────────────

async def test_tailor_one_success_transitions_tailored_then_pending_approval(pdf_path):
    job = _job(1)
    service = _service()
    telegram = AsyncMock()
    patches, _ = _patch_common(pdf_path)

    with patches[0], patches[1]:
        ok = await _tailor_one(job, service, AsyncMock(), telegram, Path("p"), Path("t"), Path("o"))

    assert ok is True
    assert service.transition_status.call_args_list == [
        ((1, ApplicationStatus.TAILORED),),
        ((1, ApplicationStatus.PENDING_APPROVAL),),
    ]
    service.register_artifact.assert_called_once()
    artifact = service.register_artifact.call_args[0][1]
    assert artifact.kind == ArtifactKind.CV_PDF
    assert artifact.path == str(pdf_path)


async def test_tailor_one_pushes_document_and_keyboard(pdf_path):
    job = _job(1, company="Grab", role="ML Engineer")
    service = _service()
    telegram = AsyncMock()
    patches, _ = _patch_common(pdf_path)

    with patches[0], patches[1]:
        await _tailor_one(job, service, AsyncMock(), telegram, Path("p"), Path("t"), Path("o"))

    telegram.send_document.assert_called_once()
    text, pdf_bytes = telegram.send_document.call_args[0]
    assert "Grab" in text and "ML Engineer" in text
    assert pdf_bytes == pdf_path.read_bytes()

    telegram.send_message_with_keyboard.assert_called_once()
    _, keyboard = telegram.send_message_with_keyboard.call_args[0]
    mark_applied, skip = keyboard[0]
    assert json.loads(mark_applied.callback_data) == {"kind": "action", "job_id": 1, "action": "applied"}
    assert json.loads(skip.callback_data) == {"kind": "action", "job_id": 1, "action": "user_skipped"}


async def test_tailor_one_guard_violation_returns_false_without_transitioning(pdf_path):
    job = _job(1)
    service = _service()
    telegram = AsyncMock()
    patches, _ = _patch_common(pdf_path, tailor_side_effect=TailoringError("guard_violation", violations=["x"]))

    with patches[0], patches[1]:
        ok = await _tailor_one(job, service, AsyncMock(), telegram, Path("p"), Path("t"), Path("o"))

    assert ok is False
    service.transition_status.assert_not_called()
    service.register_artifact.assert_not_called()
    telegram.send_document.assert_not_called()


# ── run_tailor ────────────────────────────────────────────────────────────────

async def test_run_tailor_calls_top_scored_with_batch_size(pdf_path):
    service = _service()
    service.top_scored_for_tailoring.return_value = []
    patches, _ = _patch_common(pdf_path)

    with patches[0], patches[1]:
        await run_tailor(service, AsyncMock(), AsyncMock(), _Settings(), Path("p"), Path("t"), Path("o"))

    service.top_scored_for_tailoring.assert_called_once_with(10)


async def test_run_tailor_processes_every_returned_job(pdf_path):
    jobs = [_job(1), _job(2), _job(3)]
    service = _service()
    service.top_scored_for_tailoring.return_value = jobs
    telegram = AsyncMock()
    patches, tailor_mock = _patch_common(pdf_path)

    with patches[0], patches[1]:
        await run_tailor(service, AsyncMock(), telegram, _Settings(), Path("p"), Path("t"), Path("o"))

    assert tailor_mock.call_count == 3
    assert telegram.send_document.call_count == 3


async def test_run_tailor_guard_violation_on_one_record_does_not_abort_batch(pdf_path):
    jobs = [_job(1), _job(2)]
    service = _service()
    service.top_scored_for_tailoring.return_value = jobs
    telegram = AsyncMock()

    tailor_mock = AsyncMock()
    tailor_mock.side_effect = [TailoringError("guard_violation", violations=["x"]), ArtifactResult(kind="cv_pdf", path=pdf_path)]

    with patch("scheduler.jobs.tailor.load_profile", return_value=MagicMock()), \
         patch("scheduler.jobs.tailor.tailor", tailor_mock):
        await run_tailor(service, AsyncMock(), telegram, _Settings(), Path("p"), Path("t"), Path("o"))

    assert tailor_mock.call_count == 2
    assert telegram.send_document.call_count == 1


async def test_run_tailor_unexpected_exception_on_one_record_does_not_abort_batch(pdf_path):
    jobs = [_job(1), _job(2)]
    service = _service()
    service.top_scored_for_tailoring.return_value = jobs
    # job 1's register_artifact blows up; job 2 should still be attempted.
    service.register_artifact.side_effect = [Exception("disk full"), MagicMock()]
    telegram = AsyncMock()
    patches, tailor_mock = _patch_common(pdf_path)

    with patches[0], patches[1]:
        await run_tailor(service, AsyncMock(), telegram, _Settings(), Path("p"), Path("t"), Path("o"))

    assert tailor_mock.call_count == 2
    assert telegram.send_document.call_count == 1  # only job 2 reached the push
