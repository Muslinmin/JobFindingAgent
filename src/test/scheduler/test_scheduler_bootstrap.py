"""WP-S1 — scheduler wiring tests.

Only registration/trigger/start/stop is exercised here (scheduling_v2.md
invariant 4: "the scheduler wiring is tested only in the bootstrap
integration test"). No job body is called — `deps` holds plain Mocks.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from scheduler.bootstrap import SchedulerDeps, register_jobs, start_scheduler, stop_scheduler


def _settings(**overrides) -> Settings:
    return Settings(
        scrape_hour=2, lifecycle_hour=3, followup_hour=4, tailor_hour=5, digest_day="mon", **overrides
    )


def _deps() -> SchedulerDeps:
    return SchedulerDeps(
        service=MagicMock(),
        scorer=MagicMock(),
        llm=MagicMock(),
        telegram=MagicMock(),
        adapters=[],
        profile_path=Path("profile.json"),
        queries_path=Path("search_queries.json"),
        template_path=Path("template.tex.jinja"),
        output_dir=Path("artifacts"),
    )


def _field(job, name: str) -> str:
    return str({f.name: f for f in job.trigger.fields}[name])


@pytest.fixture
def scheduler():
    # No teardown here: AsyncIOScheduler.shutdown() defers work onto the
    # event loop (see scheduler.bootstrap.stop_scheduler's docstring), so a
    # plain sync fixture finalizer running after an async test's loop has
    # already closed would raise "Event loop is closed". Tests that start
    # the scheduler are responsible for awaiting stop_scheduler themselves
    # before returning.
    return AsyncIOScheduler()


# ── registration ──────────────────────────────────────────────────────────────

def test_all_six_jobs_registered(scheduler):
    register_jobs(scheduler, _deps(), _settings())
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {"query_regen", "scrape", "lifecycle", "follow_up", "tailor", "digest"}


def test_daily_job_hours_match_settings(scheduler):
    settings = _settings()
    register_jobs(scheduler, _deps(), settings)

    assert _field(scheduler.get_job("scrape"), "hour") == str(settings.scrape_hour)
    assert _field(scheduler.get_job("lifecycle"), "hour") == str(settings.lifecycle_hour)
    assert _field(scheduler.get_job("follow_up"), "hour") == str(settings.followup_hour)
    assert _field(scheduler.get_job("tailor"), "hour") == str(settings.tailor_hour)


def test_query_regen_runs_monday_before_scrape(scheduler):
    settings = _settings()
    register_jobs(scheduler, _deps(), settings)

    job = scheduler.get_job("query_regen")
    assert _field(job, "day_of_week") == "mon"
    assert _field(job, "hour") == str(settings.scrape_hour - 1)


def test_digest_runs_monday_after_tailor(scheduler):
    settings = _settings()
    register_jobs(scheduler, _deps(), settings)

    job = scheduler.get_job("digest")
    assert _field(job, "day_of_week") == "mon"
    assert _field(job, "hour") == str(settings.tailor_hour + 1)


async def test_re_registering_after_a_start_stop_cycle_replaces_jobs(scheduler):
    """The real usage pattern: app/main.py's module-level `scheduler` is
    registered against and started/stopped across repeated FastAPI lifespan
    entries (e.g. once per test using the app's lifespan). `replace_existing`
    only takes effect once APScheduler has flushed its pending-job queue via
    a `start()` — calling `register_jobs` twice on a scheduler that has
    never been started is not a supported sequence and is not exercised
    here."""
    register_jobs(scheduler, _deps(), _settings())
    await start_scheduler(scheduler)
    await stop_scheduler(scheduler)

    register_jobs(scheduler, _deps(), _settings())
    assert len(scheduler.get_jobs()) == 6


# ── start/stop ────────────────────────────────────────────────────────────────

async def test_start_scheduler_starts_it(scheduler):
    await start_scheduler(scheduler)
    assert scheduler.running
    await stop_scheduler(scheduler)


async def test_stop_scheduler_stops_it(scheduler):
    await start_scheduler(scheduler)
    await stop_scheduler(scheduler)
    assert not scheduler.running
