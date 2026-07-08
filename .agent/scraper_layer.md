# Scraper Layer — Implementation Playbook

**Audience:** an agent (or human) implementing the scraper layer of JobFindingAgent end to end.

**Read first:** `adapters.md` (adapter contract and recon playbook), `architecture_v2.md` (scraper layer section), `careers_gov_adapter.py` (reference implementation), `job.py` (canonical `JobCreate` schema).

---

## Overview

The scraper layer has four work packages, executed in dependency order:

| WP | Description | Status |
|---|---|---|
| WP-S1 | Careers@Gov adapter — finalise and test | Code exists, needs alignment + tests |
| WP-S2 | MCF adapter — implement from scratch | Recon done, ready to build |
| WP-S3 | JobStreet adapter — recon then implement | Recon incomplete, do not build yet |


---

## Project invariants (non-negotiable for all WPs)

1. **No reasoning in adapters.** Query, parse, normalise only. No LLM calls, no scoring, no relevance decisions.
2. **No DB access, no self-HTTP.** Adapters return `list[JobCreate]`; `ingest_job` (never an HTTP call to `POST /jobs` — that route exists for out-of-process callers only). Dedup happens inside `ingest_job` (fingerprint → repository upsert).
3. **Fail soft.** Any error → `logger.warning` + return `[]`. An adapter must never crash
4. **Injectable HTTP client.** Constructor accepts `client: httpx.AsyncClient | None`. No real network in tests except `@pytest.mark.live`.
5. **Secrets in settings.** All API keys, app IDs, index names, delays, page sizes go in pydantic-settings (`.env`). Never hardcoded.
6. **`JobCreate` is the return type of `fetch()`.** Not `dict[str, Any]`. Pydantic validates at the boundary.
7. **TDD discipline.** Write tests before implementation. Red → Green → Refactor.

---

## WP-S1 — Careers@Gov adapter finalisation

### What exists

`careers_gov_adapter.py` is implemented and live-verified (2026-06-10). Two things need fixing before it is wired in:

- `to_job_create()` returns `dict[str, Any]` → must return `JobCreate`
- `posted_at` is buried in `metadata` → must be a top-level `JobCreate` field
- `description=""` (empty string) must normalise to `None` to match `JobCreate`'s `str | None` field

### Implementation tasks

1. Fix `to_job_create()`:
   - Change return type annotation to `JobCreate`
   - Move `posted_at` out of `metadata` dict and into the `JobCreate` constructor directly
   - Normalise `description`: pass `hit.description or None` (empty string → `None`)
   - Remove `posted_at` from `metadata` dict
2. Fix `fetch()` return type annotation: `list[dict[str, Any]]` → `list[JobCreate]`
3. Capture a real Algolia response payload. Trim to 3–5 hits. Include at least one HRP objectID and one GREENHOUSE objectID. Save as `tests/fixtures/careers_gov_response.json`.
4. Write tests (see specifications below).

### Test specifications

File: `tests/unit/test_careers_gov_adapter.py`

```
test_careers_gov_happy_path
  Load careers_gov_response.json fixture (3–5 hits, mix of HRP and GREENHOUSE objectIDs)
  Instantiate CareersGovSource with dummy app_id / api_key and a mock client returning the fixture
  Call await fetch("engineer")
  Assert: returns list of JobCreate instances (not dicts)
  Assert: len(result) == number of hits in fixture
  Assert: result[0].company == fixture hit agency (or fallback chain: agency → agency_abbr → "Singapore Public Service")
  Assert: result[0].role == fixture hit title
  Assert: result[0].source == "careers_gov"
  Assert: result[0].posted_at is a timezone-aware datetime (not None)
  Assert: "posted_at" not in result[0].metadata

test_careers_gov_url_construction_hrp
  Fixture hit has objectID "HRP:17676105/005056a3-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  Assert: str(result.url) == "https://jobs.careers.gov.sg/005056a3-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

test_careers_gov_url_construction_greenhouse
  Fixture hit has objectID "GREENHOUSE:4004142201"
  Assert: str(result.url) == "https://jobs.careers.gov.sg/4004142201"

test_careers_gov_description_empty_becomes_none
  Fixture hit has description == "" (empty string)
  Assert: result.description is None

test_careers_gov_empty_hits
  Mock client returns {"hits": [], "nbHits": 0, "page": 0, "nbPages": 0}
  Assert: returns []
  Assert: no warning logged

test_careers_gov_malformed_payload
  Mock client returns {"unexpected": "shape"}
  Assert: returns []
  Assert: logger.warning called once, message contains "careers_gov"

test_careers_gov_http_error
  Mock client raises httpx.HTTPStatusError (e.g. 403)
  Assert: returns []
  Assert: logger.warning called once, message contains "careers_gov"

@pytest.mark.live
test_careers_gov_live_smoke
  Instantiate CareersGovSource with real credentials from environment
  Call await fetch("software engineer")
  Assert: returns list, len > 0
  Assert: all items are JobCreate instances
  Assert: no item has posted_at in its metadata dict
```

### Definition of done

- All unit tests pass
- `mypy` reports no errors on the adapter module
- `@pytest.mark.live` smoke test passes against the real endpoint
- `architecture_v2.md` adapter table updated with live-test date and verdict

---

## WP-S2 — MCF adapter

### What is known from recon (2026-06-10)

- Keyword search: `POST https://api.mycareersfuture.gov.sg/v2/search` with a JSON body
- No authentication required
- Known payload fields: full HTML `description`, salary min/max/type, `skills[]`, `categories[]`, `employmentTypes[]`, `positionLevels[]` (includes "Fresh/entry level"), company UEN, `metadata.jobDetailsUrl` (canonical URL — use it directly, do not construct)
- `description` is HTML — must be stripped to plain text before passing to `JobCreate`

### Recon remaining before implementation

- Capture the exact `POST /v2/search` request body via DevTools (field names, pagination shape)
- Verify the canonical URL is indeed `metadata.jobDetailsUrl`
- Confirm `posted_at` equivalent field name in the payload
- Verify payload ceiling: does one request return full JD text, or is a detail call needed?
- Save a trimmed real response (3–5 hits) as `tests/fixtures/mcf_response.json`

### Implementation tasks

1. Define Pydantic response models (`McfHit`, `McfSearchResponse`) with `extra="ignore"` and field aliases matching the raw payload exactly. Include a `description` property that strips HTML.
2. Implement `McfSource`: `name = "mcf"`, constructor (settings-injected config + optional client), `async fetch()`, `to_job_create()`.
3. `to_job_create()` must:
   - Set `url` from `metadata.jobDetailsUrl` (canonical, not constructed)
   - Strip HTML from `description` (use `html.parser` or `BeautifulSoup` — pick one and pin it)
   - Set `posted_at` as a top-level `JobCreate` field (not in `metadata`)
   - Put salary range, `positionLevels`, `employmentTypes`, UEN into `metadata`
4. Write tests (see specifications below)

### Test specifications

File: `tests/unit/test_mcf_adapter.py`

```
test_mcf_happy_path
  Load mcf_response.json fixture (3–5 hits)
  Instantiate McfSource with mock client returning the fixture
  Call await fetch("software engineer")
  Assert: returns list of JobCreate instances
  Assert: len(result) == number of hits in fixture
  Assert: result[0].source == "mcf"
  Assert: result[0].url == fixture hit metadata.jobDetailsUrl   [fill exact key from fixture]
  Assert: result[0].description contains no HTML tags
  Assert: result[0].posted_at is a timezone-aware datetime      [fill field name from fixture]
  Assert: "posted_at" not in result[0].metadata
  Assert: "salary_min" in result[0].metadata                    [fill exact key from fixture]
  Assert: "position_levels" in result[0].metadata               [fill exact key from fixture]

test_mcf_html_stripped_from_description
  Fixture hit description field contains "<p>Some <b>bold</b> text.</p>"
  Assert: result.description == "Some bold text."

test_mcf_empty_results
  Mock client returns fixture with empty results array           [fill envelope key from fixture]
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
  Instantiate McfSource with real settings
  Call await fetch("software engineer")
  Assert: returns list, len > 0
  Assert: all items are JobCreate instances
  Assert: no item has HTML tags in its description
```

### Definition of done

- All unit tests pass
- `mypy` reports no errors on the adapter module
- `@pytest.mark.live` smoke test passes
- `architecture_v2.md` adapter table updated

---

## WP-S3 — JobStreet adapter

### Current recon status

Plain GET on `sg.jobstreet.com/{query}-jobs` returns server-rendered HTML. Listing data is in markup attributes but not recoverable via naive text extraction. **Recon is incomplete — do not write any implementation code until the to-do list below is fully resolved.**

### Recon to-do list

Complete these steps before writing any code. Record every finding — they become the doc update.

```


[ ] Check __NEXT_DATA__ in page source:
      - curl sg.jobstreet.com/<query>-jobs
      - grep for __NEXT_DATA__ in the HTML
      - If present: extract, parse as JSON, assess whether it contains full listing data

[ ] If a JSON endpoint is found:
      - Copy as cURL, run verbatim, then strip headers one by one to find the minimum required set
      - Verify payload ceiling: full JD text in one request, or detail call needed?
      - Identify the canonical listing URL field
      - Classify: Easy (JSON, stable) or Medium (HTML parsing, brittle)

[ ] If HTML-only route:
      - Identify which markup attributes or script blobs carry listing data
      - Assess brittleness
      - Classify as Medium or Hard

[ ] If Hard tier or bot-defended:
      - Write up findings in architecture_v2.md and adapters.md ledger
      - Do NOT implement; stop and report back

[ ] After successful recon:
      - Capture a real response payload (JSON or HTML page)
      - Trim to 3–5 results
      - Save as tests/fixtures/jobstreet_response.json (or .html)
      - Fill in the test shells below with actual field names and assertions
```

### Test specifications (shell — complete after recon)

File: `tests/unit/test_jobstreet_adapter.py`

```
test_jobstreet_happy_path
  [TODO: fill field assertions after recon and fixture capture]
  Assert: returns list of JobCreate instances
  Assert: result[0].source == "jobstreet"
  Assert: result[0].description contains no HTML tags (if HTML source)
  Assert: "posted_at" not in result[0].metadata

test_jobstreet_empty_results
  [TODO: fill envelope key after recon]
  Assert: returns []

test_jobstreet_malformed_payload
  Mock client returns {"unexpected": "shape"}
  Assert: returns []
  Assert: logger.warning called once, message contains "jobstreet"

test_jobstreet_http_error
  Mock client raises httpx.HTTPStatusError
  Assert: returns []
  Assert: logger.warning called once, message contains "jobstreet"

@pytest.mark.live
test_jobstreet_live_smoke
  [TODO: fill after recon]
  Assert: returns list, len > 0
  Assert: all items are JobCreate instances
```

---


---

## File layout

```
src/
  scraper/
    careers_gov_adapter.py   # WP-S1
    mcf_adapter.py           # WP-S2
    jobstreet_adapter.py     # WP-S3

tests/
  unit/
    test_careers_gov_adapter.py
    test_mcf_adapter.py
    test_jobstreet_adapter.py
  fixtures/
    careers_gov_response.json   # WP-S1 — capture before writing tests
    mcf_response.json           # WP-S2 — capture during recon
    jobstreet_response.json     # WP-S3 — capture during recon (.html if HTML-only)
```

---

## Build order

1. Capture `careers_gov_response.json` fixture → WP-S1 tests → WP-S1 fixes
2. Complete MCF recon → capture `mcf_response.json` → WP-S2 tests → WP-S2 implementation
