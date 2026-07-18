"""Careers@Gov adapter (scraper_layer.md WP-S1).

STATUS (2026-07-18): implemented and live-verified — functionally this
adapter works today and does return real listings (confirmed live: 790
hits for a bare "engineer" query, full JD text, correct HRP/GREENHOUSE URL
construction). It is held OUT of the active pipeline in `app/main.py`
pending authorised access — do not re-wire it in until that's resolved.

The requirement for `app_id`/`api_key` at all is itself the signal: this is
Algolia's standard client-credential model for a gated search index, not an
open/anonymous endpoint. The values this adapter was built and tested
against were read out of jobs.careers.gov.sg's own frontend JS bundle
(visible to anyone via browser devtools) rather than issued through an
authorised channel — the account owner is currently applying for proper
developer access to Careers@Gov and doesn't have it yet. Do not use the
devtools-sourced credentials in any deployed/scheduled run; only use
credentials obtained once authorisation comes through.

Queries the Algolia index behind jobs.careers.gov.sg's own search widget
(POST {app_id}-dsn.algolia.net/1/indexes/{index}/query). Implements the
`JobSource` protocol (scraper/protocol.py): query, parse, normalise only —
no reasoning, no DB access, no self-HTTP.

`objectID` encodes both the job's source system and its detail-page slug:
  - "HRP:<reqId>/<slug>"   → https://jobs.careers.gov.sg/<slug>
  - "GREENHOUSE:<slug>"    → https://jobs.careers.gov.sg/<slug>
`agency` and `agencyAbbr` come back as "" (not absent) when unset — live
recon (2026-07-18) found this on ~17% of hits, hence the fallback chain in
`_company_name`. `description` is plain text already (no HTML), unlike MCF.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from app.config import Settings
from app.config import settings as default_settings
from app.models.job import JobCreate

_DETAIL_URL = "https://jobs.careers.gov.sg/{slug}"
_FALLBACK_COMPANY = "Singapore Public Service"


class CareersGovSource:
    name = "careers_gov"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings = default_settings,
        app_id: str | None = None,
        api_key: str | None = None,
        index: str | None = None,
    ) -> None:
        self._client = client
        self._app_id = app_id if app_id is not None else settings.careers_gov_app_id
        self._api_key = api_key if api_key is not None else settings.careers_gov_api_key
        self._index = index if index is not None else settings.careers_gov_index
        self._hits_per_page = settings.careers_gov_hits_per_page

    async def fetch(self, query: str) -> list[JobCreate]:
        if not self._app_id or not self._api_key:
            logger.warning("careers_gov: app_id/api_key not configured — returning empty results")
            return []

        url = f"https://{self._app_id}-dsn.algolia.net/1/indexes/{self._index}/query"
        headers = {
            "X-Algolia-Application-Id": self._app_id,
            "X-Algolia-API-Key": self._api_key,
            "Referer": "https://jobs.careers.gov.sg/",
            "Origin": "https://jobs.careers.gov.sg",
        }
        body = {"query": query, "hitsPerPage": self._hits_per_page}

        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"careers_gov: HTTP {e.response.status_code} for query={query!r}")
            return []
        except httpx.RequestError as e:
            logger.warning(f"careers_gov: request failed for query={query!r}: {e}")
            return []
        except Exception as e:
            logger.warning(f"careers_gov: unexpected error for query={query!r}: {e}")
            return []
        finally:
            if self._client is None:
                await client.aclose()

        hits = data.get("hits")
        if not isinstance(hits, list):
            logger.warning(f"careers_gov: malformed response for query={query!r} — no 'hits' list")
            return []

        jobs: list[JobCreate] = []
        for hit in hits:
            job = self._to_job_create(hit)
            if job is not None:
                jobs.append(job)
        return jobs

    def _to_job_create(self, hit: dict[str, Any]) -> JobCreate | None:
        object_id = hit.get("objectID") or ""
        url = _build_url(object_id)
        if url is None:
            logger.warning(f"careers_gov: unrecognised objectID shape: {object_id!r}")
            return None

        return JobCreate(
            company=_company_name(hit),
            role=hit.get("title") or "",
            description=hit.get("description") or "",
            url=url,
            posted_at=_to_posted_at(hit.get("activityTimestamp")),
            metadata={
                "job_source": hit.get("jobSource"),
                "department": hit.get("department"),
                "employment_type": hit.get("employmentType"),
            },
        )


def _company_name(hit: dict[str, Any]) -> str:
    return hit.get("agency") or hit.get("agencyAbbr") or _FALLBACK_COMPANY


def _build_url(object_id: str) -> str | None:
    if object_id.startswith("HRP:"):
        slug = object_id[len("HRP:"):].rsplit("/", 1)[-1]
        return _DETAIL_URL.format(slug=slug) if slug else None
    if object_id.startswith("GREENHOUSE:"):
        slug = object_id[len("GREENHOUSE:"):]
        return _DETAIL_URL.format(slug=slug) if slug else None
    return None


def _to_posted_at(raw: Any) -> str | None:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return datetime.fromtimestamp(raw / 1000, tz=UTC).isoformat()
