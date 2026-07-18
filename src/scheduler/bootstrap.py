"""Scheduler bootstrap (scheduling_v2.md WP-S1).

Registers all six daily/weekly jobs on an `AsyncIOScheduler` with cron
triggers sourced from `Settings`. The scheduler itself is never imported in
job unit tests — every job in `scheduler/jobs/` is a plain async function
tested by calling it directly with injected fakes. This module is the one
place the scheduler wiring (registration, start, stop) is exercised, per
scheduling_v2.md invariant 4.

`SchedulerDeps` is the injection bundle: every client the jobs need,
constructed once at the FastAPI lifespan's composition root and shared
across all six jobs. The scheduler owns none of them — see
architecture_v2.md § 3 "Dependency ownership".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.llm_client import AsyncLLMClient
from app.config import Settings
from app.services.service import JobService
from scoring.protocol import Scorer
from scraper.protocol import JobSource
from scheduler.jobs.digest import run_digest
from scheduler.jobs.follow_up import run_follow_up
from scheduler.jobs.lifecycle import run_lifecycle
from scheduler.jobs.query_regen import run_query_regen
from scheduler.jobs.scrape import run_scrape
from scheduler.jobs.tailor import run_tailor
from telegram_bot.notifications.client import NotificationTelegramClient


@dataclass(frozen=True)
class SchedulerDeps:
    """Every dependency a scheduled job needs, constructed once in the
    FastAPI lifespan hook. Fields map 1:1 onto the interfaces table in
    scheduling_v2.md § Skeleton — Files & Functions."""

    service: JobService
    scorer: Scorer
    llm: AsyncLLMClient
    telegram: NotificationTelegramClient
    adapters: list[JobSource]
    profile_path: Path
    queries_path: Path
    template_path: Path
    output_dir: Path


def register_jobs(scheduler: AsyncIOScheduler, deps: SchedulerDeps, settings: Settings) -> None:
    """Register all six jobs with cron triggers from `settings`.

    Daily order matches scheduling_v2.md § Daily execution order: query_regen
    (Monday only, before scrape) -> scrape+score -> lifecycle -> follow_up ->
    tailor -> digest (Monday only, after daily jobs). Each job is bound to
    its dependencies via `functools.partial` so the registered callable
    takes no arguments, as APScheduler expects.

    `query_regen` and `digest` have no dedicated hour setting (only
    `digest_day`), since scheduling_v2.md's settings table doesn't carve out
    separate hours for them — they're positioned relative to the four daily
    hours instead: query_regen one hour before `scrape_hour` (so its output
    is on disk before that same Monday's scrape reads it), digest one hour
    after `tailor_hour` (so it reflects the day's final state).
    """
    scheduler.add_job(
        partial(
            run_query_regen,
            llm=deps.llm,
            settings=settings,
            profile_path=deps.profile_path,
            queries_path=deps.queries_path,
        ),
        CronTrigger(day_of_week=settings.digest_day, hour=max(settings.scrape_hour - 1, 0), minute=0),
        id="query_regen",
        replace_existing=True,
    )
    scheduler.add_job(
        partial(
            run_scrape,
            adapters=deps.adapters,
            service=deps.service,
            scorer=deps.scorer,
            settings=settings,
            queries_path=deps.queries_path,
            profile_path=deps.profile_path,
            delay_s=settings.adapter_delay_s,
        ),
        CronTrigger(hour=settings.scrape_hour, minute=0),
        id="scrape",
        replace_existing=True,
    )
    scheduler.add_job(
        partial(run_lifecycle, service=deps.service, telegram=deps.telegram, settings=settings),
        CronTrigger(hour=settings.lifecycle_hour, minute=0),
        id="lifecycle",
        replace_existing=True,
    )
    scheduler.add_job(
        partial(run_follow_up, service=deps.service, llm=deps.llm, telegram=deps.telegram, settings=settings),
        CronTrigger(hour=settings.followup_hour, minute=0),
        id="follow_up",
        replace_existing=True,
    )
    scheduler.add_job(
        partial(
            run_tailor,
            service=deps.service,
            llm=deps.llm,
            telegram=deps.telegram,
            settings=settings,
            profile_path=deps.profile_path,
            template_path=deps.template_path,
            output_dir=deps.output_dir,
        ),
        CronTrigger(hour=settings.tailor_hour, minute=0),
        id="tailor",
        replace_existing=True,
    )
    scheduler.add_job(
        partial(
            run_digest,
            service=deps.service,
            telegram=deps.telegram,
            settings=settings,
            llm=deps.llm if settings.digest_narrative else None,
        ),
        CronTrigger(day_of_week=settings.digest_day, hour=settings.tailor_hour + 1, minute=0),
        id="digest",
        replace_existing=True,
    )


async def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Start the scheduler. Called from the FastAPI lifespan startup phase
    alongside `start_bots()`."""
    scheduler.start()


async def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Graceful shutdown, called on lifespan teardown.

    `AsyncIOScheduler.shutdown()` defers its actual work onto the event loop
    via `call_soon_threadsafe` rather than completing synchronously, so
    `scheduler.running` is still True the instant this call returns. The
    `asyncio.sleep(0)` yields one tick so that callback runs before this
    coroutine returns — callers can rely on the scheduler being fully
    stopped as soon as `stop_scheduler` is awaited.
    """
    scheduler.shutdown()
    await asyncio.sleep(0)
