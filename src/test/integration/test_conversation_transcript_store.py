import pytest

from app.conversation.models import Role, Turn
from app.conversation.transcript_store import TranscriptStore

T1 = "2026-07-19T00:00:00+00:00"
T2 = "2026-07-19T01:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    return TranscriptStore(str(tmp_path))


# ── transcript_path_for ───────────────────────────────────────────────────────

def test_transcript_path_for_is_derived_from_session_id(store, tmp_path):
    path = store.transcript_path_for("u1")
    assert path == str(tmp_path / "u1.jsonl")


# ── append_turn ───────────────────────────────────────────────────────────────

async def test_append_writes_single_line(store):
    path = store.transcript_path_for("u1")
    await store.append_turn(path, Turn(role=Role.USER, content="hi", created_at=T1))
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 1


async def test_append_creates_base_directory_if_missing(tmp_path):
    nested = TranscriptStore(str(tmp_path / "nested" / "dir"))
    path = nested.transcript_path_for("u1")
    await nested.append_turn(path, Turn(role=Role.USER, content="hi", created_at=T1))
    with open(path) as f:
        assert len(f.readlines()) == 1


# ── read_turns ────────────────────────────────────────────────────────────────

async def test_read_turns_returns_oldest_first(store):
    path = store.transcript_path_for("u1")
    await store.append_turn(path, Turn(role=Role.USER, content="a", created_at=T1))
    await store.append_turn(path, Turn(role=Role.ASSISTANT, content="b", created_at=T2))
    turns = await store.read_turns(path)
    assert [t.content for t in turns] == ["a", "b"]
    assert turns[0].role == Role.USER
    assert turns[1].role == Role.ASSISTANT


async def test_read_turns_returns_empty_list_for_missing_file(store):
    path = store.transcript_path_for("nonexistent")
    turns = await store.read_turns(path)
    assert turns == []
