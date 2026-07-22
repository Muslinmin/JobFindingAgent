"""Reference for whoever wires the tailoring layer into a real caller
(scheduling_v2.md's daily batch job, or the agent's on-demand
`tailor_resume` tool). NOT imported by main.py and NOT run by the app —
this file exists to be read and copied from.

The tailoring layer (`src/tailoring/`) has exactly one public entry point,
`tailor()`, and it does no I/O beyond the LLM call and writing the PDF to
`output_dir` — see tailoring_build.md "Caller contract". Everything below
this line is the CALLER's job, not the layer's:

  - selecting which job(s) to tailor for
  - constructing the Profile and the LLM client
  - deciding where the PDF goes on disk
  - registering the resulting artifact with the backend
  - advancing the job's ApplicationStatus (SCORED -> TAILORED -> PENDING_APPROVAL)
  - deciding what happens on failure (retry policy, notification, logging)

That split is why the layer could be built and tested end to end (WP0-WP5)
without a database or a scheduler in scope. This file demonstrates the
missing half.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from agent.llm_client import TaskLLMClient
from app.config import settings
from app.models.enums import ApplicationStatus, ArtifactKind
from app.models.job import ArtifactCreate, Job
from app.services.service import JobService
from profile.loader import load_profile
from tailoring.tailor import TailoringError, tailor

# The renderer's Jinja template lives inside the tailoring package itself —
# it is not something a caller ever needs to author or vary.
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "tailoring" / "templates" / "cv.tex.jinja"

# Illustrative only: the tailoring layer doesn't care where the PDF goes,
# so this is a caller-side policy choice, not part of the layer's contract.
ARTIFACTS_ROOT = Path("artifacts")


async def tailor_one_job(job_service: JobService, job: Job) -> None:
    """The shared core both the daily batch and the on-demand tool would
    call — everything a caller needs to go from a SCORED `Job` to a
    registered CV artifact and a PENDING_APPROVAL job, or a clean failure.
    """
    # 1. The two things `tailor()` needs beyond the JD: a Profile, and
    #    something satisfying the LLMTailor protocol (`async def
    #    complete(prompt: str) -> str`). Both are cheap to construct per
    #    call — load_profile re-validates every invariant on every call
    #    (tailoring.md: "no downstream layer should have to check whether
    #    the object it was handed is trustworthy"), and TaskLLMClient
    #    holds no state beyond the model name.
    profile = load_profile(settings.profile_path)
    llm = TaskLLMClient()

    # 2. output_dir is the caller's own convention — tailor() will
    #    mkdir -p it and write cv.tex + cv.pdf (+ debug/ if requested).
    output_dir = ARTIFACTS_ROOT / str(job.id)

    # 3. The call. Single pass, no retry by design (tailoring.md §5) — a
    #    TailoringError means either a transient LLM/render failure or the
    #    LLM fabricated something; re-prompting the same input on the spot
    #    is not the fix for either.
    try:
        result = await tailor(
            job.description,
            profile,
            llm=llm,
            template_path=TEMPLATE_PATH,
            output_dir=output_dir,
            save_debug_artifacts=False,
        )
    except TailoringError as e:
        # e.reason is one of: 'llm_call_failed', 'schema_invalid',
        # 'guard_violation', 'render_failed'. e.violations is populated
        # only for 'guard_violation'. Left in SCORED here so the next
        # scheduled batch simply retries it — swap in whatever
        # notification/backoff policy the caller actually wants.
        logger.error(f"tailor() failed for job {job.id}: reason={e.reason} violations={e.violations}")
        return

    # 4. Register the artifact, then advance the FSM. tailor() never
    #    touches the database itself (tailoring_build.md "Caller
    #    contract") — reaching TAILORED and registering the artifact are
    #    both the caller's responsibility, in whichever order the caller's
    #    invariants require. Here: record TAILORED first so a crash
    #    between these two calls never leaves an artifact-less job stuck
    #    reporting SCORED.
    await job_service.transition_status(job.id, ApplicationStatus.TAILORED)
    await job_service.register_artifact(
        job.id, ArtifactCreate(kind=ArtifactKind.CV_PDF, path=str(result.path))
    )
    await job_service.transition_status(job.id, ApplicationStatus.PENDING_APPROVAL)
    logger.info(f"job {job.id} tailored -> {result.path}")


async def run_daily_batch(job_service: JobService, batch_size: int) -> None:
    """Scheduling_v2.md's budgeted daily job: the top N SCORED jobs, tailored
    one at a time. Sequential on purpose — each call is a real LLM + a real
    tectonic subprocess; parallelizing is a cost/throughput decision for
    whoever builds the actual scheduler, not something to default to here.
    """
    jobs = await job_service.top_scored_for_tailoring(batch_size)
    for job in jobs:
        await tailor_one_job(job_service, job)


async def tailor_on_demand(job_service: JobService, job: Job) -> None:
    """The agent's on-demand `tailor_resume` tool: same call, triggered by
    a user asking for one specific job right now instead of the scheduler
    picking it. The agent layer is responsible for having already fetched
    `job` (e.g. via a query_jobs-style tool) before calling this.
    """
    await tailor_one_job(job_service, job)
