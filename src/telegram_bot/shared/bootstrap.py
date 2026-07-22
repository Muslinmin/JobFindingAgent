import httpx
from loguru import logger
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from telegram_bot.chat.agent_client import AgentBackendClient
from telegram_bot.chat.client import ChatTelegramClient
from telegram_bot.chat.handlers import handle_message, handle_start
from telegram_bot.notifications.client import NotificationTelegramClient
from telegram_bot.notifications.handlers import handle_callback
from telegram_bot.notifications.notify_client import NotifyBackendClient


def build_applications(
    chat_bot_token: str,
    notifications_bot_token: str,
    chat_id: int,
    backend_base_url: str,
    chat_read_timeout_s: float = 240.0,
    notifications_read_timeout_s: float = 10.0,
) -> tuple[Application, Application, NotificationTelegramClient]:
    """Build both Application instances (chat + notifications).

    Each gets its own send client, its own backend httpx client, and its
    own handlers registered:
      chat:          CommandHandler("start", handle_start),
                     MessageHandler(TEXT & ~COMMAND, handle_message)
      notifications: CallbackQueryHandler(handle_callback)
    Auth is enforced inside each handler (shared.auth.is_authorised), keyed
    off `chat_id` stashed on bot_data — there is no separate guard layer to
    attach here. Send + backend clients are stashed on the respective
    app.bot_data. Returns (chat_app, notifications_app, notification_client)
    — the last for the scheduler's future push path.

    Both httpx clients get an explicit `timeout=` rather than relying on
    httpx's 5s default (concurrencyFor_agentV2.md F-4/WP-C4): a ReAct turn
    routinely exceeds 5s without anything going wrong, and a client that
    gives up early on work the server is still doing surfaces a spurious
    error and orphans the reply. `chat_read_timeout_s` must stay strictly
    greater than `settings.agent_turn_deadline_s` (the caller's job — see
    the timeout ladder in concurrencyFor_agentV2.md §3). The notifications
    client only ever calls deterministic, non-LLM routes (`/action`,
    `/follow-up`), so it keeps a much shorter default.
    """
    chat_app = ApplicationBuilder().token(chat_bot_token).build()
    chat_http = httpx.AsyncClient(base_url=backend_base_url, timeout=chat_read_timeout_s)
    chat_send_client = ChatTelegramClient(chat_app.bot, chat_id)
    chat_app.bot_data.update(
        {
            "chat_id": chat_id,
            "send_client": chat_send_client,
            "backend_client": AgentBackendClient(backend_base_url, chat_http),
            "http_client": chat_http,
        }
    )
    chat_app.add_handler(CommandHandler("start", handle_start))
    chat_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    notifications_app = ApplicationBuilder().token(notifications_bot_token).build()
    notifications_http = httpx.AsyncClient(base_url=backend_base_url, timeout=notifications_read_timeout_s)
    notification_client = NotificationTelegramClient(notifications_app.bot, chat_id)
    notifications_app.bot_data.update(
        {
            "chat_id": chat_id,
            "send_client": notification_client,
            "backend_client": NotifyBackendClient(backend_base_url, notifications_http),
            "http_client": notifications_http,
        }
    )
    notifications_app.add_handler(CallbackQueryHandler(handle_callback))

    return chat_app, notifications_app, notification_client


async def start_bots(chat_app: Application, notifications_app: Application) -> None:
    """Verify each with get_me() (fails fast on a bad token), then start
    both polling loops. Called from the FastAPI lifespan hook alongside
    APScheduler.
    """
    for app in (chat_app, notifications_app):
        await app.initialize()
        me = await app.bot.get_me()
        logger.info(f"Telegram bot verified: @{me.username}")
        await app.start()
        await app.updater.start_polling()


async def stop_bots(chat_app: Application, notifications_app: Application) -> None:
    """Graceful shutdown of both, called on lifespan teardown. Also closes
    each app's backend httpx.AsyncClient.
    """
    for app in (chat_app, notifications_app):
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        http_client: httpx.AsyncClient = app.bot_data["http_client"]
        await http_client.aclose()
