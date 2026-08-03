"""Careers@Gov adapter (scraper_layer.md WP-S1).

Reads Open Government Products' published open-data mirror of the
Careers@Gov listings — a single JSON array covering the `hrp`,
`greenhouse`, and `workable` platforms behind jobs.careers.gov.sg — rather
than scraping the live site or its (unauthorised) Algolia index. See
scraper_layer.md for the full trade-off rationale.

Implements the `JobSource` protocol (scraper/protocol.py): query, parse,
normalise only — no reasoning, no DB access, no self-HTTP beyond the one
conditional GET against the mirror.

The mirror has no server-side search, so this adapter holds the full
corpus in memory and filters it per query (coarse AND-of-tokens recall
gate; fine relevance is the scorer's job downstream). The corpus is
fetched at most once per process lifetime via an ETag conditional GET —
cheap 304s on every call after the first — with an optional max-age
(`careers_gov_cache_ttl_s`) to skip even that round-trip for a while.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx
from loguru import logger

from app.config import Settings
from app.config import settings as default_settings
from app.models.job import JobCreate

_FALLBACK_COMPANY = "Singapore Public Service"

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class CareersGovSource:
    name = "careers_gov"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings = default_settings,
    ) -> None:
        self._client = client
        self._data_url = settings.careers_gov_data_url
        self._cache_ttl_s = settings.careers_gov_cache_ttl_s

        self._records: list[dict[str, Any]] | None = None
        self._etag: str | None = None
        self._cached_at: float | None = None

    async def fetch(self, query: str) -> list[JobCreate]:
        try:
            records = await self._get_corpus()
        except Exception as e:
            logger.warning(f"careers_gov: unexpected error fetching corpus: {e}")
            records = self._records or []

        jobs: list[JobCreate] = []
        for record in _filter_records(records, query):
            job = _to_job_create(record)
            if job is not None:
                jobs.append(job)
        return jobs

    async def _get_corpus(self) -> list[dict[str, Any]]:
        if not self._data_url:
            logger.warning("careers_gov: careers_gov_data_url not configured — returning empty results")
            return []

        if (
            self._records is not None
            and self._cache_ttl_s > 0
            and self._cached_at is not None
            and time.monotonic() - self._cached_at < self._cache_ttl_s
        ):
            return self._records

        headers = {"If-None-Match": self._etag} if self._etag else {}
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.get(self._data_url, headers=headers)

            if response.status_code == 304 and self._records is not None:
                self._cached_at = time.monotonic()
                return self._records

            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                logger.warning("careers_gov: malformed corpus response — expected a JSON list")
                return self._records or []

            self._records = data
            self._etag = response.headers.get("ETag")
            self._cached_at = time.monotonic()
            return self._records
        except httpx.HTTPStatusError as e:
            logger.warning(f"careers_gov: HTTP {e.response.status_code} fetching corpus")
            return self._records or []
        except httpx.RequestError as e:
            logger.warning(f"careers_gov: request failed fetching corpus: {e}")
            return self._records or []
        finally:
            if self._client is None:
                await client.aclose()


def _filter_records(records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    tokens = query.lower().split()
    if not tokens:
        return records
    return [r for r in records if all(token in _blob(r) for token in tokens)]


def _blob(record: dict[str, Any]) -> str:
    parts = (
        record.get("jobTitle"),
        record.get("field"),
        record.get("functionalArea"),
        record.get("jobDescription"),
        record.get("jobResponsibilities"),
        record.get("jobRequirements"),
    )
    return " ".join(p for p in parts if p).lower()


def _to_job_create(record: dict[str, Any]) -> JobCreate | None:
    platform = record.get("platform") or ""
    url = _build_url(platform, record)
    if url is None:
        logger.warning(f"careers_gov: unrecognised platform {platform!r} — skipping record")
        return None

    return JobCreate(
        company=record.get("agency") or _FALLBACK_COMPANY,
        role=record.get("jobTitle") or "",
        description=_build_description(record),
        url=url,
        posted_at=_to_posted_at(record.get("startDate")),
        metadata={
            "platform": platform,
            "closingDate": record.get("closingDate"),
            "closingDateText": record.get("closingDateText"),
            "remainingDays": record.get("remainingDays"),
            "employmentType": record.get("employmentType"),
            "workArrangement": record.get("workArrangement"),
            "experienceRequired": record.get("experienceRequired"),
            "experienceYearsMin": record.get("experienceYearsMin"),
            "experienceYearsMax": record.get("experienceYearsMax"),
            "field": record.get("field"),
            "functionalArea": record.get("functionalArea"),
            "industry": record.get("industry"),
            "educationCode": record.get("educationCode"),
            "category": record.get("category"),
            "location": record.get("location"),
            "isNew": record.get("isNew"),
        },
    )


def _build_url(platform: str, record: dict[str, Any]) -> str | None:
    job_id = record.get("jobId") or ""
    if platform == "hrp":
        posting_no = record.get("postingNo") or ""
        return f"https://jobs.careers.gov.sg/jobs/hrp/{job_id}/{posting_no}"
    if platform == "greenhouse":
        return f"https://jobs.careers.gov.sg/jobs/greenhouse/{job_id}?gh_jid={job_id}"
    if platform == "workable":
        posting_no = record.get("postingNo") or ""
        return f"https://apply.workable.com/j/{posting_no}"
    return None


def _build_description(record: dict[str, Any]) -> str:
    parts = (
        record.get("jobDescription"),
        record.get("jobResponsibilities"),
        record.get("jobRequirements"),
    )
    non_empty = [p.strip() for p in parts if p and p.strip()]
    return _strip_html("\n\n".join(non_empty))


def _to_posted_at(raw: Any) -> str | None:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return datetime.fromtimestamp(raw / 1000, tz=UTC).isoformat()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def text(self) -> str:
        return "".join(self._chunks)


def _strip_html(raw: str) -> str:
    if "<" not in raw:
        return raw.strip()

    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return raw.strip()

    lines = (line.strip() for line in unescape(parser.text()).splitlines())
    return "\n".join(line for line in lines if line)
