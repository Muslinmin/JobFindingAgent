import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger

from agent.context import ConversationContext
from agent.handlers import AgentDeps, ToolDispatcher
from agent.llm_client import AgentLLMClient, TaskLLMClient
from agent.loop import Agent
from app.config import settings
from app.conversation.database import connect as connect_conversations
from app.conversation.database import initialise_schema as initialise_conversation_schema
from app.conversation.repository import ConversationRepository
from app.conversation.store import ConversationStore
from app.conversation.transcript_store import TranscriptStore
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
        chat_read_timeout_s=settings.backend_read_timeout_s,
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
        llm=TaskLLMClient(),
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

    # ── Agent layer (agent_v2.md WP-A8) ──────────────────────────────────
    # Bottom-up, no circular dependency. The conversations database is a
    # SECOND SQLite file: its writes never contend with jobs writes, which
    # is the whole reason it isn't extra tables in jobs.db.
    conversation_db = await connect_conversations(settings.conversation_db_path)
    await initialise_conversation_schema(conversation_db)
    conversation_store = ConversationStore(
        ConversationRepository(conversation_db),
        TranscriptStore(Path(settings.transcript_base_dir)),
        lambda: datetime.now(timezone.utc).isoformat(),
    )
    app.state.conversation_context = ConversationContext(
        conversation_store, settings.session_idle_minutes
    )
    app.state.agent = Agent(
        llm=AgentLLMClient(),
        dispatcher=ToolDispatcher(
            AgentDeps(
                job_service=app.state.job_service,
                scorer=app.state.scorer,
                llm=TaskLLMClient(),
                settings=settings,
                profile_path=Path(settings.profile_path),
                queries_path=Path(settings.search_queries_path),
            )
        ),
        context=app.state.conversation_context,
        profile_path=Path(settings.profile_path),
    )
    # Per-session turn locks, created once here and populated lazily by the
    # route. The scheduler receives NO reference to any of this — it and the
    # conversation store are total strangers by design.
    app.state.session_locks: dict[str, asyncio.Lock] = {}
    # Guards continue-vs-new only, so two simultaneous first messages can't
    # each start a session and split one conversation in two.
    app.state.session_resolution_lock = asyncio.Lock()
    logger.info("Agent constructed, conversation store wired")

    yield

    await stop_scheduler(scheduler)
    await stop_bots(chat_app, notifications_app)
    await conversation_db.close()
    await db.close()
    logger.info("Scheduler, Telegram bots, and both database connections shut down")


app = FastAPI(lifespan=lifespan)
register_exception_handlers(app)
app.include_router(chat_router)
app.include_router(actions_router)
app.include_router(follow_up_router)
