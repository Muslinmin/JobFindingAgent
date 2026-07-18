"""
Unit tests for scraper/careers_gov_adapter.py — CareersGovSource.

Mocks: injected httpx.AsyncClient. Fixture: fixtures/careers_gov_listings.json
(5 records shaped like the OGP `careersgovsg-jobs-data` mirror — 1 hrp with
agency, 1 hrp with blank agency + empty description fields, 1 greenhouse
with HTML jobDescription, 1 workable with HTML jobDescription/jobRequirements,
1 unrecognised platform).

Note: `JobCreate.description` is `str` (not `str | None`) — empty
descriptions normalise to "" here, not None.
"""

import json
from pathlib import Path

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.models.job import JobCreate
from scraper.careers_gov_adapter import CareersGovSource

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "careers_gov_listings.json"


def _load_fixture() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


def _mock_response(payload, status_code: int = 200, etag: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.headers = {"ETag": etag} if etag else {}
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


def _mock_client(response=None, side_effect=None):
    client = AsyncMock()
    if side_effect is not None:
        client.get = AsyncMock(side_effect=side_effect)
    else:
        client.get = AsyncMock(return_value=response)
    return client


def _settings(**overrides) -> Settings:
    base = {"careers_gov_data_url": "https://example.test/job-listings.json", "careers_gov_cache_ttl_s": 0}
    base.update(overrides)
    return Settings(**base)


def _source(client, **settings_overrides) -> CareersGovSource:
    return CareersGovSource(client=client, settings=_settings(**settings_overrides))


# ── happy path ──────────────────────────────────────────────────────────────

async def test_careers_gov_happy_path():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    assert source.name == "careers_gov"
    assert all(isinstance(r, JobCreate) for r in result)
    # 4 valid platforms map to JobCreate; the "some_future_platform" record is skipped
    assert len(result) == 4


async def test_careers_gov_posted_at_parses_as_tz_aware_datetime():
    from datetime import datetime

    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    hrp_job = next(r for r in result if r.role == "Assistant Manager / Manager, Systems Management")
    parsed = datetime.fromisoformat(hrp_job.posted_at)
    assert parsed.tzinfo is not None


# ── query filtering ─────────────────────────────────────────────────────────

async def test_careers_gov_blank_query_returns_whole_corpus():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("   ")

    assert len(result) == 4


async def test_careers_gov_filters_by_and_of_tokens():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("robotics engineering")

    roles = {r.role for r in result}
    assert "Assistant Manager / Manager, Systems Management" in roles
    assert "Robotics Software Engineer" in roles
    assert "Policy Analyst, Public Service Division" not in roles


async def test_careers_gov_filter_is_case_insensitive():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("ROBOTICS")

    assert len(result) == 2


# ── URL construction ─────────────────────────────────────────────────────────

async def test_careers_gov_url_construction_hrp():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    hrp_job = next(r for r in result if r.role == "Assistant Manager / Manager, Systems Management")
    assert hrp_job.url == "https://jobs.careers.gov.sg/jobs/hrp/17643725/005056a3-d347-1fe1-a0b5-423ec76162ad"


async def test_careers_gov_url_construction_greenhouse():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    gh_job = next(r for r in result if r.role == "Robotics Software Engineer")
    assert gh_job.url == "https://jobs.careers.gov.sg/jobs/greenhouse/4004392201?gh_jid=4004392201"


async def test_careers_gov_url_construction_workable():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    wk_job = next(r for r in result if r.role == "Policy Analyst, Public Service Division")
    assert wk_job.url == "https://apply.workable.com/j/8FCA50DA02"


async def test_careers_gov_unrecognised_platform_is_skipped():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    assert all(r.role != "Mystery Role" for r in result)


# ── field normalisation ────────────────────────────────────────────────────

async def test_careers_gov_description_empty_becomes_empty_string():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    empty_desc_job = next(r for r in result if r.role == "Manager (Supply & Accounting) - Jurong")
    assert empty_desc_job.description == ""


async def test_careers_gov_company_falls_back_when_agency_blank():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    fallback_job = next(r for r in result if r.role == "Manager (Supply & Accounting) - Jurong")
    assert fallback_job.company == "Singapore Public Service"


async def test_careers_gov_description_joins_fields_with_blank_lines():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    hrp_job = next(r for r in result if r.role == "Assistant Manager / Manager, Systems Management")
    assert "robotics engineering lead" in hrp_job.description
    assert "Manage robotics systems" in hrp_job.description
    assert "Degree in engineering" in hrp_job.description


async def test_careers_gov_strips_html_from_description():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    gh_job = next(r for r in result if r.role == "Robotics Software Engineer")
    assert "<" not in gh_job.description
    assert "GovTech is hiring a robotics engineering specialist." in gh_job.description
    assert "Build robots" in gh_job.description


async def test_careers_gov_metadata_carries_expected_fields():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("")

    hrp_job = next(r for r in result if r.role == "Assistant Manager / Manager, Systems Management")
    assert hrp_job.metadata["platform"] == "hrp"
    assert hrp_job.metadata["experienceYearsMin"] == 4
    assert hrp_job.metadata["field"] == "Science, Engineering & Technical"
    assert "posted_at" not in hrp_job.metadata


# ── ETag / caching behaviour ────────────────────────────────────────────────

async def test_careers_gov_second_fetch_sends_if_none_match_and_reuses_cache_on_304():
    fixture = _load_fixture()
    first_resp = _mock_response(fixture, etag='"abc123"')
    second_resp = _mock_response(None, status_code=304)
    client = _mock_client()
    client.get = AsyncMock(side_effect=[first_resp, second_resp])
    source = _source(client)

    first = await source.fetch("")
    second = await source.fetch("")

    assert len(first) == len(second) == 4
    assert client.get.call_count == 2
    second_call_headers = client.get.call_args_list[1].kwargs.get("headers")
    assert second_call_headers == {"If-None-Match": '"abc123"'}


async def test_careers_gov_200_on_second_fetch_refreshes_cache():
    fixture = _load_fixture()
    updated = [fixture[0]]
    client = _mock_client()
    client.get = AsyncMock(
        side_effect=[_mock_response(fixture, etag='"v1"'), _mock_response(updated, etag='"v2"')]
    )
    source = _source(client)

    await source.fetch("")
    second = await source.fetch("")

    assert len(second) == 1


async def test_careers_gov_cache_ttl_skips_network_call_within_window():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture, etag='"abc"'))
    source = _source(client, careers_gov_cache_ttl_s=3600)

    await source.fetch("")
    await source.fetch("")

    assert client.get.call_count == 1


# ── empty / malformed / error handling ─────────────────────────────────────

async def test_careers_gov_empty_corpus():
    client = _mock_client(response=_mock_response([]))
    source = _source(client)

    result = await source.fetch("engineer")

    assert result == []


async def test_careers_gov_malformed_payload_not_a_list():
    client = _mock_client(response=_mock_response({"unexpected": "shape"}))
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_called_once()
    assert "careers_gov" in mock_logger.warning.call_args[0][0]


async def test_careers_gov_http_error_returns_empty_when_no_cache():
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
    )
    client = _mock_client(response=resp)
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_called_once()
    assert "careers_gov" in mock_logger.warning.call_args[0][0]


async def test_careers_gov_http_error_falls_back_to_stale_cache():
    fixture = _load_fixture()
    error_resp = MagicMock()
    error_resp.status_code = 500
    error_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=error_resp)
    )
    client = _mock_client()
    client.get = AsyncMock(side_effect=[_mock_response(fixture, etag='"v1"'), error_resp])
    source = _source(client)

    first = await source.fetch("")
    second = await source.fetch("")

    assert len(first) == len(second) == 4


async def test_careers_gov_request_error():
    client = _mock_client(side_effect=httpx.RequestError("connection refused"))
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_called_once()


async def test_careers_gov_never_raises():
    client = _mock_client(side_effect=RuntimeError("boom"))
    source = _source(client)
    try:
        result = await source.fetch("engineer")
    except Exception:
        pytest.fail("fetch() raised an exception — it must never raise")
    assert result == []


# ── unconfigured data url ────────────────────────────────────────────────────

async def test_careers_gov_returns_empty_when_not_configured():
    source = _source(AsyncMock(), careers_gov_data_url="")
    result = await source.fetch("engineer")
    assert result == []


async def test_careers_gov_does_not_call_client_when_not_configured():
    client = _mock_client(response=_mock_response([]))
    source = _source(client, careers_gov_data_url="")
    await source.fetch("engineer")
    client.get.assert_not_called()
