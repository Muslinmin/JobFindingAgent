from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import ApplicationStatus, ArtifactKind


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str
    role: str
    description: str
    url: str
    posted_at: str | None = None
    metadata: dict[str, Any] | None = None


class Job(BaseModel):
    id: int
    fingerprint: str
    company: str
    role: str
    description: str
    url: str
    posted_at: str | None
    metadata: dict[str, Any] | None
    status: ApplicationStatus
    score: int | None
    status_changed_at: str
    follow_up_count: int
    last_follow_up_at: str | None
    follow_up_nudge_at: str | None
    seen_count: int
    last_seen_at: str
    created_at: str
    updated_at: str


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    path: str


class Artifact(BaseModel):
    id: int
    job_id: int
    kind: ArtifactKind
    path: str
    created_at: str
