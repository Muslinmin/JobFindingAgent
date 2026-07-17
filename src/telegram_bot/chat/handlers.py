import base64

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.shared.auth import is_authorised
from telegram_bot.shared.errors import handle_backend_error

GREETING = "What can I do for you today?"


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text -> guard non-empty -> AgentBackendClient.post_chat.

    On success: send `reply` via ChatTelegramClient.send_message; then for
    each item in `attachments` (if any), base64-decode `content_b64` and
    send it via send_document. No history assembled here — the agent's own
    conversation store owns that; this handler only relays. BackendError /
    transport failures route through shared.errors.handle_backend_error.
    """
    allowed_chat_id = ctx.bot_data["chat_id"]
    if not is_authorised(update, allowed_chat_id):
        return

    send_client = ctx.bot_data["send_client"]
    backend_client = ctx.bot_data["backend_client"]

    text = (update.message.text or "").strip()
    if not text:
        logger.debug("Chat bot: ignoring empty message")
        return

    try:
        result = await backend_client.post_chat(text)
    except Exception as exc:
        await handle_backend_error(send_client, exc, context="POST /chat")
        return

    await send_client.send_message(result["reply"])

    for attachment in result.get("attachments") or []:
        pdf_bytes = base64.b64decode(attachment["content_b64"])
        await send_client.send_document(
            result["reply"], pdf_bytes, filename=attachment["filename"]
        )


async def handle_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the static GREETING locally. No backend call."""
    allowed_chat_id = ctx.bot_data["chat_id"]
    if not is_authorised(update, allowed_chat_id):
        return

    send_client = ctx.bot_data["send_client"]
    await send_client.send_message(GREETING)
