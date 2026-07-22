import base64
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.chat.handlers import (
    GREETING,
    handle_message,
    handle_start,
    strip_pending_action,
)
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


# ── PENDING_ACTION marker ─────────────────────────────────────────────────────

MARKER = '<<<PENDING_ACTION {"kind":"tailor_accept","payload":{"job_id":3}}>>>'


def test_strip_removes_the_marker_and_the_whitespace_it_leaves():
    assert strip_pending_action(f"Shall I mark it applied?\n\n{MARKER}") == "Shall I mark it applied?"


def test_strip_handles_a_payload_that_wrapped_across_lines():
    wrapped = '<<<PENDING_ACTION {"kind":"profile_update",\n"payload":{"op":"add_skill"}}>>>'
    assert strip_pending_action(f"Add Python?\n{wrapped}") == "Add Python?"


def test_strip_does_not_swallow_the_text_between_two_markers():
    """Non-greedy: a greedy match would eat 'and this' as part of one marker."""
    assert strip_pending_action(f"{MARKER} and this {MARKER}") == "and this"


def test_strip_leaves_an_ordinary_reply_alone():
    assert strip_pending_action("You have 3 jobs.") == "You have 3 jobs."


async def test_the_marker_never_reaches_the_user():
    """It is machinery, not speech. The stored turn keeps it — that is what
    the next turn replays from — but it must not be displayed."""
    ctx, send_client, _ = _ctx(
        post_chat_return={"reply": f"Shall I mark it applied?\n\n{MARKER}", "attachments": []}
    )

    await handle_message(_update(CHAT_ID, "tailor it"), ctx)

    send_client.send_message.assert_called_once_with("Shall I mark it applied?")


async def test_a_reply_that_is_only_a_marker_sends_nothing():
    """Telegram rejects an empty message outright."""
    ctx, send_client, _ = _ctx(post_chat_return={"reply": MARKER, "attachments": []})

    await handle_message(_update(CHAT_ID, "x"), ctx)

    send_client.send_message.assert_not_called()


async def test_a_document_carries_no_caption_so_the_reply_is_not_repeated():
    """The reply already went out as its own message; repeating it under the
    file makes the user read the same paragraph twice, and a caption
    truncates at 1024 characters where a message allows 4096."""
    body = {
        "reply": "Here's your tailored CV.",
        "attachments": [{
            "kind": "cv_pdf", "filename": "resume.pdf", "mime_type": "application/pdf",
            "content_b64": base64.b64encode(b"%PDF").decode(),
        }],
    }
    ctx, send_client, _ = _ctx(post_chat_return=body)

    await handle_message(_update(CHAT_ID, "tailor for job 3"), ctx)

    send_client.send_message.assert_called_once_with("Here's your tailored CV.")
    assert send_client.send_document.call_args.args[0] == ""
