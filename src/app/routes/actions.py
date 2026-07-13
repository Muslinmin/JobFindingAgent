from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_job_service
from app.models.enums import ApplicationStatus, UserAction
from app.models.job import Job
from app.services.service import JobService

router = APIRouter(prefix="/jobs", tags=["actions"])


class ActionRequest(BaseModel):
    action: UserAction


@router.post("/{job_id}/action", response_model=Job)
async def perform_action(
    job_id: int,
    payload: ActionRequest,
    service: JobService = Depends(get_job_service),
) -> Job:
    target = ApplicationStatus(payload.action.value)
    return await service.transition_status(job_id, target)
