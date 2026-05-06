from unittest.mock import patch, MagicMock

from agent.llm_client import LLMClient
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
    client = LLMClient(model="gpt-4o")
    assert client.model == "gpt-4o"


# ── chat call ─────────────────────────────────────────────────────────────────

def test_llm_client_calls_litellm_completion():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="claude-sonnet-4-5").chat(
            [{"role": "user", "content": "hi"}], []
        )
    mock_completion.assert_called_once()


def test_llm_client_passes_messages_and_tools():
    messages = [{"role": "user", "content": "hi"}]
    tools    = [{"type": "function", "function": {"name": "test"}}]
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="claude-sonnet-4-5").chat(messages, tools)
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["messages"] == messages
    assert kwargs["tools"]    == tools


def test_llm_client_passes_correct_model():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="gpt-4o").chat([{"role": "user", "content": "hi"}], [])
    assert mock_completion.call_args.kwargs["model"] == "gpt-4o"


def test_llm_client_sets_tool_choice_auto():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="claude-sonnet-4-5").chat([], [])
    assert mock_completion.call_args.kwargs["tool_choice"] == "auto"


def test_llm_client_returns_response_object():
    mock_resp = _mock_response(content="pong")
    with patch("agent.llm_client.completion", return_value=mock_resp):
        response = LLMClient(model="claude-sonnet-4-5").chat([], [])
    assert response is mock_resp


def test_llm_client_response_content_accessible():
    with patch("agent.llm_client.completion",
               return_value=_mock_response(content="You have 3 jobs.")):
        response = LLMClient(model="claude-sonnet-4-5").chat([], [])
    assert response.choices[0].message.content == "You have 3 jobs."


def test_llm_client_response_tool_calls_accessible():
    tool_call = MagicMock()
    with patch("agent.llm_client.completion",
               return_value=_mock_response(tool_calls=[tool_call])):
        response = LLMClient(model="claude-sonnet-4-5").chat([], [])
    assert response.choices[0].message.tool_calls == [tool_call]
