from typing import AsyncGenerator

import aiosqlite

from app.config import settings

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'found',
    source      TEXT    NOT NULL DEFAULT 'manual',
    notes       TEXT,
    description TEXT,
    score       REAL    NOT NULL DEFAULT 0.0,
    fingerprint TEXT    NOT NULL UNIQUE,
    date_logged TEXT    NOT NULL
)
"""


async def create_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(CREATE_JOBS_TABLE)
    await conn.commit()


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(settings.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn
