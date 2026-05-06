import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.agent import run


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_llm(responses):
    """Build a mock LLMClient that returns responses in sequence."""
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


# ── basic loop behaviour ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_text_when_no_tool_called():
    """Model answers directly — loop exits on the first iteration."""
    llm = _make_llm([_text_response("You have 3 applications.")])
    with patch("agent.agent.read_profile", return_value={}):
        reply = await run(
            messages=[{"role": "user", "content": "how many jobs?"}],
            db=MagicMock(),
            llm=llm,
        )
    assert reply == "You have 3 applications."
    assert llm.chat.call_count == 1


@pytest.mark.asyncio
async def test_executes_tool_then_returns_response():
    """Model calls one tool, receives the result, then returns a text reply."""
    responses = [
        _tool_response("query_jobs", {}),
        _text_response("Here are your jobs."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock, return_value=[]):
        reply = await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(),
            llm=llm,
        )
    assert reply == "Here are your jobs."
    assert llm.chat.call_count == 2


@pytest.mark.asyncio
async def test_loop_stops_after_max_iterations():
    """If the model never stops calling tools, the loop exits after 10 iterations."""
    llm = MagicMock()
    llm.chat.return_value = _tool_response("query_jobs", {})
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock, return_value=[]):
        reply = await run(
            messages=[{"role": "user", "content": "..."}],
            db=MagicMock(),
            llm=llm,
        )
    assert "issue" in reply.lower()
    assert llm.chat.call_count == 10


@pytest.mark.asyncio
async def test_system_prompt_prepended_to_history():
    """The system prompt is always the first message sent to the model."""
    llm = _make_llm([_text_response("ok")])
    with patch("agent.agent.read_profile", return_value={}):
        await run(
            messages=[{"role": "user", "content": "hi"}],
            db=MagicMock(),
            llm=llm,
        )
    first_message = llm.chat.call_args.args[0][0]
    assert first_message["role"] == "system"


@pytest.mark.asyncio
async def test_client_messages_follow_system_prompt():
    """User messages come after the system prompt, not before."""
    llm = _make_llm([_text_response("ok")])
    with patch("agent.agent.read_profile", return_value={}):
        await run(
            messages=[{"role": "user", "content": "hello"}],
            db=MagicMock(),
            llm=llm,
        )
    history = llm.chat.call_args.args[0]
    assert history[0]["role"] == "system"
    assert history[1]["role"] == "user"
    assert history[1]["content"] == "hello"


# ── tool result handling ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_result_appended_to_history():
    """After a tool executes, its result is added to history before the next LLM call."""
    responses = [
        _tool_response("query_jobs", {}),
        _text_response("Done."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock, return_value=[{"id": 1}]):
        await run(
            messages=[{"role": "user", "content": "list jobs"}],
            db=MagicMock(),
            llm=llm,
        )
    second_call_history = llm.chat.call_args.args[0]
    roles = [m["role"] for m in second_call_history]
    assert "tool" in roles


@pytest.mark.asyncio
async def test_tool_error_does_not_crash_loop():
    """A tool returning an error JSON string is handled gracefully — loop continues."""
    responses = [
        _tool_response("update_status", {"job_id": 999, "status": "applied"}),
        _text_response("That job was not found."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.update_job_status", new_callable=AsyncMock, return_value=None):
        reply = await run(
            messages=[{"role": "user", "content": "update job 999"}],
            db=MagicMock(),
            llm=llm,
        )
    assert reply == "That job was not found."


@pytest.mark.asyncio
async def test_multiple_sequential_tool_calls():
    """Model calls two tools in separate iterations before returning a final reply."""
    responses = [
        _tool_response("query_jobs", {}, call_id="call_1"),
        _tool_response("update_status", {"job_id": 1, "status": "applied"}, call_id="call_2"),
        _text_response("All done."),
    ]
    llm = _make_llm(responses)
    record = {"id": 1, "company": "Stripe", "role": "Engineer", "url": "x",
              "status": "applied", "source": None, "notes": None,
              "fingerprint": "fp", "date_logged": "2026-05-06"}
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs", new_callable=AsyncMock, return_value=[record]), \
         patch("agent.tools.repo.update_job_status", new_callable=AsyncMock, return_value=record):
        reply = await run(
            messages=[{"role": "user", "content": "list then update"}],
            db=MagicMock(),
            llm=llm,
        )
    assert reply == "All done."
    assert llm.chat.call_count == 3


# ── profile integration ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profile_injected_into_system_prompt():
    """The current profile JSON appears inside the system prompt sent to the model."""
    profile = {"skills": ["Python"], "target_roles": ["Data Engineer"]}
    llm = _make_llm([_text_response("ok")])
    with patch("agent.agent.read_profile", return_value=profile):
        await run(
            messages=[{"role": "user", "content": "hi"}],
            db=MagicMock(),
            llm=llm,
        )
    system_content = llm.chat.call_args.args[0][0]["content"]
    assert "Data Engineer" in system_content
    assert "Python" in system_content


@pytest.mark.asyncio
async def test_profile_update_triggers_backup(tmp_path, monkeypatch):
    """When the agent calls update_profile with a changed value, a backup is created."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.json").write_text(json.dumps({"skills": ["Python"]}))

    responses = [
        _tool_response("update_profile", {"updates": {"skills": ["Python", "SQL"]}}),
        _text_response("Profile updated."),
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={"skills": ["Python"]}):
        await run(
            messages=[{"role": "user", "content": "add SQL to my skills"}],
            db=MagicMock(),
            llm=llm,
        )
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 1
