from unittest.mock import AsyncMock

from telegram_bot.chat.client import ChatTelegramClient

CHAT_ID = 12345


def _bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


async def test_send_message_targets_configured_chat_id():
    bot = _bot()
    client = ChatTelegramClient(bot, CHAT_ID)

    await client.send_message("hello")

    bot.send_message.assert_called_once_with(chat_id=CHAT_ID, text="hello")


async def test_send_document_targets_configured_chat_id_and_sends_bytes():
    bot = _bot()
    client = ChatTelegramClient(bot, CHAT_ID)

    await client.send_document("here's your CV", b"%PDF-1.4 fake", filename="resume.pdf")

    bot.send_document.assert_called_once()
    kwargs = bot.send_document.call_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["filename"] == "resume.pdf"
    assert kwargs["caption"] == "here's your CV"
    assert kwargs["document"].read() == b"%PDF-1.4 fake"
