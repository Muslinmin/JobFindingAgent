"""Tool handlers — the boundary seam (agent_v2.md §2).

Above this file: LLM binding and reasoning. Below it: services, the
repository, the tailoring layer. Each handler does three things and no
more — parse the args the model emitted, call the service, marshal the
result into the shape the tool table promises. Any *work* that appears in
a handler belongs in a service instead.

**Failure discipline.** An *expected* failure — the "Expected failures"
column of agent_v2.md §3 — is caught and returned as an `errors.py`
`{ok:false}` shape, which re-enters the ReAct loop for the model to relay.
Anything else is left to raise: an unexpected exception aborts the turn
(§4 stop condition 4) and must not be laundered into a result the model
narrates as if it were a normal outcome.

**Unwired tools.** Four tools have no caller-agnostic service to be thin
over yet, so they raise `ToolNotWiredError` rather than pretending:
`search_jobs` and `draft_followup` exist only in scheduler-private form
(`scheduler/jobs/scrape.py`, `scheduler/jobs/follow_up.py`, both entangled
with batch loops and the Telegram push), `draft_cover_letter` was specced
in architecture_v2.md but never built, and `tailor_resume` needs the
artifact backup/`replaced` logic of agent_v2.md's "Artifact identity"
section, which is also unbuilt. `tailor()` itself exists and works; only
its artifact-registration wrapper is missing. The dispatch table lists all
ten so the gap is visible here rather than as a KeyError at runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import TypeAdapter, ValidationError

from agent import errors
from app.config import Settings
from app.models.enums import ApplicationStatus, InvalidTransitionError, legal_targets
from app.models.job import JobCreate
from app.services.service import JobService, JobNotFoundError
from profile.loader import load_profile
from profile.mutate import update_profile as mutate_profile
from profile.schema import ProfileOp
from scheduler.jobs.query_regen import run_query_regen
from scoring.protocol import Scorer

_PROFILE_OP_ADAPTER: TypeAdapter[ProfileOp] = TypeAdapter(ProfileOp)

# Terminal statuses, excluded from a bare listing: "what am I working on"
# should not surface rejected jobs (agent_v2.md §3, Reading jobs). Mirrors
# the service's own split rather than importing its private name.
_TERMINAL = {
    ApplicationStatus.REJECTED,
    ApplicationStatus.USER_SKIPPED,
    ApplicationStatus.EXPIRED,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.DECLINED,
}
_ACTIVE = set(ApplicationStatus) - _TERMINAL


class ToolNotWiredError(NotImplementedError):
    """A tool whose service does not exist yet. Deliberately an exception,
    not an `{ok:false}`: the model cannot route around a missing service,
    and a structured error would invite it to narrate the gap as a normal
    refusal."""


@dataclass
class AgentDeps:
    """Everything the handlers call, injected at the composition root.

    A dataclass of concrete collaborators rather than a service locator, so
    a test constructs exactly the mocks a given handler touches and an
    unwired dependency is a construction error, not a runtime surprise.
    """

    job_service: JobService
    scorer: Scorer
    llm: Any  # text-in/text-out; TaskLLMClient satisfies it structurally
    settings: Settings
    profile_path: Path
    queries_path: Path


def _job_row(job) -> dict:
    """The projection `find_jobs` returns — id/role/company/status/score plus
    the recency key. The description is deliberately absent: it is the one
    field big enough to blow the context window, and the model has no use
    for it when resolving a reference."""
    return {
        "id": job.id,
        "role": job.role,
        "company": job.company,
        "status": job.status.value,
        "score": job.score,
        "status_changed_at": job.status_changed_at,
    }


def _status_set(raw: list[str] | None) -> set[ApplicationStatus] | None:
    if raw is None:
        return None
    return {ApplicationStatus(s) for s in raw}


# ── read ──────────────────────────────────────────────────────────────────────


async def find_jobs(args: dict, deps: AgentDeps) -> list[dict]:
    """Named lookup when a title/company is given, bare pipeline listing
    otherwise — and the two default to different status sets on purpose
    (agent_v2.md §3): a named lookup searches terminal statuses too because
    the user may be recalling a job that has since been rejected, while a
    bare listing hides them.
    """
    job_title = args.get("job_title")
    company = args.get("company")
    status_set = _status_set(args.get("status_set"))
    limit = args.get("limit", 50)

    if job_title is None and company is None:
        return [
            _job_row(job)
            for job in await deps.job_service.query_jobs(
                status_set=status_set if status_set is not None else _ACTIVE, limit=limit
            )
        ]

    # A company-only lookup is still a named lookup: match every title.
    return [
        _job_row(job)
        for job in await deps.job_service.find_jobs(
            job_title=job_title or "",
            company=company,
            status_set=status_set,  # None = all statuses, terminal included
            limit=limit,
        )
    ]


# ── scoring ───────────────────────────────────────────────────────────────────


async def score_job(args: dict, deps: AgentDeps) -> dict:
    """Assess-only. Writes nothing — no ingest, no status, no persisted score."""
    profile = load_profile(deps.profile_path)
    try:
        score = await deps.scorer.score(args["description"], profile)
    except Exception:
        return errors.embedding_unavailable()
    return {"ok": True, "score": score}


async def score_ingest(args: dict, deps: AgentDeps) -> dict:
    """Record-and-score. `ingest_job` is an upsert, so `seen_count == 1`
    means it was created just now and anything higher means it already
    existed. An existing row that already carries a score is not re-scored —
    that would spend embedding credits to recompute a value the system has
    promised never to recompute (scoring_v2.md).
    """
    job = await deps.job_service.ingest_job(
        JobCreate(
            company=args["company"],
            role=args["title"],
            description=args["description"],
            url=args.get("url", ""),
            posted_at=args.get("posted_at"),
        )
    )

    if job.score is not None:
        return {
            "ok": True,
            "id": job.id,
            "status": job.status.value,
            "score": job.score,
            "seen_count": job.seen_count,
            "was_scored": False,
        }

    profile = load_profile(deps.profile_path)
    try:
        score = await deps.scorer.score(job.description, profile)
    except Exception:
        return errors.embedding_unavailable()

    next_status = (
        ApplicationStatus.SCORED
        if score >= deps.settings.score_threshold
        else ApplicationStatus.REJECTED
    )
    updated = await deps.job_service.transition_status(job.id, next_status, score=score)

    return {
        "ok": True,
        "id": updated.id,
        "status": updated.status.value,
        "score": updated.score,
        "seen_count": updated.seen_count,
        "was_scored": True,
    }


# ── mutation ──────────────────────────────────────────────────────────────────


async def update_status(args: dict, deps: AgentDeps) -> dict:
    """Proposes a move; the service enforces the FSM. The pre-read exists
    only to report `old_status` and to name the allowed targets on
    rejection — the authority is `transition_status`, which re-reads and
    re-checks for itself, so this is not a check-then-act race.
    """
    job_id = args["job_id"]
    new_status = ApplicationStatus(args["new_status"])

    current = await deps.job_service.get_job(job_id)
    if current is None:
        return errors.not_found()

    try:
        updated = await deps.job_service.transition_status(job_id, new_status)
    except JobNotFoundError:
        return errors.not_found()
    except InvalidTransitionError:
        return errors.illegal_transition(
            current.status.value,
            new_status.value,
            sorted(s.value for s in legal_targets(current.status)),
        )

    return {
        "ok": True,
        "job_id": updated.id,
        "role": updated.role,
        "company": updated.company,
        "old_status": current.status.value,
        "new_status": updated.status.value,
    }


async def regenerate_queries(args: dict, deps: AgentDeps) -> dict:
    """Rebuilds search_queries.json from the live profile. Declines up front
    on an empty profile: with no skills and no target tracks there is
    nothing to generate from, and letting the LLM invent queries anyway
    would put fabricated intent into the scrape loop.
    """
    profile = load_profile(deps.profile_path)
    if not profile.skills and not profile.target_tracks:
        return errors.profile_empty()

    await run_query_regen(
        llm=deps.llm,
        settings=deps.settings,
        profile_path=deps.profile_path,
        queries_path=deps.queries_path,
    )

    queries = _read_queries(deps.queries_path)
    return {"ok": True, "count": len(queries), "queries_preview": queries[:5]}


def _read_queries(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def update_profile(args: dict, deps: AgentDeps) -> dict:
    """The only source-of-truth mutator and the only two-phase write.

    Takes the mutator's own typed `ProfileOp` rather than a free-form patch
    (see schemas.py's `_PROFILE_OPS` comment). A payload that doesn't
    validate into the union is an `invalid_patch` the model can correct and
    retry — it is expected, because the model composed it.
    """
    confirmed = bool(args.get("confirmed", False))
    payload = {k: v for k, v in args.items() if k != "confirmed"}

    try:
        op = _PROFILE_OP_ADAPTER.validate_python(payload)
    except ValidationError as e:
        return errors.invalid_patch(str(e))

    result = mutate_profile(op, confirmed, deps.profile_path)

    if not result.ok:
        return errors.invalid_patch(result.summary or "profile update rejected")

    if result.pending_confirmation:
        return {
            "ok": True,
            "changed": False,
            "pending_confirmation": True,
            "diff": result.diff,
        }

    return {
        "ok": True,
        "changed": result.changed,
        "summary": result.summary,
        "queries_stale": result.queries_stale,
    }


# ── not yet wired ─────────────────────────────────────────────────────────────


async def _unwired(tool: str, needs: str):
    raise ToolNotWiredError(f"{tool} has no service yet — needs {needs}")


async def search_jobs(args: dict, deps: AgentDeps) -> dict:
    await _unwired("search_jobs", "a shared scrape+ingest service extracted from scheduler/jobs/scrape.py")


async def tailor_resume(args: dict, deps: AgentDeps) -> dict:
    await _unwired("tailor_resume", "the artifact backup/register wrapper from agent_v2.md 'Artifact identity'")


async def draft_followup(args: dict, deps: AgentDeps) -> dict:
    await _unwired("draft_followup", "a drafting service extracted from scheduler/jobs/follow_up.py")


async def draft_cover_letter(args: dict, deps: AgentDeps) -> dict:
    await _unwired("draft_cover_letter", "the cover-letter service specced in architecture_v2.md but never built")


# ── dispatch ──────────────────────────────────────────────────────────────────

Handler = Callable[[dict, AgentDeps], Awaitable[Any]]

_HANDLERS: dict[str, Handler] = {
    "find_jobs": find_jobs,
    "search_jobs": search_jobs,
    "score_job": score_job,
    "score_ingest": score_ingest,
    "update_status": update_status,
    "tailor_resume": tailor_resume,
    "draft_followup": draft_followup,
    "draft_cover_letter": draft_cover_letter,
    "regenerate_queries": regenerate_queries,
    "update_profile": update_profile,
}


class ToolDispatcher:
    """Binds the handler table to one set of dependencies.

    A class rather than the module-level `dispatch(name, args)` of
    agent_v2.md §2 for one reason: the handlers need collaborators, and the
    alternative is module-global state, which would make two differently
    configured agents in one process impossible to test.
    """

    def __init__(self, deps: AgentDeps) -> None:
        self._deps = deps

    async def dispatch(self, name: str, args: dict) -> Any:
        handler = _HANDLERS.get(name)
        if handler is None:
            # The model invented a tool name. Expected enough to be data:
            # it can recover by picking a real one.
            return errors.not_found()
        return await handler(args, self._deps)
