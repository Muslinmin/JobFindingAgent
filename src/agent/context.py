"""Conversation assembly — the agent's half of the session contract.

Assembly only (agent_v2.md §2, §6). The *storage* of sessions and turns
belongs to `app.conversation` (repository + transcript store, already
built); what lives here is the policy on top of it: which session a turn
belongs to, and which turns become prompt messages.

Two things this module deliberately owns that the store refuses to:

* **`is_idle` and the idle threshold.** The store reports the latest
  session and creates new ones; it never decides which. Continue-vs-new is
  evaluated lazily, once per `/chat` call, because there is no end-of-
  session event to hang it off (agent_v2.md §6).
* **The compaction seam.** `build_context` loads the whole current session
  in v1. When a transcript outgrows the window, trimming or
  summarise-on-eviction goes in `_compact` and nothing above it changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.conversation.models import Role, Session, Turn
from app.conversation.store import ConversationStore

# What the LLM consumes: the OpenAI/LiteLLM chat-message shape. Kept as a
# plain dict rather than a model because it is handed straight to
# `acompletion` and never validated on the way.
Message = dict[str, str]

_ROLE_TO_LLM: dict[Role, str] = {
    Role.USER: "user",
    Role.ASSISTANT: "assistant",
}


def is_idle(last_activity_at: str, idle_minutes: int, now: datetime | None = None) -> bool:
    """True when the gap since `last_activity_at` exceeds the threshold, i.e.
    the next message should open a fresh session rather than continue this one.

    `now` is injectable so a test can pin the comparison; production callers
    omit it. Naive timestamps are read as UTC — everything this app writes is
    ISO-8601 UTC (conversation store invariant 6), so a missing offset is a
    formatting artefact, not a local time.
    """
    current = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(last_activity_at)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return current - last > timedelta(minutes=idle_minutes)


class ConversationContext:
    """Assembles prompt context for one agent, over one conversation store.

    Holds no per-call state: every method takes the `session_id` it operates
    on, because `/chat` is stateless per call and the agent instance is a
    singleton shared by every turn.
    """

    def __init__(self, store: ConversationStore, idle_minutes: int) -> None:
        self._store = store
        self._idle_minutes = idle_minutes

    async def resolve_session(self) -> Session:
        """Continue the latest session, or start a fresh one when there is
        none or it has gone idle. The agent's decision, made lazily on the
        way in (agent_v2.md §6) — a stale session is never closed, only
        never reused.
        """
        latest = await self._store.get_latest_session()
        if latest is None or is_idle(latest.last_activity_at, self._idle_minutes):
            return await self._store.start_session()
        return latest

    async def record(self, session_id: str, role: Role, content: str) -> None:
        """Append one turn. Returns nothing — the agent never needs the
        stamped turn back, and not returning it keeps the store's clock the
        single source of turn timestamps.
        """
        await self._store.append_turn(session_id, role, content)

    async def build_context(self, session_id: str) -> list[Message]:
        """The session's turns as LLM messages, oldest first."""
        turns = await self._store.load_history(session_id)
        return [
            {"role": _ROLE_TO_LLM[turn.role], "content": turn.content}
            for turn in _compact(turns)
        ]


def _compact(turns: list[Turn]) -> list[Turn]:
    """Compaction seam — identity in v1 (agent_v2.md §9: deferred).

    Kept as a named function rather than an inline comment so the future
    change has one obvious home and `build_context` above it never moves.
    """
    return turns
