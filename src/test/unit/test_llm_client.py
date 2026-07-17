from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from agent.llm_client import AsyncLLMClient, LLMClient
from app.config import settings


def _mock_response(content="hello", tool_calls=None):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=tool_calls)
    )])


# ── instantiation ─────────────────────────────────────────────────────────────

def test_llm_client_uses_settings_model_by_default():
    client = LLMClient()
    assert client.model == settings.model


def test_llm_client_accepts_model_override():
    client = LLMClient(model="test-model-override")
    assert client.model == "test-model-override"


# ── chat call ─────────────────────────────────────────────────────────────────

def test_llm_client_calls_litellm_completion():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="test-model").chat(
            [{"role": "user", "content": "hi"}], []
        )
    mock_completion.assert_called_once()


def test_llm_client_passes_messages_and_tools():
    messages = [{"role": "user", "content": "hi"}]
    tools    = [{"type": "function", "function": {"name": "test"}}]
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="test-model").chat(messages, tools)
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["messages"] == messages
    assert kwargs["tools"]    == tools


def test_llm_client_passes_correct_model():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="test-model-override").chat([{"role": "user", "content": "hi"}], [])
    assert mock_completion.call_args.kwargs["model"] == "test-model-override"


def test_llm_client_sets_tool_choice_auto():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="test-model").chat([], [])
    assert mock_completion.call_args.kwargs["tool_choice"] == "auto"


def test_llm_client_returns_response_object():
    mock_resp = _mock_response(content="pong")
    with patch("agent.llm_client.completion", return_value=mock_resp):
        response = LLMClient(model="test-model").chat([], [])
    assert response is mock_resp


def test_llm_client_response_content_accessible():
    with patch("agent.llm_client.completion",
               return_value=_mock_response(content="You have 3 jobs.")):
        response = LLMClient(model="test-model").chat([], [])
    assert response.choices[0].message.content == "You have 3 jobs."


def test_llm_client_response_tool_calls_accessible():
    tool_call = MagicMock()
    with patch("agent.llm_client.completion",
               return_value=_mock_response(tool_calls=[tool_call])):
        response = LLMClient(model="test-model").chat([], [])
    assert response.choices[0].message.tool_calls == [tool_call]


# ── async client (WP3 — tailoring layer) ────────────────────────────────────────

def test_async_llm_client_uses_settings_model_by_default():
    client = AsyncLLMClient()
    assert client.model == settings.model


def test_async_llm_client_accepts_model_override():
    client = AsyncLLMClient(model="test-model-override")
    assert client.model == "test-model-override"


@pytest.mark.asyncio
async def test_async_llm_client_calls_litellm_acompletion_with_single_message():
    with patch("agent.llm_client.acompletion",
               new=AsyncMock(return_value=_mock_response(content="tailored"))) as mock_acompletion:
        result = await AsyncLLMClient(model="test-model").complete("a prompt")
    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"] == [{"role": "user", "content": "a prompt"}]
    assert result == "tailored"


@pytest.mark.asyncio
async def test_async_llm_client_does_not_retry_on_rate_limit():
    from litellm.exceptions import RateLimitError

    with patch(
        "agent.llm_client.acompletion",
        new=AsyncMock(side_effect=RateLimitError("rate limited", llm_provider="test", model="test-model")),
    ) as mock_acompletion:
        with pytest.raises(RateLimitError):
            await AsyncLLMClient(model="test-model").complete("a prompt")
    mock_acompletion.assert_called_once()
