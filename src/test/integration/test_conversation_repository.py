import pytest

from app.conversation.database import connect, initialise_schema
from app.conversation.repository import ConversationRepository

T1 = "2026-07-19T00:00:00+00:00"
T2 = "2026-07-19T01:00:00+00:00"


@pytest.fixture
async def repo(tmp_path):
    conn = await connect(str(tmp_path / "conversations.db"))
    await initialise_schema(conn)
    yield ConversationRepository(conn)
    await conn.close()


# ── create_session ────────────────────────────────────────────────────────────

async def test_create_session_sets_both_clocks_to_now(repo):
    s = await repo.create_session("u1", "/tmp/u1.jsonl", now=T1)
    assert s.id == "u1"
    assert s.started_at == T1
    assert s.last_activity_at == T1
    assert s.transcript_path == "/tmp/u1.jsonl"


# ── get_session ───────────────────────────────────────────────────────────────

async def test_get_session_returns_the_row(repo):
    created = await repo.create_session("u1", "/tmp/u1.jsonl", now=T1)
    fetched = await repo.get_session(created.id)
    assert fetched == created


async def test_get_session_returns_none_for_missing_id(repo):
    fetched = await repo.get_session("nonexistent")
    assert fetched is None


# ── update_last_activity ─────────────────────────────────────────────────────

async def test_update_last_activity_moves_only_that_column(repo):
    session_at_t1 = await repo.create_session("u1", "/tmp/u1.jsonl", now=T1)
    await repo.update_last_activity(session_at_t1.id, now=T2)
    s = await repo.get_session(session_at_t1.id)
    assert s.last_activity_at == T2
    assert s.started_at == T1


# ── get_latest_session ────────────────────────────────────────────────────────

async def test_get_latest_session_returns_newest(repo):
    await repo.create_session("u1", "/tmp/u1.jsonl", now=T1)
    await repo.create_session("u2", "/tmp/u2.jsonl", now=T2)
    latest = await repo.get_latest_session()
    assert latest.id == "u2"


async def test_get_latest_session_returns_none_when_empty(repo):
    latest = await repo.get_latest_session()
    assert latest is None
