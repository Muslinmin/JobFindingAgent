from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from telegram_bot.chat.agent_client import AgentBackendClient
from telegram_bot.chat.client import ChatTelegramClient
from telegram_bot.notifications.client import NotificationTelegramClient
from telegram_bot.notifications.notify_client import NotifyBackendClient
from telegram_bot.shared.bootstrap import build_applications, start_bots, stop_bots

CHAT_ID = 12345
BACKEND_URL = "http://localhost:8000"


def _fake_app():
    app = MagicMock()
    app.bot_data = {}
    app.bot = MagicMock()
    app.add_handler = MagicMock()
    return app


def _patched_builder(apps: list):
    """Patch ApplicationBuilder so successive .token(...).build() calls
    return the given apps in order."""
    mock_cls = MagicMock()
    mock_cls.return_value.token.return_value.build.side_effect = apps
    return mock_cls


def test_build_applications_returns_two_distinct_apps_and_notification_client():
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        result_chat, result_notifications, notification_client = build_applications(
            chat_bot_token="chat-token",
            notifications_bot_token="notif-token",
            chat_id=CHAT_ID,
            backend_base_url=BACKEND_URL,
        )

    assert result_chat is chat_app
    assert result_notifications is notifications_app
    assert isinstance(notification_client, NotificationTelegramClient)


def test_chat_app_gets_send_and_backend_clients_and_both_handlers():
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        build_applications("chat-token", "notif-token", CHAT_ID, BACKEND_URL)

    assert chat_app.bot_data["chat_id"] == CHAT_ID
    assert isinstance(chat_app.bot_data["send_client"], ChatTelegramClient)
    assert isinstance(chat_app.bot_data["backend_client"], AgentBackendClient)
    assert chat_app.add_handler.call_count == 2


def test_notifications_app_gets_send_and_backend_clients_and_callback_handler():
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        build_applications("chat-token", "notif-token", CHAT_ID, BACKEND_URL)

    assert notifications_app.bot_data["chat_id"] == CHAT_ID
    assert isinstance(notifications_app.bot_data["send_client"], NotificationTelegramClient)
    assert isinstance(notifications_app.bot_data["backend_client"], NotifyBackendClient)
    assert notifications_app.add_handler.call_count == 1


def test_chat_http_client_gets_explicit_timeout_not_httpx_default():
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        build_applications("chat-token", "notif-token", CHAT_ID, BACKEND_URL, chat_read_timeout_s=99.0)

    assert chat_app.bot_data["http_client"].timeout.read == 99.0


def test_notifications_http_client_gets_explicit_timeout_not_httpx_default():
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        build_applications("chat-token", "notif-token", CHAT_ID, BACKEND_URL, notifications_read_timeout_s=7.0)

    assert notifications_app.bot_data["http_client"].timeout.read == 7.0


def test_main_wires_chat_client_timeout_longer_than_turn_deadline():
    """concurrencyFor_agentV2.md §3: the client must never give up on work
    the server is still doing — the chat bot's read timeout must exceed
    settings.agent_turn_deadline_s."""
    chat_app, notifications_app = _fake_app(), _fake_app()

    with patch("telegram_bot.shared.bootstrap.ApplicationBuilder", _patched_builder([chat_app, notifications_app])):
        build_applications(
            "chat-token",
            "notif-token",
            CHAT_ID,
            BACKEND_URL,
            chat_read_timeout_s=settings.backend_read_timeout_s,
        )

    assert chat_app.bot_data["http_client"].timeout.read > settings.agent_turn_deadline_s


def _fake_running_app():
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.bot.get_me = AsyncMock(return_value=MagicMock(username="fakebot"))
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.bot_data = {"http_client": AsyncMock()}
    return app


async def test_start_bots_verifies_and_starts_both():
    chat_app, notifications_app = _fake_running_app(), _fake_running_app()

    await start_bots(chat_app, notifications_app)

    for app in (chat_app, notifications_app):
        app.initialize.assert_awaited_once()
        app.bot.get_me.assert_awaited_once()
        app.start.assert_awaited_once()
        app.updater.start_polling.assert_awaited_once()


async def test_stop_bots_shuts_down_both_and_closes_http_clients():
    chat_app, notifications_app = _fake_running_app(), _fake_running_app()

    await stop_bots(chat_app, notifications_app)

    for app in (chat_app, notifications_app):
        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()
        app.bot_data["http_client"].aclose.assert_awaited_once()
