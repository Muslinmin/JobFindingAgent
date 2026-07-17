from unittest.mock import AsyncMock

from telegram import InlineKeyboardButton

from telegram_bot.notifications.client import NotificationTelegramClient

CHAT_ID = 67890


def _bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


async def test_send_message_targets_configured_chat_id():
    bot = _bot()
    client = NotificationTelegramClient(bot, CHAT_ID)

    await client.send_message("digest text")

    bot.send_message.assert_called_once_with(chat_id=CHAT_ID, text="digest text")


async def test_send_document_targets_configured_chat_id_and_sends_bytes():
    bot = _bot()
    client = NotificationTelegramClient(bot, CHAT_ID)

    await client.send_document("tailored CV", b"%PDF-1.4 fake", filename="resume.pdf")

    kwargs = bot.send_document.call_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["caption"] == "tailored CV"
    assert kwargs["document"].read() == b"%PDF-1.4 fake"


async def test_send_message_with_keyboard_attaches_inline_markup():
    bot = _bot()
    client = NotificationTelegramClient(bot, CHAT_ID)
    keyboard = [[InlineKeyboardButton("Mark Applied", callback_data="x")]]

    await client.send_message_with_keyboard("approve this?", keyboard)

    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["text"] == "approve this?"
    assert kwargs["reply_markup"].inline_keyboard == tuple(tuple(row) for row in keyboard)
