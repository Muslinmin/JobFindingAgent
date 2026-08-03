import aiosqlite
import pytest
from pydantic import ValidationError

from app.db.database import create_tables
from app.models.enums import ApplicationStatus, ArtifactKind
from app.models.job import Artifact, ArtifactCreate, Job, JobCreate


def test_jobcreate_requires_core_fields():
    job = JobCreate(company="Acme", role="Engineer", description="JD text", url="https://acme.com/jobs/1")
    assert job.company == "Acme"
    assert job.role == "Engineer"
    assert job.description == "JD text"
    assert job.posted_at is None
    assert job.metadata is None


@pytest.mark.parametrize(
    "extra_field,extra_value",
    [
        ("status", "discovered"),
        ("score", 8000),
        ("fingerprint", "abc123"),
        ("created_at", "2026-01-01T00:00:00Z"),
        ("seen_count", 1),
    ],
)
def test_jobcreate_refuses_caller_set_disallowed_fields(extra_field, extra_value):
    with pytest.raises(ValidationError):
        JobCreate(
            company="Acme",
            role="Engineer",
            description="JD",
            url="https://acme.com/jobs/1",
            **{extra_field: extra_value},
        )


def test_jobcreate_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        JobCreate(company="Acme", role="Engineer", url="https://acme.com/jobs/1")  # missing description


def test_job_response_round_trip_serializes_iso_timestamps():
    job = Job(
        id=1,
        fingerprint="abc",
        company="Acme",
        role="Engineer",
        description="JD",
        url="https://acme.com/jobs/1",
        posted_at=None,
        metadata=None,
        status=ApplicationStatus.DISCOVERED,
        score=None,
        status_changed_at="2026-07-13T00:00:00+00:00",
        follow_up_count=0,
        last_follow_up_at=None,
        follow_up_nudge_at=None,
        seen_count=1,
        last_seen_at="2026-07-13T00:00:00+00:00",
        created_at="2026-07-13T00:00:00+00:00",
        updated_at="2026-07-13T00:00:00+00:00",
    )
    dumped = job.model_dump()
    assert dumped["status_changed_at"] == "2026-07-13T00:00:00+00:00"
    assert dumped["status"] == ApplicationStatus.DISCOVERED


def test_artifact_create_rejects_invalid_kind():
    with pytest.raises(ValidationError):
        ArtifactCreate(kind="not_a_kind", path="/tmp/x.pdf")


def test_artifact_create_accepts_valid_kind():
    artifact = ArtifactCreate(kind=ArtifactKind.CV_PDF, path="/tmp/x.pdf")
    assert artifact.kind == ArtifactKind.CV_PDF


def test_artifact_round_trip():
    artifact = Artifact(id=1, job_id=1, kind=ArtifactKind.COVER_LETTER, path="/tmp/cl.txt", created_at="2026-07-13T00:00:00+00:00")
    assert artifact.model_dump()["kind"] == ArtifactKind.COVER_LETTER


async def test_schema_creates_tables_and_unique_fingerprint_index(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await create_tables(conn)

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('jobs', 'artifacts')"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        assert tables == {"jobs", "artifacts"}

        cursor = await conn.execute("PRAGMA index_list('jobs')")
        indexes = await cursor.fetchall()

        unique_on_fingerprint = False
        for row in indexes:
            if row[2] == 1:  # unique flag
                index_name = row[1]
                cols_cursor = await conn.execute(f"PRAGMA index_info('{index_name}')")
                cols = [c[2] for c in await cols_cursor.fetchall()]
                if cols == ["fingerprint"]:
                    unique_on_fingerprint = True
        assert unique_on_fingerprint
