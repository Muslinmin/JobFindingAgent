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
from agent.profile import read_profile

# Ensure loguru prints to stdout so pytest -s shows it
logger.remove()
logger.add(sys.stdout, level="DEBUG", colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

pytestmark = pytest.mark.live

skip_if_no_keys = pytest.mark.skipif(
    not settings.tavily_api_key or not settings.model_api_key,
    reason="TAVILY_API_KEY and MODEL_API_KEY must be set in .env — skipping live e2e test",
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


@skip_if_no_keys
async def test_profile_skills_flow_into_search_and_scoring(live_db, tmp_path, monkeypatch):
    """
    Full pipeline e2e:
      Turn 1  — user sets skills → agent calls update_profile → profile.json written
      Turn 2  — user requests search → agent calls search_jobs → Tavily returns listings
              → each job scored against profile skills and fingerprinted → inserted into DB
      Turn 3  — same search repeated → dedup holds, job count unchanged
    """
    monkeypatch.setattr(settings, "profile_path", str(tmp_path / "profile.json"))

    llm = LLMClient(model=settings.model)

    # ── Turn 1: populate profile ──────────────────────────────────────────────
    reply_1 = await run(
        messages=[{
            "role": "user",
            "content": "I am a Python developer. My skills are Python, FastAPI, and Docker.",
        }],
        db=live_db,
        llm=llm,
    )
    logger.info(f"[turn 1 reply]\n{reply_1}")

    profile = read_profile()
    logger.info(f"[profile] {profile}")
    assert "skills" in profile, "profile.json must contain 'skills' after turn 1"
    assert len(profile["skills"]) > 0, "skills list must not be empty"

    await asyncio.sleep(10)  # avoid burst-hitting LLM rate limits between turns

    # ── Turn 2: search for jobs ───────────────────────────────────────────────
    reply_2 = await run(
        messages=[{
            "role": "user",
            "content": "Find me Python backend engineer jobs in Singapore.",
        }],
        db=live_db,
        llm=llm,
    )
    logger.info(f"[turn 2 reply]\n{reply_2}")

    jobs = await repo.get_all_jobs(live_db)
    logger.info(f"[db] {len(jobs)} job(s) after search:")
    for job in jobs:
        logger.info(
            f"  {job['company']} — {job['role']} "
            f"| score={job['score']:.2f} | fp={job['fingerprint'][:12]}..."
        )

    assert len(jobs) > 0, "at least one job must be inserted after search"

    await asyncio.sleep(10)  # avoid burst-hitting LLM rate limits between turns

    for job in jobs:
        assert isinstance(job["score"], float), \
            f"score must be a float, got {type(job['score'])}"
        assert 0.0 <= job["score"] <= 1.0, \
            f"score out of range: {job['score']}"
        assert job["fingerprint"] is not None and len(job["fingerprint"]) == 64, \
            f"expected 64-char hex fingerprint, got '{job['fingerprint']}'"

    # ── Turn 3: dedup — same search must not grow the table ──────────────────
    count_before = len(jobs)
    await run(
        messages=[{
            "role": "user",
            "content": "Find me Python backend engineer jobs in Singapore.",
        }],
        db=live_db,
        llm=llm,
    )
    jobs_after = await repo.get_all_jobs(live_db)
    logger.info(f"[dedup] count before={count_before}, after={len(jobs_after)}")
    assert len(jobs_after) == count_before, (
        f"dedup failed: job count grew from {count_before} to {len(jobs_after)}"
    )
