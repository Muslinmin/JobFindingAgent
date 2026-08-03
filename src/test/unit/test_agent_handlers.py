import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.handlers import ATTACHMENT_KEY, AgentDeps, ToolDispatcher, ToolNotWiredError
from app.config import Settings
from app.models.enums import ApplicationStatus, ArtifactKind, InvalidTransitionError
from app.models.job import Artifact, Job, JobCreate
from app.services.service import JobNotFoundError
from drafting.cover_letter import DraftingError
from profile.schema import Profile, ProfileItem, Skill
from tailoring.tailor import ArtifactResult, TailoringError

T = "2026-07-22T00:00:00+00:00"


def _job(id=1, role="Backend Engineer", company="GovTech", status=ApplicationStatus.SCORED,
         score=7000, seen_count=1, description="a jd"):
    return Job(
        id=id, fingerprint=f"fp{id}", company=company, role=role, description=description,
        url="https://example.com", posted_at=None, metadata=None, status=status, score=score,
        status_changed_at=T, follow_up_count=0, last_follow_up_at=None, follow_up_nudge_at=None,
        seen_count=seen_count, last_seen_at=T, created_at=T, updated_at=T,
    )


@pytest.fixture
def profile_path(tmp_path) -> Path:
    p = Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[ProfileItem(id="exp_1", title="Backend Engineer", bullets=["Did a thing."])],
        target_tracks=["backend engineering"],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(p.model_dump(mode="json"), indent=2))
    return path


@pytest.fixture
def queries_path(tmp_path) -> Path:
    return tmp_path / "search_queries.json"


@pytest.fixture
def job_service():
    s = MagicMock()
    s.find_jobs = AsyncMock(return_value=[])
    s.query_jobs = AsyncMock(return_value=[])
    s.get_job = AsyncMock(return_value=None)
    s.ingest_job = AsyncMock(return_value=_job())
    s.transition_status = AsyncMock(return_value=_job())
    return s


@pytest.fixture
def scorer():
    s = MagicMock()
    s.score = AsyncMock(return_value=8000)
    return s


@pytest.fixture
def llm():
    c = MagicMock()
    c.complete = AsyncMock(return_value='["backend engineer python"]')
    return c


class _FakeAdapter:
    """Minimal `JobSource`: a name and a canned fetch result. Enough for
    `search_jobs`, which only ever touches those two things."""

    def __init__(self, name, jobs=None, error=None):
        self.name = name
        self._jobs = jobs or []
        self._error = error
        self.calls = []

    async def fetch(self, query):
        self.calls.append(query)
        if self._error:
            raise self._error
        return list(self._jobs)


def _job_create(company="GovTech", role="Backend Engineer"):
    return JobCreate(
        company=company, role=role, description="a jd", url="https://example.com"
    )


@pytest.fixture
def adapters():
    return [_FakeAdapter("careers_gov", [_job_create()])]


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def deps(job_service, scorer, llm, profile_path, queries_path, adapters, tmp_path, output_dir):
    return AgentDeps(
        job_service=job_service,
        scorer=scorer,
        llm=llm,
        settings=Settings(score_threshold=5000),
        profile_path=profile_path,
        queries_path=queries_path,
        adapters=adapters,
        template_path=tmp_path / "cv.tex.jinja",
        output_dir=output_dir,
    )


@pytest.fixture
def dispatcher(deps):
    return ToolDispatcher(deps)


# ── find_jobs ─────────────────────────────────────────────────────────────────

async def test_find_jobs_named_lookup_calls_find_jobs(dispatcher, job_service):
    job_service.find_jobs.return_value = [_job()]
    await dispatcher.dispatch("find_jobs", {"job_title": "backend"})
    job_service.find_jobs.assert_called_once()
    job_service.query_jobs.assert_not_called()


async def test_find_jobs_named_lookup_defaults_to_all_statuses(dispatcher, job_service):
    """A named lookup must reach terminal statuses — the user may be
    recalling a job that has since been rejected (agent_v2.md §3)."""
    await dispatcher.dispatch("find_jobs", {"job_title": "backend"})
    assert job_service.find_jobs.call_args.kwargs["status_set"] is None


async def test_find_jobs_company_only_is_still_a_named_lookup(dispatcher, job_service):
    await dispatcher.dispatch("find_jobs", {"company": "PUB"})
    job_service.find_jobs.assert_called_once()
    assert job_service.find_jobs.call_args.kwargs["company"] == "PUB"
    assert job_service.find_jobs.call_args.kwargs["job_title"] == ""


async def test_find_jobs_bare_listing_excludes_terminal_statuses(dispatcher, job_service):
    await dispatcher.dispatch("find_jobs", {})
    job_service.query_jobs.assert_called_once()
    status_set = job_service.query_jobs.call_args.kwargs["status_set"]
    assert ApplicationStatus.REJECTED not in status_set
    assert ApplicationStatus.APPLIED in status_set


async def test_find_jobs_explicit_status_set_overrides_the_default(dispatcher, job_service):
    await dispatcher.dispatch("find_jobs", {"status_set": ["rejected"]})
    assert job_service.query_jobs.call_args.kwargs["status_set"] == {ApplicationStatus.REJECTED}


async def test_find_jobs_projects_rows_without_the_description(dispatcher, job_service):
    job_service.find_jobs.return_value = [_job(description="a very long jd " * 500)]
    rows = await dispatcher.dispatch("find_jobs", {"job_title": "backend"})
    assert rows == [{
        "id": 1, "role": "Backend Engineer", "company": "GovTech",
        "status": "scored", "score": 7000, "status_changed_at": T,
    }]


async def test_find_jobs_empty_result_is_valid_not_an_error(dispatcher):
    assert await dispatcher.dispatch("find_jobs", {"job_title": "nothing"}) == []


# ── score_job ─────────────────────────────────────────────────────────────────

async def test_score_job_returns_the_score_and_writes_nothing(dispatcher, scorer, job_service):
    result = await dispatcher.dispatch("score_job", {"description": "a jd"})
    assert result == {"ok": True, "score": 8000}
    job_service.ingest_job.assert_not_called()
    job_service.transition_status.assert_not_called()


async def test_score_job_maps_scorer_failure_to_embedding_unavailable(dispatcher, scorer):
    scorer.score.side_effect = RuntimeError("embedding API down")
    result = await dispatcher.dispatch("score_job", {"description": "a jd"})
    assert result == {"ok": False, "error": "embedding_unavailable"}


# ── score_ingest ──────────────────────────────────────────────────────────────

async def test_score_ingest_scores_and_transitions_a_new_job(dispatcher, job_service, scorer):
    job_service.ingest_job.return_value = _job(score=None, status=ApplicationStatus.DISCOVERED)
    job_service.transition_status.return_value = _job(score=8000, status=ApplicationStatus.SCORED)

    result = await dispatcher.dispatch(
        "score_ingest", {"company": "GovTech", "title": "Backend Engineer", "description": "a jd"}
    )

    scorer.score.assert_called_once()
    assert job_service.transition_status.call_args.args[1] == ApplicationStatus.SCORED
    assert result["was_scored"] is True
    assert result["score"] == 8000


async def test_score_ingest_rejects_below_threshold(dispatcher, job_service, scorer):
    job_service.ingest_job.return_value = _job(score=None, status=ApplicationStatus.DISCOVERED)
    scorer.score.return_value = 100
    await dispatcher.dispatch(
        "score_ingest", {"company": "X", "title": "Y", "description": "a jd"}
    )
    assert job_service.transition_status.call_args.args[1] == ApplicationStatus.REJECTED


async def test_score_ingest_skips_rescoring_an_already_scored_job(dispatcher, job_service, scorer):
    """Never recompute a stored score — that spends embedding credits to
    recreate a value the system promised to keep (scoring_v2.md)."""
    job_service.ingest_job.return_value = _job(score=7000, seen_count=3)

    result = await dispatcher.dispatch(
        "score_ingest", {"company": "GovTech", "title": "Backend Engineer", "description": "a jd"}
    )

    scorer.score.assert_not_called()
    job_service.transition_status.assert_not_called()
    assert result["was_scored"] is False
    assert result["score"] == 7000
    assert result["seen_count"] == 3


async def test_score_ingest_reports_seen_count_so_the_model_knows_it_existed(dispatcher, job_service):
    job_service.ingest_job.return_value = _job(score=None, seen_count=1)
    job_service.transition_status.return_value = _job(score=8000, seen_count=1)
    result = await dispatcher.dispatch(
        "score_ingest", {"company": "X", "title": "Y", "description": "a jd"}
    )
    assert result["seen_count"] == 1  # 1 == created just now


async def test_score_ingest_maps_scorer_failure_to_embedding_unavailable(dispatcher, job_service, scorer):
    job_service.ingest_job.return_value = _job(score=None)
    scorer.score.side_effect = RuntimeError("down")
    result = await dispatcher.dispatch(
        "score_ingest", {"company": "X", "title": "Y", "description": "a jd"}
    )
    assert result["error"] == "embedding_unavailable"


# ── update_status ─────────────────────────────────────────────────────────────

async def test_update_status_transitions_and_reports_both_states(dispatcher, job_service):
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    job_service.transition_status.return_value = _job(status=ApplicationStatus.INTERVIEWING)

    result = await dispatcher.dispatch(
        "update_status", {"job_id": 1, "new_status": "interviewing"}
    )

    assert result == {
        "ok": True, "job_id": 1, "role": "Backend Engineer", "company": "GovTech",
        "old_status": "applied", "new_status": "interviewing",
    }


async def test_update_status_missing_job_is_not_found(dispatcher, job_service):
    job_service.get_job.return_value = None
    result = await dispatcher.dispatch("update_status", {"job_id": 99, "new_status": "offer"})
    assert result == {"ok": False, "error": "not_found"}
    job_service.transition_status.assert_not_called()


async def test_update_status_race_deleted_job_is_not_found(dispatcher, job_service):
    """Deleted between the pre-read and the write — the service is the
    authority, so its verdict still wins."""
    job_service.get_job.return_value = _job()
    job_service.transition_status.side_effect = JobNotFoundError("gone")
    result = await dispatcher.dispatch("update_status", {"job_id": 1, "new_status": "tailored"})
    assert result == {"ok": False, "error": "not_found"}


async def test_update_status_illegal_transition_carries_from_to_allowed(dispatcher, job_service):
    job_service.get_job.return_value = _job(status=ApplicationStatus.REJECTED)
    job_service.transition_status.side_effect = InvalidTransitionError("nope")

    result = await dispatcher.dispatch("update_status", {"job_id": 1, "new_status": "offer"})

    assert result["ok"] is False
    assert result["error"] == "illegal_transition"
    assert result["from"] == "rejected"
    assert result["to"] == "offer"
    assert result["allowed"] == []  # REJECTED is terminal


async def test_update_status_allowed_list_comes_from_the_fsm(dispatcher, job_service):
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    job_service.transition_status.side_effect = InvalidTransitionError("nope")
    result = await dispatcher.dispatch("update_status", {"job_id": 1, "new_status": "offer"})
    assert set(result["allowed"]) == {"interviewing", "rejected", "ghosted", "declined"}


# ── regenerate_queries ────────────────────────────────────────────────────────

async def test_regenerate_queries_returns_count_and_preview(dispatcher, queries_path, llm):
    llm.complete.return_value = json.dumps([f"query {i}" for i in range(8)])
    result = await dispatcher.dispatch("regenerate_queries", {})
    assert result["ok"] is True
    assert result["count"] == 8
    assert len(result["queries_preview"]) == 5


async def test_regenerate_queries_declines_on_an_empty_profile(dispatcher, deps, tmp_path, llm):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(Profile(name="", email="").model_dump(mode="json")))
    deps.profile_path = empty

    result = await dispatcher.dispatch("regenerate_queries", {})

    assert result == {"ok": False, "error": "profile_empty"}
    llm.complete.assert_not_called()


# ── update_profile ────────────────────────────────────────────────────────────

async def test_update_profile_unconfirmed_returns_a_diff_and_writes_nothing(dispatcher, profile_path):
    before = profile_path.read_text()

    result = await dispatcher.dispatch(
        "update_profile", {"op": "add_target_track", "track": "robotics QA"}
    )

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["pending_confirmation"] is True
    assert "robotics QA" in result["diff"]
    assert profile_path.read_text() == before


async def test_update_profile_confirmed_commits(dispatcher, profile_path):
    result = await dispatcher.dispatch(
        "update_profile", {"op": "add_target_track", "track": "robotics QA", "confirmed": True}
    )
    assert result["ok"] is True
    assert result["changed"] is True
    assert "robotics QA" in profile_path.read_text()


async def test_update_profile_reports_when_queries_go_stale(dispatcher):
    result = await dispatcher.dispatch(
        "update_profile", {"op": "add_target_track", "track": "robotics QA", "confirmed": True}
    )
    assert result["queries_stale"] is True


async def test_update_profile_takes_the_typed_op_not_a_free_form_patch(dispatcher):
    """An append op must not be expressible as a whole-field replacement —
    that is the ambiguity profile/schema.py's discriminated union exists to
    remove."""
    result = await dispatcher.dispatch(
        "update_profile", {"target_tracks": ["robotics QA"], "confirmed": True}
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_patch"


async def test_update_profile_unknown_op_is_invalid_patch(dispatcher):
    result = await dispatcher.dispatch("update_profile", {"op": "delete_everything"})
    assert result["error"] == "invalid_patch"


async def test_update_profile_missing_required_field_is_invalid_patch(dispatcher):
    result = await dispatcher.dispatch("update_profile", {"op": "add_target_track"})
    assert result["error"] == "invalid_patch"


async def test_update_profile_invariant_violation_is_invalid_patch(dispatcher):
    """add_skill naming an item id that doesn't exist is rejected before the
    file is touched."""
    result = await dispatcher.dispatch(
        "update_profile",
        {
            "op": "add_skill",
            "skill": {"id": "rust", "label": "Rust"},
            "demonstrated_by": ["exp_does_not_exist"],
            "confirmed": True,
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_patch"


# ── dispatch plumbing ─────────────────────────────────────────────────────────

async def test_dispatch_routes_by_name(dispatcher, job_service):
    await dispatcher.dispatch("find_jobs", {"job_title": "x"})
    job_service.find_jobs.assert_called_once()


async def test_dispatch_unknown_tool_is_data_not_a_crash(dispatcher):
    assert await dispatcher.dispatch("no_such_tool", {}) == {"ok": False, "error": "not_found"}


async def test_dispatch_table_covers_every_published_schema(dispatcher):
    from agent.handlers import _HANDLERS
    from agent.schemas import TOOL_SCHEMAS

    published = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert published == set(_HANDLERS)


def test_the_unwired_shape_is_an_exception_not_a_structured_error():
    """Nothing raises this now that all ten are wired, but the next stub
    must take this shape: the model cannot route around a missing service,
    and an {ok:false} would invite it to narrate the gap as a normal
    refusal — telling the user their resume 'couldn't be tailored right
    now', which is a lie about why."""
    assert issubclass(ToolNotWiredError, Exception)
    assert not issubclass(ToolNotWiredError, dict)


# ── draft_followup ────────────────────────────────────────────────────────────


async def test_draft_followup_returns_the_text_for_the_user_to_copy(dispatcher, job_service, llm):
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    llm.complete.return_value = "Hi, following up on my application..."

    result = await dispatcher.dispatch("draft_followup", {"job_id": 1})

    assert result["ok"] is True
    assert result["draft"] == "Hi, following up on my application..."
    assert (result["role"], result["company"]) == ("Backend Engineer", "GovTech")


async def test_draft_followup_persists_nothing(dispatcher, job_service):
    """A hundred-word nudge is copy-pasted out of the chat window; filing a
    copy of it is bookkeeping for its own sake."""
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    job_service.register_artifact = AsyncMock()

    await dispatcher.dispatch("draft_followup", {"job_id": 1})

    job_service.register_artifact.assert_not_called()


async def test_draft_followup_is_not_nudging(dispatcher, job_service):
    """`mark_follow_up_nudged` stamps 'we reminded you' — that belongs to
    the scheduler's push, not to a user who asked to see a draft."""
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    job_service.mark_follow_up_nudged = AsyncMock()

    await dispatcher.dispatch("draft_followup", {"job_id": 1})

    job_service.mark_follow_up_nudged.assert_not_called()
    job_service.transition_status.assert_not_called()


@pytest.mark.parametrize(
    "status", [ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEWING, ApplicationStatus.GHOSTED]
)
async def test_draft_followup_allows_every_status_a_nudge_makes_sense_for(
    dispatcher, job_service, status
):
    job_service.get_job.return_value = _job(status=status)
    assert (await dispatcher.dispatch("draft_followup", {"job_id": 1}))["ok"] is True


async def test_draft_followup_refuses_a_job_never_applied_to(dispatcher, job_service, llm):
    """There is nothing to follow up on, and saying so beats writing a
    letter about an event that never happened."""
    job_service.get_job.return_value = _job(status=ApplicationStatus.SCORED)

    result = await dispatcher.dispatch("draft_followup", {"job_id": 1})

    assert result["ok"] is False
    assert result["error"] == "guard_violation"
    assert "scored" in result["guard"]
    llm.complete.assert_not_called()


async def test_draft_followup_forwards_the_users_note_into_the_prompt(dispatcher, job_service, llm):
    job_service.get_job.return_value = _job(status=ApplicationStatus.APPLIED)

    await dispatcher.dispatch(
        "draft_followup", {"job_id": 1, "note": "mention I shipped the robotics project"}
    )

    assert "mention I shipped the robotics project" in llm.complete.await_args.args[0]


async def test_draft_followup_missing_job_is_not_found(dispatcher, job_service):
    job_service.get_job.return_value = None
    assert await dispatcher.dispatch("draft_followup", {"job_id": 99}) == {
        "ok": False,
        "error": "not_found",
    }


# ── draft_cover_letter ────────────────────────────────────────────────────────

_LETTER = "I am applying to the Backend Engineer role at GovTech. I did a thing."


async def test_draft_cover_letter_returns_the_text_and_saves_it(
    dispatcher, job_service, llm, output_dir
):
    """Both, on every call: the text so the user can review it, the file
    because 400 words is worth keeping."""
    job_service.get_job.return_value = _job()
    job_service.register_artifact = AsyncMock(
        return_value=Artifact(id=9, job_id=1, kind=ArtifactKind.COVER_LETTER, path="x", created_at=T)
    )
    llm.complete.return_value = _LETTER

    result = await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})

    assert result["ok"] is True
    assert result["letter"] == _LETTER
    assert result["artifact_id"] == 9
    assert result["filename"] == "govtech_backend-engineer_cover_letter.txt"

    saved = Path(job_service.register_artifact.await_args.args[1].path)
    assert saved.read_text() == _LETTER


async def test_draft_cover_letter_does_not_move_the_fsm(dispatcher, job_service, llm):
    job_service.get_job.return_value = _job()
    job_service.register_artifact = AsyncMock(
        return_value=Artifact(id=9, job_id=1, kind=ArtifactKind.COVER_LETTER, path="x", created_at=T)
    )
    llm.complete.return_value = _LETTER

    await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})

    job_service.transition_status.assert_not_called()


async def test_a_redraft_replaces_the_live_file_and_backs_up_the_rejected_one(
    dispatcher, job_service, llm, output_dir
):
    """The review loop needs no accept step: the latest file is always the
    current draft, and a rejected one demotes itself to a .bak."""
    job_service.get_job.return_value = _job()
    job_service.register_artifact = AsyncMock(
        return_value=Artifact(id=9, job_id=1, kind=ArtifactKind.COVER_LETTER, path="x", created_at=T)
    )

    llm.complete.return_value = _LETTER
    await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})

    llm.complete.return_value = _LETTER + " Warmly."
    second = await dispatcher.dispatch("draft_cover_letter", {"job_id": 1, "note": "warmer"})

    assert second["replaced"] is True
    live = output_dir / "1" / "govtech_backend-engineer_cover_letter.txt"
    assert live.read_text() == _LETTER + " Warmly."
    backups = list(live.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == _LETTER


async def test_draft_cover_letter_guard_violation_is_data_the_model_relays(
    dispatcher, job_service, llm
):
    """The letter claimed something the profile doesn't support. That is a
    fact about the content, and the model should say so rather than hand
    over a letter the user would have to fact-check."""
    job_service.get_job.return_value = _job()
    llm.complete.return_value = "I led a team of 12 engineers at Google using Kubernetes."

    result = await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})

    assert result["ok"] is False
    assert result["error"] == "guard_violation"
    assert result["guard"]


async def test_draft_cover_letter_retries_before_giving_up(dispatcher, job_service, llm):
    """A letter is one cheap call; refusing outright when the next pass
    very likely gets it right is the wrong trade."""
    job_service.get_job.return_value = _job()
    job_service.register_artifact = AsyncMock(
        return_value=Artifact(id=9, job_id=1, kind=ArtifactKind.COVER_LETTER, path="x", created_at=T)
    )
    llm.complete.side_effect = ["I led 12 engineers at Google.", _LETTER]

    result = await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})

    assert result["ok"] is True
    assert llm.complete.await_count == 2


async def test_draft_cover_letter_llm_failure_aborts_the_turn(dispatcher, job_service, llm):
    job_service.get_job.return_value = _job()
    llm.complete.side_effect = RuntimeError("rate limited")

    with pytest.raises(DraftingError):
        await dispatcher.dispatch("draft_cover_letter", {"job_id": 1})


async def test_draft_cover_letter_missing_job_is_not_found(dispatcher, job_service):
    job_service.get_job.return_value = None
    assert await dispatcher.dispatch("draft_cover_letter", {"job_id": 99}) == {
        "ok": False,
        "error": "not_found",
    }


# ── search_jobs ───────────────────────────────────────────────────────────────


async def test_search_jobs_ingests_and_reports_the_pipeline_counts(dispatcher, job_service, scorer):
    job_service.ingest_job.return_value = _job(status=ApplicationStatus.DISCOVERED, score=None)
    job_service.transition_status.return_value = _job(status=ApplicationStatus.SCORED, score=8000)

    result = await dispatcher.dispatch("search_jobs", {"query": "backend engineer"})

    assert result["ok"] is True
    assert (result["fetched"], result["ingested"], result["new"], result["scored"]) == (1, 1, 1, 1)
    assert result["sources"] == ["careers_gov"]
    scorer.score.assert_awaited_once()


async def test_search_jobs_returns_rows_without_the_description(dispatcher, job_service):
    """Same projection as find_jobs: the description is the one field big
    enough to blow the context window and the model has no use for it."""
    job_service.ingest_job.return_value = _job(status=ApplicationStatus.DISCOVERED, score=None)
    job_service.transition_status.return_value = _job(status=ApplicationStatus.SCORED, score=8000)

    result = await dispatcher.dispatch("search_jobs", {"query": "x"})

    assert result["jobs"] == [
        {"id": 1, "role": "Backend Engineer", "company": "GovTech",
         "status": "scored", "score": 8000, "status_changed_at": T}
    ]


async def test_search_jobs_narrows_to_the_named_sources(deps, adapters):
    other = _FakeAdapter("mcf", [_job_create(company="Grab")])
    deps.adapters = [*adapters, other]

    result = await ToolDispatcher(deps).dispatch("search_jobs", {"query": "x", "sources": ["mcf"]})

    assert result["sources"] == ["mcf"]
    assert adapters[0].calls == []
    assert other.calls == ["x"]


async def test_search_jobs_unknown_source_is_not_found_not_a_silent_fallback(dispatcher, adapters):
    """Searching somewhere other than where the user asked, and reporting it
    as success, is the quiet substitution that makes a reply untrustworthy."""
    result = await dispatcher.dispatch("search_jobs", {"query": "x", "sources": ["linkedin"]})

    assert result == {"ok": False, "error": "not_found"}
    assert adapters[0].calls == []


async def test_search_jobs_limit_caps_what_is_ingested(deps, job_service):
    deps.adapters = [_FakeAdapter("careers_gov", [_job_create() for _ in range(5)])]

    result = await ToolDispatcher(deps).dispatch("search_jobs", {"query": "x", "limit": 2})

    assert result["fetched"] == 2
    assert job_service.ingest_job.await_count == 2


async def test_search_jobs_does_not_stall_the_turn_on_politeness_delay(deps):
    """The scheduler's `adapter_delay_s` exists for a batch of dozens of
    calls; one interactive query must not spend a second of the turn
    deadline sleeping."""
    with patch("app.services.discovery.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await ToolDispatcher(deps).dispatch("search_jobs", {"query": "x"})

    sleep.assert_not_called()


# ── tailor_resume ─────────────────────────────────────────────────────────────


@pytest.fixture
def tailorable(job_service):
    """A job to tailor, and a register_artifact that returns a real row."""
    job_service.get_job.return_value = _job()
    job_service.register_artifact = AsyncMock(
        return_value=Artifact(id=7, job_id=1, kind=ArtifactKind.CV_PDF, path="x", created_at=T)
    )
    return job_service


def _renders(body=b"%PDF-1.4 fake"):
    """Stands in for `tailor()`, doing what it does from the caller's point
    of view: leave a PDF under the renderer's own `cv.pdf` name in the
    output dir it was handed, and return an `ArtifactResult` naming it."""

    async def fake_tailor(job_description, profile, **kwargs):
        directory = Path(kwargs["output_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cv.pdf").write_bytes(body)
        return ArtifactResult(kind="cv_pdf", path=directory / "cv.pdf")

    return fake_tailor


async def test_tailor_resume_registers_the_artifact_under_a_readable_name(
    dispatcher, tailorable, output_dir
):
    with patch("agent.handlers.tailor", new=_renders()):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    assert result["ok"] is True
    assert result["artifact_id"] == 7
    assert result["kind"] == "cv_pdf"
    assert result["replaced"] is False

    registered = tailorable.register_artifact.await_args.args[1]
    assert Path(registered.path).name == "govtech_backend-engineer_cv_pdf.pdf"
    assert Path(registered.path).exists()


async def test_tailor_resume_moves_the_job_to_tailored(dispatcher, tailorable, output_dir):
    """Once the file exists, TAILORED is simply true. Leaving the job at
    SCORED with a real PDF on disk made the record contradict reality."""
    tailorable.transition_status.return_value = _job(status=ApplicationStatus.TAILORED)

    with patch("agent.handlers.tailor", new=_renders(b"%PDF")):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    assert tailorable.transition_status.await_args.args[1] == ApplicationStatus.TAILORED
    assert result["status"] == "tailored"


async def test_tailor_resume_never_reaches_pending_approval(dispatcher, tailorable):
    """That state claims the user has been asked. Producing a file is not
    asking, and only the user's own acceptance may set it."""
    tailorable.transition_status.return_value = _job(status=ApplicationStatus.TAILORED)

    with patch("agent.handlers.tailor", new=_renders(b"%PDF")):
        await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    proposed = [c.args[1] for c in tailorable.transition_status.await_args_list]
    assert ApplicationStatus.PENDING_APPROVAL not in proposed


async def test_a_retailor_keeps_the_artifact_when_the_move_is_illegal(
    dispatcher, tailorable, output_dir
):
    """Re-tailoring a job already past SCORED must not fail a tool that did
    its job — keep the file, leave the status alone."""
    tailorable.get_job.return_value = _job(status=ApplicationStatus.APPLIED)
    tailorable.transition_status.side_effect = InvalidTransitionError("no")

    with patch("agent.handlers.tailor", new=_renders(b"%PDF")):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    assert result["ok"] is True
    assert result["artifact_id"] == 7
    assert result["status"] == "applied"  # unchanged, and honestly reported


async def test_tailor_resume_backs_up_the_previous_cv_rather_than_overwriting(
    dispatcher, tailorable, output_dir
):
    live = output_dir / "1" / "govtech_backend-engineer_cv_pdf.pdf"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"the old one")

    with patch("agent.handlers.tailor", new=_renders(b"the new one")):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    assert result["replaced"] is True
    assert live.read_bytes() == b"the new one"
    backups = list(live.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"the old one"


async def test_tailor_resume_carries_the_pdf_out_of_band(dispatcher, tailorable, output_dir):
    """The model is told a file exists; it never sees the file. The bytes
    ride out on the attachment channel that `loop.py` pops."""

    with patch("agent.handlers.tailor", new=_renders(b"%PDF")):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    attachment = result[ATTACHMENT_KEY]
    assert attachment["filename"] == "govtech_backend-engineer_cv_pdf.pdf"
    assert attachment["mime_type"] == "application/pdf"
    assert attachment["kind"] is ArtifactKind.CV_PDF
    assert Path(attachment["path"]).read_bytes() == b"%PDF"


async def test_tailor_resume_missing_job_is_not_found(dispatcher, job_service):
    job_service.get_job.return_value = None
    assert await dispatcher.dispatch("tailor_resume", {"job_id": 99}) == {
        "ok": False,
        "error": "not_found",
    }


async def test_tailor_resume_guard_violation_is_data_the_model_relays(dispatcher, tailorable):
    """A refused draft is a fact about the content, which the model can
    usefully relay — unlike the other three TailoringError reasons."""
    with patch(
        "agent.handlers.tailor",
        side_effect=TailoringError("guard_violation", violations=["exp_1: unauthorized skill 'k8s'"]),
    ):
        result = await dispatcher.dispatch("tailor_resume", {"job_id": 1})

    assert result["ok"] is False
    assert result["error"] == "guard_violation"
    assert "k8s" in result["guard"]


@pytest.mark.parametrize("reason", ["llm_call_failed", "schema_invalid", "render_failed"])
async def test_tailor_resume_infrastructure_failure_aborts_the_turn(dispatcher, tailorable, reason):
    """Narrating a broken renderer as 'your CV isn't available right now'
    would be a lie about why."""
    with patch("agent.handlers.tailor", side_effect=TailoringError(reason)):
        with pytest.raises(TailoringError):
            await dispatcher.dispatch("tailor_resume", {"job_id": 1})


async def test_unexpected_service_failure_propagates(dispatcher, job_service):
    """Errors-as-data covers the expected-failure column only; a service
    blowing up must abort the loop, not become a result."""
    job_service.find_jobs.side_effect = RuntimeError("database is on fire")
    with pytest.raises(RuntimeError):
        await dispatcher.dispatch("find_jobs", {"job_title": "x"})
