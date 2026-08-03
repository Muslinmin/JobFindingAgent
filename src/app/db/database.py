from typing import AsyncGenerator

import aiosqlite

from app.config import settings

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint         TEXT    NOT NULL UNIQUE,
    company             TEXT    NOT NULL,
    role                TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    url                 TEXT    NOT NULL,
    posted_at           TEXT,
    metadata            TEXT,
    status              TEXT    NOT NULL DEFAULT 'discovered',
    score               INTEGER,
    status_changed_at   TEXT    NOT NULL,
    follow_up_count     INTEGER NOT NULL DEFAULT 0,
    last_follow_up_at   TEXT,
    follow_up_nudge_at  TEXT,
    seen_count          INTEGER NOT NULL DEFAULT 1,
    last_seen_at        TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
)
"""

CREATE_ARTIFACTS_TABLE = """
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    kind        TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
)
"""


async def create_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute(CREATE_JOBS_TABLE)
    await conn.execute(CREATE_ARTIFACTS_TABLE)
    await conn.commit()


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(settings.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
