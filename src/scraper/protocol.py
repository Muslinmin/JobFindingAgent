"""The JobSource contract (architecture_v2.md § Scraper Layer).

Says nothing about how a portal is fetched (httpx JSON, Algolia, HTML
parsing) — that's deliberate, so the scrape job (scheduler/jobs/scrape.py)
can fan out across every adapter identically. No reasoning, no DB writes,
no scoring, no deduplication happen at this layer or below it; a failing
adapter returns `[]` (or raises, caught by the caller — never crashes the
pipeline) rather than propagating.
"""

from __future__ import annotations

from typing import Protocol

from app.models.job import JobCreate


class JobSource(Protocol):
    name: str

    async def fetch(self, query: str) -> list[JobCreate]: ...
