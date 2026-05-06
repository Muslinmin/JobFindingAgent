"""
Live integration tests — make real HTTP calls to the Tavily search API.

Skipped automatically when TAVILY_API_KEY is not set.
Run explicitly with:

    pytest src/test/integration/test_tavily_live.py -v -m live
"""

import asyncio
import pytest

from app.config import settings
from scraper.tavily_client import search
from scraper.parser import parse_results

pytestmark = pytest.mark.live

skip_if_no_key = pytest.mark.skipif(
    not settings.tavily_api_key,
    reason="TAVILY_API_KEY not set in .env — skipping live test",
)


@pytest.fixture(autouse=True)
async def rate_limit_delay():
    """Pause between live tests to avoid hitting Tavily rate limits."""
    yield
    await asyncio.sleep(3)


# ── tavily client ─────────────────────────────────────────────────────────────

@skip_if_no_key
async def test_tavily_returns_a_non_empty_list():
    results = await search("software engineer Singapore")
    assert isinstance(results, list)
    assert len(results) > 0


@skip_if_no_key
async def test_tavily_results_contain_url_and_title():
    results = await search("data engineer Singapore", max_results=3)
    for r in results:
        assert "url"   in r, f"Missing 'url' in result: {r}"
        assert "title" in r, f"Missing 'title' in result: {r}"


@skip_if_no_key
async def test_tavily_respects_max_results_cap():
    results = await search("software engineer Singapore", max_results=3)
    assert len(results) <= 3


@skip_if_no_key
async def test_tavily_returns_results_relevant_to_query():
    results = await search("software engineer Singapore", max_results=5)
    combined = " ".join(
        (r.get("title", "") + " " + r.get("content", "")).lower()
        for r in results
    )
    assert any(word in combined for word in ["engineer", "developer", "software", "tech"])


# ── parser on live results ────────────────────────────────────────────────────

@skip_if_no_key
async def test_parser_produces_records_from_live_results():
    raw     = await search("software engineer Singapore", max_results=5)
    parsed  = parse_results(raw)
    assert len(parsed) > 0


@skip_if_no_key
async def test_parser_records_have_correct_shape():
    raw    = await search("data engineer Singapore", max_results=5)
    parsed = parse_results(raw)
    for record in parsed:
        assert record["source"] == "tavily"
        assert record["url"]
        assert "company" in record
        assert "role"    in record
        assert "notes"   in record


@skip_if_no_key
async def test_parser_notes_do_not_exceed_500_chars():
    raw    = await search("software engineer Singapore", max_results=5)
    parsed = parse_results(raw)
    for record in parsed:
        if record["notes"] is not None:
            assert len(record["notes"]) <= 500
