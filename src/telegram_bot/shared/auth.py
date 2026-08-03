from loguru import logger
from telegram import Update


def is_authorised(update: Update, allowed_chat_id: int) -> bool:
    """True if the update's chat_id matches the single allowed user.

    Covers message, callback_query, and command updates alike — all of them
    expose `effective_chat`. An unauthorised update is logged as a warning
    by the caller, which then drops it without replying.
    """
    chat = update.effective_chat
    if chat is None or chat.id != allowed_chat_id:
        logger.warning(
            f"Rejected update from unauthorised chat_id={chat.id if chat else None}"
        )
        return False
    return True
