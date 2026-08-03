# Scraper Layer

**Read first:** `adapters.md` (adapter contract), `architecture_v2.md` (scraper layer section), `job.py` (canonical `JobCreate` schema).

The scraper layer has one active, wired-in adapter: **Careers@Gov (WP-S1)**. It reads Open Government Products' published open-data mirror of the Careers@Gov listings. (Earlier drafts also scoped MCF and JobStreet adapters; both are dropped — MCF unbuilt, JobStreet blocked.)

---

## Project invariants (non-negotiable)

1. **No reasoning in adapters.** Query, parse, normalise only. No LLM calls, no scoring, no relevance decisions.
2. **No DB access, no self-HTTP.** Adapters return `list[JobCreate]`.
3. **Fail soft.** Any error → `logger.warning` + return `[]`. An adapter must never crash the pipeline.
4. **Injectable HTTP client.** Constructor accepts `client: httpx.AsyncClient | None`. No real network in tests except `@pytest.mark.live`.
5. **Settings, not hardcoding.** URLs, delays, and cache TTLs live in pydantic-settings (`.env`), never hardcoded.
6. **`JobCreate` is the return type of `fetch()`.** Not `dict[str, Any]`. Pydantic validates at the boundary.
7. **TDD discipline.** Write tests before implementation. Red → Green → Refactor.

---

## WP-S1 — Careers@Gov adapter (built 2026-07-18)

**Source.** `CareersGovSource` (`scraper/careers_gov_adapter.py`) GETs OGP's public, MIT-licensed mirror at `careers_gov_data_url` (`app/config.py`, defaults to `raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json`) — a flat JSON array (~2,200 records) merging the `hrp`, `greenhouse`, and `workable` platforms behind jobs.careers.gov.sg. No credentials needed, unlike the earlier Algolia-based approach it replaced (which used devtools-lifted, never-authorised credentials and was held out of `main.py`). Trade-off accepted: freshness lags OGP's own scheduled scrape by some hours, acceptable for a daily/scheduled agent.

**Caching.** The corpus is fetched via `ETag`/`If-None-Match` conditional GET, cached on the instance; `304` reuses the cache with no re-parse, `200` refreshes it, and a network error falls back to the last good cache rather than failing the query. `careers_gov_cache_ttl_s` (default `0` = always revalidate) can skip even the conditional GET for a window.

**Filtering.** No server-side search exists, so the adapter holds the full corpus and applies a coarse AND-of-tokens recall gate: lower-case the query, split on whitespace, keep a record if every token is a substring of a blob built from `jobTitle` + `field` + `functionalArea` + the three description fields. A blank query returns the whole corpus.

**Field mapping → `JobCreate`.** `role` ← `jobTitle`; `company` ← `agency` (falls back to `"Singapore Public Service"` when blank); `description` ← `jobDescription`+`jobResponsibilities`+`jobRequirements` joined on blank lines with HTML stripped via a small stdlib `HTMLParser` extractor (no new dependency); `url` built per platform (`hrp`/`greenhouse`/`workable` patterns, unrecognised platforms skipped with a warning); `posted_at` ← `startDate` epoch-ms → ISO-8601; `metadata` carries `platform`, `closingDate(Text)`, `remainingDays`, `employmentType`, `workArrangement`, `experienceRequired(Min/Max)`, `field`, `functionalArea`, `industry`, `educationCode`, `category`, `location`, `isNew`.

**Wired in.** `app/main.py` builds `adapters: list[JobSource] = [CareersGovSource()]` — no longer held out.

**Tests.** 25 unit tests (`test/unit/test_careers_gov_adapter.py`, fixture `test/fixtures/careers_gov_listings.json`) covering filtering, URL construction per platform, HTML stripping, ETag/304 caching, stale-cache fallback, and fail-soft error paths. Live integration test (`test/integration/test_careers_gov_live.py`, `-m live`) needs no credentials and hits the real mirror.

**Live-verified.** Query `"robotics engineering"` against the live mirror returned 19 real matches (HTX RAUS/robotics roles, DSTA's AI-Robotics Engineer, IMDA's AMR/Robotics lead, etc.) with full job-description text and correct detail-page URLs.
