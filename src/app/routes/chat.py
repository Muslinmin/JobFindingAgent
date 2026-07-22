"""`POST /chat` — the thin transport around the agent loop (agent_v2.md §3).

The route owns three things the loop deliberately does not:

* **Session resolution.** Which session this turn belongs to is decided per
  call, on the way in, because `/chat` is stateless and there is no
  end-of-session event to hang the decision off.
* **The per-session lock.** Two overlapping turns on one session race on
  the transcript: both read the same history, both append, and the second
  reply is composed from a context missing the first. The lock is held
  across record-user → run → record-assistant, so a turn is atomic from
  the transcript's point of view. Locks are keyed by session, so unrelated
  conversations never wait on each other.

  Resolution itself needs a lock of its own, and it cannot be the
  per-session one — there is no session id yet to key it by. Two first
  messages arriving together would otherwise *both* see "no session" and
  each start one, splitting a single conversation across two transcripts
  and defeating the per-session lock entirely, since each turn would then
  hold a different lock. The resolution lock is global but held only for a
  read and possibly one insert; the slow part of the turn runs under the
  per-session lock, so this does not serialise conversations.
* **The turn deadline.** A breach returns a `200` with a best-effort reply,
  never a `500` and never a hung connection — the bot on the other end is a
  Telegram user staring at a chat window (concurrencyFor_agentV2.md §3).
* **Reading file bytes.** A handler that produced an artifact hands the
  turn a *path*; this is where it becomes base64 in the response body. The
  agent holds no Telegram client, so every file it makes has to ride out in
  this single response (architecture_v2.md, "POST /chat transport") — and
  keeping the read here is what stops megabytes of PDF from travelling
  through the reasoning loop.
"""

import asyncio
import base64
from pathlib import Path

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from app.config import settings
from app.models.enums import ArtifactKind

router = APIRouter(prefix="/chat", tags=["chat"])

_DEADLINE_REPLY = (
    "That took longer than I could wait on. It may still have gone through — "
    "ask me to check before trying it again."
)


class Attachment(BaseModel):
    kind: ArtifactKind
    filename: str
    mime_type: str
    content_b64: str  # base64-encoded file bytes


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    attachments: list[Attachment] = []


def _lock_for(request: Request, session_id: str) -> asyncio.Lock:
    """One lock per session, created on first use and kept on `app.state`.

    Safe without a lock of its own: this runs on the single event loop and
    contains no await, so no other request can interleave between the
    lookup and the insert.
    """
    locks: dict[str, asyncio.Lock] = request.app.state.session_locks
    if session_id not in locks:
        locks[session_id] = asyncio.Lock()
    return locks[session_id]


def _encode(record: dict) -> Attachment | None:
    """Read the produced file and base64 it into the response.

    A missing or unreadable file is dropped with a warning rather than
    failing the request: the reply text is already composed and is the more
    valuable half, and the artifact row still records where the file was
    meant to be. Losing the attachment degrades the turn; raising here
    would lose the answer too.
    """
    path = Path(record["path"])
    try:
        content = path.read_bytes()
    except OSError:
        logger.warning(f"chat: could not read attachment at {path}; replying without it")
        return None
    return Attachment(
        kind=record["kind"],
        filename=record["filename"],
        mime_type=record["mime_type"],
        content_b64=base64.b64encode(content).decode(),
    )


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    agent = request.app.state.agent
    context = request.app.state.conversation_context

    async with request.app.state.session_resolution_lock:
        session = await context.resolve_session()
        turn_lock = _lock_for(request, session.id)

    async with turn_lock:
        try:
            result = await asyncio.wait_for(
                agent.run(session.id, payload.message),
                timeout=settings.agent_turn_deadline_s,
            )
        except asyncio.TimeoutError:
            # The turn is abandoned, but the user turn is already recorded
            # and the loop's own work may have partially landed — so this
            # says "may still have gone through" rather than "failed".
            logger.warning(
                f"chat: turn exceeded {settings.agent_turn_deadline_s}s deadline "
                f"on session {session.id}"
            )
            return ChatResponse(reply=_DEADLINE_REPLY)

    encoded = [_encode(record) for record in result.attachments]
    return ChatResponse(reply=result.reply, attachments=[a for a in encoded if a is not None])
