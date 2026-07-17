import json

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.shared.auth import is_authorised
from telegram_bot.shared.errors import handle_backend_error


def _parse_callback_data(raw: str) -> dict:
    """Return {"kind": str, "job_id": int, "action": str | None}.

    Raises ValueError on malformed payload (bad JSON, missing "kind" or
    "job_id"). `action` is present only for kind == "action" and is an
    opaque UserAction string this layer never maps or validates.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed callback_data: {raw!r}") from exc

    if "kind" not in payload or "job_id" not in payload:
        raise ValueError(f"callback_data missing required keys: {raw!r}")

    return {
        "kind": payload["kind"],
        "job_id": int(payload["job_id"]),
        "action": payload.get("action"),
    }


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse callback_data, branch on kind:
      action   -> NotifyBackendClient.post_action(job_id, action)
      followup -> NotifyBackendClient.post_followup(job_id)
      dismiss  -> no backend call
    Always answer_callback_query; confirm on 200; BackendError / malformed
    callback_data / transport failure route through
    shared.errors.handle_backend_error. Does NOT interpret the UserAction —
    the string is copied through opaque.
    """
    allowed_chat_id = ctx.bot_data["chat_id"]
    query = update.callback_query
    await query.answer()

    if not is_authorised(update, allowed_chat_id):
        return

    send_client = ctx.bot_data["send_client"]
    backend_client = ctx.bot_data["backend_client"]

    try:
        parsed = _parse_callback_data(query.data)
    except ValueError as exc:
        logger.warning(f"Notifications bot: {exc}")
        await send_client.send_message("That button couldn't be processed.")
        return

    kind = parsed["kind"]
    job_id = parsed["job_id"]

    try:
        if kind == "action":
            await backend_client.post_action(job_id, parsed["action"])
            await send_client.send_message(f"Updated job {job_id}: {parsed['action']}.")
        elif kind == "followup":
            await backend_client.post_followup(job_id)
            await send_client.send_message(f"Follow-up recorded for job {job_id}.")
        elif kind == "dismiss":
            return
        else:
            logger.warning(f"Notifications bot: unknown callback kind={kind!r}")
    except Exception as exc:
        await handle_backend_error(send_client, exc, context=f"callback kind={kind} job={job_id}")
