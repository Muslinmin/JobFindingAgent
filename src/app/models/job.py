from datetime import datetime
from pydantic import BaseModel, HttpUrl
from app.models.enums import ApplicationStatus


class JobCreate(BaseModel):
    company: str
    role: str
    url: HttpUrl
    source: str = "manual"
    notes: str | None = None


class JobUpdate(BaseModel):
    status: ApplicationStatus
    notes: str | None = None


class JobResponse(BaseModel):
    id: int
    company: str
    role: str
    url: str
    status: ApplicationStatus
    source: str
    notes: str | None
    date_logged: datetime
    created: bool
