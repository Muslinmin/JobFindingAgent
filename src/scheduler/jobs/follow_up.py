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
from typing import Protocol

from loguru import logger
from telegram import InlineKeyboardButton

from app.config import Settings
from app.models.job import Job
from app.services.service import JobService
from telegram_bot.notifications.client import NotificationTelegramClient


class DraftLLM(Protocol):
    """The narrow contract this job needs from an LLM client — text in,
    text out. `TaskLLMClient.complete` already satisfies this structurally
    (same pattern as tailoring.prompt.LLMTailor)."""

    async def complete(self, prompt: str) -> str: ...


def _cutoff(days: int) -> str:
    """`now - days`, isoformat — shared boundary helper, same shape as
    scheduler/jobs/lifecycle.py's `_cutoff`."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _assemble_followup_prompt(role: str, company: str, applied_date: str) -> str:
    """Builds the one-shot drafting prompt from role/company/applied_date.
    No profile, no JD — deliberately minimal context for a short nudge
    email."""
    return (
        "Draft a short, polite follow-up email to send after applying for a job "
        "and not hearing back yet. Keep it under 100 words, professional, and "
        "copy-paste ready — no placeholders.\n\n"
        f"Role: {role}\n"
        f"Company: {company}\n"
        f"Applied on: {applied_date}\n"
    )


async def _draft_and_push(
    job: Job, llm: DraftLLM, telegram: NotificationTelegramClient
) -> None:
    """`llm.complete(_assemble_followup_prompt(...))` -> drafted text, then
    `telegram.send_message_with_keyboard(text, [[Sent it], [Skip]])`. Raises
    on failure; the caller in `run_follow_up` catches and logs per-record."""
    prompt = _assemble_followup_prompt(job.role, job.company, job.status_changed_at)
    draft = await llm.complete(prompt)
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
