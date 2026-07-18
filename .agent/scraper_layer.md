# Scraper Layer — Implementation Playbook

**Audience:** an agent (or human) implementing the scraper layer of JobFindingAgent.

**Read first:** `adapters.md` (adapter contract), `architecture_v2.md` (scraper layer section), `job.py` (canonical `JobCreate` schema).

The scraper layer has one active adapter: **Careers@Gov (WP-S1)**, which reads Open Government Products' published open-data mirror of the Careers@Gov listings. (Earlier drafts also scoped MCF and JobStreet adapters; both have been dropped from this playbook.)

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

## WP-S1 — Careers@Gov adapter (OGP open-data mirror)

### The trade-off decision (2026-07-18)

The adapter reads the processed job dataset that Open Government Products publishes at `opengovsg/careersgovsg-jobs-data` (`data/job-listings.json`), rather than scraping jobs.careers.gov.sg or its Algolia search index directly. We chose the published mirror over live scraping for three reasons, accepting one cost.

- **No authorisation problem.** The earlier adapter used an Algolia `app_id`/`api_key` pair lifted from the site's frontend JavaScript, which was never issued through an authorised channel — that is why it was held out of `main.py`. A public, MIT-licensed data file needs no credentials and no token, so there is nothing to authorise and nothing to hold out.
- **Full job text.** The file carries `jobDescription`, `jobResponsibilities`, and `jobRequirements` in full, whereas the Algolia hits returned only a title. The scorer and tailoring layer now receive real job-description content.
- **No maintenance burden.** OGP's own GitHub Action tracks Careers@Gov's endpoints; if the source changes, their Action breaks and gets fixed, not our adapter.

The cost we accept is **freshness**: the file is only as current as OGP's last scheduled run, so it can lag the live site by some hours. For a scheduled agent this is acceptable. There is also a low secondary risk that OGP stops maintaining the repo, mitigated by it being official and git-versioned.

### The new approach

**Source.** A single HTTPS GET on `https://raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json` — no key, no OAuth, no GitHub token. The file is a JSON array of roughly 1,900 records merged across the `hrp`, `greenhouse`, and `workable` platforms behind Careers@Gov. Its field contract is documented at `.github/instructions/job-listings.instructions.md` in that repo.

**Query filtering lives in the adapter.** The `JobSource` protocol is `async fetch(self, query: str) -> list[JobCreate]`, and a flat file has no server-side search, so the adapter holds the full corpus and returns only the records matching the query, using its own self-contained logic with no LLM call and no external request. The rule is a coarse recall gate — fine relevance is the scorer's job downstream:

- Lower-case the query and split it on whitespace into tokens.
- Build one lower-cased blob per record from `jobTitle`, `field`, `functionalArea`, and the combined description text.
- Keep a record when *every* token appears as a substring in that blob (AND-of-tokens).
- A blank or whitespace-only query returns the whole corpus (a defined fallback, not an error).

**Fetch once per cycle.** `main.py` reuses a single `CareersGovSource` instance across the whole query fan-out, so download the corpus at most once per cycle and filter each query against the in-memory copy. Use an `ETag` / `If-None-Match` conditional GET: cache the parsed records and the ETag on the instance; a `304 Not Modified` reuses the cache with no re-download or re-parse; a `200` refreshes both. An optional max-age forces periodic revalidation for a long-lived process.

**Field mapping (flat-file record → `JobCreate`).** `JobCreate` is `company: str`, `role: str`, `description: str` (required), `url: str`, `posted_at: str | None`, `metadata: dict | None`, with `extra="forbid"`.

- `role` ← `jobTitle`
- `company` ← `agency`, falling back to `"Singapore Public Service"` when empty
- `description` ← `jobDescription` + `jobResponsibilities` + `jobRequirements`, joined with blank lines, empty parts skipped; an empty overall result stays `""` (the schema is `str`, not `str | None` — do not produce `None`)
- `url` ← per platform: `hrp` → `https://jobs.careers.gov.sg/jobs/hrp/{jobId}/{postingNo}`; `greenhouse` → `https://jobs.careers.gov.sg/jobs/greenhouse/{jobId}?gh_jid={jobId}`; `workable` → `https://apply.workable.com/j/{postingNo}`; an unrecognised platform skips the record with a `logger.warning`
- `posted_at` ← `startDate` (Unix milliseconds) → ISO-8601 string (reuse the existing epoch-milliseconds helper)
- `metadata` ← `platform`, `closingDate`, `closingDateText`, `remainingDays`, `employmentType`, `workArrangement`, `experienceRequired`, `experienceYearsMin`, `experienceYearsMax`, `field`, `functionalArea`, `industry`, `educationCode`, `category`, `location`, `isNew`

`greenhouse` and `workable` descriptions carry raw HTML, so strip HTML to plain text before assigning `description`. Entry-level is `experienceYearsMin == 0`; the adapter does not filter on it, but carrying the experience fields into `metadata` lets the scorer use them.

**Code touch-points.**

- `app/config.py`: remove the Algolia settings; add `careers_gov_data_url` (defaulting to the public raw URL — safe to commit) and optionally `careers_gov_cache_ttl_s`. No secret goes in `.env`.
- `scraper/careers_gov_adapter.py`: keep the class name `CareersGovSource` and `name = "careers_gov"`; replace the body with the fetch-cache-filter-map flow above. The old Algolia implementation, fixture, and tests are superseded.
- `app/main.py`: add `CareersGovSource` to the active `adapters` list and remove the holdout comment.