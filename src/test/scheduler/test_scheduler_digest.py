"""WP-S7 — digest job tests.

Mocks the injected JobService facade and NotificationTelegramClient.
`format_digest` is pure and tested directly; `run_digest`'s LLM narrative
path is tested for exactly-one-call gating.
"""

from datetime import date
from unittest.mock import AsyncMock

from app.models.enums import ApplicationStatus
from scheduler.jobs.digest import DigestCounts, collect_counts, format_digest, run_digest


def _n(count: int) -> list[int]:
    return list(range(count))  # collect_counts only takes len() of these


def _service(
    scored=0, tailored=0, pending_approval=0, applied=0, interviewing=0, offer=0,
    first_batch=0, second_batch=0, ghosted_7d=0, expired_7d=0,
) -> AsyncMock:
    by_status = {
        ApplicationStatus.SCORED: scored,
        ApplicationStatus.TAILORED: tailored,
        ApplicationStatus.PENDING_APPROVAL: pending_approval,
        ApplicationStatus.APPLIED: applied,
        ApplicationStatus.INTERVIEWING: interviewing,
        ApplicationStatus.OFFER: offer,
    }

    service = AsyncMock()

    async def query_jobs(status_set, limit=50, offset=0):
        (status,) = status_set
        return _n(by_status[status])

    service.query_jobs.side_effect = query_jobs
    service.list_follow_up_candidates.return_value = _n(first_batch)
    service.list_second_nudge_candidates.return_value = _n(second_batch)

    async def count_status_since(status, since):
        return {ApplicationStatus.GHOSTED: ghosted_7d, ApplicationStatus.EXPIRED: expired_7d}[status]

    service.count_status_since.side_effect = count_status_since
    return service


class _Settings:
    follow_up_after_days = 7
    digest_narrative = False


# ── collect_counts ────────────────────────────────────────────────────────────

async def test_collect_counts_pulls_each_bucket():
    service = _service(
        scored=3, tailored=1, pending_approval=2, applied=4, interviewing=1, offer=1,
        first_batch=2, second_batch=1, ghosted_7d=1, expired_7d=5,
    )

    counts = await collect_counts(service, _Settings())

    assert counts == DigestCounts(
        scored=3, tailored=1, pending_approval=2, applied=4, interviewing=1, offer=1,
        follow_ups_due=3, ghosted_last_7d=1, expired_last_7d=5,
    )


async def test_collect_counts_uses_a_high_limit_not_the_default_page_size():
    service = _service(scored=1)
    await collect_counts(service, _Settings())

    scored_call = next(
        c for c in service.query_jobs.call_args_list if c.args[0] == {ApplicationStatus.SCORED}
    )
    assert scored_call.kwargs["limit"] > 50


# ── format_digest ─────────────────────────────────────────────────────────────

def test_format_digest_is_pure_and_matches_template():
    counts = DigestCounts(
        scored=3, tailored=1, pending_approval=2, applied=4, interviewing=1, offer=1,
        follow_ups_due=2, ghosted_last_7d=1, expired_last_7d=0,
    )
    text = format_digest(counts, date(2026, 7, 20))

    assert "Weekly digest — 2026-07-20" in text
    assert "3 scored" in text and "1 tailored" in text and "2 pending approval" in text
    assert "4 applied" in text and "1 interviewing" in text and "1 offer" in text
    assert "Follow-ups due:  2" in text
    assert "Ghosted (last 7d):  1" in text
    assert "Expired (last 7d):  0" in text


# ── run_digest ────────────────────────────────────────────────────────────────

async def test_run_digest_sends_one_telegram_message_no_llm_by_default():
    service = _service(scored=1)
    telegram = AsyncMock()

    await run_digest(service, telegram, _Settings())

    telegram.send_message.assert_called_once()


async def test_run_digest_narrative_false_never_calls_llm():
    service = _service()
    telegram = AsyncMock()
    llm = AsyncMock()

    await run_digest(service, telegram, _Settings(), llm=llm)

    llm.complete.assert_not_called()


async def test_run_digest_narrative_true_triggers_exactly_one_llm_call():
    class Settings(_Settings):
        digest_narrative = True

    service = _service()
    telegram = AsyncMock()
    llm = AsyncMock()
    llm.complete.return_value = "Solid week — keep it up!"

    await run_digest(service, telegram, Settings(), llm=llm)

    llm.complete.assert_called_once()
    text = telegram.send_message.call_args[0][0]
    assert "Solid week — keep it up!" in text


async def test_run_digest_never_raises():
    service = AsyncMock()
    service.query_jobs.side_effect = Exception("db down")
    telegram = AsyncMock()

    await run_digest(service, telegram, _Settings())  # must not raise

    telegram.send_message.assert_not_called()
