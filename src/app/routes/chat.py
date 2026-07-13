from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.dependencies import get_agent
from app.models.enums import ArtifactKind

router = APIRouter(prefix="/chat", tags=["chat"])


class Attachment(BaseModel):
    kind: ArtifactKind
    filename: str
    mime_type: str
    content: str  # base64-encoded file bytes


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    attachments: list[Attachment] = []


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, agent=Depends(get_agent)) -> ChatResponse:
    # Mental model: `agent` is a single instance, constructed once at the
    # composition root and injected here via DI (see agent_v2.md — not yet
    # built). This handler hands it the incoming message; whatever reply the
    # agent produces is relayed verbatim to whoever called /chat.
    raise NotImplementedError("agent layer not wired yet — see agent_v2.md")
