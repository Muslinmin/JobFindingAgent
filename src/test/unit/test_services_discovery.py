"""Discovery — the outcome struct the two callers read (agent_v2.md §7).

The pipeline mechanics (fan-out, per-adapter isolation, threshold gating,
politeness delay) are specified by `test/scheduler/test_scheduler_scrape.py`,
which has exercised them since WP-S2 and still does. What is new when this
logic became shared is `IngestOutcome`: the daily job only ever logged an
ingest count, but `search_jobs` has to *tell a user* which of "found
nothing", "found only things you already have", and "scoring is down"
happened. Those three are indistinguishable from a single integer, and this
file is what keeps them apart.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import ApplicationStatus
from app.models.job import Job, JobCreate
from app.services.discovery import ingest_and_score

T = "2026-07-22T00:00:00+00:00"
PROFILE_PATH = Path("profile.json")


class _Settings:
    score_threshold = 5000


def _job(id=1, status=ApplicationStatus.DISCOVERED, score=None):
    return Job(
        id=id, fingerprint=f"fp{id}", company="GovTech", role="Backend Engineer",
        description="a jd", url="https://example.com", posted_at=None, metadata=None,
        status=status, score=score, status_changed_at=T, follow_up_count=0,
        last_follow_up_at=None, follow_up_nudge_at=None, seen_count=1, last_seen_at=T,
        created_at=T, updated_at=T,
    )


def _job_create(company="GovTech"):
    return JobCreate(
        company=company, role="Backend Engineer", description="a jd", url="https://example.com"
    )


@pytest.fixture
def service():
    s = MagicMock()
    s.ingest_job = AsyncMock(return_value=_job())
    s.transition_status = AsyncMock(return_value=_job(status=ApplicationStatus.SCORED, score=8000))
    return s


@pytest.fixture
def scorer():
    return MagicMock(score=AsyncMock(return_value=8000))


@pytest.fixture(autouse=True)
def _profile():
    with patch("app.services.discovery.load_profile", return_value=MagicMock()):
        yield


async def test_nothing_fetched_is_reported_as_nothing_fetched(service, scorer):
    outcome = await ingest_and_score([], service, scorer, PROFILE_PATH, _Settings())
    assert (outcome.fetched, outcome.ingested, outcome.new, outcome.scored) == (0, 0, 0, 0)
    service.ingest_job.assert_not_called()


async def test_the_counts_narrow_at_each_stage(service, scorer):
    outcome = await ingest_and_score(
        [_job_create(), _job_create()], service, scorer, PROFILE_PATH, _Settings()
    )
    assert (outcome.fetched, outcome.ingested, outcome.new, outcome.scored) == (2, 2, 2, 2)


async def test_an_already_known_job_counts_as_ingested_but_not_new(service, scorer):
    """"I found five, you already had all five" is a different answer from
    "I found nothing", and only `new` tells them apart."""
    service.ingest_job.return_value = _job(status=ApplicationStatus.APPLIED, score=8000)

    outcome = await ingest_and_score([_job_create()], service, scorer, PROFILE_PATH, _Settings())

    assert (outcome.fetched, outcome.ingested, outcome.new, outcome.scored) == (1, 1, 0, 0)
    scorer.score.assert_not_called()


async def test_a_scoring_outage_shows_up_as_new_but_unscored(service, scorer):
    scorer.score.side_effect = RuntimeError("embedding API down")

    outcome = await ingest_and_score([_job_create()], service, scorer, PROFILE_PATH, _Settings())

    assert (outcome.new, outcome.scored) == (1, 0)
    assert outcome.records[0].status == ApplicationStatus.DISCOVERED


async def test_records_reflect_the_row_as_it_now_stands(service, scorer):
    """Reporting DISCOVERED/None after a successful transition would be a
    lie the moment the caller renders it."""
    outcome = await ingest_and_score([_job_create()], service, scorer, PROFILE_PATH, _Settings())

    assert len(outcome.records) == 1
    assert outcome.records[0].status == ApplicationStatus.SCORED
    assert outcome.records[0].score == 8000


async def test_a_failed_ingest_is_absent_from_the_records_entirely(service, scorer):
    service.ingest_job.side_effect = [RuntimeError("db down"), _job(id=2)]
    service.transition_status.side_effect = (
        lambda job_id, status, score=None: _job(id=job_id, status=status, score=score)
    )

    outcome = await ingest_and_score(
        [_job_create(), _job_create()], service, scorer, PROFILE_PATH, _Settings()
    )

    assert outcome.fetched == 2
    assert outcome.ingested == 1
    assert [r.id for r in outcome.records] == [2]
