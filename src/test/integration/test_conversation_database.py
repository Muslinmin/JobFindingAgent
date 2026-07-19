import pytest

from app.conversation.database import connect, initialise_schema


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "conversations.db")
    conn = await connect(db_path)
    yield conn
    await conn.close()


async def test_initialise_schema_creates_sessions_table(db):
    await initialise_schema(db)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_initialise_schema_is_idempotent(db):
    await initialise_schema(db)
    await initialise_schema(db)  # must not raise on second call
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    assert await cursor.fetchone() is not None


async def test_connect_returns_usable_connection(tmp_path):
    db_path = str(tmp_path / "conversations.db")
    conn = await connect(db_path)
    await initialise_schema(conn)
    cursor = await conn.execute(
        "INSERT INTO sessions (id, started_at, last_activity_at, transcript_path) "
        "VALUES (?, ?, ?, ?)",
        ("u1", "2026-07-19T00:00:00+00:00", "2026-07-19T00:00:00+00:00", "/tmp/u1.jsonl"),
    )
    await conn.commit()
    row = await (await conn.execute("SELECT id FROM sessions WHERE id = 'u1'")).fetchone()
    assert row[0] == "u1"
    await conn.close()
