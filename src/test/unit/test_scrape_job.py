"""
Unit tests for jobs/scrape_job.py — scrape_job() plain function.

Jobs are tested by calling scrape_job() directly — no scheduler is started.
Mocks: tavily_search (Tavily client), httpx.AsyncClient (POST /jobs).
Tested: search terms forwarded, results posted, empty list handling, failure handling.

Run with:
    pytest src/test/unit/test_scrape_job.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from jobs.scrape_job import scrape_job


FAKE_RAW_RESULTS = [
    {"title": "Data Engineer — GovTech",  "url": "https://careers.gov.sg/1", "content": "Python, SQL."},
    {"title": "Backend Engineer | DBS",   "url": "https://dbs.com/jobs/2",   "content": "FastAPI, Docker."},
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_http_client(status_code: int = 201):
    mock_response = MagicMock()
    mock_response.status_code = status_code

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_client


# ── Tavily call ───────────────────────────────────────────────────────────────

async def test_scrape_job_calls_tavily_with_configured_query():
    mock_cls, mock_client = _mock_http_client()

    with patch("jobs.scrape_job.tavily_search", new_callable=AsyncMock, return_value=[]) as mock_tavily, \
         patch("jobs.scrape_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.scrape_job.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        mock_settings.api_base_url = "http://localhost:8000"
        await scrape_job()

    mock_tavily.assert_called_once_with("software engineer Singapore")


# ── result forwarding ─────────────────────────────────────────────────────────

async def test_scrape_job_posts_each_parsed_result_to_jobs_api():
    mock_cls, mock_client = _mock_http_client()

    with patch("jobs.scrape_job.tavily_search", new_callable=AsyncMock, return_value=FAKE_RAW_RESULTS), \
         patch("jobs.scrape_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.scrape_job.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        mock_settings.api_base_url = "http://localhost:8000"
        await scrape_job()

    assert mock_client.post.call_count == 2


async def test_scrape_job_posts_jobcreate_shaped_payloads():
    mock_cls, mock_client = _mock_http_client()

    with patch("jobs.scrape_job.tavily_search", new_callable=AsyncMock, return_value=FAKE_RAW_RESULTS), \
         patch("jobs.scrape_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.scrape_job.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        mock_settings.api_base_url = "http://localhost:8000"
        await scrape_job()

    for posted_call in mock_client.post.call_args_list:
        payload = posted_call[1].get("json") or posted_call[0][1]
        assert "company" in payload
        assert "role" in payload
        assert "url" in payload


# ── empty results ─────────────────────────────────────────────────────────────

async def test_scrape_job_empty_results_makes_no_post_calls():
    mock_cls, mock_client = _mock_http_client()

    with patch("jobs.scrape_job.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("jobs.scrape_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.scrape_job.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        mock_settings.api_base_url = "http://localhost:8000"
        await scrape_job()

    mock_client.post.assert_not_called()


# ── Tavily failure ────────────────────────────────────────────────────────────

async def test_scrape_job_tavily_failure_exits_cleanly(caplog):
    mock_cls, mock_client = _mock_http_client()

    with patch("jobs.scrape_job.tavily_search", new_callable=AsyncMock, side_effect=Exception("Tavily down")), \
         patch("jobs.scrape_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.scrape_job.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        mock_settings.api_base_url = "http://localhost:8000"
        await scrape_job()

    mock_client.post.assert_not_called()
