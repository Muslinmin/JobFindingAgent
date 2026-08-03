from io import BytesIO

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


class NotificationTelegramClient:
    """Outbound push surface for the notifications bot, handed to the
    scheduler. chat_id is baked in at construction. Stateless — push-only,
    never pulls.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send_message(self, text: str) -> None:
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def send_document(self, text: str, pdf_bytes: bytes, filename: str = "resume.pdf") -> None:
        await self._bot.send_document(
            chat_id=self._chat_id,
            document=BytesIO(pdf_bytes),
            filename=filename,
            caption=text,
        )

    async def send_message_with_keyboard(
        self, text: str, keyboard: list[list[InlineKeyboardButton]]
    ) -> None:
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
