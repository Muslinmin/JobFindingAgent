from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.models.enums import InvalidTransitionError
from app.services.service import InvalidStateError, JobNotFoundError


async def _job_not_found_handler(request: Request, exc: JobNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


async def _invalid_transition_handler(request: Request, exc: InvalidTransitionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


async def _invalid_state_handler(request: Request, exc: InvalidStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(JobNotFoundError, _job_not_found_handler)
    app.add_exception_handler(InvalidTransitionError, _invalid_transition_handler)
    app.add_exception_handler(InvalidStateError, _invalid_state_handler)
