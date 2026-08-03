"""WP-S2 — scrape + inline score job tests.

Mirrors scheduling_v2.md's WP-S2 test catalog. The pipeline these
`test_pipeline_*` / `test_score_*` cases exercise now lives in
`app/services/discovery.py` (it moved out of this job when `search_jobs`
needed the same behaviour — agent_v2.md §7), so they import `fan_out` from
there under its old local name and patch `discovery`'s `load_profile` and
`logger`. They stay in this file because the daily scrape is still the
behaviour being specified; `run_scrape`'s file-loading wrapper, which is
all that remains job-private, is covered separately at the end.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import ApplicationStatus
from app.models.job import Job, JobCreate
from app.services.discovery import fan_out as _fan_out
from scheduler.jobs.scrape import _load_queries, run_scrape


def _discovered_job(job_id: int = 1, description: str = "JD text") -> Job:
    return Job(
        id=job_id,
        fingerprint=f"fp{job_id}",
        company="Acme",
        role="Engineer",
        description=description,
        url=f"https://example.com/{job_id}",
        posted_at=None,
        metadata=None,
        status=ApplicationStatus.DISCOVERED,
        score=None,
        status_changed_at="2026-01-01T00:00:00+00:00",
        follow_up_count=0,
        last_follow_up_at=None,
        follow_up_nudge_at=None,
        seen_count=1,
        last_seen_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _job_create(company: str = "Acme", role: str = "Engineer") -> JobCreate:
    return JobCreate(company=company, role=role, description="JD text", url="https://example.com/1")


class _FakeAdapter:
    def __init__(self, name: str, result):
        self.name = name
        self._result = result
        self.calls: list[str] = []

    async def fetch(self, query: str):
        self.calls.append(query)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Settings:
    score_threshold = 7000


def _mock_scorer(score: int = 8000) -> AsyncMock:
    scorer = AsyncMock()
    scorer.score.return_value = score
    return scorer


def _mock_service(ingest_returns=None) -> AsyncMock:
    service = AsyncMock()
    if ingest_returns is not None:
        service.ingest_job.side_effect = ingest_returns
    else:
        service.ingest_job.return_value = _discovered_job()
    return service


PROFILE_PATH = Path("profile.json")


def _patch_load_profile():
    return patch("app.services.discovery.load_profile", return_value=MagicMock())


# ── fan-out ───────────────────────────────────────────────────────────────────

async def test_pipeline_fans_out_across_adapters():
    adapter_a = _FakeAdapter("a", [_job_create(), _job_create()])
    adapter_b = _FakeAdapter("b", [_job_create(), _job_create()])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile():
        await _fan_out(["engineer"], [adapter_a, adapter_b], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    assert adapter_a.calls == ["engineer"]
    assert adapter_b.calls == ["engineer"]
    assert service.ingest_job.call_count == 4


async def test_pipeline_fans_out_across_queries():
    adapter = _FakeAdapter("a", [_job_create(), _job_create()])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile():
        await _fan_out(["engineer", "analyst"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    assert adapter.calls == ["engineer", "analyst"]
    assert service.ingest_job.call_count == 4


async def test_pipeline_one_adapter_failure_does_not_abort_others():
    # loguru doesn't route through stdlib `logging`, so `caplog` can't see
    # it — patch the module's `logger` directly instead.
    adapter_a = _FakeAdapter("a", Exception("portal down"))
    adapter_b = _FakeAdapter("b", [_job_create(), _job_create()])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile(), patch("app.services.discovery.logger") as mock_logger:
        await _fan_out(["engineer"], [adapter_a, adapter_b], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    assert service.ingest_job.call_count == 2
    mock_logger.warning.assert_called_once()
    assert "failed for query" in mock_logger.warning.call_args[0][0]


async def test_pipeline_ingest_failure_does_not_abort_pipeline():
    adapter = _FakeAdapter("a", [_job_create(), _job_create(), _job_create()])
    service = _mock_service(ingest_returns=[Exception("db down"), _discovered_job(2), _discovered_job(3)])
    scorer = _mock_scorer()

    with _patch_load_profile(), patch("app.services.discovery.logger") as mock_logger:
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    assert service.ingest_job.call_count == 3
    assert any("ingest_job failed" in c.args[0] for c in mock_logger.warning.call_args_list)


async def test_pipeline_delay_between_requests():
    adapter = _FakeAdapter("a", [])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile(), patch("app.services.discovery.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await _fan_out(["engineer", "analyst"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=2.5)

    assert sleep.call_count == 2
    sleep.assert_called_with(2.5)


async def test_pipeline_empty_adapter_result():
    adapter = _FakeAdapter("a", [])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile(), patch("app.services.discovery.logger") as mock_logger:
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.ingest_job.assert_not_called()
    mock_logger.warning.assert_not_called()


async def test_pipeline_ingests_correct_jobcreate():
    jc = _job_create(company="Grab", role="Data Engineer")
    adapter = _FakeAdapter("a", [jc])
    service = _mock_service()
    scorer = _mock_scorer()

    with _patch_load_profile():
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.ingest_job.assert_called_once_with(jc)


# ── scoring gate ──────────────────────────────────────────────────────────────

async def test_score_at_or_above_threshold_transitions_to_scored():
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service(ingest_returns=[_discovered_job(1)])
    scorer = _mock_scorer(score=7000)

    with _patch_load_profile():
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.transition_status.assert_called_once_with(1, ApplicationStatus.SCORED, score=7000)


async def test_score_below_threshold_transitions_to_rejected():
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service(ingest_returns=[_discovered_job(1)])
    scorer = _mock_scorer(score=6999)

    with _patch_load_profile():
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.transition_status.assert_called_once_with(1, ApplicationStatus.REJECTED, score=6999)


async def test_duplicate_hit_is_not_scored():
    """A job that ingest_job resolves to something other than DISCOVERED
    (a repost/cross-portal duplicate bumping seen_count) must not be
    re-scored — it was scored on its first sighting."""
    dup = _discovered_job(1)
    dup.status = ApplicationStatus.SCORED
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service(ingest_returns=[dup])
    scorer = _mock_scorer()

    with _patch_load_profile():
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    scorer.score.assert_not_called()
    service.transition_status.assert_not_called()


async def test_scoring_failure_leaves_job_discovered():
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service(ingest_returns=[_discovered_job(1)])
    scorer = AsyncMock()
    scorer.score.side_effect = Exception("embeddings API down")

    with _patch_load_profile(), patch("app.services.discovery.logger") as mock_logger:
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.transition_status.assert_not_called()
    assert any("left DISCOVERED for retry" in c.args[0] for c in mock_logger.warning.call_args_list)


async def test_missing_profile_ingests_without_scoring():
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service(ingest_returns=[_discovered_job(1)])
    scorer = _mock_scorer()

    with patch("app.services.discovery.load_profile", side_effect=FileNotFoundError()), \
         patch("app.services.discovery.logger") as mock_logger:
        await _fan_out(["engineer"], [adapter], service, scorer, PROFILE_PATH, _Settings(), delay_s=0)

    service.ingest_job.assert_called_once()
    scorer.score.assert_not_called()
    service.transition_status.assert_not_called()
    assert any("ingesting this batch without scoring" in c.args[0] for c in mock_logger.warning.call_args_list)


# ── _load_queries ─────────────────────────────────────────────────────────────

def test_load_queries_missing_file_returns_none(tmp_path):
    with patch("scheduler.jobs.scrape.logger") as mock_logger:
        assert _load_queries(tmp_path / "nope.json") is None
    assert "not found" in mock_logger.warning.call_args[0][0]


def test_load_queries_invalid_json_returns_none(tmp_path):
    path = tmp_path / "search_queries.json"
    path.write_text("{not valid json")
    assert _load_queries(path) is None


def test_load_queries_not_a_list_returns_none(tmp_path):
    path = tmp_path / "search_queries.json"
    path.write_text(json.dumps({"not": "a list"}))
    assert _load_queries(path) is None


def test_load_queries_returns_list(tmp_path):
    path = tmp_path / "search_queries.json"
    path.write_text(json.dumps(["engineer", "analyst"]))
    assert _load_queries(path) == ["engineer", "analyst"]


# ── run_scrape wrapper ────────────────────────────────────────────────────────

async def test_run_scrape_reads_queries_file_each_run(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service()
    scorer = _mock_scorer()

    with patch("scheduler.jobs.scrape._load_queries", return_value=["engineer"]) as mock_load, \
         _patch_load_profile():
        await run_scrape(
            adapters=[adapter], service=service, scorer=scorer, settings=_Settings(),
            queries_path=queries_path, profile_path=PROFILE_PATH, delay_s=0,
        )

    mock_load.assert_called_once_with(queries_path)
    assert adapter.calls == ["engineer"]


async def test_run_scrape_missing_queries_file_returns():
    adapter = _FakeAdapter("a", [_job_create()])
    service = _mock_service()
    scorer = _mock_scorer()

    with patch("scheduler.jobs.scrape._load_queries", return_value=None):
        await run_scrape(
            adapters=[adapter], service=service, scorer=scorer, settings=_Settings(),
            queries_path=Path("nonexistent.json"), profile_path=PROFILE_PATH, delay_s=0,
        )

    assert adapter.calls == []
    service.ingest_job.assert_not_called()
