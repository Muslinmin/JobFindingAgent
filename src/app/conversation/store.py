import uuid
from collections.abc import Callable

from app.conversation.models import Role, Session, Turn
from app.conversation.repository import ConversationRepository
from app.conversation.transcript_store import TranscriptStore


class ConversationStore:
    def __init__(
        self,
        repository: ConversationRepository,
        transcript_store: TranscriptStore,
        clock: Callable[[], str],  # returns ISO-8601 UTC 'now'; injected for testability
    ) -> None:
        self._repository = repository
        self._transcript_store = transcript_store
        self._clock = clock

    async def start_session(self) -> Session:
        """Generate a session id, derive its transcript path, create the row, return it."""
        session_id = str(uuid.uuid4())
        transcript_path = self._transcript_store.transcript_path_for(session_id)
        return await self._repository.create_session(session_id, transcript_path, self._clock())

    async def get_latest_session(self) -> Session | None:
        """Return the most recent session so the agent can decide reuse versus new."""
        return await self._repository.get_latest_session()

    async def append_turn(self, session_id: str, role: Role, content: str) -> Turn:
        """Stamp created_at, append the turn to the transcript file, and re-stamp the
        session's last_activity_at. One event, one write to each store."""
        now = self._clock()
        turn = Turn(role=role, content=content, created_at=now)
        transcript_path = self._transcript_store.transcript_path_for(session_id)
        await self._transcript_store.append_turn(transcript_path, turn)
        await self._repository.update_last_activity(session_id, now)
        return turn

    async def load_history(self, session_id: str) -> list[Turn]:
        """Return every turn of the session, oldest first, for prompt building."""
        transcript_path = self._transcript_store.transcript_path_for(session_id)
        return await self._transcript_store.read_turns(transcript_path)
