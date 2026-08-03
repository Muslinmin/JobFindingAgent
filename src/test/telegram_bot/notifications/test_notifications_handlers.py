import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.notifications.handlers import _parse_callback_data, handle_callback
from telegram_bot.shared.errors import BackendError

CHAT_ID = 67890


def _update(chat_id: int, callback_data: str):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    return update


def _ctx(chat_id: int = CHAT_ID):
    send_client = AsyncMock()
    backend_client = AsyncMock()
    ctx = MagicMock()
    ctx.bot_data = {"chat_id": chat_id, "send_client": send_client, "backend_client": backend_client}
    return ctx, send_client, backend_client


# ── _parse_callback_data ──────────────────────────────────────────────────────

def test_parse_action_callback():
    parsed = _parse_callback_data(json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42}))
    assert parsed == {"kind": "action", "job_id": 42, "action": "APPLIED"}


def test_parse_followup_callback_has_no_action():
    parsed = _parse_callback_data(json.dumps({"kind": "followup", "job_id": 42}))
    assert parsed == {"kind": "followup", "job_id": 42, "action": None}


def test_parse_malformed_json_raises_value_error():
    with pytest.raises(ValueError):
        _parse_callback_data("not json")


def test_parse_missing_job_id_raises_value_error():
    with pytest.raises(ValueError):
        _parse_callback_data(json.dumps({"kind": "action", "action": "APPLIED"}))


# ── handle_callback: routing ──────────────────────────────────────────────────

async def test_action_kind_posts_action_verbatim_and_confirms():
    ctx, send_client, backend_client = _ctx()
    backend_client.post_action.return_value = {"id": 42, "status": "applied"}
    data = json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    backend_client.post_action.assert_called_once_with(42, "APPLIED")
    send_client.send_message.assert_called_once()


async def test_followup_kind_posts_empty_body_and_confirms():
    ctx, send_client, backend_client = _ctx()
    backend_client.post_followup.return_value = {"id": 42, "follow_up_count": 1}
    data = json.dumps({"kind": "followup", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    backend_client.post_followup.assert_called_once_with(42)
    send_client.send_message.assert_called_once()


async def test_dismiss_kind_makes_no_backend_call():
    ctx, send_client, backend_client = _ctx()
    data = json.dumps({"kind": "dismiss", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    backend_client.post_action.assert_not_called()
    backend_client.post_followup.assert_not_called()
    send_client.send_message.assert_not_called()


# ── handle_callback: answer_callback_query always fires ───────────────────────

async def test_answer_callback_query_fires_on_action():
    ctx, send_client, backend_client = _ctx()
    data = json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42})
    update = _update(CHAT_ID, data)

    await handle_callback(update, ctx)

    update.callback_query.answer.assert_called_once()


async def test_answer_callback_query_fires_on_dismiss():
    ctx, send_client, backend_client = _ctx()
    data = json.dumps({"kind": "dismiss", "job_id": 42})
    update = _update(CHAT_ID, data)

    await handle_callback(update, ctx)

    update.callback_query.answer.assert_called_once()


async def test_answer_callback_query_fires_even_when_unauthorised():
    ctx, send_client, backend_client = _ctx(chat_id=CHAT_ID)
    data = json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42})
    update = _update(99999, data)

    await handle_callback(update, ctx)

    update.callback_query.answer.assert_called_once()
    backend_client.post_action.assert_not_called()
    send_client.send_message.assert_not_called()


# ── handle_callback: errors route through shared error handler ───────────────

async def test_action_404_routes_through_error_handler():
    ctx, send_client, backend_client = _ctx()
    backend_client.post_action.side_effect = BackendError(404, "job 42 not found")
    data = json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    send_client.send_message.assert_called_once()
    text = send_client.send_message.call_args[0][0]
    assert "can't find that job" in text.lower()


async def test_action_409_routes_through_error_handler():
    ctx, send_client, backend_client = _ctx()
    backend_client.post_action.side_effect = BackendError(409, "Cannot transition")
    data = json.dumps({"kind": "action", "action": "APPLIED", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    text = send_client.send_message.call_args[0][0]
    assert "doesn't apply to this job right now" in text


async def test_followup_409_job_not_applied_routes_through_error_handler():
    ctx, send_client, backend_client = _ctx()
    backend_client.post_followup.side_effect = BackendError(409, "job 42 is not APPLIED")
    data = json.dumps({"kind": "followup", "job_id": 42})

    await handle_callback(_update(CHAT_ID, data), ctx)

    send_client.send_message.assert_called_once()


async def test_malformed_callback_data_sends_generic_message_no_backend_call():
    ctx, send_client, backend_client = _ctx()

    await handle_callback(_update(CHAT_ID, "not json"), ctx)

    backend_client.post_action.assert_not_called()
    backend_client.post_followup.assert_not_called()
    send_client.send_message.assert_called_once()
