"""Discovery — fetch from adapters, ingest, score inline (agent_v2.md §7).

Extracted verbatim from `scheduler/jobs/scrape.py`, where this logic lived
as the private `_fan_out` / `_ingest_and_score` pair. It moved because a
second caller appeared: the agent's `search_jobs` tool runs the *same*
pipeline on demand for one query, and two copies of "ingest, then score
only what landed at DISCOVERED" would drift the moment either side changed
its threshold or its error isolation. The scrape job keeps what is
genuinely its own — reading `search_queries.json`, the cron shape — and
delegates the pipeline here.

The two callers differ in exactly two knobs, both parameters: `delay_s`
(portal politeness, which the batch job needs across dozens of adapter
calls and a single interactive query does not) and `limit` (a cap the
agent's caller-supplied `limit` maps onto; the batch job takes everything
an adapter offers).

`profile_path` is threaded down to `ingest_and_score` — the one place that
needs a `Profile` instance — rather than loaded once at the top and passed
around as an instance. `load_profile` re-validates on every call, so a
mid-run profile edit is cheap to pick up and never silently trusted stale
(profile/loader.py).

Per-adapter and per-record failures are isolated: one adapter raising, or
one ingest/score call raising, must never abort the rest of the batch
(scheduling_v2.md invariant 5). That isolation is also why `IngestOutcome`
reports counts rather than raising — a partial run is the normal outcome,
not an error, and both callers need to say how partial it was.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from app.config import Settings
from app.models.enums import ApplicationStatus
from app.models.job import Job, JobCreate
from app.services.service import JobService
from profile.loader import load_profile
from scoring.protocol import Scorer
from scraper.protocol import JobSource


@dataclass
class IngestOutcome:
    """What one pass of the pipeline actually did.

    The four counts narrow at each stage — `fetched >= ingested >= new >=
    scored` — so a caller can tell "the adapter found nothing" apart from
    "everything it found was already in the database" apart from "scoring
    is down". That distinction is the whole reason this is a struct and not
    an int: the scrape job only ever logged `ingested`, but the agent has to
    tell the user which of those three happened.

    `records` holds the post-transition `Job` for anything scored and the
    ingested `Job` otherwise, so it always reflects the row as it now
    stands in the database.
    """

    fetched: int = 0
    ingested: int = 0
    new: int = 0
    scored: int = 0
    records: list[Job] = field(default_factory=list)

    def absorb(self, other: "IngestOutcome") -> None:
        self.fetched += other.fetched
        self.ingested += other.ingested
        self.new += other.new
        self.scored += other.scored
        self.records.extend(other.records)


async def ingest_and_score(
    jobs: list[JobCreate],
    service: JobService,
    scorer: Scorer,
    profile_path: Path,
    settings: Settings,
) -> IngestOutcome:
    """Ingest each job via `service.ingest_job` (dedup + upsert happens
    there). A job that landed at DISCOVERED is genuinely new rather than a
    repost bump of `seen_count`, so it — and only it — gets scored, gating
    SCORED vs REJECTED on `settings.score_threshold`. The computed integer
    goes to `transition_status(..., score=score)` so it is stamped once and
    never recomputed (scoring_v2.md: "score once, store, never recompute");
    every other caller of `transition_status` omits `score`, leaving the
    stored value untouched.

    A scoring failure (scoring_v2.md: the scorer raises rather than
    fabricating a 0) is caught per-record and logged — the job is left at
    DISCOVERED for the next run to retry.
    """
    outcome = IngestOutcome(fetched=len(jobs))
    if not jobs:
        return outcome

    try:
        profile = load_profile(profile_path)
    except Exception:
        logger.warning(
            f"discovery: could not load profile from {profile_path}; "
            "ingesting this batch without scoring — records stay DISCOVERED"
        )
        profile = None

    for job_create in jobs:
        try:
            job = await service.ingest_job(job_create)
        except Exception:
            logger.warning(
                f"discovery: ingest_job failed for {job_create.company}/{job_create.role}"
            )
            continue

        outcome.ingested += 1
        outcome.records.append(job)

        if job.status != ApplicationStatus.DISCOVERED:
            continue  # repost/dup bump of an existing record — already scored previously
        outcome.new += 1

        if profile is None:
            continue  # left at DISCOVERED; retried once profile.json is fixed

        try:
            score = await scorer.score(job.description, profile)
        except Exception:
            logger.warning(f"discovery: scoring failed for job {job.id}; left DISCOVERED for retry")
            continue

        next_status = (
            ApplicationStatus.SCORED
            if score >= settings.score_threshold
            else ApplicationStatus.REJECTED
        )
        try:
            updated = await service.transition_status(job.id, next_status, score=score)
        except Exception:
            logger.warning(f"discovery: transition_status failed for job {job.id}")
            continue

        # The row as it now stands — the caller reports status and score,
        # and DISCOVERED/None would be a lie the instant this succeeded.
        outcome.records[-1] = updated
        outcome.scored += 1

    return outcome


async def fan_out(
    queries: list[str],
    adapters: list[JobSource],
    service: JobService,
    scorer: Scorer,
    profile_path: Path,
    settings: Settings,
    delay_s: float,
    limit: int | None = None,
) -> IngestOutcome:
    """For each query x adapter: `adapter.fetch(query)`, then
    `ingest_and_score` on the results. Wraps each adapter call in its own
    try/except so one failure never aborts the others, and sleeps `delay_s`
    between adapter calls (portal politeness). Logs a per-adapter-per-query
    summary: adapter name, query, jobs fetched, ingests succeeded.

    `limit` truncates each adapter's results before they are ingested, not
    after — this is a cap on how much work one call does, and a job that is
    never ingested is one the user never pays an embedding call for.
    """
    total = IngestOutcome()

    for query in queries:
        for adapter in adapters:
            try:
                jobs = await adapter.fetch(query)
            except Exception:
                logger.warning(
                    f"discovery: adapter '{getattr(adapter, 'name', adapter)}' "
                    f"failed for query={query!r}"
                )
                if delay_s:
                    await asyncio.sleep(delay_s)
                continue

            if limit is not None:
                jobs = jobs[:limit]

            outcome = await ingest_and_score(jobs, service, scorer, profile_path, settings)
            total.absorb(outcome)
            logger.info(
                f"discovery: adapter={getattr(adapter, 'name', adapter)} query={query!r} "
                f"fetched={outcome.fetched} ingested={outcome.ingested}"
            )

            if delay_s:
                await asyncio.sleep(delay_s)

    return total
