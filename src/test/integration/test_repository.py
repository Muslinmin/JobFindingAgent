import hashlib
from datetime import datetime, timezone

import aiosqlite
import pytest

from app.db.database import create_tables
from app.db.repository import (
    find_jobs,
    get_job_by_id,
    list_jobs,
    upsert_job,
    write_status,
)
from app.models.enums import ApplicationStatus
from app.models.job import JobCreate


def make_fingerprint(company: str, role: str) -> str:
    raw = f"{company.lower()}|{role.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await create_tables(conn)
        yield conn


SAMPLE_JOB = JobCreate(
    company="Acme",
    role="Engineer",
    description="Build things.",
    url="https://acme.com/jobs/1",
)
SAMPLE_FP = make_fingerprint("Acme", "Engineer")


# ── upsert_job ──────────────────────────────────────────────────────────────

async def test_upsert_job_creates_new_record_as_discovered(db):
    job = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    assert job.company == "Acme"
    assert job.role == "Engineer"
    assert job.status == ApplicationStatus.DISCOVERED
    assert job.seen_count == 1


async def test_upsert_job_on_duplicate_fingerprint_bumps_seen_count(db):
    await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    again = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    assert again.seen_count == 2


async def test_upsert_job_on_duplicate_fingerprint_does_not_reset_status(db):
    first = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    await write_status(db, first.id, ApplicationStatus.APPLIED, _now())

    again = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    assert again.status == ApplicationStatus.APPLIED


# ── get_job_by_id ─────────────────────────────────────────────────────────────

async def test_get_job_by_id_returns_the_record(db):
    created = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    fetched = await get_job_by_id(db, created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.company == "Acme"


async def test_get_job_by_id_returns_none_for_missing_id(db):
    fetched = await get_job_by_id(db, 9999)
    assert fetched is None


# ── write_status ──────────────────────────────────────────────────────────────

async def test_write_status_persists_change(db):
    created = await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    updated = await write_status(db, created.id, ApplicationStatus.APPLIED, _now())
    assert updated.status == ApplicationStatus.APPLIED

    fetched = await get_job_by_id(db, created.id)
    assert fetched.status == ApplicationStatus.APPLIED


async def test_write_status_returns_none_for_missing_id(db):
    updated = await write_status(db, 9999, ApplicationStatus.APPLIED, _now())
    assert updated is None


# ── list_jobs ─────────────────────────────────────────────────────────────────

async def test_list_jobs_filters_by_status_set(db):
    job_a = JobCreate(company="Acme", role="Engineer", description="d", url="https://acme.com/1")
    job_b = JobCreate(company="Globex", role="Developer", description="d", url="https://globex.com/2")

    await upsert_job(db, job_a, make_fingerprint("Acme", "Engineer"), _now())
    created_b = await upsert_job(db, job_b, make_fingerprint("Globex", "Developer"), _now())
    await write_status(db, created_b.id, ApplicationStatus.APPLIED, _now())

    results = await list_jobs(db, {ApplicationStatus.DISCOVERED}, limit=50, offset=0)
    assert len(results) == 1
    assert results[0].company == "Acme"


async def test_list_jobs_returns_empty_for_empty_status_set(db):
    await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    results = await list_jobs(db, set(), limit=50, offset=0)
    assert results == []


async def test_list_jobs_respects_limit(db):
    for i in range(3):
        job = JobCreate(company=f"Co{i}", role="Engineer", description="d", url=f"https://x.com/{i}")
        await upsert_job(db, job, make_fingerprint(f"Co{i}", "Engineer"), _now())

    results = await list_jobs(db, {ApplicationStatus.DISCOVERED}, limit=2, offset=0)
    assert len(results) == 2


# ── find_jobs ─────────────────────────────────────────────────────────────────

async def test_find_jobs_matches_role_substring_case_insensitive(db):
    await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    results = await find_jobs(db, job_title="engineer")
    assert len(results) == 1
    assert results[0].company == "Acme"


async def test_find_jobs_filters_by_company(db):
    job_a = JobCreate(company="Acme", role="Engineer", description="d", url="https://acme.com/1")
    job_b = JobCreate(company="Globex", role="Engineer", description="d", url="https://globex.com/2")
    await upsert_job(db, job_a, make_fingerprint("Acme", "Engineer"), _now())
    await upsert_job(db, job_b, make_fingerprint("Globex", "Engineer"), _now())

    results = await find_jobs(db, job_title="Engineer", company="Globex")
    assert len(results) == 1
    assert results[0].company == "Globex"


async def test_find_jobs_returns_empty_for_empty_status_set(db):
    await upsert_job(db, SAMPLE_JOB, SAMPLE_FP, _now())
    results = await find_jobs(db, job_title="Engineer", status_set=set())
    assert results == []
