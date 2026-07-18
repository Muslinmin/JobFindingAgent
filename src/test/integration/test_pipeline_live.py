"""
Live end-to-end smoke test for the scheduling layer — real Careers@Gov
corpus, real embedding API, real tailoring LLM + tectonic compile, real
Telegram push. Runs scrape+score (scheduler/jobs/scrape.py) against a
throwaway temp DB with the real profile and one real query, pushes a
summary + the digest job's own formatted output, then tailors a small
capped batch of the newly-SCORED jobs (scheduler/jobs/tailor.py) — real
LLM completion + real PDF compile + real Telegram document push — so the
whole pre-approval pipeline is visible outside the test process, not just
in assertions.

Nothing here touches your real jobs.db — a fresh sqlite file in tmp_path is
used and discarded. Nothing here touches your real artifacts/ dir either —
tailored PDFs are written under tmp_path.

Skipped automatically unless TELEGRAM_NOTIFICATIONS_BOT_TOKEN,
TELEGRAM_CHAT_ID, EMBEDDING_API_KEY, and MODEL_API_KEY are all set in
.env, and `tectonic` is on PATH (conda: job-finder env's bin/). Run
explicitly with:

    pytest src/test/integration/test_pipeline_live.py -v -m live -s
"""

import json
import shutil
from pathlib import Path

import aiosqlite
import pytest
from loguru import logger
from telegram import Bot

from agent.llm_client import AsyncLLMClient
from app.config import settings
from app.db.database import create_tables
from app.models.enums import ApplicationStatus
from app.services.service import JobService
from dedup.fingerprint import fingerprint
from scheduler.jobs.digest import run_digest
from scheduler.jobs.scrape import run_scrape
from scheduler.jobs.tailor import run_tailor
from scoring.embedder import LiteLLMEmbedder
from scoring.embedding_scorer import EmbeddingScorer
from scraper.careers_gov_adapter import CareersGovSource
from telegram_bot.notifications.client import NotificationTelegramClient

pytestmark = pytest.mark.live

skip_if_not_configured = pytest.mark.skipif(
    not (
        settings.telegram_notifications_bot_token
        and settings.telegram_chat_id
        and settings.embedding_api_key
        and settings.model_api_key
    ),
    reason=(
        "TELEGRAM_NOTIFICATIONS_BOT_TOKEN / TELEGRAM_CHAT_ID / EMBEDDING_API_KEY / "
        "MODEL_API_KEY not fully set in .env — skipping live pipeline test"
    ),
)
skip_if_no_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None,
    reason="tectonic not on PATH — skipping the tailoring stage (activate the job-finder conda env)",
)

# A query drawn straight from profile.json's target_tracks so the live
# Careers@Gov corpus actually has something relevant to filter down to.
LIVE_QUERY = "robotics"

# Capped small on purpose: each tailored record is one real LLM completion
# (JD + full profile, ~3.5k tokens) plus one real tectonic subprocess
# compile. This is a mechanism smoke test, not a production batch run —
# scheduler/jobs/tailor.py's own tailor_batch_size setting governs the
# real scheduled job; this constant only bounds this one live test.
LIVE_TAILOR_BATCH_SIZE = 2


@skip_if_not_configured
@skip_if_no_tectonic
async def test_live_scrape_score_and_notify(tmp_path):
    # ── 1. Throwaway real DB — never touches the real jobs.db ────────────
    db = await aiosqlite.connect(str(tmp_path / "live_test.db"))
    db.row_factory = aiosqlite.Row
    await create_tables(db)
    service = JobService(db, fingerprint)

    # ── 2. Real scorer — hits the embeddings API for real ─────────────────
    scorer = EmbeddingScorer(LiteLLMEmbedder())

    # ── 3. Real adapter — hits the public Careers@Gov OGP mirror for real ─
    adapters = [CareersGovSource()]

    # ── 4. One real query, written to a temp queries file ──────────────────
    queries_path = tmp_path / "search_queries.json"
    queries_path.write_text(json.dumps([LIVE_QUERY]))

    # ── 5. Run the scrape job for real: fetch -> ingest -> score, blocking ─
    print(f"\n[live] scraping Careers@Gov for {LIVE_QUERY!r} ...")
    await run_scrape(
        adapters=adapters,
        service=service,
        scorer=scorer,
        settings=settings,
        queries_path=queries_path,
        profile_path=Path(settings.profile_path),
        delay_s=0,
    )

    # ── 6. Verify the batch actually resolved — nothing left mid-pipeline ──
    discovered = await service.query_jobs({ApplicationStatus.DISCOVERED}, limit=1000)
    scored = await service.query_jobs({ApplicationStatus.SCORED}, limit=1000)
    rejected = await service.query_jobs({ApplicationStatus.REJECTED}, limit=1000)

    print(
        f"[live] discovered_left={len(discovered)} scored={len(scored)} rejected={len(rejected)}"
    )
    assert len(discovered) == 0, "jobs left DISCOVERED — scoring did not complete for the whole batch"
    assert len(scored) + len(rejected) > 0, "no jobs were ingested at all — check the query/corpus"

    # ── 6b. Log every individual score — logger.info goes to both stderr
    #      and logs/app.log (conftest.py imports app.main, which registers
    #      the file sink), so this is captured even without -s. ────────────
    all_jobs = sorted([*scored, *rejected], key=lambda j: j.score, reverse=True)
    logger.info(f"[live] {len(all_jobs)} scored jobs for query {LIVE_QUERY!r} (threshold={settings.score_threshold}):")
    for j in all_jobs:
        logger.info(f"[live]   score={j.score:5d}  status={j.status.value:8s}  {j.role} @ {j.company}")

    # ── 7. Push a real summary to Telegram — the visible proof ─────────────
    bot = Bot(token=settings.telegram_notifications_bot_token)
    telegram = NotificationTelegramClient(bot, settings.telegram_chat_id)

    top = sorted(scored, key=lambda j: j.score, reverse=True)[:5]
    lines = [
        f"LIVE TEST — scrape+score for {LIVE_QUERY!r}",
        f"Scored: {len(scored)}   Rejected: {len(rejected)}",
        "",
        "Top scored:",
        *[f"  {j.score}  {j.role} @ {j.company}" for j in top],
    ]
    await telegram.send_message("\n".join(lines))
    print("[live] summary pushed to Telegram")

    # ── 8. Bonus: exercise the already-built digest job too — a second,
    #      independently-formatted push against the same live data ────────
    await run_digest(service, telegram, settings)
    print("[live] digest pushed to Telegram — check your notifications bot chat")

    # ── 9. Tailor a small capped batch of the newly-SCORED jobs — real LLM
    #      completion + real tectonic PDF compile + real Telegram document
    #      push per success. tailor_batch_size is overridden via a copy so
    #      the global settings object (and thus the real scheduled job) is
    #      never touched by this test. ─────────────────────────────────────
    if scored:
        tailor_settings = settings.model_copy(update={"tailor_batch_size": LIVE_TAILOR_BATCH_SIZE})
        output_dir = tmp_path / "artifacts"
        print(f"[live] tailoring up to {LIVE_TAILOR_BATCH_SIZE} of {len(scored)} scored jobs ...")

        await run_tailor(
            service=service,
            llm=AsyncLLMClient(),
            telegram=telegram,
            settings=tailor_settings,
            profile_path=Path(settings.profile_path),
            template_path=Path(settings.tailoring_template_path),
            output_dir=output_dir,
        )

        pending_approval = await service.query_jobs({ApplicationStatus.PENDING_APPROVAL}, limit=1000)
        still_scored = await service.query_jobs({ApplicationStatus.SCORED}, limit=1000)
        attempted = min(LIVE_TAILOR_BATCH_SIZE, len(scored))
        print(
            f"[live] tailoring: {len(pending_approval)}/{attempted} reached PENDING_APPROVAL "
            f"({len(still_scored)} still SCORED — guard violation or render failure, see logs/app.log)"
        )
        for job in pending_approval:
            print(f"[live]   tailored+pushed: {job.role} @ {job.company}")
    else:
        print("[live] no SCORED jobs this run — skipping the tailoring stage")

    await db.close()
