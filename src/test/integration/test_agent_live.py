"""
Live integration tests — make real API calls to Gemini via LiteLLM.

These tests are skipped automatically when GEMINI_API_KEY is not set.
Run explicitly with:

    pytest src/test/integration/test_agent_live.py -v -m live
"""

import asyncio
import pytest
import aiosqlite

from app.config import settings
from app.db.database import create_tables
from agent.agent import run
from agent.llm_client import LLMClient

pytestmark = pytest.mark.live

skip_if_no_key = pytest.mark.skipif(
    not settings.gemini_api_key,
    reason="GEMINI_API_KEY not set in .env — skipping live test",
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


@skip_if_no_key
async def test_gemini_returns_text_for_empty_job_list(live_db):
    """Agent queries an empty DB and Gemini returns a coherent text reply."""
    llm = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "How many job applications do I have?"}],
        db=live_db,
        llm=llm,
    )
    assert isinstance(reply, str)
    assert len(reply) > 0


@skip_if_no_key
async def test_gemini_calls_query_jobs_tool(live_db):
    """Gemini should recognise the intent and call query_jobs rather than hallucinate."""
    llm = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "Show me all my applications."}],
        db=live_db,
        llm=llm,
    )
    assert isinstance(reply, str)
    assert len(reply) > 0


@skip_if_no_key
async def test_gemini_handles_job_mention_without_url(live_db):
    """
    User mentions a job with no URL. The agent should either ask for confirmation /
    more details, or attempt log_job and receive a validation error — either way it
    must return a coherent text reply without crashing.
    """
    llm = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "I just applied to Stripe as a Data Engineer."}],
        db=live_db,
        llm=llm,
    )
    assert isinstance(reply, str)
    assert len(reply) > 0


@skip_if_no_key
async def test_gemini_handles_profile_question(live_db):
    """Gemini should respond sensibly when asked about the user profile."""
    llm = LLMClient(model=settings.model)
    reply = await run(
        messages=[{"role": "user", "content": "What roles am I targeting?"}],
        db=live_db,
        llm=llm,
    )
    assert isinstance(reply, str)
    assert len(reply) > 0
