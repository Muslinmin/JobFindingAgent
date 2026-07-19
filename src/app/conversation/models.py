from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class Turn(BaseModel):
    """One line in a session's JSON Lines transcript file."""

    role: Role
    content: str
    created_at: str  # ISO-8601 UTC


class Session(BaseModel):
    """One row in the conversations SQLite database."""

    id: str  # UUID string; opaque handle for one conversation thread
    started_at: str  # ISO-8601 UTC
    last_activity_at: str  # ISO-8601 UTC; re-stamped on every appended turn
    transcript_path: str  # filesystem path to this session's JSON Lines file
