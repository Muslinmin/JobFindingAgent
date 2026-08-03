import base64
import re

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.shared.auth import is_authorised
from telegram_bot.shared.errors import handle_backend_error

GREETING = "What can I do for you today?"

# `agent/prompts/system.md` tells the model to end a turn that proposes a
# two-turn confirmation with `<<<PENDING_ACTION {...}>>>`. It is machinery,
# not speech, so it is stripped here — at the display boundary and nowhere
# earlier. The *stored* turn keeps it, which is the whole point: the next
# turn reads the payload back out of history and replays it verbatim.
# Non-greedy so two markers in one message can't be swallowed as one, and
# DOTALL because the payload may wrap across lines.
_PENDING_ACTION_RE = re.compile(r"<<<PENDING_ACTION\b.*?>>>", re.DOTALL)


def strip_pending_action(reply: str) -> str:
    """Remove every marker and tidy up the whitespace it leaves behind.

    A turn that is *only* a marker would otherwise send an empty message,
    which Telegram rejects outright — so the caller checks for empty after
    stripping rather than assuming there is always prose left over.
    """
    return _PENDING_ACTION_RE.sub("", reply).strip()


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text -> guard non-empty -> AgentBackendClient.post_chat.

    On success: strip the PENDING_ACTION marker, send `reply` via
    ChatTelegramClient.send_message; then for each item in `attachments`
    (if any), base64-decode `content_b64` and send it via send_document.
    No history assembled here — the agent's own conversation store owns
    that; this handler only relays. BackendError / transport failures route
    through shared.errors.handle_backend_error.
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

    reply = strip_pending_action(result["reply"])
    if reply:
        await send_client.send_message(reply)

    for attachment in result.get("attachments") or []:
        # Empty caption, not the reply: the reply already went out as its
        # own message above, and repeating it under the file means the user
        # reads the same paragraph twice. It would also truncate — a
        # caption is capped at 1024 characters where a message allows 4096.
        await send_client.send_document(
            "", base64.b64decode(attachment["content_b64"]), filename=attachment["filename"]
        )


async def handle_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the static GREETING locally. No backend call."""
    allowed_chat_id = ctx.bot_data["chat_id"]
    if not is_authorised(update, allowed_chat_id):
        return

    send_client = ctx.bot_data["send_client"]
    await send_client.send_message(GREETING)
