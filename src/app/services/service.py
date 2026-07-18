from datetime import datetime, timezone
from typing import Callable

import aiosqlite

from app.db import repository as repo
from app.models.enums import ApplicationStatus, transition
from app.models.job import Artifact, ArtifactCreate, Job, JobCreate

_TERMINAL_STATUSES = {
    ApplicationStatus.REJECTED,
    ApplicationStatus.USER_SKIPPED,
    ApplicationStatus.EXPIRED,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.DECLINED,
}
_ACTIVE_STATUSES = set(ApplicationStatus) - _TERMINAL_STATUSES


class JobNotFoundError(Exception):
    pass


class InvalidStateError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobService:
    def __init__(self, db: aiosqlite.Connection, fingerprint_fn: Callable[[JobCreate], str]):
        self._db = db
        self._fingerprint_fn = fingerprint_fn

    async def ingest_job(self, job: JobCreate) -> Job:
        fingerprint = self._fingerprint_fn(job)
        return await repo.upsert_job(self._db, job, fingerprint, _now())

    async def transition_status(
        self, job_id: int, to_status: ApplicationStatus, score: int | None = None
    ) -> Job:
        current = await repo.get_job_by_id(self._db, job_id)
        if current is None:
            raise JobNotFoundError(f"job {job_id} not found")
        transition(current.status, to_status)
        return await repo.write_status(self._db, job_id, to_status, _now(), score=score)

    async def query_jobs(
        self,
        status_set: set[ApplicationStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        resolved = status_set if status_set is not None else _ACTIVE_STATUSES
        return await repo.list_jobs(self._db, resolved, limit, offset)

    async def find_jobs(
        self,
        job_title: str,
        company: str | None = None,
        status_set: set[ApplicationStatus] | None = None,
        limit: int = 50,
    ) -> list[Job]:
        return await repo.find_jobs(self._db, job_title, company, status_set, limit)

    async def top_scored_for_tailoring(self, limit: int) -> list[Job]:
        return await repo.top_scored_for_tailoring(self._db, limit)

    async def register_artifact(self, job_id: int, artifact: ArtifactCreate) -> Artifact:
        job = await repo.get_job_by_id(self._db, job_id)
        if job is None:
            raise JobNotFoundError(f"job {job_id} not found")
        return await repo.insert_artifact(self._db, job_id, artifact, _now())

    async def record_follow_up(self, job_id: int) -> Job:
        job = await repo.get_job_by_id(self._db, job_id)
        if job is None:
            raise JobNotFoundError(f"job {job_id} not found")
        if job.status != ApplicationStatus.APPLIED:
            raise InvalidStateError(f"job {job_id} is not APPLIED (status={job.status.value})")
        return await repo.record_follow_up(self._db, job_id, _now())

    async def mark_follow_up_nudged(self, job_id: int) -> Job:
        job = await repo.mark_follow_up_nudged(self._db, job_id, _now())
        if job is None:
            raise JobNotFoundError(f"job {job_id} not found")
        return job

    # ── Lifecycle & follow-up job reads (scheduling_v2.md WP-S3/WP-S4) ──
    # Each returns records already filtered by the relevant time rule —
    # the scheduler jobs never build these WHERE clauses themselves.

    async def list_expired_pending_approval(self, before: str) -> list[Job]:
        return await repo.list_expired_pending_approval(self._db, before)

    async def list_stale_scored(self, before: str) -> list[Job]:
        return await repo.list_stale_scored(self._db, before)

    async def list_ghost_candidates(self, before: str) -> list[Job]:
        return await repo.list_ghost_candidates(self._db, before)

    async def list_follow_up_candidates(self, before: str) -> list[Job]:
        return await repo.list_follow_up_candidates(self._db, before)

    async def list_second_nudge_candidates(self, before: str) -> list[Job]:
        return await repo.list_second_nudge_candidates(self._db, before)

    async def count_status_since(self, status: ApplicationStatus, since: str) -> int:
        return await repo.count_status_since(self._db, status, since)
