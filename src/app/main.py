from loguru import logger
from fastapi import FastAPI

from app.config import settings
from app.routes.jobs import router as jobs_router

logger.add(
    "logs/app.log",
    rotation="10 MB",
    retention="7 days",
    level=settings.log_level,
    format="{time} | {level} | {module} | {message}",
)

app = FastAPI()
app.include_router(jobs_router)
