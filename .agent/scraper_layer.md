# Scraper Layer — Implementation Playbook

> ⚠️ **NOT ADVISABLE AS-IS.** Everything below — reverse-engineering an
> Algolia key out of a site's frontend JS (Careers@Gov), hitting an
> undocumented `/v2/search` + per-job `/v2/jobs/{uuid}` endpoint (MCF), and
> the now-abandoned attempt against a Cloudflare-gated, robots.txt-disallowed
> site (JobStreet) — is scraping against endpoints that were never published
> for third-party programmatic use. Singapore's official developer guide for
> government data, **https://guide.data.gov.sg**, describes the sanctioned
> path: register for an **API key** through data.gov.sg's own Dataset/
> Real-Time API program (`developer-guide/api-overview`,
> `how-to-request-an-api-key`) and consume data through that, not by scraping
> a portal's frontend. Careers@Gov is already held out of the active
> pipeline for exactly this reason — see WP-S1 below — and none of this
> should be treated as a template for how to reach other `.gov.sg` data.
> Before building further on this file, check whether the target data is
> available through data.gov.sg's own API catalogue first.

**Audience:** an agent (or human) implementing the scraper layer of JobFindingAgent end to end.

**Read first:** `adapters.md` (adapter contract and recon playbook), `architecture_v2.md` (scraper layer section), `careers_gov_adapter.py` (reference implementation), `job.py` (canonical `JobCreate` schema).

---

## Overview

The scraper layer has four work packages, executed in dependency order:

| WP | Description | Status |
|---|---|---|
| WP-S1 | Careers@Gov adapter — finalise and test | Built + live-verified (2026-07-18), but **held out of `main.py`'s active adapters pending authorised developer access** — application in progress, do not re-wire until resolved |
| WP-S2 | MCF adapter — implement from scratch | Recon in progress (2026-07-18) |
| WP-S3 | JobStreet adapter — recon then implement | Recon complete (2026-07-18) — **blocked, do not implement** |


---

## Project invariants (non-negotiable for all WPs)

1. **No reasoning in adapters.** Query, parse, normalise only. No LLM calls, no scoring, no relevance decisions.
2. **No DB access, no self-HTTP.** Adapters return `list[JobCreate]`; 
3. **Fail soft.** Any error → `logger.warning` + return `[]`. An adapter must never crash
4. **Injectable HTTP client.** Constructor accepts `client: httpx.AsyncClient | None`. No real network in tests except `@pytest.mark.live`.
5. **Secrets in settings.** All API keys, app IDs, index names, delays, page sizes go in pydantic-settings (`.env`). Never hardcoded.
6. **`JobCreate` is the return type of `fetch()`.** Not `dict[str, Any]`. Pydantic validates at the boundary.
7. **TDD discipline.** Write tests before implementation. Red → Green → Refactor.

---

## WP-S1 — Careers@Gov adapter finalisation

### Status (2026-07-18): implemented and live-verified, but held out of the pipeline

**Access note (2026-07-18):** the credentials this adapter was built and live-tested against were read out of jobs.careers.gov.sg's own frontend JS bundle (devtools-visible), not issued through an authorised channel. Requiring `app_id`/`api_key` at all is itself a signal that this is a gated Algolia index, not an anonymous one. The account owner is currently applying for proper Careers@Gov developer access and doesn't have it yet — `main.py` does NOT include `CareersGovSource` in its active adapters list, and `.env`'s `CAREERS_GOV_APP_ID`/`CAREERS_GOV_API_KEY` are intentionally blank. **Do not wire this adapter back in, or paste devtools-sourced credentials into `.env`, until authorisation comes through.** The code and tests are otherwise complete and ready to go the moment real credentials exist.

**Correction to the original doc:** `careers_gov_adapter.py` was found to be a one-off recon script (module-level `httpx.post()` that ran on import, hardcoded `APP_ID`/`API_KEY`, no class, no `fetch()`, no `to_job_create()`) — not the "code exists, needs alignment" state this section originally described. Recon itself was solid (endpoint, headers, index name confirmed live), so this ended up being a from-scratch implementation rather than a fixup.

**Schema deviation:** `JobCreate.description` (`app/models/job.py`) is `str`, not `str | None` — it always has been. The original instruction to normalise `description=""` → `None` can't work against the current schema, and changing `description` to `str | None` would ripple into `scorer.score(job.description, ...)` (scheduler/jobs/scrape.py) and the tailoring renderer, both of which assume a string. Rather than touch the shared schema for this adapter, empty descriptions normalise to `""` (a no-op), not `None`. Revisit only if another adapter also needs `None` and the schema change is made deliberately, with the downstream callers updated too.

Everything else — Algolia endpoint/headers/index, HRP/GREENHOUSE `objectID` → detail-URL construction, `agency → agencyAbbr → "Singapore Public Service"` company fallback, `posted_at` as a top-level field (epoch-ms `activityTimestamp` → ISO-8601 string, kept out of `metadata`) — matches the original spec.

### What was built

- `app/config.py`: `careers_gov_app_id`, `careers_gov_api_key`, `careers_gov_index`, `careers_gov_hits_per_page` settings (all empty/default until put in `.env`; adapter returns `[]` and logs a warning if unconfigured, same pattern as `tavily_client.py`)
- `scraper/careers_gov_adapter.py`: `CareersGovSource` class implementing the `JobSource` protocol — `name`, constructor (`client`/`settings`/optional credential overrides), `async fetch(query) -> list[JobCreate]`, `_to_job_create()`
- `src/test/fixtures/careers_gov_response.json` — 4 real hits captured live 2026-07-18 (1 HRP with agency, 1 GREENHOUSE, 1 HRP with `agency`/`agencyAbbr` both `""`, 1 HRP with `description` == `""`)
- `src/test/unit/test_careers_gov_adapter.py` — 14 tests, all passing
- `src/test/integration/test_careers_gov_live.py` — `@pytest.mark.live` smoke test, passing against the real endpoint (skips automatically if credentials aren't set)

Note on paths: this repo's actual test convention is `src/test/unit/` and `src/test/integration/` (see `test_tavily_client.py` / `test_tavily_live.py`), not the `tests/unit/` / `tests/fixtures/` paths named in the original spec below — the file layout section at the bottom of this doc has been corrected to match.

### Definition of done

- [x] All unit tests pass (14/14)
- [x] `mypy` reports no errors on the adapter module
- [x] `@pytest.mark.live` smoke test passes against the real endpoint
- [x] `architecture_v2.md` adapter table updated with live-test date and verdict

---

## WP-S2 — MCF adapter

### Recon complete (2026-07-18)

Confirmed live, no auth, no special headers required (tested with a bare `curl -X POST`, no `User-Agent`/`Content-Type` even needed, though sending `Content-Type: application/json` is obviously correct practice). `robots.txt` on `www.mycareersfuture.gov.sg` is wide open (`Disallow:` empty, no `anthropic-ai` block, unlike JobStreet); `api.mycareersfuture.gov.sg/robots.txt` 404s, which is normal for a pure API host.

- **Search**: `POST https://api.mycareersfuture.gov.sg/v2/search`, JSON body `{"search": "<query>"}`. No auth.
- **Pagination is via query string, not the JSON body**: `?limit=20&page=N` (confirmed from the response's own `_links.next/self/first/last` HATEOAS URLs). `page`/`limit` keys inside the POST body are silently ignored — a body of `{"search": "...", "page": 1}` returns page 0 every time. `limit` appears fixed at 20 regardless of what's requested.
- Envelope: `{"_links", "searchRankingId", "results", "total", "countWithoutFilters"}`. `results` is the hit list.

**⚠️ Correction to `architecture_v2.md`'s 2026-06-10 finding — this is the important one:** search results do **NOT** include `description`. A `/v2/search` hit's keys are `address, categories, employmentTypes, flexibleWorkArrangements, hiringCompany, job_role_score, metadata, positionLevels, postedCompany, recency_score, retriever_score, salary, schemes, score, shiftPattern, skills, skills_match_score, status, title, title_match_score, uuid` — no `description` anywhere. The full HTML JD only exists behind a **separate detail call**: `GET https://api.mycareersfuture.gov.sg/v2/jobs/{uuid}`, confirmed live (returns `description` as HTML, e.g. `<p>...</p><ul><li>...`). **This means `McfSource.fetch()` cannot be a single request per query — it's 1 search call + up to `limit` detail calls (one per hit) to get real JD text.** That's a real design fork someone needs to decide before implementing (see below), not a "~50 lines of httpx" adapter as originally estimated.

- **Company**: `postedCompany` (name/UEN) is always present — usually the actual employer, but when a recruitment agency posts on someone's behalf, `postedCompany` is the *agency* and `hiringCompany` (present, non-null in that case) is the real end employer. Fallback chain should be `hiringCompany.name → postedCompany.name` (agency is the fallback, not the primary), confirmed live via a real agency-posted listing (Hansing Recruitment posting for Newland Payment Technology).
- **`posted_at` candidate**: `metadata.newPostingDate` (date-only, e.g. `"2026-07-14"`) — `metadata.updatedAt` also exists (`"2026-07-14T02:46:01"`, no timezone marker) but reads as a last-modified timestamp, not the original posting date. Recommend `newPostingDate`.
- **Canonical URL**: `metadata.jobDetailsUrl` confirmed — full absolute URL, use directly, don't construct.
- **`positionLevels[]`** confirmed as `[{"id": int, "position": str}]`; observed values so far: `Professional`, `Junior Executive`, `Senior Executive`, `Executive` (didn't happen to see "Fresh/entry level" in this sample, but the field shape is confirmed).
- Fixtures captured: `src/test/fixtures/mcf_search_response.json` (3 real hits — 2 normal + 1 agency-posted with `hiringCompany` populated) and `src/test/fixtures/mcf_detail_response.json` (2 real detail responses keyed `normal`/`agency_posted`, both with real HTML `description`).

### Open design question before implementing (not decided — flagging for whoever builds this)

Given description requires a per-hit detail call: does `fetch(query)` (a) make all N detail calls inline before returning (simple, but `hitsPerPage` × HTTP round-trips per query, needs its own delay/throttle beyond the existing per-adapter `delay_s`), or (b) return search-level `JobCreate`s with a short/no description and accept weaker JD text (cheap, matches the old v1 Tavily limitation this whole rewrite was meant to fix), or (c) some capped/batched middle ground? This wasn't resolved during recon — worth a decision before WP-S2 implementation starts.

### Implementation tasks

1. Define Pydantic response models (`McfHit`, `McfSearchResponse`) with `extra="ignore"` and field aliases matching the raw payload exactly. Include a `description` property that strips HTML.
2. Implement `McfSource`: `name = "mcf"`, constructor (settings-injected config + optional client), `async fetch()`, `to_job_create()`.
3. `to_job_create()` must:
   - Set `url` from `metadata.jobDetailsUrl` (canonical, not constructed)
   - Strip HTML from `description` (use `html.parser` or `BeautifulSoup` — pick one and pin it)
   - Set `posted_at` as a top-level `JobCreate` field (not in `metadata`)
   - Put salary range, `positionLevels`, `employmentTypes`, UEN into `metadata`
4. Write tests (see specifications below)

### Test specifications (still a shell — implementation not started; resolve the open design question above first)

File: `src/test/unit/test_mcf_adapter.py` (repo convention — not `tests/unit/`). Fixtures already captured: `src/test/fixtures/mcf_search_response.json`, `src/test/fixtures/mcf_detail_response.json`.

```
test_mcf_happy_path
  Load mcf_search_response.json (+ mcf_detail_response.json if the detail-call design is chosen)
  Instantiate McfSource with mock client returning the fixture(s)
  Call await fetch("software engineer")
  Assert: returns list of JobCreate instances
  Assert: len(result) == number of hits in the search fixture
  Assert: result[0].source == "mcf"
  Assert: result[0].url == fixture hit metadata.jobDetailsUrl   [confirmed key]
  Assert: result[0].description contains no HTML tags
  Assert: result[0].posted_at is a timezone-aware value derived from metadata.newPostingDate [confirmed key]
  Assert: "posted_at" not in result[0].metadata
  Assert: "salary_min" in result[0].metadata                    [from salary.minimum]
  Assert: "position_levels" in result[0].metadata               [from positionLevels[].position]

test_mcf_company_prefers_hiring_company_over_posted_company
  Use the agency-posted hit in the fixture (hiringCompany populated)
  Assert: result.company == hiringCompany.name, not postedCompany.name (the agency)

test_mcf_html_stripped_from_description
  Fixture hit description field contains "<p>Some <b>bold</b> text.</p>"
  Assert: result.description == "Some bold text."

test_mcf_empty_results
  Mock client returns {"results": [], "total": 0, "countWithoutFilters": 0}
  Assert: returns []

test_mcf_malformed_payload
  Mock client returns {"unexpected": "shape"}
  Assert: returns []
  Assert: logger.warning called once, message contains "mcf"

test_mcf_http_error
  Mock client raises httpx.HTTPStatusError
  Assert: returns []
  Assert: logger.warning called once, message contains "mcf"

@pytest.mark.live
test_mcf_live_smoke
  Instantiate McfSource with real settings (no auth needed)
  Call await fetch("software engineer")
  Assert: returns list, len > 0
  Assert: all items are JobCreate instances
  Assert: no item has HTML tags in its description
```

### Definition of done

- Open design question above resolved (inline detail calls vs. search-only vs. capped/batched)
- All unit tests pass
- `mypy` reports no errors on the adapter module
- `@pytest.mark.live` smoke test passes
- `architecture_v2.md` adapter table updated

---

## WP-S3 — JobStreet adapter

### Recon findings (2026-07-18) — BLOCKED, do not implement

Two independent blockers, either one alone is sufficient to stop here:

1. **Cloudflare bot-challenge on listing pages.** `GET sg.jobstreet.com/software-engineer-jobs` (realistic desktop UA + `Accept-Language` header) returns an HTTP 200 shell that is actually a Cloudflare "Just a moment..." Turnstile challenge page (`cf-cache-status: DYNAMIC`, CSP referencing `challenges.cloudflare.com`). No `__NEXT_DATA__`, no listing data — the page cannot render without solving a JS challenge. This alone classifies the listing route as bot-defended / Hard tier.

2. **robots.txt explicitly disallows AI agents from job content.** `sg.jobstreet.com/robots.txt` has a dedicated block:
   ```
   User-agent: anthropic-ai
   Disallow: /companies
   Disallow: */job/*
   ```
   `/graphql` and `/api/jobsearch/` are also disallowed for `User-agent: *`. This means job listing/detail pages are off-limits to Claude/Anthropic-driven scraping regardless of technical feasibility — this is a policy blocker independent of the Cloudflare issue.

**Verdict: do not implement a JobStreet adapter.** The old ad-hoc recon script (`jobstreet_adapter.py`, which fetched `*/job/{id}` pages) has been removed since re-running it would violate the robots.txt directive above. If JobStreet coverage becomes a hard requirement later, it would need an authorized/licensed data path (e.g. an official partner API), not scraping — re-open this WP only if that changes.

### Recon to-do list (completed 2026-07-18, kept for reference)

```
[x] Check __NEXT_DATA__ in page source → not reachable, Cloudflare challenge intercepts the listing page
[x] Check robots.txt → explicit anthropic-ai Disallow on /companies and */job/*
[x] Classify → Hard tier / bot-defended
[x] Write up findings → this section
[x] Do NOT implement → confirmed, stopping here
```

### Test specifications

Moot — WP-S3 is blocked (see recon findings above). No adapter, no tests, no fixture. Do not resurrect this section without re-opening the WP first.

---


---

## File layout

Note: paths below use this repo's actual test convention (`src/test/...`), not a root-level `tests/` dir. WP-S3 (JobStreet) is blocked — no adapter or test files for it.

```
src/
  scraper/
    careers_gov_adapter.py   # WP-S1
    mcf_adapter.py           # WP-S2

  test/
    unit/
      test_careers_gov_adapter.py
      test_mcf_adapter.py
    fixtures/
      careers_gov_response.json   # WP-S1 — capture before writing tests
      mcf_response.json           # WP-S2 — capture during recon
```

---

## Build order

1. Capture `careers_gov_response.json` fixture → WP-S1 tests → WP-S1 fixes
2. Complete MCF recon → capture `mcf_response.json` → WP-S2 tests → WP-S2 implementation
