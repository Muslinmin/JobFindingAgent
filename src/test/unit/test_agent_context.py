from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.context import ConversationContext, is_idle
from app.conversation.models import Role, Session, Turn

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
T0 = "2026-07-22T11:59:00+00:00"  # 1 minute ago


def _session(id="s1", last_activity_at=T0):
    return Session(
        id=id,
        started_at=T0,
        last_activity_at=last_activity_at,
        transcript_path=f"/tmp/{id}.jsonl",
    )


def _turn(role, content, created_at=T0):
    return Turn(role=role, content=content, created_at=created_at)


@pytest.fixture
def store():
    s = MagicMock()
    s.get_latest_session = AsyncMock(return_value=None)
    s.start_session = AsyncMock(return_value=_session())
    s.append_turn = AsyncMock(return_value=None)
    s.load_history = AsyncMock(return_value=[])
    return s


@pytest.fixture
def context(store):
    return ConversationContext(store, idle_minutes=30)


# ── is_idle ───────────────────────────────────────────────────────────────────

def test_is_idle_false_within_threshold():
    assert is_idle("2026-07-22T11:45:00+00:00", 30, now=NOW) is False


def test_is_idle_true_past_threshold():
    assert is_idle("2026-07-22T11:00:00+00:00", 30, now=NOW) is True


def test_is_idle_false_exactly_at_threshold():
    # Strictly greater-than: the boundary minute still continues the session.
    assert is_idle("2026-07-22T11:30:00+00:00", 30, now=NOW) is False


def test_is_idle_treats_naive_timestamp_as_utc():
    assert is_idle("2026-07-22T11:45:00", 30, now=NOW) is False
    assert is_idle("2026-07-22T11:00:00", 30, now=NOW) is True


# ── resolve_session (continue-vs-new) ─────────────────────────────────────────

async def test_resolve_session_starts_new_when_none_exists(context, store):
    store.get_latest_session.return_value = None
    session = await context.resolve_session()
    store.start_session.assert_called_once()
    assert session.id == "s1"


async def test_resolve_session_continues_active_session(context, store):
    latest = _session(id="active", last_activity_at=datetime.now(timezone.utc).isoformat())
    store.get_latest_session.return_value = latest
    session = await context.resolve_session()
    assert session is latest
    store.start_session.assert_not_called()


async def test_resolve_session_starts_new_when_latest_is_idle(context, store):
    store.get_latest_session.return_value = _session(
        id="stale", last_activity_at="2020-01-01T00:00:00+00:00"
    )
    session = await context.resolve_session()
    store.start_session.assert_called_once()
    assert session.id == "s1"


# ── record ────────────────────────────────────────────────────────────────────

async def test_record_appends_turn_through_the_store(context, store):
    await context.record("s1", Role.USER, "hi")
    store.append_turn.assert_called_once_with("s1", Role.USER, "hi")


async def test_record_never_stamps_a_timestamp_itself(context, store):
    # The store owns the clock; the agent hands it only role and content.
    await context.record("s1", Role.ASSISTANT, "hello")
    args = store.append_turn.call_args.args
    assert args == ("s1", Role.ASSISTANT, "hello")


# ── build_context ─────────────────────────────────────────────────────────────

async def test_build_context_maps_turns_to_llm_messages_in_order(context, store):
    store.load_history.return_value = [
        _turn(Role.USER, "first"),
        _turn(Role.ASSISTANT, "second"),
        _turn(Role.USER, "third"),
    ]
    messages = await context.build_context("s1")
    assert messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]


async def test_build_context_loads_the_whole_session(context, store):
    store.load_history.return_value = [_turn(Role.USER, str(i)) for i in range(50)]
    messages = await context.build_context("s1")
    assert len(messages) == 50  # v1: no windowing, the compaction seam is a no-op


async def test_build_context_on_empty_session(context, store):
    store.load_history.return_value = []
    assert await context.build_context("s1") == []


async def test_round_trip_record_then_build_context(store):
    """The one integration-shaped assertion: what gets recorded is what comes
    back out as context, in order."""
    written: list[Turn] = []

    async def append(session_id, role, content):
        written.append(_turn(role, content))

    store.append_turn = AsyncMock(side_effect=append)
    store.load_history = AsyncMock(side_effect=lambda sid: written)
    context = ConversationContext(store, idle_minutes=30)

    await context.record("s1", Role.USER, "tailor my cv")
    await context.record("s1", Role.ASSISTANT, "which job?")

    assert await context.build_context("s1") == [
        {"role": "user", "content": "tailor my cv"},
        {"role": "assistant", "content": "which job?"},
    ]


# ── compaction seam ───────────────────────────────────────────────────────────

def test_compaction_seam_exists_and_is_a_noop():
    from agent.context import _compact

    turns = [_turn(Role.USER, "a"), _turn(Role.ASSISTANT, "b")]
    assert _compact(turns) == turns
