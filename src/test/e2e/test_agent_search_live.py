"""
Live e2e tests — full pipeline: user message → agent LLM → search_jobs tool
→ real Tavily API → DB insertion.

Skipped automatically when either key is missing.
Run explicitly with:

    pytest src/test/e2e/test_agent_search_live.py -v -m live -s
"""

import asyncio
import pytest
import aiosqlite
from loguru import logger
import sys

from app.config import settings
from app.db.database import create_tables
from app.db import repository as repo
from agent.agent import run
from agent.llm_client import LLMClient

# Ensure loguru prints to stdout so pytest -s shows it
logger.remove()
logger.add(sys.stdout, level="DEBUG", colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

pytestmark = pytest.mark.live

skip_if_no_keys = pytest.mark.skipif(
    not settings.tavily_api_key or not settings.gemini_api_key,
    reason="TAVILY_API_KEY or GEMINI_API_KEY not set in .env — skipping live e2e test",
)


@pytest.fixture(autouse=True)
async def rate_limit_delay():
    """Pause between live tests to avoid hitting LLM provider rate limits."""
    yield
    await asyncio.sleep(15)


@pytest.fixture
async def live_db(tmp_path):
    db_path = str(tmp_path / "live_test.db")
    async with aiosqlite.connect(db_path) as conn:
        await create_tables(conn)
        conn.row_factory = aiosqlite.Row
        yield conn


# ── agent + tavily pipeline ───────────────────────────────────────────────────

@skip_if_no_keys
async def test_agent_search_returns_text_reply(live_db):
    """Agent receives a search request and returns a coherent reply."""
    llm   = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "find me data engineer jobs in Singapore"}],
        db=live_db,
        llm=llm,
    )
    logger.info(f"[agent reply]\n{reply}")
    assert isinstance(reply, str)
    assert len(reply) > 0


@skip_if_no_keys
async def test_agent_search_inserts_jobs_into_db(live_db):
    """After a search request, the DB should contain at least one job record."""
    llm = LLMClient(model=settings.model)
    await run(
        messages=[{"role": "user", "content": "find me software engineer jobs in Singapore"}],
        db=live_db,
        llm=llm,
    )
    jobs = await repo.get_all_jobs(live_db)
    logger.info(f"[db] {len(jobs)} job(s) inserted:")
    for job in jobs:
        logger.info(f"  id={job['id']} | {job['company']} — {job['role']} | status={job['status']} | {job['url']}")
    assert len(jobs) > 0


@skip_if_no_keys
async def test_agent_search_reply_acknowledges_results(live_db):
    """Agent reply should mention jobs, results, or listings — not a generic error."""
    llm   = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "search for backend engineer jobs in Singapore"}],
        db=live_db,
        llm=llm,
    )
    logger.info(f"[agent reply]\n{reply}")
    lower = reply.lower()
    assert any(word in lower for word in ["job", "found", "result", "engineer", "position", "listing", "role"])


@skip_if_no_keys
async def test_agent_search_jobs_have_status_found(live_db):
    """All jobs inserted via search should start with status 'found'."""
    llm = LLMClient(model=settings.model)
    await run(
        messages=[{"role": "user", "content": "find me data engineer jobs in Singapore"}],
        db=live_db,
        llm=llm,
    )
    jobs = await repo.get_all_jobs(live_db)
    logger.info(f"[db] {len(jobs)} job(s) status check:")
    for job in jobs:
        logger.info(f"  {job['company']} — {job['role']} | status={job['status']}")
        assert job["status"] == "found"
