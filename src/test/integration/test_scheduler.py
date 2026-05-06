"""
Integration tests for the APScheduler wiring in app/main.py.

Tests call _scheduled_scrape() directly (mocking Tavily + get_db) and verify
the scheduler job is registered correctly on app startup.

Run with:
    pytest src/test/integration/test_scheduler.py -v
"""

import pytest
import aiosqlite
from unittest.mock import patch, AsyncMock, MagicMock

from app.main import app, lifespan
from app.db.database import create_tables
from app.db import repository as repo


FAKE_TAVILY_RESULTS = [
    {"title": "Data Engineer — GovTech", "url": "https://careers.gov.sg/1", "content": "Python, SQL."},
    {"title": "Backend Engineer | DBS",  "url": "https://dbs.com/jobs/2",   "content": "FastAPI."},
]


@pytest.fixture
async def mem_db():
    """In-memory DB, tables created, row_factory set."""
    async with aiosqlite.connect(":memory:") as conn:
        await create_tables(conn)
        conn.row_factory = aiosqlite.Row
        yield conn


def _fake_get_db(conn):
    """Return an async generator that yields the given connection."""
    async def _gen():
        yield conn
    return _gen


def _mock_db_lifespan():
    """Patch the aiosqlite connect + create_tables calls inside the lifespan."""
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__  = AsyncMock(return_value=False)
    return (
        patch("app.main.aiosqlite.connect", return_value=mock_conn),
        patch("app.main.create_tables",     new_callable=AsyncMock),
    )


# ── scheduler registration ────────────────────────────────────────────────────

async def test_scheduler_job_is_registered():
    """
    The 'daily_scrape' job must exist in the scheduler after the app
    lifespan starts. If it's missing, the scheduled scrape will never run.
    """
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            job = scheduler.get_job("daily_scrape")
            assert job is not None, "daily_scrape job was not registered in the scheduler"


async def test_scheduler_job_interval_is_24_hours():
    """
    The trigger interval must be exactly 24 hours. A wrong value would
    cause scrapes to run too frequently (burning quota) or too rarely.
    """
    from app.main import scheduler
    p1, p2 = _mock_db_lifespan()
    with p1, p2:
        async with lifespan(app):
            job = scheduler.get_job("daily_scrape")
            assert job is not None
            assert job.trigger.interval.total_seconds() == 86400


# ── _scheduled_scrape behaviour ───────────────────────────────────────────────

async def test_scheduled_scrape_inserts_jobs(mem_db):
    """
    When Tavily returns results, _scheduled_scrape() must parse and insert
    each record into the DB. Verifies the full Tavily → parser → repo chain
    fires correctly inside the scheduler job.
    """
    from app.main import _scheduled_scrape

    with patch("app.main.tavily_search", new_callable=AsyncMock, return_value=FAKE_TAVILY_RESULTS), \
         patch("app.main.get_db", new=_fake_get_db(mem_db)):
        await _scheduled_scrape()

    jobs = await repo.get_all_jobs(mem_db)
    assert len(jobs) == 2
    urls = {j["url"] for j in jobs}
    assert "https://careers.gov.sg/1" in urls
    assert "https://dbs.com/jobs/2"   in urls


async def test_scheduled_scrape_skips_duplicates(mem_db):
    """
    Running _scheduled_scrape() twice with the same Tavily results must not
    double-insert. The fingerprint constraint in the DB makes inserts idempotent —
    this test confirms the scheduler respects that and doesn't raise on duplicates.
    """
    from app.main import _scheduled_scrape

    with patch("app.main.tavily_search", new_callable=AsyncMock, return_value=FAKE_TAVILY_RESULTS), \
         patch("app.main.get_db", new=_fake_get_db(mem_db)):
        await _scheduled_scrape()
        await _scheduled_scrape()

    jobs = await repo.get_all_jobs(mem_db)
    assert len(jobs) == 2


async def test_scheduled_scrape_handles_tavily_failure(mem_db):
    """
    If Tavily is unavailable, _scheduled_scrape() must not raise. The
    tavily_client already returns [] on failure — this test confirms the
    scheduler job handles that gracefully and leaves the DB untouched.
    """
    from app.main import _scheduled_scrape

    with patch("app.main.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("app.main.get_db", new=_fake_get_db(mem_db)):
        await _scheduled_scrape()

    jobs = await repo.get_all_jobs(mem_db)
    assert len(jobs) == 0
