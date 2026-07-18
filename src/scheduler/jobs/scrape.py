"""Scrape + inline score job (scheduling_v2.md WP-S2).

Daily job. Reads `search_queries.json` fresh on every run (so the weekly
query_regen job's output takes effect without an app restart), fans out
across every registered adapter for each query, ingests each normalised
`JobCreate` via the injected `JobService` facade (dedup happens inside
`ingest_job`, no self-HTTP), then immediately scores each newly-DISCOVERED
record and transitions it to SCORED or REJECTED. Scoring is inline because
DISCOVERED is meant to be a transient state — a job should never still be
DISCOVERED at the start of the next scrape cycle.

`profile_path` is threaded all the way down to `_ingest_and_score` — the
one place that actually needs a `Profile` instance to hand to
`scorer.score(jd_text, candidate)` — rather than loading once in
`run_scrape` and passing a `Profile` instance through `_fan_out`. Every
function in this module that touches the profile therefore takes a path
and loads it itself, the same shape as `queries_path` /  `_load_queries`;
no function signature in this file carries a bare `Profile` instance.
`load_profile` re-validates on every call, so a mid-run profile edit is
cheap to pick up and never silently trusted stale (profile/loader.py).

Adapter and per-job failures are isolated: one adapter raising, or one
ingest/score call raising, must not abort the rest of the batch
(scheduling_v2.md invariant 5). `run_scrape` is the thin file-loading
wrapper; `_fan_out` is the testable core that takes the query list as an
argument.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger

from app.config import Settings
from app.models.enums import ApplicationStatus
from app.models.job import JobCreate
from app.services.service import JobService
from profile.loader import load_profile
from scoring.protocol import Scorer
from scraper.protocol import JobSource


def _load_queries(path: Path) -> list[str] | None:
    """Read `search_queries.json`. Returns None (and logs a warning) if the
    file is missing — the caller must not scrape with a stale/empty list."""
    if not path.exists():
        logger.warning(f"scrape: queries file not found at {path}")
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning(f"scrape: queries file at {path} is not valid JSON")
        return None
    if not isinstance(data, list):
        logger.warning(f"scrape: queries file at {path} does not contain a JSON list")
        return None
    return data


async def _ingest_and_score(
    jobs: list[JobCreate],
    service: JobService,
    scorer: Scorer,
    profile_path: Path,
    settings: Settings,
) -> int:
    """Ingest each job via `service.ingest_job` (dedup + upsert happens
    there). For a job that landed at DISCOVERED (i.e. genuinely new, not a
    repost bump of `seen_count`), `load_profile(profile_path)` and score it,
    gating SCORED vs REJECTED on `settings.score_threshold`. The computed
    integer is passed to `transition_status(..., score=score)` so it is
    stamped once and never recomputed (scoring_v2.md: "score once, store,
    never recompute") — every other caller of `transition_status` in this
    package omits `score`, leaving the stored value untouched. A scoring
    failure (scoring_v2.md: the scorer raises rather than fabricating a 0)
    is caught per-record here and logged — the job is left at DISCOVERED
    for the next run to retry, matching invariant 5 (one record's failure
    never aborts the batch). Returns the number of ingest calls that
    succeeded (for the per-adapter-per-query summary log)."""
    if not jobs:
        return 0

    try:
        profile = load_profile(profile_path)
    except Exception:
        logger.warning(
            f"scrape: could not load profile from {profile_path}; "
            "ingesting this batch without scoring — records stay DISCOVERED"
        )
        profile = None

    n_ingested = 0
    for job_create in jobs:
        try:
            job = await service.ingest_job(job_create)
            n_ingested += 1
        except Exception:
            logger.warning(f"scrape: ingest_job failed for {job_create.company}/{job_create.role}")
            continue

        if job.status != ApplicationStatus.DISCOVERED:
            continue  # repost/dup bump of an existing record — already scored previously

        if profile is None:
            continue  # left at DISCOVERED; retried once profile.json is fixed

        try:
            score = await scorer.score(job.description, profile)
        except Exception:
            logger.warning(f"scrape: scoring failed for job {job.id}; left DISCOVERED for retry")
            continue

        next_status = (
            ApplicationStatus.SCORED if score >= settings.score_threshold else ApplicationStatus.REJECTED
        )
        try:
            await service.transition_status(job.id, next_status, score=score)
        except Exception:
            logger.warning(f"scrape: transition_status failed for job {job.id}")

    return n_ingested


async def _fan_out(
    queries: list[str],
    adapters: list[JobSource],
    service: JobService,
    scorer: Scorer,
    profile_path: Path,
    settings: Settings,
    delay_s: float,
) -> None:
    """Testable core. For each query x adapter: `adapter.fetch(query)`, then
    `_ingest_and_score` on the results. Wraps each adapter call and each
    ingest in its own try/except so one failure never aborts the others;
    sleeps `delay_s` between adapter calls (portal politeness). Logs a
    per-adapter-per-query summary: adapter name, query, jobs fetched,
    ingests succeeded."""
    for query in queries:
        for adapter in adapters:
            try:
                jobs = await adapter.fetch(query)
            except Exception:
                logger.warning(f"scrape: adapter '{getattr(adapter, 'name', adapter)}' failed for query={query!r}")
                if delay_s:
                    await asyncio.sleep(delay_s)
                continue

            n_ingested = await _ingest_and_score(jobs, service, scorer, profile_path, settings)
            logger.info(
                f"scrape: adapter={getattr(adapter, 'name', adapter)} query={query!r} "
                f"fetched={len(jobs)} ingested={n_ingested}"
            )

            if delay_s:
                await asyncio.sleep(delay_s)


async def run_scrape(
    adapters: list[JobSource],
    service: JobService,
    scorer: Scorer,
    settings: Settings,
    queries_path: Path,
    profile_path: Path,
    delay_s: float = 1.0,
) -> None:
    """Read the CURRENT `search_queries.json` via `_load_queries` on every
    run (missing file: log a warning and return without touching any
    adapter or the service), then delegate to `_fan_out` — `profile_path`
    rides along unopened; `_ingest_and_score` is the one place it's
    actually loaded."""
    queries = _load_queries(queries_path)
    if queries is None:
        return
    await _fan_out(queries, adapters, service, scorer, profile_path, settings, delay_s)
