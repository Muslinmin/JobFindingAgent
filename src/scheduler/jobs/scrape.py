"""Scrape + inline score job (scheduling_v2.md WP-S2).

Daily job. Reads `search_queries.json` fresh on every run (so the weekly
query_regen job's output takes effect without an app restart) and hands the
list to `app/services/discovery.py`, which fans out across every registered
adapter, ingests each normalised `JobCreate` via the injected `JobService`
facade (dedup happens inside `ingest_job`, no self-HTTP), then immediately
scores each newly-DISCOVERED record and transitions it to SCORED or
REJECTED. Scoring is inline because DISCOVERED is meant to be a transient
state — a job should never still be DISCOVERED at the start of the next
scrape cycle.

The pipeline itself used to live here as `_fan_out` / `_ingest_and_score`.
It moved to `discovery.py` when the agent's `search_jobs` tool needed the
same behaviour on demand (agent_v2.md §7): the batch loop and the
interactive call must not drift apart on thresholds or error isolation.
What stays here is what is genuinely the scheduled job's own — reading the
queries file, and deciding that a missing one means "do nothing" rather
than "scrape with an empty list".

Adapter and per-job failures are isolated inside `discovery.py`: one
adapter raising, or one ingest/score call raising, must not abort the rest
of the batch (scheduling_v2.md invariant 5).
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from app.config import Settings
from app.services.discovery import fan_out
from app.services.service import JobService
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
    adapter or the service), then delegate to `discovery.fan_out` —
    `profile_path` rides along unopened; `ingest_and_score` is the one place
    it's actually loaded. No `limit`: the daily batch takes everything an
    adapter offers, because capping recall is the scorer's job here, not
    the fetcher's."""
    queries = _load_queries(queries_path)
    if queries is None:
        return

    outcome = await fan_out(queries, adapters, service, scorer, profile_path, settings, delay_s)
    logger.info(
        f"scrape: fetched={outcome.fetched} ingested={outcome.ingested} "
        f"new={outcome.new} scored={outcome.scored}"
    )
