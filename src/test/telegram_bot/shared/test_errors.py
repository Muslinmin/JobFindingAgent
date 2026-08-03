import httpx
from unittest.mock import AsyncMock

from telegram_bot.shared.errors import BackendError, handle_backend_error


def _client():
    client = AsyncMock()
    client.send_message = AsyncMock()
    return client


async def test_422_sends_bad_request_message():
    client = _client()
    await handle_backend_error(client, BackendError(422, "message is empty"), "POST /chat")
    text = client.send_message.call_args[0][0]
    assert "message is empty" in text


async def test_404_sends_job_not_found_message():
    client = _client()
    await handle_backend_error(client, BackendError(404, "job 42 not found"), "action job=42")
    text = client.send_message.call_args[0][0]
    assert "can't find that job" in text.lower()


async def test_409_sends_illegal_transition_message_distinct_from_404():
    client = _client()
    await handle_backend_error(
        client, BackendError(409, "Cannot transition from 'tailored' to 'applied'"), "action job=42"
    )
    text = client.send_message.call_args[0][0]
    assert "can't find that job" not in text.lower()
    assert "doesn't apply to this job right now" in text


async def test_500_sends_generic_server_error_message():
    client = _client()
    await handle_backend_error(client, BackendError(500, "agent raised"), "POST /chat")
    client.send_message.assert_called_once()


async def test_transport_failure_sends_unreachable_message():
    client = _client()
    await handle_backend_error(client, httpx.ConnectError("refused"), "POST /chat")
    text = client.send_message.call_args[0][0]
    assert "couldn't reach" in text.lower()


async def test_unexpected_exception_sends_generic_message_and_does_not_raise():
    client = _client()
    await handle_backend_error(client, ValueError("weird"), "POST /chat")
    client.send_message.assert_called_once()
