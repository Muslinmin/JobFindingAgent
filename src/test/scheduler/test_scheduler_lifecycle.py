"""WP-S3 — lifecycle job tests.

Mocks the injected JobService facade and NotificationTelegramClient per
scheduling_v2.md's test spec. Boundary correctness (exactly-N-days vs
N+1-days) is the facade/repository's job (`list_expired_pending_approval`
etc. already gate on `status_changed_at < cutoff`); these tests verify the
job's own responsibilities: which facade methods it calls with which
ApplicationStatus, that Telegram fires only for GHOSTED, and that one bad
record doesn't abort the batch.
"""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.models.enums import ApplicationStatus
from app.models.job import Job
from scheduler.jobs.lifecycle import (
    _cutoff,
    _expire_pending,
    _ghost_silent_applicants,
    _reject_stale,
    run_lifecycle,
)


def _job(job_id: int, company: str = "Acme", role: str = "Engineer") -> Job:
    return Job(
        id=job_id,
        fingerprint=f"fp-{job_id}",
        company=company,
        role=role,
        description="JD",
        url=f"https://example.com/{job_id}",
        posted_at=None,
        metadata=None,
        status=ApplicationStatus.SCORED,
        score=8000,
        status_changed_at="2026-01-01T00:00:00+00:00",
        follow_up_count=0,
        last_follow_up_at=None,
        follow_up_nudge_at=None,
        seen_count=1,
        last_seen_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _service(**method_returns):
    service = AsyncMock()
    for name, value in method_returns.items():
        getattr(service, name).return_value = value
    return service


class _Settings:
    pending_expiry_days = 14
    stale_after_days = 14
    ghost_after_days = 35


# ── _cutoff ───────────────────────────────────────────────────────────────────

def test_cutoff_is_isoformat_n_days_ago():
    from datetime import datetime, timedelta, timezone

    before = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff = datetime.fromisoformat(_cutoff(14))
    assert abs((cutoff - before).total_seconds()) < 5


# ── expire pending ────────────────────────────────────────────────────────────

async def test_expire_pending_transitions_each_candidate():
    service = _service(list_expired_pending_approval=[_job(1), _job(2)])
    n = await _expire_pending(service, _Settings())

    assert n == 2
    service.transition_status.assert_any_call(1, ApplicationStatus.EXPIRED)
    service.transition_status.assert_any_call(2, ApplicationStatus.EXPIRED)


async def test_expire_pending_one_failure_does_not_abort_others():
    service = _service(list_expired_pending_approval=[_job(1), _job(2)])
    service.transition_status.side_effect = [Exception("db error"), None]

    n = await _expire_pending(service, _Settings())

    assert n == 1
    assert service.transition_status.call_count == 2


# ── reject stale ──────────────────────────────────────────────────────────────

async def test_reject_stale_transitions_each_candidate():
    service = _service(list_stale_scored=[_job(1)])
    n = await _reject_stale(service, _Settings())

    assert n == 1
    service.transition_status.assert_called_once_with(1, ApplicationStatus.REJECTED)


# ── ghost silent applicants ───────────────────────────────────────────────────

async def test_ghost_sends_one_telegram_note_per_transition():
    service = _service(list_ghost_candidates=[_job(1, company="PUB", role="Data Engineer")])
    telegram = AsyncMock()

    n = await _ghost_silent_applicants(service, telegram, _Settings())

    assert n == 1
    service.transition_status.assert_called_once_with(1, ApplicationStatus.GHOSTED)
    telegram.send_message.assert_called_once_with(
        "No response from PUB (Data Engineer) — marked as ghosted."
    )


async def test_ghost_one_failure_does_not_abort_others_and_skips_its_telegram_note():
    service = _service(list_ghost_candidates=[_job(1), _job(2)])
    service.transition_status.side_effect = [Exception("db error"), None]
    telegram = AsyncMock()

    n = await _ghost_silent_applicants(service, telegram, _Settings())

    assert n == 1
    telegram.send_message.assert_called_once()


# ── run_lifecycle (composition) ───────────────────────────────────────────────

async def test_run_lifecycle_telegram_never_called_for_expire_or_reject():
    service = _service(
        list_expired_pending_approval=[_job(1)],
        list_stale_scored=[_job(2)],
        list_ghost_candidates=[],
    )
    telegram = AsyncMock()

    await run_lifecycle(service, telegram, _Settings())

    telegram.send_message.assert_not_called()
    service.transition_status.assert_any_call(1, ApplicationStatus.EXPIRED)
    service.transition_status.assert_any_call(2, ApplicationStatus.REJECTED)


async def test_run_lifecycle_one_bucket_failing_does_not_skip_the_others():
    service = _service(
        list_expired_pending_approval=[_job(1)],
        list_ghost_candidates=[_job(2)],
    )
    service.list_stale_scored.side_effect = Exception("facade read failed")
    telegram = AsyncMock()

    await run_lifecycle(service, telegram, _Settings())

    service.transition_status.assert_any_call(1, ApplicationStatus.EXPIRED)
    service.transition_status.assert_any_call(2, ApplicationStatus.GHOSTED)
    telegram.send_message.assert_called_once()


async def test_run_lifecycle_never_raises():
    service = _service()
    service.list_expired_pending_approval.side_effect = Exception("boom")
    service.list_stale_scored.side_effect = Exception("boom")
    service.list_ghost_candidates.side_effect = Exception("boom")
    telegram = AsyncMock()

    await run_lifecycle(service, telegram, _Settings())  # must not raise
