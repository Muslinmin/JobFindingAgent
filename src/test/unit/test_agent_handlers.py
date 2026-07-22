import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.handlers import AgentDeps, ToolDispatcher, ToolNotWiredError
from app.config import Settings
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.models.job import Job
from app.services.service import JobNotFoundError
from profile.schema import Profile, ProfileItem, Skill

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


@pytest.fixture
def deps(job_service, scorer, llm, profile_path, queries_path):
    return AgentDeps(
        job_service=job_service,
        scorer=scorer,
        llm=llm,
        settings=Settings(score_threshold=5000),
        profile_path=profile_path,
        queries_path=queries_path,
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


@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_jobs", {"query": "backend engineer"}),
        ("tailor_resume", {"job_id": 1}),
        ("draft_followup", {"job_id": 1}),
        ("draft_cover_letter", {"job_id": 1}),
    ],
)
async def test_unwired_tools_raise_rather_than_returning_a_plausible_result(dispatcher, tool, args):
    """These four have no service yet. Raising aborts the turn; returning an
    {ok:false} would invite the model to narrate the gap as a normal refusal
    and tell the user their resume 'could not be tailored right now'."""
    with pytest.raises(ToolNotWiredError):
        await dispatcher.dispatch(tool, args)


async def test_unexpected_service_failure_propagates(dispatcher, job_service):
    """Errors-as-data covers the expected-failure column only; a service
    blowing up must abort the loop, not become a result."""
    job_service.find_jobs.side_effect = RuntimeError("database is on fire")
    with pytest.raises(RuntimeError):
        await dispatcher.dispatch("find_jobs", {"job_title": "x"})
