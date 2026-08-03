"""WP-S4 — follow-up job tests.

Both qualifying buckets are pre-filtered server-side (mocked here); the
job's own responsibility is drafting + pushing only for what the facade
returns, stamping `follow_up_nudge_at` on success, and never touching
`transition_status` (the ghost clock keys off `status_changed_at`, which
this job must never write).
"""

import json
from unittest.mock import AsyncMock

from app.models.enums import ApplicationStatus
from app.models.job import Job
from scheduler.jobs.follow_up import (
    assemble_followup_prompt as _assemble_followup_prompt,
    _cutoff,
    _draft_and_push,
    run_follow_up,
)


def _job(job_id: int = 1, company: str = "PUB", role: str = "Data Engineer") -> Job:
    return Job(
        id=job_id,
        fingerprint=f"fp{job_id}",
        company=company,
        role=role,
        description="JD",
        url=f"https://example.com/{job_id}",
        posted_at=None,
        metadata=None,
        status=ApplicationStatus.APPLIED,
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


def _service(first_batch=None, second_batch=None) -> AsyncMock:
    service = AsyncMock()
    service.list_follow_up_candidates.return_value = first_batch or []
    service.list_second_nudge_candidates.return_value = second_batch or []
    return service


def _llm(draft: str = "Following up on my application...") -> AsyncMock:
    llm = AsyncMock()
    llm.complete.return_value = draft
    return llm


class _Settings:
    follow_up_after_days = 7


# ── _assemble_followup_prompt ─────────────────────────────────────────────────

def test_assemble_prompt_contains_role_company_and_date():
    prompt = _assemble_followup_prompt("Data Engineer", "PUB", "2026-01-01T00:00:00+00:00")
    assert "Data Engineer" in prompt
    assert "PUB" in prompt
    assert "2026-01-01T00:00:00+00:00" in prompt


# ── _draft_and_push ───────────────────────────────────────────────────────────

async def test_draft_and_push_sends_drafted_text_and_correct_buttons():
    job = _job(job_id=42, company="PUB", role="Data Engineer")
    llm = _llm("Hi, just checking in on my application.")
    telegram = AsyncMock()

    await _draft_and_push(job, llm, telegram)

    telegram.send_message_with_keyboard.assert_called_once()
    text, keyboard = telegram.send_message_with_keyboard.call_args[0]
    assert "Hi, just checking in on my application." in text
    assert "PUB" in text and "Data Engineer" in text

    sent_it, skip = keyboard[0]
    assert sent_it.text == "Sent it"
    assert json.loads(sent_it.callback_data) == {"kind": "followup", "job_id": 42}
    assert skip.text == "Skip"
    assert json.loads(skip.callback_data) == {"kind": "dismiss", "job_id": 42}


# ── run_follow_up ─────────────────────────────────────────────────────────────

async def test_first_nudge_candidates_are_drafted_and_pushed():
    job = _job(1)
    service = _service(first_batch=[job])
    llm = _llm()
    telegram = AsyncMock()

    await run_follow_up(service, llm, telegram, _Settings())

    llm.complete.assert_called_once()
    telegram.send_message_with_keyboard.assert_called_once()
    service.mark_follow_up_nudged.assert_called_once_with(1)


async def test_second_nudge_candidates_are_drafted_and_pushed():
    job = _job(2)
    service = _service(second_batch=[job])
    llm = _llm()
    telegram = AsyncMock()

    await run_follow_up(service, llm, telegram, _Settings())

    llm.complete.assert_called_once()
    service.mark_follow_up_nudged.assert_called_once_with(2)


async def test_llm_not_called_when_no_candidates():
    service = _service()
    llm = _llm()
    telegram = AsyncMock()

    await run_follow_up(service, llm, telegram, _Settings())

    llm.complete.assert_not_called()
    telegram.send_message_with_keyboard.assert_not_called()
    service.mark_follow_up_nudged.assert_not_called()


async def test_one_record_failure_does_not_abort_the_others():
    job1, job2 = _job(1), _job(2)
    service = _service(first_batch=[job1, job2])
    llm = AsyncMock()
    llm.complete.side_effect = [Exception("LLM down"), "draft text"]
    telegram = AsyncMock()

    await run_follow_up(service, llm, telegram, _Settings())

    assert llm.complete.call_count == 2
    service.mark_follow_up_nudged.assert_called_once_with(2)


async def test_never_calls_transition_status():
    """The ghost clock keys off status_changed_at; this job must never
    touch it, directly or indirectly."""
    service = _service(first_batch=[_job(1)], second_batch=[_job(2)])
    llm = _llm()
    telegram = AsyncMock()

    await run_follow_up(service, llm, telegram, _Settings())

    service.transition_status.assert_not_called()


# ── _cutoff ───────────────────────────────────────────────────────────────────

def test_cutoff_is_isoformat_n_days_ago():
    from datetime import datetime, timedelta, timezone

    before = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff = datetime.fromisoformat(_cutoff(7))
    assert abs((cutoff - before).total_seconds()) < 5
