# Scoring & Deduplication — Implementation Guide

---

## What This Layer Does

Two pure functions. No I/O, no DB calls, no side effects.

| Function | Input | Output |
|---|---|---|
| `score_job` | job description `str`, resume keywords `list[str]` | `float` between `0.0` and `1.0` |
| `fingerprint_job` | company `str`, role `str`, url `str` | SHA-256 hex digest `str` |

Both are wired into **two** ingestion paths:
- `POST /jobs` route handler — for manually logged jobs
- `_search_jobs` tool executor — for jobs discovered via Tavily search

Every job entering either path gets scored and fingerprinted before the DB write.

---

## File Layout

```
scoring/
├── __init__.py
├── scorer.py          # score_job()
└── fingerprint.py     # fingerprint_job()

src/test/
├── unit/
│   ├── test_scorer.py
│   ├── test_fingerprint.py
│   └── test_search_tool_executor.py   # asserts score arg is passed to insert_job
└── integration/
    └── test_scoring_ingestion.py      # POST /jobs route: score + dedup via HTTP
```

---

## TDD Order

Write every test before writing the implementation. Run the test suite after each function is complete. No implementation file should be touched until its test file is fully written.

**Sequence:**
1. Write `test_scorer.py` → run (all fail) → write `scorer.py` → run (all pass)
2. Write `test_fingerprint.py` → run (all fail) → write `fingerprint.py` → run (all pass)
3. Wire both into the ingestion path → write integration assertions → run (all pass)

---

## scorer.py

### Contract

```python
def score_job(description: str, keywords: list[str]) -> float:
    """
    Returns the fraction of keywords found in description.
    Case-insensitive. Returns 0.0 if keywords is empty.
    """
```

### Logic

```
score = matched_keywords / total_keywords
```

- Lowercase both `description` and each keyword before matching.
- A keyword matches if it appears anywhere as a substring in the description.
- If `keywords` is empty, return `0.0` — do not divide by zero.
- Result is already in `[0.0, 1.0]` by construction; no clamping needed.

### Tests — `test_scorer.py`

Write one test per behaviour. Use `pytest`.

```
CASE: all keywords present         → score == 1.0
CASE: no keywords present          → score == 0.0
CASE: half keywords present        → score == 0.5
CASE: keywords list is empty       → score == 0.0
CASE: description is empty string  → score == 0.0
CASE: matching is case-insensitive → "Python" in desc matches keyword "python"
CASE: keyword is multi-word        → "machine learning" found as substring → counts as 1 match
CASE: duplicate keywords in list   → treat list as-is, no dedup (caller's responsibility)
```

Full coverage means every branch in `score_job` is exercised. There are two branches: the empty-keywords guard and the division path.

---

## fingerprint.py

### Contract

```python
def fingerprint_job(company: str, role: str, url: str) -> str:
    """
    Returns a SHA-256 hex digest of normalised (company + role + url).
    Normalisation: strip whitespace, lowercase, concatenate with '|' separator.
    """
```

### Logic

```python
import hashlib

raw = "|".join([company.strip().lower(), role.strip().lower(), url.strip().lower()])
return hashlib.sha256(raw.encode()).hexdigest()
```

### Tests — `test_fingerprint.py`

```
CASE: same inputs → same fingerprint (deterministic)
CASE: different company → different fingerprint
CASE: different role → different fingerprint
CASE: different url → different fingerprint
CASE: case difference only ("Google" vs "google") → same fingerprint
CASE: leading/trailing whitespace only → same fingerprint as stripped version
CASE: output is a 64-character hex string
```

---

## Wiring Into Ingestion

Scoring and fingerprinting run in two places. Both follow the same pattern:

1. Call `fingerprint_job(company, role, url)` — unique identity of the record.
2. Call `score_job(description, keywords)` — relevance against profile skills.
3. Pass both to `repo.insert_job(db, job, fingerprint, score)`.

The `fingerprint` column has a `UNIQUE` constraint in the DB. A duplicate insert returns the existing record silently — no error raised to the caller.

### Path 1 — `POST /jobs` route handler (`app/routes/jobs.py`)

Called when a job is manually logged via the API.

```python
profile  = read_profile()
keywords = profile.get("skills", [])
fp    = fingerprint_job(job.company, job.role, str(job.url))
score = score_job(job.description or "", keywords)
record, created = await insert_job(db, job, fp, score)
```

### Path 2 — `_search_jobs` tool executor (`agent/tools.py`)

Called when the agent runs a Tavily search. Keywords are read once per search call and applied to every result in the batch.

```python
profile  = read_profile()
keywords = profile.get("skills", [])
for record in parsed:
    fp    = fingerprint_job(job.company, job.role, str(job.url))
    score = score_job(job.description or "", keywords)
    await repo.insert_job(db, job, fp, score)
```

### Where keywords come from

Keywords are read from `profile.json` at call time — specifically the `skills` field. This is the same profile the agent builds conversationally. When the user tells the agent their skills, the profile is updated, and the scorer automatically reflects the change on the next ingestion run.

If `profile.json` does not exist or `skills` is absent, `keywords` defaults to `[]` and every job scores `0.0` — no error raised.

### Description field and scoring

The scorer receives `job.description`, which is populated by the scraper parser from Tavily's `content` field. If Tavily returns only a URL with no content snippet, `description` is `None` and the job scores `0.0`. This is expected — a missing description means there is nothing to match against.

---

## Profile Dependency

No new config is needed. The scorer reads from the same `profile.json` the agent already owns. The profile's `skills` field is the single source of truth for scoring keywords.

To populate skills, the user tells the agent in conversation:

> "Add Python, FastAPI, and Docker to my skills."

The agent calls `update_profile({"skills": ["Python", "FastAPI", "Docker"]})`. From that point forward, every job ingested via `POST /jobs` is scored against those skills automatically.

---

## Upgrade Path

The `score_job` signature is the stable interface. Any future upgrade (TF-IDF, embedding cosine similarity) replaces only the function body. All call sites and all existing tests continue to pass unchanged — tests assert on the contract (`float` in `[0.0, 1.0]`, correct edge-case values), not on the internal algorithm.

---

## Known Limitations

Tavily returns job board search result pages (LinkedIn, Indeed, Glassdoor) rather than individual job postings. The content snippets are short and generic, so scores tend to be low (e.g. `0.33` when one of three skills appears in the snippet). Improving score fidelity requires deeper content extraction — a future scraper enhancement.

---

## Definition of Done

- [x] `test_scorer.py` — all cases pass, 100% branch coverage on `scorer.py`
- [x] `test_fingerprint.py` — all cases pass, 100% branch coverage on `fingerprint.py`
- [x] `fingerprint` and `score` fields populated on every record entering `POST /jobs`
- [x] `fingerprint` and `score` fields populated on every record inserted via `_search_jobs` tool
- [x] Unit test asserts `score` arg is explicitly passed to `repo.insert_job` in `_search_jobs`
- [x] Duplicate insert returns existing record, no error
- [x] Keywords sourced from `profile.get("skills", [])` — no hardcoding, no `.env` config
- [x] Empty or missing `skills` in profile results in score `0.0`, no error
- [x] `description` field (not `notes`) used as scorer input — parser verified by unit tests
- [x] No direct DB calls anywhere inside `scorer.py` or `fingerprint.py`
- [x] E2e test confirms full pipeline: profile skills → Tavily search → scored jobs → dedup
