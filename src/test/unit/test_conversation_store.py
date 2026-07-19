from unittest.mock import AsyncMock, MagicMock

import pytest

from app.conversation.models import Role, Session, Turn
from app.conversation.store import ConversationStore

T1 = "2026-07-19T00:00:00+00:00"


def _session(id="u1", started_at=T1, last_activity_at=T1, transcript_path="/tmp/u1.jsonl"):
    return Session(id=id, started_at=started_at, last_activity_at=last_activity_at, transcript_path=transcript_path)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.create_session = AsyncMock(return_value=_session())
    repo.get_latest_session = AsyncMock(return_value=None)
    repo.update_last_activity = AsyncMock(return_value=None)
    repo.get_session = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_transcript_store():
    ts = MagicMock()
    ts.transcript_path_for = MagicMock(side_effect=lambda sid: f"/tmp/{sid}.jsonl")
    ts.append_turn = AsyncMock(return_value=None)
    ts.read_turns = AsyncMock(return_value=[])
    return ts


@pytest.fixture
def clock():
    return MagicMock(return_value=T1)


@pytest.fixture
def store(mock_repository, mock_transcript_store, clock):
    return ConversationStore(mock_repository, mock_transcript_store, clock)


# ── start_session ─────────────────────────────────────────────────────────────

async def test_start_session_creates_row_with_derived_path(store, mock_repository, mock_transcript_store):
    await store.start_session()
    mock_repository.create_session.assert_called_once()
    session_id_arg, transcript_path_arg, now_arg = mock_repository.create_session.call_args.args
    assert transcript_path_arg == mock_transcript_store.transcript_path_for(session_id_arg)
    assert now_arg == T1


async def test_start_session_returns_the_created_session(store, mock_repository):
    expected = _session(id="u2")
    mock_repository.create_session.return_value = expected
    result = await store.start_session()
    assert result == expected


# ── get_latest_session ────────────────────────────────────────────────────────

async def test_get_latest_session_delegates_to_repository(store, mock_repository):
    expected = _session()
    mock_repository.get_latest_session.return_value = expected
    result = await store.get_latest_session()
    assert result == expected
    mock_repository.get_latest_session.assert_called_once()


async def test_get_latest_session_returns_none_when_repository_has_none(store, mock_repository):
    mock_repository.get_latest_session.return_value = None
    result = await store.get_latest_session()
    assert result is None


# ── append_turn ───────────────────────────────────────────────────────────────

async def test_append_turn_writes_transcript_and_stamps_activity(
    store, mock_repository, mock_transcript_store
):
    await store.append_turn("u1", Role.USER, "hi")
    mock_transcript_store.append_turn.assert_called_once()
    mock_repository.update_last_activity.assert_called_once()


async def test_append_turn_uses_clock_for_created_at_and_activity_stamp(
    store, mock_repository, mock_transcript_store
):
    await store.append_turn("u1", Role.USER, "hi")

    _, turn_arg = mock_transcript_store.append_turn.call_args.args
    assert turn_arg.created_at == T1
    assert turn_arg.role == Role.USER
    assert turn_arg.content == "hi"

    session_id_arg, now_arg = mock_repository.update_last_activity.call_args.args
    assert session_id_arg == "u1"
    assert now_arg == T1


async def test_append_turn_derives_path_without_repository_lookup(
    store, mock_repository, mock_transcript_store
):
    await store.append_turn("u1", Role.ASSISTANT, "hello")
    path_arg, _ = mock_transcript_store.append_turn.call_args.args
    assert path_arg == mock_transcript_store.transcript_path_for("u1")
    mock_repository.get_session.assert_not_called()


async def test_append_turn_returns_the_turn(store):
    turn = await store.append_turn("u1", Role.USER, "hi")
    assert isinstance(turn, Turn)
    assert turn.content == "hi"
    assert turn.role == Role.USER


# ── load_history ──────────────────────────────────────────────────────────────

async def test_load_history_reads_from_derived_transcript_path(store, mock_transcript_store):
    turns = [Turn(role=Role.USER, content="a", created_at=T1)]
    mock_transcript_store.read_turns.return_value = turns

    result = await store.load_history("u1")

    assert result == turns
    path_arg = mock_transcript_store.read_turns.call_args.args[0]
    assert path_arg == mock_transcript_store.transcript_path_for("u1")
