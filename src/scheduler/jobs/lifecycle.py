"""Lifecycle job — three deterministic time rules (scheduling_v2.md WP-S3).

Daily job, zero LLM calls. All three rules key off `status_changed_at`
(never `updated_at`) via the facade's dedicated bucket reads
(`list_expired_pending_approval`, `list_stale_scored`, `list_ghost_candidates`
— app/services/service.py), each already filtered server-side:

    PENDING_APPROVAL  older than pending_expiry_days  -> EXPIRED   (silent)
    SCORED            older than stale_after_days      -> REJECTED  (silent)
    APPLIED/INTERVIEWING older than ghost_after_days   -> GHOSTED   (+ Telegram note)

Each transition is a `service.transition_status(job_id, to_status)` call —
never a direct database write. A single record's `transition_status`
failure is caught and logged; the batch continues (invariant 5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from app.config import Settings
from app.models.enums import ApplicationStatus
from app.services.service import JobService
from telegram_bot.notifications.client import NotificationTelegramClient


def _cutoff(days: int) -> str:
    """`now - days`, isoformat — the boundary every bucket read compares
    `status_changed_at` against. Exactly `days` old does NOT transition
    (strictly-older, matching the WP-S3 boundary tests)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def _expire_pending(service: JobService, settings: Settings) -> int:
    """PENDING_APPROVAL -> EXPIRED, silent. Returns the count transitioned."""
    jobs = await service.list_expired_pending_approval(_cutoff(settings.pending_expiry_days))
    n = 0
    for job in jobs:
        try:
            await service.transition_status(job.id, ApplicationStatus.EXPIRED)
            n += 1
        except Exception:
            logger.exception(f"lifecycle: failed to expire job {job.id}")
    return n


async def _reject_stale(service: JobService, settings: Settings) -> int:
    """SCORED -> REJECTED, silent. Returns the count transitioned."""
    jobs = await service.list_stale_scored(_cutoff(settings.stale_after_days))
    n = 0
    for job in jobs:
        try:
            await service.transition_status(job.id, ApplicationStatus.REJECTED)
            n += 1
        except Exception:
            logger.exception(f"lifecycle: failed to reject stale job {job.id}")
    return n


async def _ghost_silent_applicants(
    service: JobService, telegram: NotificationTelegramClient, settings: Settings
) -> int:
    """APPLIED/INTERVIEWING -> GHOSTED. Sends one Telegram note per
    transition: 'No response from {company} ({role}) — marked as ghosted.'
    Returns the count transitioned."""
    jobs = await service.list_ghost_candidates(_cutoff(settings.ghost_after_days))
    n = 0
    for job in jobs:
        try:
            await service.transition_status(job.id, ApplicationStatus.GHOSTED)
            await telegram.send_message(
                f"No response from {job.company} ({job.role}) — marked as ghosted."
            )
            n += 1
        except Exception:
            logger.exception(f"lifecycle: failed to ghost job {job.id}")
    return n


async def run_lifecycle(
    service: JobService,
    telegram: NotificationTelegramClient,
    settings: Settings,
) -> None:
    """Run all three rules in one invocation. Logs a summary
    (n_expired, n_stale_rejected, n_ghosted) at the end. Each rule's own
    per-record failures are caught inside the corresponding helper; each
    bucket is also wrapped here so one bucket raising unexpectedly (e.g. the
    facade read itself failing) doesn't skip the remaining two."""
    n_expired = n_stale_rejected = n_ghosted = 0

    try:
        n_expired = await _expire_pending(service, settings)
    except Exception:
        logger.exception("lifecycle: _expire_pending bucket failed")

    try:
        n_stale_rejected = await _reject_stale(service, settings)
    except Exception:
        logger.exception("lifecycle: _reject_stale bucket failed")

    try:
        n_ghosted = await _ghost_silent_applicants(service, telegram, settings)
    except Exception:
        logger.exception("lifecycle: _ghost_silent_applicants bucket failed")

    logger.info(
        f"lifecycle: n_expired={n_expired} n_stale_rejected={n_stale_rejected} n_ghosted={n_ghosted}"
    )
