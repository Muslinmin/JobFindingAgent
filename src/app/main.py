from contextlib import asynccontextmanager

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from loguru import logger
from telegram.ext import Application, MessageHandler, filters

from app.config import settings
from app.db import repository as repo
from app.db.database import create_tables, get_db
from app.models.job import JobCreate
from app.routes.chat import router as chat_router
from app.routes.jobs import router as jobs_router
from bot.bot import handle_message
from scoring.fingerprint import fingerprint_job
from scraper.parser import parse_results
from scraper.tavily_client import search as tavily_search

logger.add(
    "logs/app.log",
    rotation="10 MB",
    retention="7 days",
    level=settings.log_level,
    format="{time} | {level} | {module} | {message}",
)


async def _scheduled_scrape() -> None:
    logger.info(f"Scheduled scrape starting — query: '{settings.scrape_query}'")
    raw  = await tavily_search(settings.scrape_query)
    jobs = parse_results(raw)
    logger.info(f"Scheduled scrape: {len(jobs)} results from Tavily")

    inserted_count = 0
    async for db in get_db():
        for record in jobs:
            try:
                job_data   = JobCreate(**record)
                fp         = fingerprint_job(job_data.company, job_data.role, str(job_data.url))
                _, created = await repo.insert_job(db, job_data, fp)
                if created:
                    inserted_count += 1
            except Exception as e:
                logger.warning(f"Scheduled scrape: failed to insert record: {e}")

    logger.info(f"Scheduled scrape complete — {inserted_count} new records inserted")


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app):
    async with aiosqlite.connect(settings.db_path) as conn:
        await create_tables(conn)

    scheduler.add_job(
        _scheduled_scrape,
        trigger=IntervalTrigger(hours=24),
        id="daily_scrape",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — daily scrape job registered")

    ptb_app = Application.builder().token(settings.telegram_bot_token).build()
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.updater.start_polling()
    logger.info("Telegram bot started — polling for messages")

    yield

    await ptb_app.updater.stop()
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("Telegram bot stopped")
    scheduler.shutdown()
    logger.info("APScheduler stopped")


app = FastAPI(lifespan=lifespan)
app.include_router(jobs_router)
app.include_router(chat_router)
