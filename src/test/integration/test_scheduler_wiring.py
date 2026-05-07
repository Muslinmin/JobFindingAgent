"""
Integration tests for scheduler wiring — both scrape and digest jobs registered.

Verifies the lifespan hook registers both jobs with the correct triggers and
shuts the scheduler down cleanly. Does not test job behaviour (covered by unit tests).

Run with:
    pytest src/test/integration/test_scheduler_wiring.py -v
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.main import app, lifespan


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_db_lifespan():
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    return (
        patch("app.main.aiosqlite.connect", return_value=mock_conn),
        patch("app.main.create_tables", new_callable=AsyncMock),
    )


# ── job registration ──────────────────────────────────────────────────────────

async def test_both_scrape_and_digest_jobs_registered():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            assert scheduler.get_job("daily_scrape") is not None, \
                "daily_scrape job was not registered"
            assert scheduler.get_job("weekly_digest") is not None, \
                "weekly_digest job was not registered"


async def test_scrape_job_interval_is_24_hours():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            job = scheduler.get_job("daily_scrape")
            assert job is not None
            assert job.trigger.interval.total_seconds() == 86400


async def test_digest_job_cron_is_monday():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            job = scheduler.get_job("weekly_digest")
            assert job is not None
            fields = {f.name: f for f in job.trigger.fields}
            assert str(fields["day_of_week"]) == "mon"


async def test_lifespan_teardown_shuts_scheduler_cleanly():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            pass

    assert not scheduler.running, "Scheduler should be stopped after lifespan exits"
