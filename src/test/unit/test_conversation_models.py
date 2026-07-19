import pytest
from pydantic import ValidationError

from app.conversation.models import Role, Session, Turn

T1 = "2026-07-19T00:00:00+00:00"


# ── Role ────────────────────────────────────────────────────────────────────

def test_role_accepts_user_and_assistant():
    assert Role("USER") == Role.USER
    assert Role("ASSISTANT") == Role.ASSISTANT


def test_turn_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Turn(role="SYSTEM", content="x", created_at=T1)


# ── Turn ────────────────────────────────────────────────────────────────────

def test_turn_round_trips_iso_utc_created_at():
    turn = Turn(role=Role.USER, content="hi", created_at=T1)
    assert turn.created_at == T1
    assert turn.model_dump()["created_at"] == T1


def test_turn_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        Turn(role=Role.USER, created_at=T1)  # missing content


# ── Session ─────────────────────────────────────────────────────────────────

def test_session_round_trips_iso_utc_fields():
    s = Session(id="u1", started_at=T1, last_activity_at=T1, transcript_path="/tmp/u1.jsonl")
    assert s.started_at == T1
    assert s.last_activity_at == T1
    assert s.transcript_path == "/tmp/u1.jsonl"


def test_session_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        Session(id="u1", started_at=T1, transcript_path="/tmp/u1.jsonl")  # missing last_activity_at
