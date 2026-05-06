import pytest
from loguru import logger
from unittest.mock import AsyncMock, patch


# ── basic response shape ──────────────────────────────────────────────────────

async def test_chat_returns_200(async_client):
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="You have 0 applications."):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "how many jobs?"}]
        })
    logger.info(f"[test_chat_returns_200] status={response.status_code}")
    assert response.status_code == 200


async def test_chat_response_contains_reply_key(async_client):
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="You have 0 applications."):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "how many jobs?"}]
        })
    logger.info(f"[test_chat_response_contains_reply_key] body={response.json()}")
    assert "reply" in response.json()


async def test_chat_reply_matches_agent_output(async_client):
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="You have 0 applications."):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "how many jobs?"}]
        })
    logger.info(f"[test_chat_reply_matches_agent_output] reply={response.json()['reply']}")
    assert response.json()["reply"] == "You have 0 applications."


# ── message history forwarding ────────────────────────────────────────────────

async def test_chat_passes_full_message_history(async_client):
    """The route must forward the entire history to run(), not just the last message."""
    history = [
        {"role": "user",      "content": "find me jobs"},
        {"role": "assistant", "content": "I found 3 roles..."},
        {"role": "user",      "content": "log the first one"},
    ]
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="Logged.") as mock_run:
        response = await async_client.post("/chat", json={"messages": history})
    passed_messages = mock_run.call_args.args[0]
    logger.info(f"[test_chat_passes_full_message_history] messages_received={len(passed_messages)} reply={response.json()['reply']}")
    assert len(passed_messages) == 3


async def test_chat_preserves_message_order(async_client):
    history = [
        {"role": "user",      "content": "first message"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user",      "content": "second message"},
    ]
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="ok") as mock_run:
        await async_client.post("/chat", json={"messages": history})
    passed = mock_run.call_args.args[0]
    logger.info(f"[test_chat_preserves_message_order] first={passed[0]['content']} last={passed[2]['content']}")
    assert passed[0]["content"] == "first message"
    assert passed[2]["content"] == "second message"


async def test_chat_works_with_empty_message_list(async_client):
    with patch("app.routes.chat.run", new_callable=AsyncMock,
               return_value="Hello! How can I help?"):
        response = await async_client.post("/chat", json={"messages": []})
    logger.info(f"[test_chat_works_with_empty_message_list] reply={response.json()['reply']}")
    assert response.status_code == 200


# ── session_id field ──────────────────────────────────────────────────────────

async def test_chat_session_id_is_optional(async_client):
    """session_id is reserved for future use — omitting it must not cause a 422."""
    with patch("app.routes.chat.run", new_callable=AsyncMock, return_value="ok"):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "hi"}]
        })
    logger.info(f"[test_chat_session_id_is_optional] status={response.status_code}")
    assert response.status_code == 200


async def test_chat_accepts_session_id_when_provided(async_client):
    with patch("app.routes.chat.run", new_callable=AsyncMock, return_value="ok"):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "session_id": "abc-123",
        })
    logger.info(f"[test_chat_accepts_session_id_when_provided] status={response.status_code}")
    assert response.status_code == 200


# ── validation ────────────────────────────────────────────────────────────────

async def test_chat_missing_messages_field_returns_422(async_client):
    """messages is a required field — omitting it must return a validation error."""
    response = await async_client.post("/chat", json={})
    logger.info(f"[test_chat_missing_messages_field_returns_422] status={response.status_code} body={response.json()}")
    assert response.status_code == 422


async def test_chat_non_list_messages_returns_422(async_client):
    response = await async_client.post("/chat", json={"messages": "not a list"})
    logger.info(f"[test_chat_non_list_messages_returns_422] status={response.status_code} body={response.json()}")
    assert response.status_code == 422