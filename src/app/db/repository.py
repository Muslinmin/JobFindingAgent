import json

import aiosqlite

from app.models.enums import ApplicationStatus, ArtifactKind
from app.models.job import Artifact, ArtifactCreate, Job, JobCreate


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=row["id"],
        fingerprint=row["fingerprint"],
        company=row["company"],
        role=row["role"],
        description=row["description"],
        url=row["url"],
        posted_at=row["posted_at"],
        metadata=json.loads(row["metadata"]) if row["metadata"] is not None else None,
        status=ApplicationStatus(row["status"]),
        score=row["score"],
        status_changed_at=row["status_changed_at"],
        follow_up_count=row["follow_up_count"],
        last_follow_up_at=row["last_follow_up_at"],
        follow_up_nudge_at=row["follow_up_nudge_at"],
        seen_count=row["seen_count"],
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_artifact(row: aiosqlite.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        job_id=row["job_id"],
        kind=ArtifactKind(row["kind"]),
        path=row["path"],
        created_at=row["created_at"],
    )


async def upsert_job(db: aiosqlite.Connection, job: JobCreate, fingerprint: str, now: str) -> Job:
    db.row_factory = aiosqlite.Row
    metadata_json = json.dumps(job.metadata) if job.metadata is not None else None
    cursor = await db.execute(
        """
        INSERT INTO jobs (
            fingerprint, company, role, description, url, posted_at, metadata,
            status, score, status_changed_at, follow_up_count, last_follow_up_at,
            follow_up_nudge_at, seen_count, last_seen_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            seen_count = seen_count + 1,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        RETURNING *
        """,
        (
            fingerprint,
            job.company,
            job.role,
            job.description,
            job.url,
            job.posted_at,
            metadata_json,
            ApplicationStatus.DISCOVERED.value,
            None,
            now,
            0,
            None,
            None,
            1,
            now,
            now,
            now,
        ),
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_job(row)


async def get_job_by_id(db: aiosqlite.Connection, job_id: int) -> Job | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def write_status(
    db: aiosqlite.Connection,
    job_id: int,
    next_status: ApplicationStatus,
    now: str,
    score: int | None = None,
) -> Job | None:
    """`score` is optional: most transitions (EXPIRED, GHOSTED, TAILORED, ...)
    don't touch it, so COALESCE(?, score) leaves the existing value alone
    when None is passed. The scrape job's DISCOVERED -> SCORED/REJECTED
    transition is the one caller that supplies a real value — the only
    point in the pipeline a job's score is ever computed
    (scoring_v2.md: "score once, store, never recompute")."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        UPDATE jobs
        SET status = ?, score = COALESCE(?, score), status_changed_at = ?, updated_at = ?
        WHERE id = ?
        RETURNING *
        """,
        (next_status.value, score, now, now, job_id),
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_job(row) if row else None


async def insert_artifact(
    db: aiosqlite.Connection, job_id: int, artifact: ArtifactCreate, now: str
) -> Artifact:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        INSERT INTO artifacts (job_id, kind, path, created_at)
        VALUES (?, ?, ?, ?)
        RETURNING *
        """,
        (job_id, artifact.kind.value, artifact.path, now),
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_artifact(row)


async def record_follow_up(db: aiosqlite.Connection, job_id: int, now: str) -> Job | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        UPDATE jobs
        SET follow_up_count = follow_up_count + 1,
            last_follow_up_at = ?,
            updated_at = ?
        WHERE id = ?
        RETURNING *
        """,
        (now, now, job_id),
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_job(row) if row else None


async def mark_follow_up_nudged(db: aiosqlite.Connection, job_id: int, now: str) -> Job | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        UPDATE jobs
        SET follow_up_nudge_at = ?, updated_at = ?
        WHERE id = ?
        RETURNING *
        """,
        (now, now, job_id),
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_job(row) if row else None


async def list_jobs(
    db: aiosqlite.Connection, status_set: set[ApplicationStatus], limit: int, offset: int
) -> list[Job]:
    if not status_set:
        return []
    db.row_factory = aiosqlite.Row
    placeholders = ",".join("?" for _ in status_set)
    params = [s.value for s in status_set] + [limit, offset]
    cursor = await db.execute(
        f"""
        SELECT * FROM jobs
        WHERE status IN ({placeholders})
        ORDER BY status_changed_at DESC, id
        LIMIT ? OFFSET ?
        """,
        params,
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def find_jobs(
    db: aiosqlite.Connection,
    job_title: str,
    company: str | None = None,
    status_set: set[ApplicationStatus] | None = None,
    limit: int = 50,
) -> list[Job]:
    db.row_factory = aiosqlite.Row
    conditions = ["role LIKE ? COLLATE NOCASE"]
    params: list = [f"%{job_title}%"]

    if company is not None:
        conditions.append("company LIKE ? COLLATE NOCASE")
        params.append(f"%{company}%")

    if status_set is not None:
        if not status_set:
            return []
        placeholders = ",".join("?" for _ in status_set)
        conditions.append(f"status IN ({placeholders})")
        params.extend(s.value for s in status_set)

    where_clause = " AND ".join(conditions)
    params.append(limit)
    cursor = await db.execute(
        f"""
        SELECT * FROM jobs
        WHERE {where_clause}
        ORDER BY status_changed_at DESC, id
        LIMIT ?
        """,
        params,
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def top_scored_for_tailoring(db: aiosqlite.Connection, limit: int) -> list[Job]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status = ?
        ORDER BY score DESC, status_changed_at DESC, id ASC
        LIMIT ?
        """,
        (ApplicationStatus.SCORED.value, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def list_expired_pending_approval(db: aiosqlite.Connection, before: str) -> list[Job]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status = ? AND status_changed_at < ?
        ORDER BY status_changed_at
        """,
        (ApplicationStatus.PENDING_APPROVAL.value, before),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def list_ghost_candidates(db: aiosqlite.Connection, before: str) -> list[Job]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status IN (?, ?) AND status_changed_at < ?
        ORDER BY status_changed_at
        """,
        (ApplicationStatus.APPLIED.value, ApplicationStatus.INTERVIEWING.value, before),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def list_follow_up_candidates(db: aiosqlite.Connection, before: str) -> list[Job]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status = ?
          AND status_changed_at < ?
          AND follow_up_count = 0
          AND follow_up_nudge_at IS NULL
        ORDER BY status_changed_at
        """,
        (ApplicationStatus.APPLIED.value, before),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def list_stale_scored(db: aiosqlite.Connection, before: str) -> list[Job]:
    """SCORED records untouched since `before` — the lifecycle job's
    stale-rejection bucket (scheduling_v2.md WP-S3)."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status = ? AND status_changed_at < ?
        ORDER BY status_changed_at
        """,
        (ApplicationStatus.SCORED.value, before),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def count_status_since(db: aiosqlite.Connection, status: ApplicationStatus, since: str) -> int:
    """How many records transitioned INTO `status` at or after `since` —
    the digest job's 'ghosted/expired in the last 7 days' counters
    (scheduling_v2.md WP-S7). Distinct from list_ghost_candidates/
    list_expired_pending_approval, which find records STILL SITTING in a
    status past a threshold, for the lifecycle job to transition."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND status_changed_at >= ?",
        (status.value, since),
    )
    row = await cursor.fetchone()
    return row["n"]


async def list_second_nudge_candidates(db: aiosqlite.Connection, before: str) -> list[Job]:
    """APPLIED records already nudged once whose last follow-up predates
    `before` — the follow-up job's optional second-nudge bucket
    (scheduling_v2.md WP-S4)."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE status = ?
          AND follow_up_count = 1
          AND last_follow_up_at < ?
        ORDER BY status_changed_at
        """,
        (ApplicationStatus.APPLIED.value, before),
    )
    rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]
