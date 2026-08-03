"""Digest job (scheduling_v2.md WP-S7).

Weekly (Monday, after all daily jobs complete). Pulls pipeline state counts
through the injected `JobService` facade and pushes a structured summary to
Telegram. Zero LLM calls by default — `settings.digest_narrative` can
enable exactly one optional completion to append a one-line summary
sentence.

`follow_ups_due` reuses the same two facade queries the follow-up job
itself uses (`list_follow_up_candidates`, `list_second_nudge_candidates`)
so the digest's number can never drift from what the follow-up job would
actually act on. `ghosted_last_7d`/`expired_last_7d` use the new
`service.count_status_since` facade method — distinct from the lifecycle
job's bucket reads, which find records STILL PAST a threshold rather than
counting recent transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from loguru import logger

from app.config import Settings
from app.models.enums import ApplicationStatus
from app.services.service import JobService
from telegram_bot.notifications.client import NotificationTelegramClient


class NarrativeLLM(Protocol):
    """Same narrow text-in/text-out shape as the other jobs' LLM
    protocols. Only used when `settings.digest_narrative` is true."""

    async def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class DigestCounts:
    """Pipeline state snapshot for the weekly digest."""

    scored: int
    tailored: int
    pending_approval: int
    applied: int
    interviewing: int
    offer: int
    follow_ups_due: int
    ghosted_last_7d: int
    expired_last_7d: int


# "Effectively unlimited" cap for the plain per-status counts below. The
# facade's query_jobs defaults to limit=50 (a UI-page-sized default); the
# digest needs a true count, not a page, so it passes this instead. The
# pipeline's own throttles (tailor_batch_size, stale_after_days) keep any
# single status's population far below this in practice (§ Throughput &
# Cost Control, architecture_v2.md).
_COUNT_LIMIT = 10_000


def _cutoff(days: int) -> str:
    """`now - days`, isoformat — same shape as lifecycle.py/follow_up.py's
    own `_cutoff` (duplicated locally rather than shared, matching this
    package's convention of each module stating only what it needs)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def collect_counts(service: JobService, settings: Settings) -> DigestCounts:
    """Active-state counts via `service.query_jobs({status})` per status;
    `follow_ups_due` = len(first-nudge bucket) + len(second-nudge bucket);
    `ghosted_last_7d`/`expired_last_7d` via `service.count_status_since`
    with `since = now - 7 days`."""

    async def _count(status: ApplicationStatus) -> int:
        return len(await service.query_jobs({status}, limit=_COUNT_LIMIT))

    scored = await _count(ApplicationStatus.SCORED)
    tailored = await _count(ApplicationStatus.TAILORED)
    pending_approval = await _count(ApplicationStatus.PENDING_APPROVAL)
    applied = await _count(ApplicationStatus.APPLIED)
    interviewing = await _count(ApplicationStatus.INTERVIEWING)
    offer = await _count(ApplicationStatus.OFFER)

    follow_up_cutoff = _cutoff(settings.follow_up_after_days)
    first_batch = await service.list_follow_up_candidates(follow_up_cutoff)
    second_batch = await service.list_second_nudge_candidates(follow_up_cutoff)
    follow_ups_due = len(first_batch) + len(second_batch)

    since = _cutoff(7)
    ghosted_last_7d = await service.count_status_since(ApplicationStatus.GHOSTED, since)
    expired_last_7d = await service.count_status_since(ApplicationStatus.EXPIRED, since)

    return DigestCounts(
        scored=scored,
        tailored=tailored,
        pending_approval=pending_approval,
        applied=applied,
        interviewing=interviewing,
        offer=offer,
        follow_ups_due=follow_ups_due,
        ghosted_last_7d=ghosted_last_7d,
        expired_last_7d=expired_last_7d,
    )


def format_digest(counts: DigestCounts, today: date) -> str:
    """Pure: counts + date -> Telegram message body, matching the template
    in scheduling_v2.md WP-S7. Zero LLM, zero I/O."""
    return (
        f"Weekly digest — {today.isoformat()}\n"
        f"Active:    {counts.scored} scored · {counts.tailored} tailored · "
        f"{counts.pending_approval} pending approval\n"
        f"Applied:   {counts.applied} applied · {counts.interviewing} interviewing · "
        f"{counts.offer} offer\n"
        f"Follow-ups due:  {counts.follow_ups_due}\n"
        f"Ghosted (last 7d):  {counts.ghosted_last_7d}\n"
        f"Expired (last 7d):  {counts.expired_last_7d}"
    )


async def run_digest(
    service: JobService,
    telegram: NotificationTelegramClient,
    settings: Settings,
    llm: NarrativeLLM | None = None,
) -> None:
    """collect_counts -> format_digest -> optionally append one LLM-drafted
    narrative line if `settings.digest_narrative` -> telegram.send_message.
    Catches and logs its own exceptions; never propagates to the scheduler
    loop (invariant 5)."""
    try:
        counts = await collect_counts(service, settings)
        text = format_digest(counts, date.today())

        if settings.digest_narrative and llm is not None:
            narrative = await llm.complete(
                f"In one upbeat sentence, summarize this weekly job-search digest:\n\n{text}"
            )
            text = f"{text}\n\n{narrative}"

        await telegram.send_message(text)
    except Exception:
        logger.exception("digest: failed to run")
