from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_job_service
from app.models.job import Job
from app.services.service import JobService

router = APIRouter(prefix="/jobs", tags=["follow-up"])


class FollowUpRequest(BaseModel):
    note: str | None = None


@router.post("/{job_id}/follow-up", response_model=Job)
async def follow_up(
    job_id: int,
    payload: FollowUpRequest,
    service: JobService = Depends(get_job_service),
) -> Job:
    return await service.record_follow_up(job_id)
