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


def _mock_telegram_lifespan():
    mock_updater = MagicMock()
    mock_updater.start_polling = AsyncMock()
    mock_updater.stop = AsyncMock()

    mock_ptb_app = MagicMock()
    mock_ptb_app.initialize = AsyncMock()
    mock_ptb_app.start = AsyncMock()
    mock_ptb_app.stop = AsyncMock()
    mock_ptb_app.shutdown = AsyncMock()
    mock_ptb_app.updater = mock_updater
    mock_ptb_app.add_handler = MagicMock()

    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_ptb_app

    mock_application_cls = MagicMock()
    mock_application_cls.builder.return_value = mock_builder

    return mock_ptb_app, patch("app.main.Application", mock_application_cls)


# ── job registration ──────────────────────────────────────────────────────────

async def test_both_scrape_and_digest_jobs_registered():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    _, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            assert scheduler.get_job("daily_scrape") is not None, \
                "daily_scrape job was not registered"
            assert scheduler.get_job("weekly_digest") is not None, \
                "weekly_digest job was not registered"


async def test_scrape_job_interval_is_24_hours():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    _, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            job = scheduler.get_job("daily_scrape")
            assert job is not None
            assert job.trigger.interval.total_seconds() == 86400


async def test_digest_job_cron_is_monday():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    _, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            job = scheduler.get_job("weekly_digest")
            assert job is not None
            fields = {f.name: f for f in job.trigger.fields}
            assert str(fields["day_of_week"]) == "mon"


async def test_lifespan_teardown_shuts_scheduler_cleanly():
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    _, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            pass

    assert not scheduler.running, "Scheduler should be stopped after lifespan exits"


# ── telegram bot lifecycle ────────────────────────────────────────────────────

async def test_bot_polling_starts_on_startup():
    p1, p2 = _mock_db_lifespan()
    mock_ptb_app, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            mock_ptb_app.updater.start_polling.assert_called_once()


async def test_bot_polling_stops_on_teardown():
    p1, p2 = _mock_db_lifespan()
    mock_ptb_app, p3 = _mock_telegram_lifespan()
    with p1, p2, p3:
        async with lifespan(app):
            pass

    mock_ptb_app.updater.stop.assert_called_once()
    mock_ptb_app.stop.assert_called_once()
    mock_ptb_app.shutdown.assert_called_once()
