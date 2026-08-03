from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.chat.agent_client import AgentBackendClient
from telegram_bot.shared.errors import BackendError

BASE_URL = "http://localhost:8000"


def _http(status_code: int, body: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    http = AsyncMock()
    http.post = AsyncMock(return_value=resp)
    return http


async def test_post_chat_sends_message_only_no_extra_fields():
    http = _http(200, {"reply": "hi", "attachments": []})
    client = AgentBackendClient(BASE_URL, http)

    await client.post_chat("how many jobs?")

    http.post.assert_called_once_with(f"{BASE_URL}/chat", json={"message": "how many jobs?"})


async def test_post_chat_returns_reply_only_body():
    http = _http(200, {"reply": "You have 3 jobs.", "attachments": []})
    client = AgentBackendClient(BASE_URL, http)

    result = await client.post_chat("how many jobs?")

    assert result == {"reply": "You have 3 jobs.", "attachments": []}


async def test_post_chat_returns_body_with_attachments():
    body = {
        "reply": "Here's your CV.",
        "attachments": [
            {"kind": "cv_pdf", "filename": "resume.pdf", "mime_type": "application/pdf", "content_b64": "abc"}
        ],
    }
    http = _http(200, body)
    client = AgentBackendClient(BASE_URL, http)

    result = await client.post_chat("tailor for job 3")

    assert result["attachments"][0]["content_b64"] == "abc"


async def test_post_chat_422_raises_backend_error():
    http = _http(422, {"detail": "message is empty"})
    client = AgentBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_chat("")

    assert exc_info.value.status_code == 422


async def test_post_chat_500_raises_backend_error():
    http = _http(500, {"detail": "agent raised"})
    client = AgentBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_chat("hi")

    assert exc_info.value.status_code == 500
