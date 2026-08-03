from fastapi import Request

from app.services.service import JobService


async def get_job_service(request: Request) -> JobService:
    return request.app.state.job_service
