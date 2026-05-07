"""
Unit tests for _query_jobs compact output.

Verifies that _query_jobs strips description and fingerprint from results,
returning only the fields the agent needs to identify and reason about jobs.
These fields go into conversation history on every subsequent message — keeping
them minimal prevents context window bloat when the database grows.

Run with:
    pytest src/test/unit/test_query_jobs_compact.py -v
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.tools import execute_tool


# ── fixtures ──────────────────────────────────────────────────────────────────

def _full_db_record(id=1, company="GovTech", role="Backend Engineer",
                    status="found", url="https://careers.gov.sg/1"):
    """Full SELECT * row as returned by the repository."""
    return {
        "id": id,
        "company": company,
        "role": role,
        "url": url,
        "status": status,
        "source": "tavily",
        "notes": None,
        "description": "We are looking for a backend engineer with 5 years of Python experience...",
        "score": 0.75,
        "fingerprint": "a" * 64,
        "date_logged": "2026-05-07T00:00:00+00:00",
    }


# ── field stripping ───────────────────────────────────────────────────────────

async def test_query_jobs_strips_description():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert "description" not in json.loads(result)[0]


async def test_query_jobs_strips_fingerprint():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert "fingerprint" not in json.loads(result)[0]


# ── required fields present ───────────────────────────────────────────────────

async def test_query_jobs_retains_id():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record(id=42)]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert json.loads(result)[0]["id"] == 42


async def test_query_jobs_retains_company_and_role():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record(company="Stripe", role="Data Engineer")]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    job = json.loads(result)[0]
    assert job["company"] == "Stripe"
    assert job["role"] == "Data Engineer"


async def test_query_jobs_retains_status():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record(status="screening")]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert json.loads(result)[0]["status"] == "screening"


async def test_query_jobs_retains_url_score_date_logged_notes():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    job = json.loads(result)[0]
    assert "url" in job
    assert "score" in job
    assert "date_logged" in job
    assert "notes" in job


# ── scale consistency ─────────────────────────────────────────────────────────

async def test_query_jobs_strips_description_and_fingerprint_across_all_records():
    records = [_full_db_record(id=i, company=f"Company{i}") for i in range(1, 11)]
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=records):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    jobs = json.loads(result)
    assert len(jobs) == 10
    for job in jobs:
        assert "description" not in job
        assert "fingerprint" not in job
        assert "id" in job
        assert "company" in job
        assert "role" in job
        assert "status" in job
