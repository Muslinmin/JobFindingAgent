import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from agent.tools import execute_tool


# ── helpers ──────────────────────────────────────────────────────────────────

def _mock_job_record(id=1, company="GovTech", role="Engineer",
                     url="https://careers.gov.sg/1", status="found"):
    return {
        "id": id,
        "company": company,
        "role": role,
        "url": url,
        "status": status,
        "source": "manual",
        "notes": None,
        "fingerprint": "abc123",
        "date_logged": "2026-05-06T00:00:00",
    }


# ── log_job ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_job_returns_created_true_on_new_insert():
    record = _mock_job_record()
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(record, True)):
        result = await execute_tool("log_job", {
            "company": "GovTech",
            "role": "Engineer",
            "url": "https://careers.gov.sg/1",
        }, db=MagicMock())
    assert json.loads(result)["created"] is True


@pytest.mark.asyncio
async def test_log_job_returns_created_false_on_duplicate():
    record = _mock_job_record()
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(record, False)):
        result = await execute_tool("log_job", {
            "company": "GovTech",
            "role": "Engineer",
            "url": "https://careers.gov.sg/1",
        }, db=MagicMock())
    assert json.loads(result)["created"] is False


@pytest.mark.asyncio
async def test_log_job_result_contains_job_record():
    record = _mock_job_record(company="Stripe", role="Data Engineer")
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(record, True)):
        result = await execute_tool("log_job", {
            "company": "Stripe",
            "role": "Data Engineer",
            "url": "https://stripe.com/jobs/1",
        }, db=MagicMock())
    parsed = json.loads(result)
    assert parsed["job"]["company"] == "Stripe"
    assert parsed["job"]["role"] == "Data Engineer"


@pytest.mark.asyncio
async def test_log_job_forwards_optional_fields():
    record = _mock_job_record()
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(record, True)) as mock_insert:
        await execute_tool("log_job", {
            "company": "GovTech",
            "role": "Engineer",
            "url": "https://careers.gov.sg/1",
            "source": "LinkedIn",
            "notes": "Referred by a friend",
        }, db=MagicMock())
    call_args = mock_insert.call_args
    job_arg = call_args.args[1]
    assert job_arg.source == "LinkedIn"
    assert job_arg.notes == "Referred by a friend"


# ── update_status ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_status_returns_job_on_success():
    record = _mock_job_record(status="applied")
    with patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               return_value=record):
        result = await execute_tool("update_status",
                                    {"job_id": 1, "status": "applied"},
                                    db=MagicMock())
    assert json.loads(result)["status"] == "applied"


@pytest.mark.asyncio
async def test_update_status_returns_error_on_invalid_transition():
    from app.models.enums import InvalidTransitionError
    with patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               side_effect=InvalidTransitionError("Cannot transition from 'offer' to 'applied'")):
        result = await execute_tool("update_status",
                                    {"job_id": 1, "status": "applied"},
                                    db=MagicMock())
    parsed = json.loads(result)
    assert "error" in parsed
    assert "offer" in parsed["error"]


@pytest.mark.asyncio
async def test_update_status_returns_error_on_missing_job():
    with patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               return_value=None):
        result = await execute_tool("update_status",
                                    {"job_id": 9999, "status": "applied"},
                                    db=MagicMock())
    assert json.loads(result)["error"] == "Job not found"


@pytest.mark.asyncio
async def test_update_status_forwards_notes():
    record = _mock_job_record(status="screening")
    with patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               return_value=record) as mock_update:
        await execute_tool("update_status",
                           {"job_id": 1, "status": "screening", "notes": "Phone screen done"},
                           db=MagicMock())
    call_kwargs = mock_update.call_args
    assert call_kwargs.args[3] == "Phone screen done"


# ── query_jobs ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_jobs_returns_list():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[{"id": 1}]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert isinstance(json.loads(result), list)


@pytest.mark.asyncio
async def test_query_jobs_returns_empty_list_when_no_jobs():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert json.loads(result) == []


@pytest.mark.asyncio
async def test_query_jobs_forwards_status_filter():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[]) as mock_get:
        await execute_tool("query_jobs", {"status": "applied"}, db=MagicMock())
    assert mock_get.call_args.kwargs["status_filter"] == "applied"


@pytest.mark.asyncio
async def test_query_jobs_omits_filter_when_not_provided():
    with patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[]) as mock_get:
        await execute_tool("query_jobs", {}, db=MagicMock())
    assert mock_get.call_args.kwargs.get("status_filter") is None


# ── update_profile ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_profile_returns_success_status():
    with patch("agent.tools.read_profile", return_value={}), \
         patch("agent.tools.write_profile"):
        result = await execute_tool("update_profile",
                                    {"updates": {"skills": ["Python"]}},
                                    db=MagicMock())
    assert json.loads(result)["status"] == "profile updated"


@pytest.mark.asyncio
async def test_update_profile_merges_into_existing():
    existing = {"target_roles": ["Data Engineer"], "skills": ["Python"]}
    with patch("agent.tools.read_profile", return_value=existing), \
         patch("agent.tools.write_profile") as mock_write:
        await execute_tool("update_profile",
                           {"updates": {"skills": ["Python", "SQL"]}},
                           db=MagicMock())
    written = mock_write.call_args.args[0]
    assert written["target_roles"] == ["Data Engineer"]
    assert written["skills"] == ["Python", "SQL"]


@pytest.mark.asyncio
async def test_update_profile_result_contains_merged_profile():
    existing = {"target_roles": ["Backend Engineer"]}
    with patch("agent.tools.read_profile", return_value=existing), \
         patch("agent.tools.write_profile"):
        result = await execute_tool("update_profile",
                                    {"updates": {"experience_years": 3}},
                                    db=MagicMock())
    profile = json.loads(result)["profile"]
    assert profile["target_roles"] == ["Backend Engineer"]
    assert profile["experience_years"] == 3


# ── unknown tool ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    result = await execute_tool("nonexistent_tool", {}, db=MagicMock())
    assert "Unknown tool" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_unknown_tool_error_includes_tool_name():
    result = await execute_tool("fly_to_moon", {}, db=MagicMock())
    assert "fly_to_moon" in json.loads(result)["error"]
