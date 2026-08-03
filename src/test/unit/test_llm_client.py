import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from agent.llm_client import AgentLLMClient, LLMCallFailed, TaskLLMClient
from app.config import settings


def _mock_response(content="hello", tool_calls=None):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=tool_calls)
    )])


# ── task client (WP3 — tailoring layer, shared by other single-pass jobs) ──────

def test_task_llm_client_uses_settings_model_by_default():
    client = TaskLLMClient()
    assert client.model == settings.model


def test_task_llm_client_accepts_model_override():
    client = TaskLLMClient(model="test-model-override")
    assert client.model == "test-model-override"


@pytest.mark.asyncio
async def test_task_llm_client_calls_litellm_acompletion_with_single_message():
    with patch("agent.llm_client.acompletion",
               new=AsyncMock(return_value=_mock_response(content="tailored"))) as mock_acompletion:
        result = await TaskLLMClient(model="test-model").complete("a prompt")
    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"] == [{"role": "user", "content": "a prompt"}]
    assert result == "tailored"


@pytest.mark.asyncio
async def test_task_llm_client_does_not_retry_on_rate_limit():
    from litellm.exceptions import RateLimitError

    with patch(
        "agent.llm_client.acompletion",
        new=AsyncMock(side_effect=RateLimitError("rate limited", llm_provider="test", model="test-model")),
    ) as mock_acompletion:
        with pytest.raises(RateLimitError):
            await TaskLLMClient(model="test-model").complete("a prompt")
    mock_acompletion.assert_called_once()


# ── agent client (WP-A7 / WP-C1b — interactive, tool-calling, retrying) ────────

TOOLS = [{"type": "function", "function": {"name": "find_jobs", "parameters": {}}}]


def _agent_client(**overrides):
    defaults = dict(model="test-model", max_retries=3, retry_wait_s=0, timeout_s=60)
    return AgentLLMClient(**{**defaults, **overrides})


def test_agent_client_defaults_come_from_the_timeout_ladder_settings():
    client = AgentLLMClient()
    assert client.model == settings.model
    assert client.max_retries == settings.llm_max_retries
    assert client.retry_wait_s == settings.llm_retry_wait_s
    assert client.timeout_s == settings.llm_call_timeout_s


async def test_agent_client_passes_tools_through_to_acompletion():
    with patch(
        "agent.llm_client.acompletion", new=AsyncMock(return_value=_mock_response())
    ) as mock_acompletion:
        await _agent_client().chat([{"role": "user", "content": "hi"}], TOOLS)

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["tools"] == TOOLS
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


async def test_agent_client_sends_the_tool_calling_contract():
    """`reasoning_effort` is what makes function tools work at all on the
    gpt-5.6 family via /v1/chat/completions; `parallel_tool_calls=False` is
    what makes the loop's one-call-at-a-time design a property of the
    request. `drop_params` keeps both no-ops on providers without the knob."""
    with patch(
        "agent.llm_client.acompletion", new=AsyncMock(return_value=_mock_response())
    ) as mock_acompletion:
        await _agent_client().chat([], TOOLS)

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["reasoning_effort"] == settings.llm_reasoning_effort
    assert kwargs["parallel_tool_calls"] is False
    assert kwargs["drop_params"] is True


async def test_agent_client_omits_tool_params_when_there_are_no_tools():
    """OpenAI rejects `parallel_tool_calls` on a request carrying no
    `tools`, so it cannot be passed unconditionally."""
    with patch(
        "agent.llm_client.acompletion", new=AsyncMock(return_value=_mock_response())
    ) as mock_acompletion:
        await _agent_client().chat([{"role": "user", "content": "hi"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert "parallel_tool_calls" not in kwargs
    assert "tools" not in kwargs


async def test_agent_client_omits_reasoning_effort_when_unset(monkeypatch):
    """A reasoning model whose enum has no 'none' 400s on the value, and
    drop_params only removes unsupported *names* — hence the escape hatch."""
    monkeypatch.setattr(settings, "llm_reasoning_effort", "")
    with patch(
        "agent.llm_client.acompletion", new=AsyncMock(return_value=_mock_response())
    ) as mock_acompletion:
        await _agent_client().chat([], TOOLS)

    assert "reasoning_effort" not in mock_acompletion.call_args.kwargs


async def test_agent_client_returns_the_raw_response():
    """The loop needs both the text and any tool_calls, and has to append the
    assistant message back verbatim — so nothing is parsed out here."""
    response = _mock_response(content="hello")
    with patch("agent.llm_client.acompletion", new=AsyncMock(return_value=response)):
        assert await _agent_client().chat([], TOOLS) is response


async def test_agent_client_retries_and_succeeds():
    from litellm.exceptions import RateLimitError

    attempts = AsyncMock(
        side_effect=[
            RateLimitError("rate limited", llm_provider="test", model="test-model"),
            _mock_response(content="recovered"),
        ]
    )
    with patch("agent.llm_client.acompletion", new=attempts):
        result = await _agent_client().chat([], TOOLS)

    assert result.choices[0].message.content == "recovered"
    assert attempts.call_count == 2


async def test_agent_client_raises_after_exhausting_retries():
    from litellm.exceptions import RateLimitError

    attempts = AsyncMock(
        side_effect=RateLimitError("rate limited", llm_provider="test", model="test-model")
    )
    with patch("agent.llm_client.acompletion", new=attempts):
        with pytest.raises(LLMCallFailed):
            await _agent_client(max_retries=3).chat([], TOOLS)

    assert attempts.call_count == 3


async def test_agent_client_preserves_the_underlying_error_as_cause():
    with patch("agent.llm_client.acompletion", new=AsyncMock(side_effect=ValueError("bad key"))):
        with pytest.raises(LLMCallFailed) as excinfo:
            await _agent_client(max_retries=2).chat([], TOOLS)

    assert isinstance(excinfo.value.__cause__, ValueError)


async def test_agent_client_waits_between_retries_without_blocking():
    """Backoff must be `await asyncio.sleep`, never `time.sleep` — this app
    runs the API, both bots, and the scheduler on one event loop."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    attempts = AsyncMock(side_effect=[ValueError("x"), _mock_response(content="ok")])
    task = asyncio.create_task(ticker())
    with patch("agent.llm_client.acompletion", new=attempts):
        await _agent_client(retry_wait_s=0.01).chat([], TOOLS)
    task.cancel()

    assert ticks > 1


async def test_agent_client_does_not_retry_a_cancelled_turn():
    """When the caller's turn deadline fires it cancels this coroutine;
    catching that to sleep and retry would burn time on a turn that has
    already given up."""
    attempts = AsyncMock(side_effect=asyncio.CancelledError())
    with patch("agent.llm_client.acompletion", new=attempts):
        with pytest.raises(asyncio.CancelledError):
            await _agent_client().chat([], TOOLS)

    assert attempts.call_count == 1


async def test_agent_client_bounds_each_call_with_its_own_timeout():
    async def never_returns(*args, **kwargs):
        await asyncio.sleep(10)

    with patch("agent.llm_client.acompletion", new=never_returns):
        with pytest.raises(LLMCallFailed):
            await _agent_client(max_retries=1, timeout_s=0.01).chat([], TOOLS)
