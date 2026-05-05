import hashlib
import pytest
import aiosqlite
from app.db.repository import (
    insert_job,
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    delete_job,
)
from app.db.database import create_tables
from app.models.enums import ApplicationStatus
from app.models.job import JobCreate


def make_fingerprint(company: str, role: str, url: str) -> str:
    raw = f"{company.lower()}|{role.lower()}|{url.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await create_tables(conn)
        yield conn


SAMPLE_JOB = JobCreate(
    company="Acme",
    role="Engineer",
    url="https://acme.com/jobs/1",
)
SAMPLE_FP = make_fingerprint("Acme", "Engineer", "https://acme.com/jobs/1")


async def test_insert_job_returns_true_on_first_insert(db):
    record, created = await insert_job(db, SAMPLE_JOB, SAMPLE_FP)
    assert created is True
    assert record["company"] == "Acme"


async def test_insert_job_returns_false_on_duplicate(db):
    await insert_job(db, SAMPLE_JOB, SAMPLE_FP)
    record, created = await insert_job(db, SAMPLE_JOB, SAMPLE_FP)
    assert created is False
    assert record["company"] == "Acme"


async def test_get_all_jobs_filters_by_status(db):
    job_found = JobCreate(company="Acme", role="Engineer", url="https://acme.com/jobs/1")
    job_applied = JobCreate(company="Globex", role="Developer", url="https://globex.com/jobs/2")

    fp1 = make_fingerprint("Acme", "Engineer", "https://acme.com/jobs/1")
    fp2 = make_fingerprint("Globex", "Developer", "https://globex.com/jobs/2")

    await insert_job(db, job_found, fp1)
    record2, _ = await insert_job(db, job_applied, fp2)
    await update_job_status(db, record2["id"], ApplicationStatus.APPLIED)

    results = await get_all_jobs(db, status_filter=ApplicationStatus.FOUND)
    assert len(results) == 1
    assert results[0]["company"] == "Acme"


async def test_update_status_persists_change(db):
    record, _ = await insert_job(db, SAMPLE_JOB, SAMPLE_FP)
    updated = await update_job_status(db, record["id"], ApplicationStatus.APPLIED)
    assert updated["status"] == ApplicationStatus.APPLIED.value

    fetched = await get_job_by_id(db, record["id"])
    assert fetched["status"] == ApplicationStatus.APPLIED.value


async def test_delete_returns_false_for_missing_id(db):
    deleted = await delete_job(db, 9999)
    assert deleted is False
