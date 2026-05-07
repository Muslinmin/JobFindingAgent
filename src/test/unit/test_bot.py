"""
Unit tests for bot/bot.py — handle_message handler.

Mocks: httpx.AsyncClient (POST /chat), context.bot.send_message (Telegram send).
Tested: access control, payload shape, response delivery, error handling.

Run with:
    pytest src/test/unit/test_bot.py -v
"""

import os
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from bot.bot import handle_message

KNOWN_CHAT_ID = 12345
UNKNOWN_CHAT_ID = 99999


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_update(chat_id: int, text: str = "hello"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    return update


def _make_context(history: list | None = None):
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.user_data = {"history": history or []}
    return context


def _mock_http_client(reply: str = "agent reply"):
    """Return a patched httpx.AsyncClient that responds with the given reply."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"reply": reply}
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_client


# ── access control ────────────────────────────────────────────────────────────

async def test_unknown_chat_id_is_rejected():
    mock_cls, mock_client = _mock_http_client()
    context = _make_context()

    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        await handle_message(_make_update(UNKNOWN_CHAT_ID), context)

    mock_client.post.assert_not_called()
    context.bot.send_message.assert_not_called()


async def test_known_chat_id_is_forwarded():
    mock_cls, mock_client = _mock_http_client()
    context = _make_context()

    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID, "hi"), context)

    mock_client.post.assert_called_once()


# ── payload shape ─────────────────────────────────────────────────────────────

async def test_payload_contains_message_text_and_history():
    mock_cls, mock_client = _mock_http_client()
    prior_history = [{"role": "assistant", "content": "Hello!"}]
    context = _make_context(history=prior_history)

    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID, "how many jobs?"), context)

    _, kwargs = mock_client.post.call_args
    payload = kwargs.get("json") or mock_client.post.call_args[0][1]
    messages = payload["messages"]
    assert messages[-1] == {"role": "user", "content": "how many jobs?"}
    assert {"role": "assistant", "content": "Hello!"} in messages


# ── response delivery ─────────────────────────────────────────────────────────

async def test_agent_reply_sent_to_correct_chat_id():
    mock_cls, _ = _mock_http_client(reply="Here are your jobs.")
    context = _make_context()

    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID), context)

    context.bot.send_message.assert_called_once()
    call_kwargs = context.bot.send_message.call_args[1]
    assert call_kwargs["chat_id"] == KNOWN_CHAT_ID
    assert call_kwargs["text"] == "Here are your jobs."


# ── error handling ────────────────────────────────────────────────────────────

async def test_empty_reply_sends_fallback():
    mock_response = MagicMock()
    mock_response.json.return_value = {"reply": ""}
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    context = _make_context()
    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID), context)

    context.bot.send_message.assert_called_once()
    text = context.bot.send_message.call_args[1]["text"]
    assert isinstance(text, str) and len(text) > 0


async def test_malformed_json_response_sends_fallback():
    mock_response = MagicMock()
    mock_response.json.side_effect = ValueError("no JSON")
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    context = _make_context()
    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID), context)

    context.bot.send_message.assert_called_once()


async def test_endpoint_down_sends_fallback_and_does_not_raise():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    context = _make_context()
    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID), context)

    context.bot.send_message.assert_called_once()
    text = context.bot.send_message.call_args[1]["text"]
    assert isinstance(text, str) and len(text) > 0


# ── integration: mocked round-trip ────────────────────────────────────────────

async def test_mocked_round_trip_full_flow():
    mock_cls, _ = _mock_http_client(reply="You have 3 jobs tracked.")
    context = _make_context()

    with patch("bot.bot.settings") as mock_settings, \
         patch("bot.bot.httpx.AsyncClient", mock_cls):
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID, "how many jobs?"), context)

    context.bot.send_message.assert_called_once()
    assert context.bot.send_message.call_args[1]["text"] == "You have 3 jobs tracked."


# ── integration: live LLM round-trip (gated) ─────────────────────────────────

@pytest.mark.skipif(
    not os.getenv("RUN_LIVE_TESTS"),
    reason="set RUN_LIVE_TESTS=true to run live tests",
)
async def test_live_llm_round_trip():
    context = _make_context()

    with patch("bot.bot.settings") as mock_settings:
        mock_settings.telegram_chat_id = KNOWN_CHAT_ID
        mock_settings.api_base_url = "http://localhost:8000"
        await handle_message(_make_update(KNOWN_CHAT_ID, "how many jobs am I tracking?"), context)

    context.bot.send_message.assert_called_once()
    text = context.bot.send_message.call_args[1]["text"]
    assert isinstance(text, str) and len(text) > 0
