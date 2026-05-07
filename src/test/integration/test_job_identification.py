"""
Integration tests for the agent's job identification flow.

Tests that the agent correctly sequences query_jobs → identify → update_status
when a user refers to a job by name, and that the compact index (not full records)
is what enters the LLM's context. The LLM does the name matching — these tests
verify the mechanism, not the LLM's intelligence.

Mocks: LLMClient (injected), repo calls.
Tested: tool sequencing, compact field enforcement in LLM history, error recovery.

Run with:
    pytest src/test/integration/test_job_identification.py -v
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.agent import run


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_llm(responses):
    mock = MagicMock()
    mock.chat.side_effect = responses
    return mock


def _text_response(content):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=None)
    )])


def _tool_response(tool_name, arguments, call_id="call_1"):
    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(arguments)
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=None, tool_calls=[tool_call])
    )])


def _full_db_record(id=1, company="Palantir", role="Backend Software Engineer",
                    status="found"):
    """Full DB record as returned by SELECT * — includes description and fingerprint."""
    return {
        "id": id, "company": company, "role": role, "status": status,
        "url": "https://jobs.lever.co/palantir/123",
        "source": "tavily", "notes": None,
        "description": "Long job description that must not reach the LLM context...",
        "score": 0.85,
        "fingerprint": "a" * 64,
        "date_logged": "2026-05-07T00:00:00+00:00",
    }


def _get_tool_result_from_history(llm, call_index=-1):
    """Extract the tool result message from the LLM call at call_index."""
    history = llm.chat.call_args_list[call_index].args[0]
    return next(m for m in history if m.get("role") == "tool")


# ── sequencing ────────────────────────────────────────────────────────────────

async def test_agent_sequences_query_then_update():
    """Agent calls query_jobs to get the list, then update_status with the ID — 3 LLM calls total."""
    updated = {**_full_db_record(), "status": "applied"}
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _tool_response("update_status", {"job_id": 1, "status": "applied"}, call_id="call_2"),
        _text_response("Updated Palantir to applied."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]), \
         patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               return_value=updated):
        reply = await run(
            messages=[{"role": "user", "content": "update Palantir to applied"}],
            db=MagicMock(), llm=llm,
        )
    assert reply == "Updated Palantir to applied."
    assert llm.chat.call_count == 3


# ── compact index in LLM history ──────────────────────────────────────────────

async def test_compact_list_in_llm_history_excludes_description():
    """Description must not appear in the tool result that enters the LLM's context."""
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _text_response("Here are your jobs."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]):
        await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(), llm=llm,
        )
    tool_result = _get_tool_result_from_history(llm)
    jobs = json.loads(tool_result["content"])
    assert "description" not in jobs[0]


async def test_compact_list_in_llm_history_excludes_fingerprint():
    """Fingerprint must not appear in the tool result that enters the LLM's context."""
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _text_response("Here are your jobs."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record()]):
        await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(), llm=llm,
        )
    tool_result = _get_tool_result_from_history(llm)
    jobs = json.loads(tool_result["content"])
    assert "fingerprint" not in jobs[0]


async def test_compact_list_in_llm_history_retains_id():
    """ID must be present in LLM context so the model can pass it to update_status."""
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _text_response("Done."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record(id=7)]):
        await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(), llm=llm,
        )
    tool_result = _get_tool_result_from_history(llm)
    jobs = json.loads(tool_result["content"])
    assert jobs[0]["id"] == 7


async def test_compact_list_retains_company_role_status_for_llm_matching():
    """Company, role, and status must be present so the LLM can match by name."""
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _text_response("Done."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[_full_db_record(company="Stripe", role="Data Eng", status="applied")]):
        await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(), llm=llm,
        )
    tool_result = _get_tool_result_from_history(llm)
    job = json.loads(tool_result["content"])[0]
    assert job["company"] == "Stripe"
    assert job["role"] == "Data Eng"
    assert job["status"] == "applied"


# ── error recovery ────────────────────────────────────────────────────────────

async def test_missing_job_id_arg_returns_error_not_crash():
    """LLM calls update_status without job_id — KeyError caught, returned as error JSON, loop continues."""
    responses = [
        _tool_response("update_status", {"status": "applied"}, call_id="call_1"),
        _text_response("I need the job ID to update — could you clarify?"),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}):
        reply = await run(
            messages=[{"role": "user", "content": "update my job to applied"}],
            db=MagicMock(), llm=llm,
        )
    assert reply == "I need the job ID to update — could you clarify?"
    tool_result = _get_tool_result_from_history(llm, call_index=1)
    assert "error" in json.loads(tool_result["content"])


async def test_invalid_status_value_returns_error_not_crash():
    """LLM passes a status not in the enum — ValueError caught, returned as error JSON, loop continues."""
    responses = [
        _tool_response("update_status", {"job_id": 1, "status": "ghosted"}, call_id="call_1"),
        _text_response("That status is not valid."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}):
        reply = await run(
            messages=[{"role": "user", "content": "mark as ghosted"}],
            db=MagicMock(), llm=llm,
        )
    assert reply == "That status is not valid."
    tool_result = _get_tool_result_from_history(llm, call_index=1)
    assert "error" in json.loads(tool_result["content"])


async def test_wrong_job_id_returns_not_found_gracefully():
    """LLM picks a wrong job_id — repo returns None — agent explains to user without crashing."""
    responses = [
        _tool_response("update_status", {"job_id": 999, "status": "applied"}, call_id="call_1"),
        _text_response("That job ID was not found in the database."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.update_job_status", new_callable=AsyncMock,
               return_value=None):
        reply = await run(
            messages=[{"role": "user", "content": "update job 999 to applied"}],
            db=MagicMock(), llm=llm,
        )
    assert reply == "That job ID was not found in the database."


# ── ambiguous match ───────────────────────────────────────────────────────────

async def test_ambiguous_company_name_both_ids_visible_to_llm():
    """Two records with the same company — both IDs must be in the compact list so the LLM can ask."""
    record_a = _full_db_record(id=1, company="Jobstreet", role="Back End Developer Jobs in Singapore")
    record_b = _full_db_record(id=2, company="Jobstreet", role="Backend Jobs in Singapore (with Salaries)")
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _text_response("I found two Jobstreet listings — which one did you mean?"),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock,
               return_value=[record_a, record_b]):
        reply = await run(
            messages=[{"role": "user", "content": "update Jobstreet to screening"}],
            db=MagicMock(), llm=llm,
        )
    assert reply == "I found two Jobstreet listings — which one did you mean?"
    tool_result = _get_tool_result_from_history(llm)
    ids = {j["id"] for j in json.loads(tool_result["content"])}
    assert ids == {1, 2}
