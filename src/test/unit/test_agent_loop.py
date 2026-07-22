import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.loop import MAX_ITERATIONS, Agent
from app.conversation.models import Role
from profile.schema import Profile, Skill


def _call(name, args: dict, id="call_1"):
    return SimpleNamespace(
        id=id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def profile_path(tmp_path):
    p = Profile(name="Jane", email="jane@example.com", skills=[Skill(id="py", label="Python")])
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(p.model_dump(mode="json")))
    return path


@pytest.fixture
def context():
    c = MagicMock()
    c.record = AsyncMock(return_value=None)
    c.build_context = AsyncMock(return_value=[{"role": "user", "content": "hi"}])
    return c


@pytest.fixture
def dispatcher():
    d = MagicMock()
    d.dispatch = AsyncMock(return_value={"ok": True})
    return d


@pytest.fixture
def llm():
    c = MagicMock()
    c.chat = AsyncMock(return_value=_response(content="done"))
    return c


@pytest.fixture
def agent(llm, dispatcher, context, profile_path):
    return Agent(llm=llm, dispatcher=dispatcher, context=context, profile_path=profile_path)


# ── stop condition 1: final answer ────────────────────────────────────────────

async def test_final_answer_returns_the_text(agent, llm):
    llm.chat.return_value = _response(content="You have 3 active jobs.")
    assert await agent.run("s1", "what's in my pipeline?") == "You have 3 active jobs."


async def test_final_answer_after_one_tool_call(agent, llm, dispatcher):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "PUB"})]),
        _response(content="Found it — PUB, Data Analyst."),
    ]
    dispatcher.dispatch.return_value = [{"id": 42, "role": "Data Analyst", "company": "PUB"}]

    reply = await agent.run("s1", "did I apply to PUB?")

    assert reply == "Found it — PUB, Data Analyst."
    dispatcher.dispatch.assert_called_once_with("find_jobs", {"company": "PUB"})


async def test_tool_result_is_fed_back_to_the_model(agent, llm, dispatcher):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"company": "PUB"})]),
        _response(content="ok"),
    ]
    dispatcher.dispatch.return_value = [{"id": 42}]

    await agent.run("s1", "x")

    second_call_messages = llm.chat.call_args_list[1].args[0]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"]) == [{"id": 42}]


async def test_assistant_tool_calls_go_back_on_the_conversation(agent, llm, dispatcher):
    """Providers reject a tool result whose matching tool_calls entry is
    missing from the history."""
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {})]),
        _response(content="ok"),
    ]
    await agent.run("s1", "x")

    messages = llm.chat.call_args_list[1].args[0]
    assistant = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant) == 1
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "find_jobs"


# ── errors-as-data re-enters the loop ─────────────────────────────────────────

async def test_expected_error_result_re_enters_the_loop(agent, llm, dispatcher):
    """An {ok:false} is data the model reads and reacts to — it must not
    abort the turn."""
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 1, "new_status": "offer"})]),
        _response(content="That job is rejected, so I can't move it to offer."),
    ]
    dispatcher.dispatch.return_value = {
        "ok": False, "error": "illegal_transition", "from": "rejected", "to": "offer", "allowed": [],
    }

    reply = await agent.run("s1", "got an offer from PUB")

    assert "can't move it" in reply
    assert llm.chat.call_count == 2


async def test_expected_error_is_passed_through_verbatim(agent, llm, dispatcher):
    error = {"ok": False, "error": "not_found"}
    llm.chat.side_effect = [
        _response(tool_calls=[_call("update_status", {"job_id": 99, "new_status": "offer"})]),
        _response(content="no such job"),
    ]
    dispatcher.dispatch.return_value = error

    await agent.run("s1", "x")

    messages = llm.chat.call_args_list[1].args[0]
    tool_message = [m for m in messages if m.get("role") == "tool"][0]
    assert json.loads(tool_message["content"]) == error


# ── stop condition 2: iteration cap ───────────────────────────────────────────

async def test_iteration_cap_returns_best_effort_not_an_error(agent, llm, dispatcher):
    # Distinct args every time, so the no-progress guard never fires first.
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": f"q{i}"})])
        for i in range(MAX_ITERATIONS)
    ]
    reply = await agent.run("s1", "x")

    assert "couldn't fully finish" in reply
    assert llm.chat.call_count == MAX_ITERATIONS


async def test_iteration_cap_still_records_an_assistant_turn(agent, llm, context):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": f"q{i}"})])
        for i in range(MAX_ITERATIONS)
    ]
    await agent.run("s1", "x")

    recorded = [c.args for c in context.record.call_args_list]
    assert recorded[-1][1] == Role.ASSISTANT


# ── stop condition 3: no progress ─────────────────────────────────────────────

async def test_no_progress_guard_fires_on_identical_repeated_call(agent, llm, dispatcher):
    same = {"job_title": "backend"}
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", same)]),
        _response(tool_calls=[_call("find_jobs", same)]),
        _response(content="unreachable"),
    ]
    reply = await agent.run("s1", "x")

    assert "stuck repeating" in reply
    assert llm.chat.call_count == 2


async def test_no_progress_guard_ignores_same_tool_with_different_args(agent, llm):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": "backend"})]),
        _response(tool_calls=[_call("find_jobs", {"job_title": "frontend"})]),
        _response(content="here are both"),
    ]
    assert await agent.run("s1", "x") == "here are both"


# ── stop condition 4: consecutive exceptions ──────────────────────────────────

async def test_two_consecutive_tool_exceptions_abort_the_turn(agent, llm, dispatcher):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": "a"})]),
        _response(tool_calls=[_call("find_jobs", {"job_title": "b"})]),
        _response(content="unreachable"),
    ]
    dispatcher.dispatch.side_effect = RuntimeError("db is on fire")

    reply = await agent.run("s1", "x")

    assert "Something went wrong" in reply


async def test_a_single_tool_exception_does_not_abort(agent, llm, dispatcher):
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": "a"})]),
        _response(content="recovered"),
    ]
    dispatcher.dispatch.side_effect = [RuntimeError("transient"), {"ok": True}]

    assert await agent.run("s1", "x") == "recovered"


async def test_exception_never_becomes_a_tool_result(agent, llm, dispatcher):
    """The model must not get to narrate a crash as a normal outcome."""
    llm.chat.side_effect = [
        _response(tool_calls=[_call("find_jobs", {"job_title": "a"})]),
        _response(content="recovered"),
    ]
    dispatcher.dispatch.side_effect = [RuntimeError("boom"), {"ok": True}]

    await agent.run("s1", "x")

    messages = llm.chat.call_args_list[1].args[0]
    assert [m for m in messages if m.get("role") == "tool"] == []


async def test_two_consecutive_llm_exceptions_abort_the_turn(agent, llm):
    llm.chat.side_effect = [RuntimeError("429"), RuntimeError("429")]
    assert "Something went wrong" in await agent.run("s1", "x")


async def test_llm_exception_recovers_if_the_retry_succeeds(agent, llm):
    llm.chat.side_effect = [RuntimeError("429"), _response(content="recovered")]
    assert await agent.run("s1", "x") == "recovered"


# ── turn recording ────────────────────────────────────────────────────────────

async def test_records_user_turn_before_and_assistant_turn_after(agent, llm, context):
    llm.chat.return_value = _response(content="hello back")
    await agent.run("s1", "hello")

    calls = [c.args for c in context.record.call_args_list]
    assert calls[0] == ("s1", Role.USER, "hello")
    assert calls[1] == ("s1", Role.ASSISTANT, "hello back")


async def test_composes_context_after_recording_the_user_turn(agent, llm, context):
    """The user's own message must be in the history the model sees."""
    order = []
    context.record = AsyncMock(side_effect=lambda *a: order.append("record"))
    context.build_context = AsyncMock(side_effect=lambda sid: order.append("build") or [])

    await agent.run("s1", "hello")

    assert order[:2] == ["record", "build"]


async def test_tools_are_offered_on_every_llm_call(agent, llm):
    from agent.schemas import TOOL_SCHEMAS

    await agent.run("s1", "x")
    assert llm.chat.call_args.args[1] == TOOL_SCHEMAS


# ── malformed model output ────────────────────────────────────────────────────

async def test_malformed_tool_arguments_do_not_crash_the_turn(agent, llm, dispatcher):
    bad = SimpleNamespace(id="c1", function=SimpleNamespace(name="find_jobs", arguments="{not json"))
    llm.chat.side_effect = [_response(tool_calls=[bad]), _response(content="ok")]

    assert await agent.run("s1", "x") == "ok"
    dispatcher.dispatch.assert_called_once_with("find_jobs", {})


async def test_empty_final_answer_falls_back_to_best_effort(agent, llm):
    llm.chat.return_value = _response(content=None)
    assert "couldn't fully finish" in await agent.run("s1", "x")


# ── event loop responsiveness ─────────────────────────────────────────────────

async def test_the_event_loop_stays_responsive_during_a_turn(agent, llm):
    """Nothing in the loop may block the single event loop this app runs the
    API, both bots, and the scheduler on."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    async def slow_chat(messages, tools=None):
        await asyncio.sleep(0.02)
        return _response(content="done")

    llm.chat = AsyncMock(side_effect=slow_chat)

    task = asyncio.create_task(ticker())
    reply = await agent.run("s1", "x")
    task.cancel()

    assert reply == "done"
    assert ticks > 1
