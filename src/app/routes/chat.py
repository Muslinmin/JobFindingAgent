from fastapi import APIRouter, Depends
from pydantic import BaseModel

from agent.agent import run
from app.db.database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    messages: list
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, db=Depends(get_db)):
    reply = await run(payload.messages, db)
    return {"reply": reply}
