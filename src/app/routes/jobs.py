import hashlib

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.db.database import get_db
from app.db.repository import (
    delete_job,
    get_all_jobs,
    get_job_by_id,
    insert_job,
    update_job_status,
)
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.models.job import JobCreate, JobResponse, JobUpdate

router = APIRouter(prefix="/jobs", tags=["jobs"])


def make_fingerprint(job: JobCreate) -> str:
    raw = f"{job.company.lower()}|{job.role.lower()}|{str(job.url).lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("", status_code=201, response_model=JobResponse)
async def create_job(job: JobCreate, db: aiosqlite.Connection = Depends(get_db)):
    fp = make_fingerprint(job)
    record, created = await insert_job(db, job, fp)
    return JobResponse(**record, created=created)


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    status: ApplicationStatus | None = Query(default=None),
    db: aiosqlite.Connection = Depends(get_db),
):
    jobs = await get_all_jobs(db, status_filter=status)
    return [JobResponse(**j, created=False) for j in jobs]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, db: aiosqlite.Connection = Depends(get_db)):
    record = await get_job_by_id(db, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**record, created=False)


@router.patch("/{job_id}/status", response_model=JobResponse)
async def update_status(
    job_id: int,
    body: JobUpdate,
    db: aiosqlite.Connection = Depends(get_db),
):
    try:
        record = await update_job_status(db, job_id, body.status, body.notes)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**record, created=False)


@router.delete("/{job_id}", status_code=204)
async def delete_job_route(job_id: int, db: aiosqlite.Connection = Depends(get_db)):
    deleted = await delete_job(db, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
    return Response(status_code=204)
