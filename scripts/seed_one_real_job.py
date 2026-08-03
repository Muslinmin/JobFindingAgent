"""Manual, one-off script — NOT part of the pytest suite.

Seeds exactly ONE real PENDING_APPROVAL job into the real jobs.db (not a
temp/test DB) so the running app (uvicorn app.main:app) has something real
for a Telegram button tap to act on. Real Careers@Gov fetch, real
embedding-API scoring, real tailoring LLM call, real tectonic PDF compile,
real Telegram document push — same building blocks as
test/integration/test_pipeline_live.py, but writing into the app's actual
database instead of a throwaway tmp_path one.

Usage (run while `uvicorn app.main:app` is already running against the
same jobs.db — sqlite tolerates the short-lived second connection):

    PYTHONPATH=src python scripts/seed_one_real_job.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import aiosqlite  # noqa: E402
from loguru import logger  # noqa: E402
from telegram import Bot  # noqa: E402

from agent.llm_client import TaskLLMClient  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.database import create_tables  # noqa: E402
from app.models.enums import ApplicationStatus  # noqa: E402
from app.services.service import JobService  # noqa: E402
from dedup.fingerprint import fingerprint  # noqa: E402
from scheduler.jobs.scrape import run_scrape  # noqa: E402
from scheduler.jobs.tailor import run_tailor  # noqa: E402
from scoring.embedder import LiteLLMEmbedder  # noqa: E402
from scoring.embedding_scorer import EmbeddingScorer  # noqa: E402
from scraper.careers_gov_adapter import CareersGovSource  # noqa: E402
from telegram_bot.notifications.client import NotificationTelegramClient  # noqa: E402

LIVE_QUERY = "robotics"


async def main() -> None:
    db_path = REPO_ROOT / settings.db_path.lstrip("./")
    print(f"Seeding into real DB: {db_path}")

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await create_tables(db)
    service = JobService(db, fingerprint)

    scorer = EmbeddingScorer(LiteLLMEmbedder())
    adapters = [CareersGovSource()]

    queries_path = REPO_ROOT / "search_queries.json.seed_tmp"
    queries_path.write_text(json.dumps([LIVE_QUERY]))

    print(f"Scraping + scoring {LIVE_QUERY!r} ...")
    await run_scrape(
        adapters=adapters,
        service=service,
        scorer=scorer,
        settings=settings,
        queries_path=queries_path,
        profile_path=Path(settings.profile_path),
        delay_s=0,
    )
    queries_path.unlink(missing_ok=True)

    scored = await service.query_jobs({ApplicationStatus.SCORED}, limit=1000)
    print(f"Scored: {len(scored)}")
    if not scored:
        print("No SCORED jobs this run — nothing to tailor. Try again or lower score_threshold.")
        await db.close()
        return

    bot = Bot(token=settings.telegram_notifications_bot_token)
    telegram = NotificationTelegramClient(bot, settings.telegram_chat_id)

    tailor_settings = settings.model_copy(update={"tailor_batch_size": 1})
    print("Tailoring 1 job (real LLM + real tectonic compile) ...")
    await run_tailor(
        service=service,
        llm=TaskLLMClient(),
        telegram=telegram,
        settings=tailor_settings,
        profile_path=Path(settings.profile_path),
        template_path=Path(settings.tailoring_template_path),
        output_dir=REPO_ROOT / "artifacts",
    )

    pending = await service.query_jobs({ApplicationStatus.PENDING_APPROVAL}, limit=10)
    if pending:
        job = pending[0]
        print(f"\nSeeded real job id={job.id}: {job.role} @ {job.company} (PENDING_APPROVAL)")
        print("Check your Telegram notifications bot chat for the CV + buttons.")
    else:
        print("\nTailoring did not reach PENDING_APPROVAL — check logs/app.log for the reason.")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
