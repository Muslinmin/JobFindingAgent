"""WP-A8 — `POST /chat` transport: session resolution, per-session locking,
and the turn deadline.

Built on a real FastAPI app with a real ConversationStore over temp files,
because the properties under test are about *ordering and interleaving*,
which a mocked store cannot demonstrate. Only the LLM is faked.
"""

import asyncio
import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from agent.context import ConversationContext
from agent.loop import Agent, TurnResult
from app.conversation.database import connect, initialise_schema
from app.conversation.repository import ConversationRepository
from app.conversation.store import ConversationStore
from app.conversation.transcript_store import TranscriptStore
from app.models.enums import ArtifactKind
from app.routes.chat import router as chat_router
from profile.schema import Profile, Skill


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def profile_path(tmp_path):
    p = Profile(name="Jane", email="jane@example.com", skills=[Skill(id="py", label="Python")])
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(p.model_dump(mode="json")))
    return path


@pytest.fixture
async def store(tmp_path):
    connection = await connect(str(tmp_path / "conversations.db"))
    await initialise_schema(connection)
    yield ConversationStore(
        ConversationRepository(connection),
        TranscriptStore(tmp_path / "transcripts"),
        lambda: datetime.now(timezone.utc).isoformat(),
    )
    await connection.close()


@pytest.fixture
def llm():
    c = MagicMock()
    c.chat = AsyncMock(return_value=_response(content="hello back"))
    return c


@pytest.fixture
def dispatcher():
    d = MagicMock()
    d.dispatch = AsyncMock(return_value={"ok": True})
    return d


@pytest.fixture
async def client(store, llm, dispatcher, profile_path):
    app = FastAPI()
    app.include_router(chat_router)

    context = ConversationContext(store, idle_minutes=30)
    app.state.conversation_context = context
    app.state.agent = Agent(
        llm=llm, dispatcher=dispatcher, context=context, profile_path=profile_path
    )
    app.state.session_locks = {}
    app.state.session_resolution_lock = asyncio.Lock()
    app.state.store = store  # test-only handle

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── end to end ────────────────────────────────────────────────────────────────

async def test_chat_returns_the_agents_reply(client):
    response = await client.post("/chat", json={"message": "hi"})
    assert response.status_code == 200
    assert response.json()["reply"] == "hello back"


async def test_chat_records_both_turns(client, store):
    await client.post("/chat", json={"message": "hi"})

    session = await store.get_latest_session()
    turns = await store.load_history(session.id)

    assert [(t.role.value, t.content) for t in turns] == [
        ("USER", "hi"), ("ASSISTANT", "hello back"),
    ]


async def test_a_second_message_continues_the_same_session(client, store):
    await client.post("/chat", json={"message": "first"})
    await client.post("/chat", json={"message": "second"})

    session = await store.get_latest_session()
    turns = await store.load_history(session.id)
    assert [t.content for t in turns] == ["first", "hello back", "second", "hello back"]


async def test_prior_turns_are_visible_to_the_model_on_the_next_call(client, llm):
    await client.post("/chat", json={"message": "first"})
    await client.post("/chat", json={"message": "second"})

    messages = llm.chat.call_args_list[1].args[0]
    contents = [m["content"] for m in messages if m["role"] in ("user", "assistant")]
    assert contents == ["first", "hello back", "second"]


async def test_empty_message_is_rejected_by_validation(client):
    assert (await client.post("/chat", json={"message": ""})).status_code == 422


async def test_response_has_no_attachments_when_the_agent_produced_no_file(client):
    assert (await client.post("/chat", json={"message": "hi"})).json()["attachments"] == []


async def test_a_file_the_agent_produced_rides_out_base64_encoded(client, tmp_path):
    """The agent holds no Telegram client, so every file it makes has to
    leave in this one response (architecture_v2.md, POST /chat transport)."""
    pdf = tmp_path / "govtech_backend-engineer_cv_pdf.pdf"
    pdf.write_bytes(b"%PDF-1.4 pretend")

    client._transport.app.state.agent.run = AsyncMock(return_value=TurnResult(
        reply="Tailored it.",
        attachments=[{"kind": ArtifactKind.CV_PDF, "filename": pdf.name,
                      "path": str(pdf), "mime_type": "application/pdf"}],
    ))

    body = (await client.post("/chat", json={"message": "tailor my cv"})).json()

    assert body["reply"] == "Tailored it."
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["filename"] == pdf.name
    assert attachment["kind"] == "cv_pdf"
    assert base64.b64decode(attachment["content_b64"]) == b"%PDF-1.4 pretend"


async def test_a_missing_file_degrades_the_turn_instead_of_failing_it(client, tmp_path):
    """The reply is the more valuable half and is already composed; losing
    the attachment must not lose the answer too."""
    client._transport.app.state.agent.run = AsyncMock(return_value=TurnResult(
        reply="Tailored it.",
        attachments=[{"kind": ArtifactKind.CV_PDF, "filename": "gone.pdf",
                      "path": str(tmp_path / "gone.pdf"), "mime_type": "application/pdf"}],
    ))

    response = await client.post("/chat", json={"message": "tailor my cv"})

    assert response.status_code == 200
    assert response.json()["reply"] == "Tailored it."
    assert response.json()["attachments"] == []


# ── per-session locking ───────────────────────────────────────────────────────

async def test_overlapping_turns_on_one_session_serialise(client, llm, store):
    """Without the lock both turns read the same history and the second
    reply is composed from a context missing the first."""
    in_flight = 0
    max_concurrent = 0

    async def slow_chat(messages, tools=None):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _response(content="hello back")

    llm.chat = AsyncMock(side_effect=slow_chat)

    await asyncio.gather(
        client.post("/chat", json={"message": "first"}),
        client.post("/chat", json={"message": "second"}),
    )

    assert max_concurrent == 1


async def test_overlapping_turns_never_interleave_the_transcript(client, llm, store):
    async def slow_chat(messages, tools=None):
        await asyncio.sleep(0.05)
        return _response(content="reply")

    llm.chat = AsyncMock(side_effect=slow_chat)

    await asyncio.gather(
        client.post("/chat", json={"message": "first"}),
        client.post("/chat", json={"message": "second"}),
    )

    session = await store.get_latest_session()
    turns = await store.load_history(session.id)
    roles = [t.role.value for t in turns]
    # Strict USER/ASSISTANT alternation — an interleave would show two
    # USER turns in a row.
    assert roles == ["USER", "ASSISTANT", "USER", "ASSISTANT"]


async def test_different_sessions_do_not_block_each_other(client):
    """A turn held on one session must not stall a turn on another — the
    locks are keyed by session precisely so unrelated conversations run
    concurrently."""
    app = client._transport.app
    order = []

    # Occupy some *other* session's lock for longer than the real turn takes.
    other = asyncio.Lock()
    app.state.session_locks["some-other-session"] = other

    async def hold_other_lock():
        async with other:
            await asyncio.sleep(0.1)
            order.append("other-finished")

    async def do_turn():
        await client.post("/chat", json={"message": "hi"})
        order.append("turn-finished")

    await asyncio.gather(hold_other_lock(), do_turn())

    # The turn did not wait on the unrelated lock.
    assert order == ["turn-finished", "other-finished"]


async def test_two_simultaneous_first_messages_share_one_session(client, llm):
    """Regression: session resolution used to run outside any lock, so two
    concurrent first messages each saw "no session" and each started one —
    splitting a conversation across two transcripts, and defeating the
    per-session lock, since the two turns then held different locks."""

    async def slow_chat(messages, tools=None):
        await asyncio.sleep(0.05)
        return _response(content="reply")

    llm.chat = AsyncMock(side_effect=slow_chat)

    await asyncio.gather(
        client.post("/chat", json={"message": "first"}),
        client.post("/chat", json={"message": "second"}),
    )

    app = client._transport.app
    assert len(app.state.session_locks) == 1


async def test_a_lock_is_created_per_session(client):
    await client.post("/chat", json={"message": "hi"})
    app = client._transport.app
    assert len(app.state.session_locks) == 1


async def test_the_same_session_reuses_one_lock(client):
    await client.post("/chat", json={"message": "one"})
    await client.post("/chat", json={"message": "two"})
    app = client._transport.app
    assert len(app.state.session_locks) == 1


# ── turn deadline ─────────────────────────────────────────────────────────────

async def test_deadline_breach_returns_200_with_a_fallback_reply(client, monkeypatch):
    """Never a 500, never a hung connection — a Telegram user is waiting."""
    from app.routes import chat as chat_module

    monkeypatch.setattr(chat_module.settings, "agent_turn_deadline_s", 0.01)

    async def never_finishes(session_id, message):
        await asyncio.sleep(5)

    app = client._transport.app
    app.state.agent.run = never_finishes

    response = await client.post("/chat", json={"message": "hi"})

    assert response.status_code == 200
    assert "took longer than I could wait" in response.json()["reply"]


async def test_deadline_breach_releases_the_lock(client, monkeypatch):
    """A timed-out turn must not wedge the session forever."""
    from app.routes import chat as chat_module

    monkeypatch.setattr(chat_module.settings, "agent_turn_deadline_s", 0.01)

    async def never_finishes(session_id, message):
        await asyncio.sleep(5)

    app = client._transport.app
    original = app.state.agent.run
    app.state.agent.run = never_finishes

    await client.post("/chat", json={"message": "hi"})

    monkeypatch.setattr(chat_module.settings, "agent_turn_deadline_s", 30)
    app.state.agent.run = original
    response = await asyncio.wait_for(
        client.post("/chat", json={"message": "again"}), timeout=2
    )
    assert response.json()["reply"] == "hello back"
