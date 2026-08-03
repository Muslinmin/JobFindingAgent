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

**Attachments.** A handler that produces a *file* returns its narration
dict with an extra `ATTACHMENT_KEY` entry. `loop.py` pops that key before
the result is serialised into a tool message, so the model is told a file
exists and never sees the file itself; the popped record rides out on the
turn's `TurnResult` for `POST /chat` to encode. It carries a path, not
bytes — reading and base64-encoding is transport's job, and megabytes of
PDF have no business travelling through the reasoning loop.

**All ten tools are wired.** `ToolNotWiredError` survives as the shape the
next stub should take — an exception, never an `{ok:false}`, because the
model cannot route around a missing service.

**Three tools produce documents, and each delivers differently** — the
difference is what the user can actually do with the thing in a chat
window. `tailor_resume` ships a PDF as an attachment and tells the model
only that it exists; a PDF cannot be read in a message bubble.
`draft_cover_letter` returns the text (the user reviews it by reading it)
*and* persists it, since 400 words is worth keeping. `draft_followup`
returns text and persists nothing — a hundred-word nudge gets copy-pasted
straight out of the chat, and filing a copy of it would be bookkeeping for
its own sake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from agent import errors
from app.config import Settings
from app.models.enums import (
    ApplicationStatus,
    ArtifactKind,
    InvalidTransitionError,
    legal_targets,
)
from app.models.job import JobCreate
from app.services.artifacts import artifact_dir, store_artifact
from app.services.discovery import fan_out
from app.services.service import JobService, JobNotFoundError
from drafting.cover_letter import DraftingError
from drafting.cover_letter import draft_cover_letter as compose_cover_letter
from drafting.followup import draft_followup as compose_followup
from profile.loader import load_profile
from profile.mutate import update_profile as mutate_profile
from profile.schema import ProfileOp
from scheduler.jobs.query_regen import run_query_regen
from scoring.protocol import Scorer
from scraper.protocol import JobSource
from tailoring.tailor import TailoringError, tailor

_PROFILE_OP_ADAPTER: TypeAdapter[ProfileOp] = TypeAdapter(ProfileOp)

# The private key a file-producing handler smuggles its artifact out on.
# Underscore-prefixed so it cannot collide with a field the tool table
# promises the model, and popped by `loop.py` before serialisation.
ATTACHMENT_KEY = "_attachment"

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

# Statuses a follow-up email makes sense for. Drafting one for a job never
# applied to is incoherent — there is nothing to follow up on — and saying
# so is more useful than producing a letter about an event that never
# happened. GHOSTED is in: no reply for weeks is exactly when a nudge is
# worth sending.
_FOLLOW_UP_STATUSES = {
    ApplicationStatus.APPLIED,
    ApplicationStatus.INTERVIEWING,
    ApplicationStatus.GHOSTED,
}


class ToolNotWiredError(NotImplementedError):
    """A tool whose service does not exist yet. Deliberately an exception,
    not an `{ok:false}`: the model cannot route around a missing service,
    and a structured error would invite it to narrate the gap as a normal
    refusal.

    Nothing raises this today — all ten tools are wired. It stays as the
    shape the next stub should take, and as the thing
    `test_agent_handlers.py` asserts about that shape."""


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
    # The same adapter instances the scheduler holds, deliberately shared:
    # an adapter that caches a corpus (scraper/careers_gov_adapter.py) is
    # only worth caching once per process.
    adapters: list[JobSource]
    template_path: Path
    output_dir: Path


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


# ── discovery ─────────────────────────────────────────────────────────────────


async def search_jobs(args: dict, deps: AgentDeps) -> dict:
    """The daily scrape, on demand, for one query. It ingests — it does not
    preview: every result enters the pipeline and gets scored, because
    over-broad recall is what the scorer is for.

    `delay_s=0` where the scheduler passes `adapter_delay_s`. Politeness
    throttling exists to stop a batch of dozens of adapter calls hammering
    a portal; a single interactive query is one request, and the second it
    would sleep comes out of the user's turn deadline.

    Naming an unconfigured source is `not_found` rather than a silent
    fall-back to every source — searching somewhere other than where the
    user asked, and reporting it as success, is the kind of quiet
    substitution that makes the whole reply untrustworthy.
    """
    query = args["query"]
    limit = args.get("limit", 20)
    requested = args.get("sources")

    adapters = deps.adapters
    if requested:
        wanted = {name.lower() for name in requested}
        adapters = [a for a in deps.adapters if a.name.lower() in wanted]
        if not adapters:
            return errors.not_found()

    outcome = await fan_out(
        [query],
        adapters,
        deps.job_service,
        deps.scorer,
        deps.profile_path,
        deps.settings,
        delay_s=0,
        limit=limit,
    )

    return {
        "ok": True,
        "query": query,
        "sources": [a.name for a in adapters],
        "fetched": outcome.fetched,
        "ingested": outcome.ingested,
        "new": outcome.new,
        "scored": outcome.scored,
        "jobs": [_job_row(job) for job in outcome.records],
    }


# ── artifacts ─────────────────────────────────────────────────────────────────


async def tailor_resume(args: dict, deps: AgentDeps) -> dict:
    """Produce a tailored CV, store it, register it, and move the job to
    TAILORED.

    **It moves the FSM exactly one step, and no further.** Once the file
    exists, TAILORED is simply true — it is a fact about what the system
    produced, and it says nothing about the user having approved anything.
    Leaving the job at SCORED with a real PDF on disk made the record
    contradict reality, which is what sent a live user round three manual
    status moves to say "I applied". The scheduler's tailor job already
    makes this same move, so both paths now leave a tailored job in the
    same state.

    What it still must not do is reach PENDING_APPROVAL. That state means
    "the system produced a CV and is waiting for a human", which is a claim
    about the user, not about the file.

    The move is *proposed*, not pre-checked (invariant 3) — a re-tailor of
    a job already past SCORED comes back `InvalidTransitionError`, and the
    right answer there is to keep the artifact and leave the status alone,
    not to fail a tool that did its job. The returned `status` is whatever
    the record actually says afterwards.

    Only `guard_violation` is caught of the `TailoringError` reasons. It is
    the one the model can do something useful with — the truthfulness
    guards refused the draft, which is a fact about the *content* and worth
    relaying. The other three (`llm_call_failed`, `schema_invalid`,
    `render_failed`) are infrastructure breaking, so they raise and abort
    the turn rather than being narrated as if the CV were merely
    unavailable today.
    """
    job = await deps.job_service.get_job(args["job_id"])
    if job is None:
        return errors.not_found()

    profile = load_profile(deps.profile_path)

    try:
        result = await tailor(
            job.description,
            profile,
            llm=deps.llm,
            template_path=deps.template_path,
            output_dir=artifact_dir(deps.output_dir, job.id),
        )
    except TailoringError as e:
        if e.reason == "guard_violation":
            return errors.guard_violation("; ".join(e.violations) or "truthfulness guard")
        raise

    stored = await store_artifact(
        job, ArtifactKind.CV_PDF, result.path, deps.job_service, deps.output_dir
    )

    # Register first, then move. A crash between the two leaves a job whose
    # artifact exists but whose status lags, which the next tailor corrects;
    # the other order would claim TAILORED with nothing to show for it.
    status = job.status
    try:
        status = (
            await deps.job_service.transition_status(job.id, ApplicationStatus.TAILORED)
        ).status
    except InvalidTransitionError:
        logger.info(
            f"tailor_resume: job {job.id} is {job.status.value}, not SCORED — "
            "keeping the artifact and leaving the status alone"
        )

    return {
        "ok": True,
        "job_id": job.id,
        "artifact_id": stored.artifact_id,
        "kind": stored.kind.value,
        "status": status.value,
        "replaced": stored.replaced,
        ATTACHMENT_KEY: {
            "kind": stored.kind,
            "filename": stored.filename,
            "path": str(stored.path),
            "mime_type": "application/pdf",
        },
    }


# ── drafting ──────────────────────────────────────────────────────────────────


async def draft_followup(args: dict, deps: AgentDeps) -> dict:
    """Returns the email text for the model to relay, and persists nothing.

    Deliberately unlike `tailor_resume` and `draft_cover_letter`: a
    hundred-word nudge is read and copy-pasted out of the chat window, so
    an artifact row and a `.txt` on the server would be filing a copy of
    something the user already has. `ArtifactKind.FOLLOW_UP_EMAIL` stays
    unused until there is a reason to keep the history.

    **Drafting is not nudging.** No `status`, no `follow_up_count`, and in
    particular no `mark_follow_up_nudged` — that stamps "we reminded you",
    which belongs to the scheduler's push, not to a user who asked to see a
    draft.
    """
    job = await deps.job_service.get_job(args["job_id"])
    if job is None:
        return errors.not_found()

    if job.status not in _FOLLOW_UP_STATUSES:
        return errors.guard_violation(
            f"a follow-up needs a job that was applied to; this one is {job.status.value}"
        )

    draft = await compose_followup(
        job.role, job.company, job.status_changed_at, deps.llm, note=args.get("note")
    )

    return {
        "ok": True,
        "job_id": job.id,
        "role": job.role,
        "company": job.company,
        "draft": draft,
    }


async def draft_cover_letter(args: dict, deps: AgentDeps) -> dict:
    """Returns the letter text *and* persists it. Both, on every call.

    The text goes back so the model can show it — the user reviews a cover
    letter by reading it, and a redraft is just the next turn saying "too
    formal". The file is written every time because `store_artifact`
    already gives the review loop the right shape for free: a redraft
    replaces the live file and backs up the one it displaced, so the latest
    file is always the current draft and a rejected one demotes itself to a
    `.bak`. Nothing has to be withheld from the user and no separate
    "accept" step has to be recorded.

    Never moves the FSM — a cover letter is not an application event.
    """
    job = await deps.job_service.get_job(args["job_id"])
    if job is None:
        return errors.not_found()

    profile = load_profile(deps.profile_path)

    try:
        letter = await compose_cover_letter(
            job.description,
            profile,
            job.company,
            job.role,
            llm=deps.llm,
            note=args.get("note"),
        )
    except DraftingError as e:
        if e.reason == "guard_violation":
            return errors.guard_violation("; ".join(e.violations) or "truthfulness guard")
        raise

    # Written under the renderer-equivalent staging name, then moved to the
    # canonical one by `store_artifact` — the same two-step the PDF path
    # takes, so backup-on-replace behaves identically for both kinds.
    staged = artifact_dir(deps.output_dir, job.id) / "cover_letter.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(letter)

    stored = await store_artifact(
        job, ArtifactKind.COVER_LETTER, staged, deps.job_service, deps.output_dir
    )

    return {
        "ok": True,
        "job_id": job.id,
        "artifact_id": stored.artifact_id,
        "kind": stored.kind.value,
        "filename": stored.filename,
        "replaced": stored.replaced,
        "letter": letter,
    }


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
