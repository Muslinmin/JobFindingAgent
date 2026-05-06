from datetime import datetime, timezone

import aiosqlite

from app.models.enums import ApplicationStatus, transition
from app.models.job import JobCreate


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


async def insert_job(db: aiosqlite.Connection, job: JobCreate, fingerprint: str, score: float = 0.0) -> tuple[dict, bool]:
    try:
        cursor = await db.execute(
            """
            INSERT INTO jobs (company, role, url, status, source, notes, description, score, fingerprint, date_logged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.company,
                job.role,
                str(job.url),
                ApplicationStatus.FOUND.value,
                job.source,
                job.notes,
                job.description,
                score,
                fingerprint,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
        record = await get_job_by_id(db, cursor.lastrowid)
        return record, True
    except aiosqlite.IntegrityError:
        record = await get_job_by_fingerprint(db, fingerprint)
        return record, False


async def get_all_jobs(db: aiosqlite.Connection, status_filter: ApplicationStatus | None = None) -> list[dict]:
    db.row_factory = aiosqlite.Row
    if status_filter is not None:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE status = ?", (status_filter.value,)
        )
    else:
        cursor = await db.execute("SELECT * FROM jobs")
    rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def get_job_by_id(db: aiosqlite.Connection, job_id: int) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def get_job_by_fingerprint(db: aiosqlite.Connection, fingerprint: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def update_job_status(
    db: aiosqlite.Connection,
    job_id: int,
    next_status: ApplicationStatus,
    notes: str | None = None,
) -> dict | None:
    record = await get_job_by_id(db, job_id)
    if record is None:
        return None
    current = ApplicationStatus(record["status"])
    transition(current, next_status)  # raises InvalidTransitionError if invalid
    await db.execute(
        "UPDATE jobs SET status = ?, notes = COALESCE(?, notes) WHERE id = ?",
        (next_status.value, notes, job_id),
    )
    await db.commit()
    return await get_job_by_id(db, job_id)


async def delete_job(db: aiosqlite.Connection, job_id: int) -> bool:
    cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0
