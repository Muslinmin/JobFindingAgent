from unittest.mock import MagicMock

from telegram_bot.shared.auth import is_authorised

ALLOWED = 12345


def _update(chat_id: int | None):
    update = MagicMock()
    if chat_id is None:
        update.effective_chat = None
    else:
        update.effective_chat.id = chat_id
    return update


def test_matching_chat_id_passes():
    assert is_authorised(_update(ALLOWED), ALLOWED) is True


def test_mismatched_chat_id_is_rejected():
    assert is_authorised(_update(99999), ALLOWED) is False


def test_missing_effective_chat_is_rejected():
    assert is_authorised(_update(None), ALLOWED) is False
