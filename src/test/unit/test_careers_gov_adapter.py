"""
Unit tests for scraper/careers_gov_adapter.py — CareersGovSource.

Mocks: injected httpx.AsyncClient. Fixture: fixtures/careers_gov_response.json
(4 real hits captured live 2026-07-18 via the same Algolia endpoint — 1 HRP
with agency, 1 GREENHOUSE, 1 HRP with agency/agencyAbbr == "" (fallback
case), 1 HRP with description == "" (empty-description case)).

Note: `JobCreate.description` is `str` (not `str | None`) — empty
descriptions normalise to "" here, not None.
"""

import json
from pathlib import Path

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.job import JobCreate
from scraper.careers_gov_adapter import CareersGovSource

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "careers_gov_response.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _mock_response(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


def _mock_client(response=None, side_effect=None):
    client = AsyncMock()
    if side_effect is not None:
        client.post = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=response)
    return client


def _source(client) -> CareersGovSource:
    return CareersGovSource(client=client, app_id="test-app-id", api_key="test-api-key", index="job_index")


# ── happy path ──────────────────────────────────────────────────────────────

async def test_careers_gov_happy_path():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    assert all(isinstance(r, JobCreate) for r in result)
    assert len(result) == len(fixture["hits"])

    first = fixture["hits"][0]
    assert result[0].company == first["agency"]
    assert result[0].role == first["title"]
    assert source.name == "careers_gov"
    assert result[0].posted_at is not None
    assert "posted_at" not in (result[0].metadata or {})


async def test_careers_gov_posted_at_parses_as_tz_aware_datetime():
    from datetime import datetime

    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    parsed = datetime.fromisoformat(result[0].posted_at)
    assert parsed.tzinfo is not None


# ── URL construction ──────────────────────────────────────────────────────────

async def test_careers_gov_url_construction_hrp():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    hrp_job = next(r for r in result if "005056a3-d347-1fe1-a0b4-f9af7c1d62ad" in r.url)
    assert hrp_job.url == "https://jobs.careers.gov.sg/005056a3-d347-1fe1-a0b4-f9af7c1d62ad"


async def test_careers_gov_url_construction_greenhouse():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    gh_job = next(r for r in result if r.metadata and r.metadata.get("job_source") == "GREENHOUSE")
    assert gh_job.url == "https://jobs.careers.gov.sg/4004392201"


async def test_careers_gov_unrecognised_object_id_is_skipped():
    fixture = _load_fixture()
    fixture["hits"] = [{**fixture["hits"][0], "objectID": "SOMETHING_ELSE:123"}]
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    assert result == []


# ── field normalisation ────────────────────────────────────────────────────────

async def test_careers_gov_description_empty_becomes_empty_string():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    empty_desc_job = next(r for r in result if r.role == "Assistant Engineer (Facilities)")
    assert empty_desc_job.description == ""


async def test_careers_gov_company_falls_back_when_agency_and_abbr_blank():
    fixture = _load_fixture()
    client = _mock_client(response=_mock_response(fixture))
    source = _source(client)

    result = await source.fetch("engineer")

    fallback_job = next(r for r in result if r.role == "Manager (Supply & Accounting) - Jurong")
    assert fallback_job.company == "Singapore Public Service"


# ── empty / malformed / error handling ─────────────────────────────────────────

async def test_careers_gov_empty_hits():
    client = _mock_client(response=_mock_response({"hits": [], "nbHits": 0, "page": 0, "nbPages": 0}))
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_not_called()


async def test_careers_gov_malformed_payload():
    client = _mock_client(response=_mock_response({"unexpected": "shape"}))
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_called_once()
    assert "careers_gov" in mock_logger.warning.call_args[0][0]


async def test_careers_gov_http_error():
    resp = MagicMock()
    resp.status_code = 403
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
    )
    client = _mock_client(response=resp)
    source = _source(client)

    with patch("scraper.careers_gov_adapter.logger") as mock_logger:
        result = await source.fetch("engineer")

    assert result == []
    mock_logger.warning.assert_called_once()
    assert "careers_gov" in mock_logger.warning.call_args[0][0]


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


# ── unconfigured credentials ────────────────────────────────────────────────────

async def test_careers_gov_returns_empty_when_not_configured():
    source = CareersGovSource(client=AsyncMock(), app_id="", api_key="", index="job_index")
    result = await source.fetch("engineer")
    assert result == []


async def test_careers_gov_does_not_call_client_when_not_configured():
    client = _mock_client(response=_mock_response({"hits": []}))
    source = CareersGovSource(client=client, app_id="", api_key="", index="job_index")
    await source.fetch("engineer")
    client.post.assert_not_called()
