import base64
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.chat.handlers import GREETING, handle_message, handle_start
from telegram_bot.shared.errors import BackendError

CHAT_ID = 12345


def _update(chat_id: int, text: str | None = "hello"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    return update


def _ctx(chat_id: int = CHAT_ID, post_chat_return=None, post_chat_side_effect=None):
    send_client = AsyncMock()
    backend_client = AsyncMock()
    if post_chat_side_effect is not None:
        backend_client.post_chat.side_effect = post_chat_side_effect
    else:
        backend_client.post_chat.return_value = post_chat_return or {"reply": "ok", "attachments": []}

    ctx = MagicMock()
    ctx.bot_data = {"chat_id": chat_id, "send_client": send_client, "backend_client": backend_client}
    return ctx, send_client, backend_client


async def test_unauthorised_chat_id_is_ignored():
    ctx, send_client, backend_client = _ctx(chat_id=CHAT_ID)

    await handle_message(_update(99999, "hi"), ctx)

    backend_client.post_chat.assert_not_called()
    send_client.send_message.assert_not_called()


async def test_forwards_message_text_to_backend():
    ctx, send_client, backend_client = _ctx()

    await handle_message(_update(CHAT_ID, "how many jobs?"), ctx)

    backend_client.post_chat.assert_called_once_with("how many jobs?")


async def test_empty_message_is_guarded_no_backend_call():
    ctx, send_client, backend_client = _ctx()

    await handle_message(_update(CHAT_ID, "   "), ctx)

    backend_client.post_chat.assert_not_called()
    send_client.send_message.assert_not_called()


async def test_reply_only_response_sends_text_no_document():
    ctx, send_client, backend_client = _ctx(
        post_chat_return={"reply": "You have 3 jobs.", "attachments": []}
    )

    await handle_message(_update(CHAT_ID, "how many jobs?"), ctx)

    send_client.send_message.assert_called_once_with("You have 3 jobs.")
    send_client.send_document.assert_not_called()


async def test_response_with_attachment_decodes_and_sends_document_after_text():
    pdf_bytes = b"%PDF-1.4 fake"
    body = {
        "reply": "Here's your tailored CV.",
        "attachments": [
            {
                "kind": "cv_pdf",
                "filename": "resume.pdf",
                "mime_type": "application/pdf",
                "content_b64": base64.b64encode(pdf_bytes).decode(),
            }
        ],
    }
    ctx, send_client, backend_client = _ctx(post_chat_return=body)

    await handle_message(_update(CHAT_ID, "tailor for job 3"), ctx)

    send_client.send_message.assert_called_once_with("Here's your tailored CV.")
    send_client.send_document.assert_called_once()
    args, kwargs = send_client.send_document.call_args
    assert args[1] == pdf_bytes
    assert kwargs["filename"] == "resume.pdf"


async def test_backend_error_routes_through_error_handler():
    ctx, send_client, backend_client = _ctx(post_chat_side_effect=BackendError(500, "agent raised"))

    await handle_message(_update(CHAT_ID, "hi"), ctx)

    send_client.send_message.assert_called_once()


async def test_start_sends_greeting_with_no_backend_call():
    ctx, send_client, backend_client = _ctx()

    await handle_start(_update(CHAT_ID, "/start"), ctx)

    send_client.send_message.assert_called_once_with(GREETING)
    backend_client.post_chat.assert_not_called()


async def test_start_unauthorised_chat_id_is_ignored():
    ctx, send_client, backend_client = _ctx(chat_id=CHAT_ID)

    await handle_start(_update(99999, "/start"), ctx)

    send_client.send_message.assert_not_called()
