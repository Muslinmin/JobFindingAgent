"""Tailor job (scheduling_v2.md WP-S5).

Daily job, runs after lifecycle. Selects the top `tailor_batch_size` SCORED
records via `service.top_scored_for_tailoring(limit)` — the deterministic
ranking (score DESC, posted_at DESC, id ASC) is enforced inside the
repository query itself (app/db/repository.py `top_scored_for_tailoring`),
not in this job.

`profile_path` is threaded all the way down to `_tailor_one`, which calls
`load_profile(profile_path)` itself, once per record, immediately before
calling `tailor()` — the same "path in, load at the point of use" shape as
scheduler/jobs/scrape.py. `tailor()` itself still only ever accepts an
already-loaded `Profile` instance (tailoring/tailor.py's contract is
unchanged); `_tailor_one` is the boundary that does that load. Reloading
once per record (not once per run) means a profile edit lands mid-batch,
not just at the next run — and the cost is one cheap local JSON parse for
a batch that's already bounded to `tailor_batch_size` (default 10).

Per-record flow mirrors `app/tailoring_usage_example.py`'s `tailor_one_job`
exactly, with a Telegram push appended: `tailor()` -> on success,
`service.transition_status(TAILORED)` (required — the FSM only allows
SCORED -> {TAILORED, REJECTED}, so PENDING_APPROVAL can never be reached
directly from SCORED) -> `service.register_artifact(cv_pdf)` ->
`service.transition_status(PENDING_APPROVAL)` -> `telegram.send_document(...)`
+ `telegram.send_message_with_keyboard([[Mark Applied], [Skip]])`.

A `TailoringError` (guard violation, schema-invalid, LLM/render failure) on
one record is logged and that record is left at SCORED for tomorrow's
batch to retry — never aborts the rest of the batch (invariant 5).
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from telegram import InlineKeyboardButton

from agent.llm_client import TaskLLMClient
from app.config import Settings
from app.models.enums import ApplicationStatus, ArtifactKind, UserAction
from app.models.job import ArtifactCreate, Job
from app.services.service import JobService
from profile.loader import load_profile
from tailoring.tailor import TailoringError, tailor
from telegram_bot.notifications.client import NotificationTelegramClient


async def _tailor_one(
    job: Job,
    service: JobService,
    llm: TaskLLMClient,
    telegram: NotificationTelegramClient,
    profile_path: Path,
    template_path: Path,
    output_dir: Path,
) -> bool:
    """`load_profile(profile_path)` -> tailor -> TAILORED -> register_artifact
    -> PENDING_APPROVAL -> push. Returns whether the record was
    successfully tailored — `run_tailor` uses this to count the batch, not
    exceptions, since a `TailoringError` is caught and logged here rather
    than raised. Any other exception (a service call failing) propagates to
    the caller, which logs and moves to the next record (invariant 5)."""
    try:
        profile = load_profile(profile_path)
    except Exception:
        logger.exception(f"tailor: failed to load profile for job {job.id}")
        return False

    try:
        result = await tailor(
            job.description,
            profile,
            llm=llm,
            template_path=template_path,
            output_dir=Path(output_dir) / str(job.id),
        )
    except TailoringError as e:
        logger.error(f"tailor() failed for job {job.id}: reason={e.reason} violations={e.violations}")
        return False

    # Record TAILORED first so a crash between these two calls never
    # leaves an artifact-less job stuck reporting SCORED (matches
    # app/tailoring_usage_example.py's tailor_one_job ordering).
    await service.transition_status(job.id, ApplicationStatus.TAILORED)
    await service.register_artifact(
        job.id, ArtifactCreate(kind=ArtifactKind.CV_PDF, path=str(result.path))
    )
    await service.transition_status(job.id, ApplicationStatus.PENDING_APPROVAL)

    text = f"{job.role} at {job.company}\nScore: {job.score}\n{job.url}"
    pdf_bytes = Path(result.path).read_bytes()
    await telegram.send_document(text, pdf_bytes, filename=f"{job.company}_{job.role}.pdf")

    # callback_data contract: telegram_bot/notifications/handlers.py's
    # _parse_callback_data expects {"kind": "action", "job_id": ..., "action": ...},
    # where "action" is an opaque UserAction string it never interprets.
    keyboard = [
        [
            InlineKeyboardButton(
                "Mark Applied",
                callback_data=json.dumps(
                    {"kind": "action", "job_id": job.id, "action": UserAction.APPLIED.value}
                ),
            ),
            InlineKeyboardButton(
                "Skip",
                callback_data=json.dumps(
                    {"kind": "action", "job_id": job.id, "action": UserAction.USER_SKIPPED.value}
                ),
            ),
        ]
    ]
    await telegram.send_message_with_keyboard("Approve this application?", keyboard)
    return True


async def run_tailor(
    service: JobService,
    llm: TaskLLMClient,
    telegram: NotificationTelegramClient,
    settings: Settings,
    profile_path: Path,
    template_path: Path,
    output_dir: Path,
) -> None:
    """Select the top `tailor_batch_size` SCORED records and tailor each in
    turn via `_tailor_one`, passing `profile_path` straight through
    unopened. Sequential on purpose — each call is a real LLM completion
    plus a real `tectonic` subprocess compile (tailoring/tailor.py already
    offloads the blocking compile to a worker thread; parallelizing across
    records is a throughput policy decision outside this job's scope, per
    app/tailoring_usage_example.py `run_daily_batch`)."""
    jobs = await service.top_scored_for_tailoring(settings.tailor_batch_size)

    n_tailored = 0
    for job in jobs:
        try:
            if await _tailor_one(job, service, llm, telegram, profile_path, template_path, output_dir):
                n_tailored += 1
        except Exception:
            logger.exception(f"tailor: failed to process job {job.id}")

    logger.info(f"tailor: n_tailored={n_tailored} of {len(jobs)} candidates")
