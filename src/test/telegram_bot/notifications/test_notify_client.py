from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.notifications.notify_client import NotifyBackendClient
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


async def test_post_action_posts_action_body_to_correct_endpoint():
    http = _http(200, {"id": 42, "status": "applied"})
    client = NotifyBackendClient(BASE_URL, http)

    result = await client.post_action(42, "applied")

    http.post.assert_called_once_with(f"{BASE_URL}/jobs/42/action", json={"action": "applied"})
    assert result == {"id": 42, "status": "applied"}


async def test_post_action_404_raises_backend_error():
    http = _http(404, {"detail": "job 42 not found"})
    client = NotifyBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_action(42, "applied")

    assert exc_info.value.status_code == 404


async def test_post_action_409_raises_backend_error():
    http = _http(409, {"detail": "Cannot transition from 'tailored' to 'applied'"})
    client = NotifyBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_action(42, "applied")

    assert exc_info.value.status_code == 409


async def test_post_action_422_raises_backend_error():
    http = _http(422, {"detail": "invalid action"})
    client = NotifyBackendClient(BASE_URL, http)

    with pytest.raises(BackendError):
        await client.post_action(42, "not_a_real_action")


async def test_post_followup_posts_empty_body_to_correct_endpoint():
    http = _http(200, {"id": 42, "follow_up_count": 1})
    client = NotifyBackendClient(BASE_URL, http)

    result = await client.post_followup(42)

    http.post.assert_called_once_with(f"{BASE_URL}/jobs/42/follow-up", json={})
    assert result == {"id": 42, "follow_up_count": 1}


async def test_post_followup_409_raises_backend_error_job_not_applied():
    http = _http(409, {"detail": "job 42 is not APPLIED"})
    client = NotifyBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_followup(42)

    assert exc_info.value.status_code == 409


async def test_post_followup_404_raises_backend_error():
    http = _http(404, {"detail": "job 42 not found"})
    client = NotifyBackendClient(BASE_URL, http)

    with pytest.raises(BackendError) as exc_info:
        await client.post_followup(42)

    assert exc_info.value.status_code == 404
