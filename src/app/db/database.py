import os
from typing import AsyncGenerator

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "jobs.db")

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'found',
    source      TEXT    NOT NULL DEFAULT 'manual',
    notes       TEXT,
    fingerprint TEXT    NOT NULL UNIQUE,
    date_logged TEXT    NOT NULL
)
"""


async def create_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(CREATE_JOBS_TABLE)
    await conn.commit()


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn
