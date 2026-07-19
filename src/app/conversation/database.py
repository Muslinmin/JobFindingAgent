import aiosqlite

CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    last_activity_at    TEXT NOT NULL,
    transcript_path     TEXT NOT NULL
)
"""


async def connect(database_path: str) -> aiosqlite.Connection:
    """Open a connection to the conversations database and apply pragmas."""
    conn = await aiosqlite.connect(database_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def initialise_schema(connection: aiosqlite.Connection) -> None:
    """Create the sessions table if it does not already exist."""
    await connection.execute(CREATE_SESSIONS_TABLE)
    await connection.commit()
