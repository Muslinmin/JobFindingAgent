"""Follow-up job (scheduling_v2.md WP-S4).

Daily job. The *check* is deterministic and free — both qualifying buckets
are pre-filtered server-side by the facade (`list_follow_up_candidates`,
`list_second_nudge_candidates` — app/services/service.py), keyed off
`status_changed_at` / `last_follow_up_at`, never on user activity elsewhere.
The LLM is only touched to draft the handful of emails that actually
qualify:

    first nudge:  APPLIED, status_changed_at > follow_up_after_days,
                  follow_up_count = 0, not yet nudged
    second nudge: APPLIED, follow_up_count = 1,
                  last_follow_up_at > follow_up_after_days (optional)

A successful draft+push stamps `follow_up_nudge_at` via
`service.mark_follow_up_nudged` — "we reminded you", distinct from
`record_follow_up` ("you told us you sent it"), which only fires from the
user's `[Sent it]` button tap via `POST /jobs/{id}/follow-up`, never from
this job. The ghost clock is untouched here: it keys off
`status_changed_at`, which this job never writes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from loguru import logger
from telegram import InlineKeyboardButton

from app.config import Settings
from app.models.job import Job
from app.services.service import JobService
from drafting.followup import DraftLLM, assemble_followup_prompt, draft_followup
from telegram_bot.notifications.client import NotificationTelegramClient

# `DraftLLM` and `assemble_followup_prompt` are re-exported rather than
# defined here: the drafting itself moved to `drafting/followup.py` when the
# agent's `draft_followup` tool needed it (agent_v2.md §7), and the two
# callers must not drift on the prompt. What stayed is the *nudge* — bucket
# selection, the push, and the `mark_follow_up_nudged` stamp.
__all__ = ["DraftLLM", "assemble_followup_prompt", "run_follow_up"]


def _cutoff(days: int) -> str:
    """`now - days`, isoformat — shared boundary helper, same shape as
    scheduler/jobs/lifecycle.py's `_cutoff`."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def _draft_and_push(
    job: Job, llm: DraftLLM, telegram: NotificationTelegramClient
) -> None:
    """`draft_followup(...)` -> drafted text, then
    `telegram.send_message_with_keyboard(text, [[Sent it], [Skip]])`. Raises
    on failure; the caller in `run_follow_up` catches and logs per-record.

    No `note`: a scheduled nudge has nobody to ask. That parameter only
    ever arrives from the agent path.
    """
    draft = await draft_followup(job.role, job.company, job.status_changed_at, llm)
    text = f"Follow-up due — {job.role} at {job.company}\n\n{draft}"

    # callback_data contract: telegram_bot/notifications/handlers.py's
    # _parse_callback_data expects {"kind": ..., "job_id": ...}. "Sent it"
    # -> POST /jobs/{id}/follow-up (kind=followup); "Skip" -> no backend
    # call (kind=dismiss) — this job never touches follow_up_count/status.
    keyboard = [
        [
            InlineKeyboardButton(
                "Sent it", callback_data=json.dumps({"kind": "followup", "job_id": job.id})
            ),
            InlineKeyboardButton(
                "Skip", callback_data=json.dumps({"kind": "dismiss", "job_id": job.id})
            ),
        ]
    ]
    await telegram.send_message_with_keyboard(text, keyboard)


async def run_follow_up(
    service: JobService,
    llm: DraftLLM,
    telegram: NotificationTelegramClient,
    settings: Settings,
) -> None:
    """Pull both qualifying buckets via the facade, draft + push for each,
    then `service.mark_follow_up_nudged(job.id)` on success. One record's
    failure (LLM error, Telegram error) is caught and logged; the batch
    continues (invariant 5)."""
    first_batch = await service.list_follow_up_candidates(_cutoff(settings.follow_up_after_days))
    second_batch = await service.list_second_nudge_candidates(_cutoff(settings.follow_up_after_days))

    n_nudged = 0
    for job in [*first_batch, *second_batch]:
        try:
            await _draft_and_push(job, llm, telegram)
            await service.mark_follow_up_nudged(job.id)
            n_nudged += 1
        except Exception:
            logger.exception(f"follow_up: failed to nudge job {job.id}")

    logger.info(f"follow_up: n_nudged={n_nudged}")
