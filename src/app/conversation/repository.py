import aiosqlite

from app.conversation.models import Session


def _row_to_session(row: aiosqlite.Row) -> Session:
    return Session(
        id=row["id"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        transcript_path=row["transcript_path"],
    )


class ConversationRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_session(self, session_id: str, transcript_path: str, now: str) -> Session:
        """Insert a new session row with started_at = last_activity_at = now."""
        self._connection.row_factory = aiosqlite.Row
        cursor = await self._connection.execute(
            """
            INSERT INTO sessions (id, started_at, last_activity_at, transcript_path)
            VALUES (?, ?, ?, ?)
            RETURNING *
            """,
            (session_id, now, now, transcript_path),
        )
        row = await cursor.fetchone()
        await self._connection.commit()
        return _row_to_session(row)

    async def get_session(self, session_id: str) -> Session | None:
        """Fetch one session row by id, or None if it does not exist."""
        self._connection.row_factory = aiosqlite.Row
        cursor = await self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None

    async def get_latest_session(self) -> Session | None:
        """Fetch the most recently started session, or None if there are none."""
        self._connection.row_factory = aiosqlite.Row
        cursor = await self._connection.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None

    async def update_last_activity(self, session_id: str, now: str) -> None:
        """Stamp last_activity_at = now for one session. Touches nothing else."""
        await self._connection.execute(
            "UPDATE sessions SET last_activity_at = ? WHERE id = ?",
            (now, session_id),
        )
        await self._connection.commit()
