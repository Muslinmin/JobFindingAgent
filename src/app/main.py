from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger

from agent.llm_client import AsyncLLMClient
from app.config import settings
from app.db.database import create_tables
from app.exception_handlers import register_exception_handlers
from app.routes.actions import router as actions_router
from app.routes.chat import router as chat_router
from app.routes.follow_up import router as follow_up_router
from app.services.service import JobService
from dedup.fingerprint import fingerprint
from scheduler.bootstrap import SchedulerDeps, register_jobs, start_scheduler, stop_scheduler
from scoring.embedder import LiteLLMEmbedder
from scoring.embedding_scorer import EmbeddingScorer
from scraper.careers_gov_adapter import CareersGovSource
from scraper.protocol import JobSource
from telegram_bot.shared.bootstrap import build_applications, start_bots, stop_bots

logger.add(
    "logs/app.log",
    rotation="10 MB",
    retention="7 days",
    level=settings.log_level,
    format="{time} | {level} | {module} | {message}",
)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await create_tables(db)
    app.state.job_service = JobService(db, fingerprint)
    logger.info("Database initialized, JobService constructed")

    # Built once here so the profile-embedding cache (embedding_scorer.py)
    # stays warm across an entire scheduled run — see scoring_v2.md §Integration.
    app.state.scorer = EmbeddingScorer(LiteLLMEmbedder())
    logger.info("EmbeddingScorer constructed")

    chat_app, notifications_app, notification_client = build_applications(
        chat_bot_token=settings.telegram_chat_bot_token,
        notifications_bot_token=settings.telegram_notifications_bot_token,
        chat_id=settings.telegram_chat_id,
        backend_base_url=settings.api_base_url,
    )
    # Scheduler's future push path — see telegram_v2.md § Step 4 WP-T1.
    app.state.notification_client = notification_client
    await start_bots(chat_app, notifications_app)
    logger.info("Both Telegram bots started")

    # CareersGovSource reads OGP's public open-data mirror (scraper_layer.md
    # WP-S1) — no credentials needed. MCF unbuilt (WP-S2, recon in
    # progress). JobStreet blocked (WP-S3).
    adapters: list[JobSource] = [CareersGovSource()]
    scheduler_deps = SchedulerDeps(
        service=app.state.job_service,
        scorer=app.state.scorer,
        llm=AsyncLLMClient(),
        telegram=app.state.notification_client,
        adapters=adapters,
        profile_path=Path(settings.profile_path),
        queries_path=Path(settings.search_queries_path),
        template_path=Path(settings.tailoring_template_path),
        output_dir=Path(settings.tailoring_output_dir),
    )
    register_jobs(scheduler, scheduler_deps, settings)
    await start_scheduler(scheduler)
    logger.info("Scheduler started — all six jobs registered")

    yield

    await stop_scheduler(scheduler)
    await stop_bots(chat_app, notifications_app)
    await db.close()
    logger.info("Scheduler, Telegram bots, and database connection shut down")


app = FastAPI(lifespan=lifespan)
register_exception_handlers(app)
app.include_router(chat_router)
app.include_router(actions_router)
app.include_router(follow_up_router)
