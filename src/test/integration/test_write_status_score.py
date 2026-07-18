"""Regression coverage for the score-persistence bug caught by the live
pipeline test (test/integration/test_pipeline_live.py): `transition_status`
computed a score in scheduler/jobs/scrape.py but never wrote it to the DB —
`write_status` only ever touched status/status_changed_at/updated_at, so
every scored job's `score` column stayed NULL forever, silently breaking
`top_scored_for_tailoring`'s `ORDER BY score DESC`.

Uses a real in-memory sqlite DB (not test/integration/test_repository.py,
which predates the current schema and fails to collect for unrelated
reasons — `insert_job` doesn't exist in this repository module).
"""

import aiosqlite
import pytest

from app.db import repository as repo
from app.db.database import create_tables
from app.models.enums import ApplicationStatus
from app.models.job import JobCreate
from app.services.service import JobService
from dedup.fingerprint import fingerprint


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_tables(conn)
        yield conn


async def _discovered_job(db) -> int:
    job = await repo.upsert_job(
        db,
        JobCreate(company="Acme", role="Engineer", description="JD", url="https://example.com/1"),
        "fp1",
        "2026-01-01T00:00:00+00:00",
    )
    return job.id


# ── repository level ──────────────────────────────────────────────────────────

async def test_write_status_persists_score_when_provided(db):
    job_id = await _discovered_job(db)

    updated = await repo.write_status(db, job_id, ApplicationStatus.SCORED, "2026-01-02T00:00:00+00:00", score=8123)

    assert updated.score == 8123
    assert updated.status == ApplicationStatus.SCORED


async def test_write_status_leaves_score_untouched_when_omitted(db):
    job_id = await _discovered_job(db)
    await repo.write_status(db, job_id, ApplicationStatus.SCORED, "2026-01-02T00:00:00+00:00", score=8123)

    updated = await repo.write_status(db, job_id, ApplicationStatus.TAILORED, "2026-01-03T00:00:00+00:00")

    assert updated.score == 8123, "a transition with no score kwarg must not null out the stored score"
    assert updated.status == ApplicationStatus.TAILORED


# ── service level ─────────────────────────────────────────────────────────────

async def test_transition_status_persists_score(db):
    service = JobService(db, fingerprint)
    job = await service.ingest_job(
        JobCreate(company="Grab", role="Data Engineer", description="JD", url="https://example.com/2")
    )

    updated = await service.transition_status(job.id, ApplicationStatus.SCORED, score=7500)

    assert updated.score == 7500
    fetched = await repo.get_job_by_id(db, job.id)
    assert fetched.score == 7500


async def test_transition_status_default_score_none_does_not_null_existing_score(db):
    service = JobService(db, fingerprint)
    job = await service.ingest_job(
        JobCreate(company="Grab", role="Data Engineer", description="JD", url="https://example.com/3")
    )
    await service.transition_status(job.id, ApplicationStatus.SCORED, score=6500)

    updated = await service.transition_status(job.id, ApplicationStatus.TAILORED)

    assert updated.score == 6500
