from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger
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
    logger.debug(f"[chat] received {len(payload.messages)} messages")
    try:
        reply = await run(payload.messages, db)
        logger.debug(f"[chat] agent reply length={len(reply or '')}")
        return {"reply": reply or ""}
    except Exception as e:
        logger.error(f"[chat] unhandled {type(e).__name__}: {e}")
        return JSONResponse(status_code=500, content={"reply": "", "error": f"{type(e).__name__}: {e}"})
