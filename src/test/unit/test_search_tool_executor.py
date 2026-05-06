import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agent.tools import execute_tool


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_db():
    return MagicMock()


def _mock_job(id=1, company="GovTech", role="Data Engineer"):
    return {
        "id": id, "company": company, "role": role,
        "url": "https://careers.gov.sg/1", "status": "found",
        "source": "tavily", "notes": None, "date_logged": "2026-01-01",
    }


FAKE_PARSED = [
    {"company": "GovTech", "role": "Data Engineer",
     "url": "https://careers.gov.sg/1", "source": "tavily", "notes": None},
]


# ── return shape ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_jobs_returns_json_string():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("agent.tools.parse_results", return_value=[]):
        result = await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())
    assert isinstance(result, str)
    data = json.loads(result)
    assert "query"    in data
    assert "found"    in data
    assert "inserted" in data
    assert "count"    in data


@pytest.mark.asyncio
async def test_search_jobs_result_includes_correct_found_and_count():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=FAKE_PARSED), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(_mock_job(), True)):
        result = await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())
    data = json.loads(result)
    assert data["found"] == 1
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_search_jobs_result_echoes_query():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("agent.tools.parse_results", return_value=[]):
        result = await execute_tool("search_jobs", {"query": "fintech backend"}, _make_db())
    assert json.loads(result)["query"] == "fintech backend"


# ── delegation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_jobs_delegates_query_to_tavily():
    mock_search = AsyncMock(return_value=[])
    with patch("agent.tools.tavily_search", mock_search), \
         patch("agent.tools.parse_results", return_value=[]):
        await execute_tool("search_jobs", {"query": "backend engineer SG"}, _make_db())
    mock_search.assert_called_once_with("backend engineer SG")


@pytest.mark.asyncio
async def test_search_jobs_uses_settings_query_when_none_provided():
    mock_search = AsyncMock(return_value=[])
    with patch("agent.tools.tavily_search", mock_search), \
         patch("agent.tools.parse_results", return_value=[]), \
         patch("agent.tools.settings") as mock_settings:
        mock_settings.scrape_query = "software engineer Singapore"
        await execute_tool("search_jobs", {}, _make_db())
    mock_search.assert_called_once_with("software engineer Singapore")


# ── empty / graceful ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_jobs_returns_zero_count_when_tavily_empty():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("agent.tools.parse_results", return_value=[]):
        result = await execute_tool("search_jobs", {"query": "anything"}, _make_db())
    data = json.loads(result)
    assert data["count"]    == 0
    assert data["inserted"] == []


# ── insert failure resilience ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_jobs_continues_when_one_insert_fails():
    two_records = [
        {"company": "A", "role": "Engineer", "url": "https://a.com/1", "source": "tavily", "notes": None},
        {"company": "B", "role": "Engineer", "url": "https://b.com/2", "source": "tavily", "notes": None},
    ]
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}, {}]), \
         patch("agent.tools.parse_results", return_value=two_records), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               side_effect=[Exception("DB error"), (_mock_job(id=2, company="B"), True)]):
        result = await execute_tool("search_jobs", {"query": "engineer"}, _make_db())
    data = json.loads(result)
    assert data["found"] == 2
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_search_jobs_returns_empty_when_all_inserts_fail():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=FAKE_PARSED), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               side_effect=Exception("DB down")):
        result = await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())
    data = json.loads(result)
    assert data["count"]    == 0
    assert data["inserted"] == []


# ── scoring wired through tool executor ──────────────────────────────────────

@pytest.mark.asyncio
async def test_search_jobs_passes_score_to_insert_job():
    """score_job must be called and its result forwarded to repo.insert_job — not silently defaulted."""
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=FAKE_PARSED), \
         patch("agent.tools.read_profile", return_value={"skills": ["python"]}), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(_mock_job(), True)) as mock_insert:
        await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())

    assert mock_insert.called
    call_args = mock_insert.call_args
    # insert_job(db, job, fp, score) — score is the 4th positional arg
    assert len(call_args.args) >= 4, "score must be passed as positional arg to insert_job"
    score = call_args.args[3]
    assert isinstance(score, float), f"score must be a float, got {type(score)}"
    assert 0.0 <= score <= 1.0, f"score out of range: {score}"


@pytest.mark.asyncio
async def test_search_jobs_score_reflects_profile_skills():
    """When profile skills match the job description, score must be > 0.0."""
    parsed_with_description = [{
        "company": "GovTech", "role": "Data Engineer",
        "url": "https://careers.gov.sg/1", "source": "tavily",
        "notes": None, "description": "We use Python and SQL daily.",
    }]
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=parsed_with_description), \
         patch("agent.tools.read_profile", return_value={"skills": ["python", "sql"]}), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(_mock_job(), True)) as mock_insert:
        await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())

    score = mock_insert.call_args.args[3]
    assert score == 1.0, f"both skills match description — expected 1.0, got {score}"
