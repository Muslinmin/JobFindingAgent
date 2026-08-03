"""RR-1…8 — reference resolution (agent_v2.md §7).

Two kinds of row, and the split matters:

* `[unit]` rows are **plumbing**. The model's tool choice is scripted, so
  what is under test is deterministic: that the right service is called
  with the right id, that a service verdict reaches the user unchanged,
  that an error result re-enters the loop. These run in CI.
* `[eval]` rows are **model judgment** — whether the model resolves, asks,
  or lists when faced with 0/1/N candidates. Non-deterministic by nature,
  so they live in `test_agent_reference_resolution_live.py` behind
  `-m live`; asserting them here would make CI flaky for a reason that has
  nothing to do with the code.

The thing being proven throughout: resolution and FSM enforcement are
separate concerns. The loop resolves; the backend adjudicates; the model
narrates what it is told rather than what it reasoned.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.handlers import AgentDeps, ToolDispatcher
from agent.loop import Agent
from app.config import Settings
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.models.job import Job
from app.services.service import JobNotFoundError
from profile.schema import Profile, Skill

T = "2026-07-22T00:00:00+00:00"


def _job(id, role, company, status=ApplicationStatus.APPLIED, score=7000, changed_at=T):
    return Job(
        id=id, fingerprint=f"fp{id}", company=company, role=role, description="jd",
        url="https://example.com", posted_at=None, metadata=None, status=status, score=score,
        status_changed_at=changed_at, follow_up_count=0, last_follow_up_at=None,
        follow_up_nudge_at=None, seen_count=1, last_seen_at=T, created_at=T, updated_at=T,
    )


def _call(name, args, id="c1"):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _response(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


@pytest.fixture
def profile_path(tmp_path):
    p = Profile(name="Jane", email="jane@example.com", skills=[Skill(id="py", label="Python")])
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(p.model_dump(mode="json")))
    return path


@pytest.fixture
def job_service():
    s = MagicMock()
    s.find_jobs = AsyncMock(return_value=[])
    s.query_jobs = AsyncMock(return_value=[])
    s.get_job = AsyncMock(return_value=None)
    s.transition_status = AsyncMock()
    s.ingest_job = AsyncMock()
    return s


@pytest.fixture
def context():
    c = MagicMock()
    c.record = AsyncMock(return_value=None)
    c.build_context = AsyncMock(return_value=[])
    return c


@pytest.fixture
def llm():
    return MagicMock(chat=AsyncMock(return_value=_response(content="ok")))


@pytest.fixture
def agent(llm, job_service, context, profile_path, tmp_path):
    deps = AgentDeps(
        job_service=job_service,
        scorer=MagicMock(score=AsyncMock(return_value=8000)),
        llm=MagicMock(complete=AsyncMock(return_value="[]")),
        settings=Settings(score_threshold=5000),
        profile_path=profile_path,
        queries_path=tmp_path / "queries.json",
        adapters=[],
        template_path=tmp_path / "cv.tex.jinja",
        output_dir=tmp_path / "artifacts",
    )
    return Agent(
        llm=llm, dispatcher=ToolDispatcher(deps), context=context, profile_path=profile_path
    )


# ── RR-1 [unit] — one match resolves and mutates ──────────────────────────────

async def test_rr1_single_match_resolves_then_transitions(agent, llm, job_service):
    pub = _job(42, "Data Analyst", "PUB", status=ApplicationStatus.APPLIED)
    job_service.find_jobs.return_value = [pub]
    job_service.get_job.return_value = pub
    job_service.transition_status.return_value = _job(
        42, "Data Analyst", "PUB", status=ApplicationStatus.INTERVIEWING
    )
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "PUB"})]),
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "interviewing"}, id="c2")]),
        _response(content="Marked PUB Data Analyst as interviewing."),
    ]

    reply = (await agent.run("s1", "got an interview with PUB")).reply

    job_service.transition_status.assert_called_once()
    assert job_service.transition_status.call_args.args[0] == 42
    assert job_service.transition_status.call_args.args[1] == ApplicationStatus.INTERVIEWING
    assert "interviewing" in reply


async def test_rr1_transition_stamps_status_changed_at(agent, llm, job_service):
    """The stamp is the service's job — the agent must not send one."""
    pub = _job(42, "Data Analyst", "PUB")
    job_service.get_job.return_value = pub
    job_service.transition_status.return_value = _job(
        42, "Data Analyst", "PUB", status=ApplicationStatus.INTERVIEWING, changed_at="2026-07-22T09:00:00+00:00"
    )
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "interviewing"})]),
        _response(content="done"),
    ]

    await agent.run("s1", "x")

    assert "status_changed_at" not in job_service.transition_status.call_args.kwargs


# ── RR-2 [unit half] — no match, no mutation, no invented id ──────────────────

async def test_rr2_no_match_produces_no_mutation(agent, llm, job_service):
    job_service.find_jobs.return_value = []
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "PUB"})]),
        _response(content="I don't have a PUB job on file. Want me to search for it?"),
    ]

    reply = (await agent.run("s1", "got an interview with PUB")).reply

    job_service.transition_status.assert_not_called()
    assert "PUB" in reply


# ── RR-3/RR-4 [unit half] — N matches, then a follow-up narrows it ────────────

async def test_rr3_multiple_matches_are_all_returned_for_disambiguation(agent, llm, job_service):
    job_service.find_jobs.return_value = [
        _job(58, "Software Engineer", "GovTech"),
        _job(60, "Data Analyst", "GovTech"),
        _job(61, "Product Manager", "GovTech"),
    ]
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "GovTech"})]),
        _response(content="I found three GovTech roles — which one?"),
    ]

    await agent.run("s1", "rejected by GovTech")

    messages = llm.chat.call_args_list[1].args[0]
    rows = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert len(rows) == 3
    assert {r["role"] for r in rows} == {"Software Engineer", "Data Analyst", "Product Manager"}
    job_service.transition_status.assert_not_called()


async def test_rr4_followup_reference_mutates_the_chosen_row(agent, llm, job_service):
    """'the data analyst one' → job 60. The narrowing is the model's; what
    is asserted here is that the id it picked is the one that gets moved."""
    analyst = _job(60, "Data Analyst", "GovTech")
    job_service.get_job.return_value = analyst
    job_service.transition_status.return_value = _job(
        60, "Data Analyst", "GovTech", status=ApplicationStatus.REJECTED
    )
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 60, "new_status": "rejected"})]),
        _response(content="Marked the GovTech Data Analyst role as rejected."),
    ]

    await agent.run("s1", "the data analyst one")

    assert job_service.transition_status.call_args.args[0] == 60


# ── RR-5 [unit half] — positional reference rests on a deterministic order ────

async def test_rr5_find_jobs_order_is_deterministic_and_preserved(agent, llm, job_service):
    """'the third one' is only meaningful if the row order is fixed. The
    service orders by status_changed_at DESC then id ASC; the handler must
    not reorder it."""
    ordered = [
        _job(11, "A", "X", changed_at="2026-07-22T05:00:00+00:00"),
        _job(12, "B", "X", changed_at="2026-07-22T04:00:00+00:00"),
        _job(13, "C", "X", changed_at="2026-07-22T03:00:00+00:00"),
        _job(14, "D", "X", changed_at="2026-07-22T02:00:00+00:00"),
        _job(15, "E", "X", changed_at="2026-07-22T01:00:00+00:00"),
    ]
    job_service.find_jobs.return_value = ordered
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "X"})]),
        _response(content="listed"),
    ]

    await agent.run("s1", "show me the X jobs")

    messages = llm.chat.call_args_list[1].args[0]
    rows = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert [r["id"] for r in rows] == [11, 12, 13, 14, 15]


async def test_rr5_third_row_id_is_what_gets_tailored(agent, llm, job_service, monkeypatch):
    """Continues RR-5: 'tailor my CV for the third one' → row 3's id. The
    tailoring service isn't wired yet, so this asserts the id reaches the
    handler rather than the artifact it would produce."""
    from agent import handlers

    seen = {}

    async def fake_tailor(args, deps):
        seen.update(args)
        return {"ok": True, "job_id": args["job_id"]}

    monkeypatch.setitem(handlers._HANDLERS, "tailor_resume", fake_tailor)

    llm.chat.side_effect = [
        _response(tool_calls=[_call("tailor_resume", {"job_id": 13})]),
        _response(content="Tailored your CV for C at X."),
    ]

    await agent.run("s1", "tailor my CV for the third one")

    assert seen["job_id"] == 13


# ── RR-6 [unit half] — 'that one' with a single salient candidate ─────────────

async def test_rr6a_single_salient_referent_reaches_the_draft_tool(agent, llm, monkeypatch):
    from agent import handlers

    seen = {}

    async def fake_draft(args, deps):
        seen.update(args)
        return {"ok": True, "job_id": args["job_id"], "text": "draft"}

    monkeypatch.setitem(handlers._HANDLERS, "draft_followup", fake_draft)

    llm.chat.side_effect = [
        _response(tool_calls=[_call("draft_followup", {"job_id": 42})]),
        _response(content="Here's a follow-up draft."),
    ]

    await agent.run("s1", "draft a follow-up for that one")

    assert seen["job_id"] == 42


# ── RR-7 [unit] — resolved id that no longer exists ──────────────────────────

async def test_rr7_deleted_job_returns_not_found_and_the_loop_re_enters(agent, llm, job_service):
    job_service.get_job.return_value = None
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "offer"})]),
        _response(content="That job no longer exists."),
    ]

    reply = (await agent.run("s1", "got an offer")).reply

    messages = llm.chat.call_args_list[1].args[0]
    result = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert result == {"ok": False, "error": "not_found"}
    assert "no longer exists" in reply
    assert llm.chat.call_count == 2  # re-entered, did not abort


async def test_rr7_job_deleted_mid_turn_is_also_not_found(agent, llm, job_service):
    job_service.get_job.return_value = _job(42, "Data Analyst", "PUB")
    job_service.transition_status.side_effect = JobNotFoundError("gone")
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "interviewing"})]),
        _response(content="That job no longer exists."),
    ]

    await agent.run("s1", "x")

    messages = llm.chat.call_args_list[1].args[0]
    result = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert result["error"] == "not_found"


# ── RR-8 [unit] — resolution succeeds, the FSM refuses ───────────────────────

async def test_rr8_illegal_transition_verdict_reaches_the_model_intact(agent, llm, job_service):
    """The reply must be anchored to the SERVICE's verdict, not the model's
    own reasoning about the state machine — which is why the allowed list
    travels in the tool result."""
    job_service.get_job.return_value = _job(42, "Data Analyst", "PUB", status=ApplicationStatus.REJECTED)
    job_service.transition_status.side_effect = InvalidTransitionError("no")
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "offer"})]),
        _response(content="That job is marked rejected, so I can't move it to offer. Did something change?"),
    ]

    reply = (await agent.run("s1", "got an offer from PUB")).reply

    messages = llm.chat.call_args_list[1].args[0]
    result = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert result["error"] == "illegal_transition"
    assert result["from"] == "rejected"
    assert result["to"] == "offer"
    assert result["allowed"] == []
    assert "rejected" in reply


async def test_rr8_resolution_succeeded_even_though_the_move_failed(agent, llm, job_service):
    """Proves the two are separate: the id resolved fine; the FSM is what
    refused."""
    job_service.get_job.return_value = _job(42, "Data Analyst", "PUB", status=ApplicationStatus.REJECTED)
    job_service.transition_status.side_effect = InvalidTransitionError("no")
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "offer"})]),
        _response(content="can't"),
    ]

    await agent.run("s1", "x")

    job_service.get_job.assert_called_once_with(42)
    job_service.transition_status.assert_called_once()


async def test_rr8_allowed_targets_come_from_the_fsm_not_the_model(agent, llm, job_service):
    job_service.get_job.return_value = _job(42, "Data Analyst", "PUB", status=ApplicationStatus.APPLIED)
    job_service.transition_status.side_effect = InvalidTransitionError("no")
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "accepted"})]),
        _response(content="can't"),
    ]

    await agent.run("s1", "x")

    messages = llm.chat.call_args_list[1].args[0]
    result = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert set(result["allowed"]) == {"interviewing", "rejected", "ghosted", "declined"}


# ── boundary "nevers" (agent_v2.md §1) ───────────────────────────────────────

async def test_agent_never_writes_the_database_directly(agent, llm, job_service):
    """Every durable change goes through a service — the agent holds no
    connection and no repository."""
    assert not hasattr(agent, "_db")
    llm.chat.return_value = _response(content="ok")
    await agent.run("s1", "hi")
    assert job_service.transition_status.call_count == 0


async def test_agent_never_enforces_the_fsm_itself(agent, llm, job_service):
    """It proposes an illegal move and lets the backend refuse, rather than
    pre-filtering it — otherwise two FSMs exist and they will drift."""
    job_service.get_job.return_value = _job(42, "A", "X", status=ApplicationStatus.REJECTED)
    job_service.transition_status.side_effect = InvalidTransitionError("no")
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 42, "new_status": "offer"})]),
        _response(content="can't"),
    ]

    await agent.run("s1", "x")

    job_service.transition_status.assert_called_once()  # it DID propose


async def test_agent_holds_no_authoritative_fact_in_history(agent, llm, context):
    """History is resolution-only. What gets recorded is the turn text —
    never a structured fact the system would later trust."""
    llm.chat.return_value = _response(content="You have 3 active jobs.")
    await agent.run("s1", "how many?")

    for call in context.record.call_args_list:
        assert isinstance(call.args[2], str)
