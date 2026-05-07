import httpx
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings

FALLBACK = "Sorry, I'm unable to reach the assistant right now. Please try again later."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != settings.telegram_chat_id:
        return

    text = update.message.text
    history: list = context.user_data.get("history", [])
    messages = history + [{"role": "user", "content": text}]

    try:
        async with httpx.AsyncClient(base_url=settings.api_base_url) as client:
            resp = await client.post("/chat", json={"messages": messages})
            data = resp.json()
            reply = data.get("reply") or ""
    except Exception as exc:
        logger.error(f"Bot: POST /chat failed: {exc}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=FALLBACK)
        return

    if not reply:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=FALLBACK)
        return

    context.user_data["history"] = messages + [{"role": "assistant", "content": reply}]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
