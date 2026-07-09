# Scheduling Layer v2 — Source of Truth

---

## Responsibility

The scheduler is the pipeline's heartbeat. It drives every automated stage on a
deterministic clock, calling the LLM only at the stages that genuinely need
judgement. It is **not** the agent — the agent is reactive (wakes on demand via
`POST /chat`). The scheduler is proactive (wakes on a clock).

All writes go through the backend service layer. The `JobService` facade — a single object that gathers the backend's functions behind one clean interface — is injected into the scheduler as an instance, so each job calls those functions directly as ordinary in-process calls rather than reaching the backend over HTTP. The scheduler never writes to the database directly.

---

## Two Loops at Two Speeds

**Slow loop** — weekly (Monday) or on profile change. One LLM call. Reads
`profile.json`, generates `search_queries.json`. Output is human-vetoable.

**Fast loop** — daily. Six jobs running in dependency order. Almost entirely
deterministic; zero LLM calls for scrape, scoring, and lifecycle.

### Daily execution order

```
query_regen  (Monday only, runs before scrape so same-day queries are fresh)
     ↓
  scrape + score  (inline: DISCOVERED → SCORED | REJECTED)
     ↓
  lifecycle       (PENDING_APPROVAL → EXPIRED; SCORED → REJECTED; APPLIED/INTERVIEWING → GHOSTED)
     ↓
  follow-up       (APPLIED past threshold → LLM draft → Telegram push)
     ↓
  tailor          (top-N SCORED → tailoring service → PDF → PENDING_APPROVAL → Telegram push)
     ↓
  digest          (Monday only, after daily jobs complete)
```

---

## Scheduled Jobs Summary

| Job | Cadence | LLM? | Responsibility |
|---|---|---|---|
| `scrape` | daily | no | queries → adapters → `ingest_job()` → score → SCORED \| REJECTED |
| `lifecycle` | daily | no | three deterministic time rules on `status_changed_at` |
| `follow_up` | daily | yes (small N) | APPLIED past threshold → draft email → Telegram push |
| `tailor` | daily | yes (budgeted) | top-N SCORED → tailor → PDF → PENDING_APPROVAL → Telegram push |
| `query_regen` | weekly (Mon) + on-demand | yes (1 call) | profile → `search_queries.json` |
| `digest` | weekly (Mon) | no (default) | pipeline summary → Telegram |

---

## Design Decisions

**1. APScheduler registered in the FastAPI lifespan hook.**
All jobs are plain async functions. The scheduler is never imported in tests;
jobs are unit-tested by calling the function directly with injected dependencies.

**2. Execution order within the daily run is enforced.**
Scrape completes before tailor selects from SCORED records. Lifecycle runs before
follow-up and tailor so time-rule transitions are already applied for the day.
Digest runs last so it reflects the final daily state.

**3. Failures are isolated per job.**
One adapter failing inside the scrape job must not abort the tailor job. Each
scheduled function catches its own exceptions, logs via loguru, and returns —
never propagates to the scheduler loop. One record failing inside a batch job
(e.g. guard violation in tailor) must not abort the remaining records.

**4. All jobs are testable as plain functions.**
No scheduler fixtures in tests. Each job is `async def run_<job>(deps...) -> None`.
Tests call it directly with mocked dependencies. The scheduler wiring (APScheduler
registration, cron triggers) is tested only in the bootstrap integration test.

**5. LLM is only touched by three jobs. (+1 embedding model for scoring)**
`tailor`, `follow_up` (drafting step only), and `query_regen`. Scraper uses an embedding model. 

**6. All time comparisons key off `status_changed_at`, not `updated_at`.**
`status_changed_at` is stamped on every FSM transition. Follow-up and ghost clocks
measure how long a record has been *in its current status* — not when it was last
touched. Follow-up activity (`follow_up_count`, `last_follow_up_at`) is tracked
separately and never resets the ghost clock.

---

## Settings (pydantic-settings)

| Setting | Default | Used by |
|---|---|---|
| `scrape_hour` | `2` | scrape job cron trigger |
| `lifecycle_hour` | `3` | lifecycle job cron trigger |
| `followup_hour` | `4` | follow-up job cron trigger |
| `tailor_hour` | `5` | tailor job cron trigger |
| `digest_day` | `monday` | digest + query_regen cron trigger |
| `tailor_batch_size` | `10` | tailor job — max records per day |
| `score_threshold` | `7000` | scrape job — SCORED vs REJECTED boundary |
| `follow_up_after_days` | `7` | follow-up job — minimum days in APPLIED before first nudge |
| `pending_expiry_days` | `14` | lifecycle job — PENDING_APPROVAL → EXPIRED |
| `stale_after_days` | `14` | lifecycle job — SCORED → REJECTED |
| `ghost_after_days` | `35` | lifecycle job — APPLIED/INTERVIEWING → GHOSTED |

---

## Work Packages

### WP-S1 — Scheduler Bootstrap

**Scope:** Register APScheduler in the FastAPI lifespan hook. Define job slots as
stubs (log "job started / finished", no logic). Add all settings above to
`pydantic-settings` config.

**Implementation notes:**
- Use `AsyncIOScheduler` from APScheduler with `CronTrigger` per job.
- Scheduler starts in the `startup` phase of the lifespan context manager and
  shuts down in the `shutdown` phase.
- Each job slot is a stub: `async def run_scrape(...): logger.info("scrape started")`.
- Settings use `pydantic-settings` with `.env` file support; all timing settings
  have sensible defaults.

**Tests:**
- Start the app with `AsyncClient` and lifespan; assert `GET /health` returns 200
  while the scheduler is running.
- Assert the scheduler starts and stops cleanly (no exception on lifespan
  startup/shutdown).
- Assert each job is registered with the correct cron trigger (inspect
  APScheduler's job list).

**Definition of done:**
- Scheduler starts and stops with the app lifespan.
- All six job slots registered (stubs).
- All settings configurable via `.env`.
- No import of the scheduler in any job unit test.

---

### WP-S2 — Scrape Job

**Scope:** Daily job. Reads `search_queries.json`, fans out across all registered
adapters for each query, ingests each normalized `JobCreate` via the injected `ingest_job` service function, then
immediately scores each new `DISCOVERED` record and transitions it to `SCORED` or
`REJECTED`.

**Scoring is inline with the scrape job.** There is no value in a `DISCOVERED`
record sitting unscored between runs. `DISCOVERED` is a transient state — a job
should never be in `DISCOVERED` at the start of a new scrape cycle.

**Pipeline design notes:**
`run_scrape` is a single async function that fans out queries across adapters and passes each result to the injected `JobService` facade by calling `service.ingest_job()` directly, so there is no network call. Deduplication is unchanged — it still happens inside `ingest_job` (fingerprint → repository upsert).


The `JobService` facade is injected as an instance, not imported directly; the scrape job calls `service.ingest_job(...)`. This keeps the pipeline decoupled from the service module and trivially mockable in tests, because a test passes a mock facade exposing only `ingest_job` (no HTTP client, no service object graph).

```python
async def run_scrape(
    adapters: list[JobSource],
    service: JobService,   # injected in-process facade — no HTTP calls
    scorer: Scorer,        # injected instance, separate from the facade; scorer.score(...) is async — one embeddings-API network call per record
    settings: Settings,    # holds score_threshold for the SCORED / REJECTED gate
    queries_path: Path,    # search_queries.json — re-read on every run
    delay_s: float = 1.0,  # pause between adapter fetches
) -> None:
    queries = _load_queries(queries_path)   # read fresh each run — picks up the weekly query_regen output
    if queries is None:                     # missing file: already logged inside _load_queries
        return
    await _fan_out(queries, adapters, service, scorer, settings, delay_s)
```

```python
scheduler.add_job(
    run_scrape, "interval", hours=24,
    kwargs={"adapters": ..., "service": service, "scorer": scorer,
            "settings": settings, "queries_path": queries_path},
)
```

`run_scrape` re-reads `search_queries.json` at the start of every run rather than receiving a fixed query list. This matters because the weekly `query_regen` job rewrites that file; if the query list were bound once when the scheduler registered the job, every regeneration would be ignored until the application restarted. Reading the file each run keeps the daily scrape current. The fan-out itself lives in `_fan_out`, which takes the query list as an argument and is the unit-tested core, while `run_scrape` is the thin wrapper that loads the file and calls it.

The scorer is a separate injected instance, not part of the `JobService` facade; the scrape job holds both. Its `score` method is `async` on purpose, because producing an embedding is a network call to the embeddings API. Marking it `async` and awaiting it lets the event loop — the async runtime that interleaves tasks while they wait on input/output — do other work during each round-trip instead of stalling the whole process on the network. That is the right default for every I/O-bound call in this pipeline, and the scorer is I/O-bound because it hits the network once per record.

#### Implementation tasks

`run_scrape` first reads the current queries via `_load_queries(queries_path)` on every run (missing file → log a warning and return), then delegates the fan-out below to `_fan_out`:

1. For each query × adapter: call `adapter.fetch(query)`, then `await service.ingest_job(job_create)` for each returned `JobCreate`
2. Wrap each `adapter.fetch()` call in try/except — one adapter failure must not abort the others
3. Wrap each `service.ingest_job()` call in try/except — one ingest failure must not abort remaining ingests
4. Apply configurable `delay_s` between adapter calls
5. Log a summary per adapter per query: adapter name, query, number of jobs fetched, number of ingests succeeded



**Tests:**
**Definition of done:**
- Job callable as a plain function with injected dependencies.
- All failure paths (missing queries file, adapter exception, score below
  threshold) covered by unit tests.
- Scheduler registers it for `scrape_hour` daily.


```
# The fan-out tests below target _fan_out directly (queries passed in). run_scrape's
# file-loading is covered by the two loader tests at the end. Each fan-out test provides:
#   mock_service  — AsyncMock ingest_job (+ transition_status)
#   mock_scorer   — AsyncMock score returning a fixed int
#   mock_settings — score_threshold set so ingested jobs land SCORED
# delay_s=0 unless the test is specifically about the delay.

test_pipeline_fans_out_across_adapters
  Two mock adapters (mock_adapter_a, mock_adapter_b) each returning 2 JobCreate instances
  One query string: ["engineer"]
  Call await _fan_out(queries=["engineer"], adapters=[mock_adapter_a, mock_adapter_b], service=mock_service, scorer=mock_scorer, settings=mock_settings, delay_s=0)
  Assert: mock_adapter_a.fetch called once with "engineer"
  Assert: mock_adapter_b.fetch called once with "engineer"
  Assert: mock_service.ingest_job called 4 times total (2 adapters × 2 jobs each)

test_pipeline_fans_out_across_queries
  One mock adapter returning 2 JobCreate instances per call
  Two query strings: ["engineer", "analyst"]
  Assert: adapter.fetch called twice (once per query)
  Assert: mock_service.ingest_job called 4 times total

test_pipeline_one_adapter_failure_does_not_abort_others
  mock_adapter_a.fetch raises an exception
  mock_adapter_b.fetch returns 2 JobCreate instances
  Call _fan_out with both adapters
  Assert: mock_service.ingest_job called 2 times (mock_adapter_b's jobs ingested)
  Assert: no exception propagates out of _fan_out()
  Assert: logger.warning called at least once

test_pipeline_ingest_failure_does_not_abort_pipeline
  One mock adapter returning 3 JobCreate instances
  mock_service.ingest_job raises an exception on the first call, succeeds on subsequent calls
  Assert: no exception propagates out of _fan_out()
  Assert: remaining ingests still attempted (mock_service.ingest_job called 3 times total)
  Assert: logger.warning called at least once

test_pipeline_delay_between_requests
  Mock asyncio.sleep
  One adapter, two queries
  Assert: asyncio.sleep called with delay_s value between adapter calls

test_pipeline_empty_adapter_result
  Mock adapter returns []
  Assert: mock_service.ingest_job never called
  Assert: no exception raised
  Assert: no warning logged

test_pipeline_ingests_correct_jobcreate
  Mock adapter returns one JobCreate with known field values
  Assert: mock_service.ingest_job called once with that exact JobCreate instance
  (identity/equality check on the argument — no serialization involved)

test_run_scrape_reads_queries_file_each_run
  Patch _load_queries to return ["engineer"]; one mock adapter returning 1 JobCreate
  Call await run_scrape(adapters=[adapter], service=mock_service, scorer=mock_scorer,
                        settings=mock_settings, queries_path=<tmp path>)
  Assert: _load_queries called once during the run (queries read at call time, not bound at registration)
  Assert: adapter.fetch called with "engineer"

test_run_scrape_missing_queries_file_returns
  _load_queries returns None (file absent)
  Call await run_scrape(..., queries_path=<nonexistent path>)
  Assert: no adapter.fetch, no ingest, logger.warning called, no exception raised
```

---

### WP-S3 — Lifecycle Job

**Scope:** Daily job. Three deterministic time rules, zero LLM calls.

```
PENDING_APPROVAL  older than pending_expiry_days  → EXPIRED
SCORED            older than stale_after_days      → REJECTED
APPLIED / INTERVIEWING  older than ghost_after_days → GHOSTED  (+ Telegram note)
```

All comparisons key off `status_changed_at`. Each transition is a
`service.transition_status(job_id, to_status)` call on the injected `JobService` facade — never a direct database write.

For each `GHOSTED` transition, send a one-line Telegram notification:
`"No response from {company} ({role}) — marked as ghosted."`

`EXPIRED` and `REJECTED` transitions are silent (no Telegram push).

**Implementation notes:**
- Query each bucket via `service.query_jobs(status_set, ...)`; filter in Python on
  `status_changed_at` age vs the relevant threshold.
- Process all three buckets in a single job invocation; log counts at the end
  (`n_expired`, `n_stale_rejected`, `n_ghosted`).
- Telegram client injected; not called for EXPIRED or REJECTED transitions.

**Tests:**
- Mock the injected `JobService` facade (its `query_jobs` and `transition_status` methods) and the Telegram client.
- Inject records at controlled `status_changed_at` values (exactly at threshold
  = no transition; one day over = transition fires).
- Assert each rule fires correctly at the boundary.
- Assert Telegram is called exactly once per `GHOSTED` transition.
- Assert Telegram is never called for `EXPIRED` or `REJECTED` transitions.
- Assert one record's `transition_status` failure is caught and logged without aborting the
  remaining records.

**Definition of done:**
- All three rules and boundary conditions covered by unit tests.
- Job callable as a plain function.
- Scheduler registers it for `lifecycle_hour` daily.

---

### WP-S4 — Follow-up Job

**Scope:** Daily job. Deterministic check (zero tokens) first; LLM only for the
handful of jobs that cross the threshold.

```
for job where status = APPLIED
         AND status_changed_at older than follow_up_after_days
         AND follow_up_count = 0:
    draft = llm_draft_followup(role, company, applied_date)
    Telegram push: context + draft email + [Sent it] [Skip]
```

`[Sent it]` → `service.record_follow_up(job_id)` (increments `follow_up_count`, stamps
`last_follow_up_at`; status stays `APPLIED`).

**Ghost clock is NOT reset by follow-up activity.** The ghost clock keys off
`status_changed_at`; follow-up activity is tracked in `follow_up_count` /
`last_follow_up_at` separately.

**Optional second nudge:** `follow_up_count = 1 AND last_follow_up_at older than
follow_up_after_days` → same draft + push flow.

**Implementation notes:**
- Query via `service.query_jobs({APPLIED}, ...)`; filter in Python on `status_changed_at`
  age and `follow_up_count`.
- LLM call is one per qualifying record: `llm_draft_followup(role, company,
  applied_date) -> str`. Use the same LiteLLM client as the rest of the pipeline;
  mock in all non-`live` tests.
- Telegram push includes the drafted email text inline (copy-paste ready) and
  inline keyboard buttons `[Sent it]` `[Skip]`.

**Tests:**
- Mock LLM and Telegram client.
- Assert the check query filters correctly (status, age, follow_up_count = 0).
- Assert LLM is called only for qualifying records (not for records below the
  age threshold or with follow_up_count > 0).
- Assert Telegram push fires once per qualifying record.
- Assert `service.record_follow_up(job_id)` is called when `[Sent it]` fires.
- Assert `status_changed_at` is unchanged after a follow-up (ghost clock not
  reset).
- Assert second-nudge logic fires for `follow_up_count = 1` past the interval.

**Definition of done:**
- LLM mocked in all non-`live` tests.
- Both nudge paths tested.
- Job callable as a plain function.
- Scheduler registers it for `followup_hour` daily.

---

### WP-S5 — Tailor Job

**Scope:** Daily job, runs after lifecycle. Selects the top `tailor_batch_size`
SCORED records by the deterministic ranking query, calls the tailoring service for
each, registers the PDF artifact, transitions to `PENDING_APPROVAL`, and pushes to
Telegram.

**Selection ranking (deterministic, implemented inside `select_top_scored`):**
```sql
SELECT ... WHERE status = 'SCORED'
ORDER BY score DESC,      -- exact integer comparison
         posted_at DESC,  -- tie-break 1: fresher listing has more runway
         id ASC           -- tie-break 2: total determinism
LIMIT :tailor_batch_size
```

**Guard violation** (from tailoring service) → log the failure, mark the
individual record failed, continue to the next record. One failure never aborts
the batch. FSM does not update for that job.

**Telegram push per successfully tailored job:** role, company, score, listing
URL, tailored CV PDF attached, inline keyboard `[Mark Applied]` `[Skip]`
(Phase 1); `[Apply for me]` `[Skip]` (Phase 2).

**Implementation notes:**
- Call `service.select_top_scored(limit=tailor_batch_size)` to get the batch. This
  dedicated service read applies the deterministic ranking (score descending, then
  `posted_at` descending, then `id` ascending); ordering is enforced inside the
  service, not in the job.
- For each record: call `tailoring_service.tailor(jd, profile)` → validate
  schema and guards → `render(tailored, profile)` → PDF bytes →
  `service.register_artifact(job_id, artifact)` →
  `service.transition_status(job_id, PENDING_APPROVAL)` → Telegram push with PDF.
- Tailoring service, Telegram client.
**Tests:**
- Mock tailoring service and Telegram client.
- Inject N+1 SCORED records; assert only N are processed (batch size cap).
- Assert selection order matches the deterministic ranking.
- Assert Telegram push fires once per successfully tailored job.
- Assert guard violations are logged and skipped without aborting the batch.
- Assert a tailoring service exception on one record does not abort the others.

**Definition of done:**
- Budget cap tested (N+1 records → only N tailored).
- Guard violation and exception paths tested.
- Job callable as a plain function.
- Scheduler registers it for `tailor_hour` daily.

---

### WP-S6 — Query Regen Job

**Scope:** Weekly (Monday, before the daily scrape) and on-demand (triggerable via
the agent's `regenerate_queries` tool). Reads `profile.json`, makes one LLM call,
writes `search_queries.json`.

**Backup-on-change:** if `search_queries.json` already exists and the new output
differs, the existing file is backed up before overwrite. If the content is
identical, no write occurs (idempotent).

**Output is human-vetoable:** the generated file is plain JSON, editable before
the next scrape run picks it up.

**Implementation notes:**
- Load `profile.json`; if missing, log a warning (no profile = no
  queries).
- One LLM call: `llm_generate_queries(profile) -> list[str]`.
- Write to `search_queries.json`; backup existing to
  `search_queries.json.bak` on change.
- Expose as both a scheduled job and an importable function called by the
  agent's `regenerate_queries` tool.

**Tests:**
- Mock the LLM returning a fixture query list.
- Assert `search_queries.json` is written with the correct content.
- Assert backup is created when the file exists and differs.
- Assert no write occurs when content is identical (idempotency).
- Assert missing `profile.json` logs a warning and returns without writing (no error raised).
- All file I/O uses `tmp_path`.

**Definition of done:**
- LLM mocked in all non-`live` tests.
- File I/O tested with `tmp_path`.
- Callable as a plain function and triggerable by the agent.
- Scheduler registers it for Monday before `scrape_hour`.

---

### WP-S7 — Digest Job

**Scope:** Weekly (Monday, after all daily jobs complete). Pulls pipeline state
counts through the injected `JobService` facade and pushes a structured summary to Telegram. LLM is
optional — default is a deterministic template (zero tokens).

**Default Telegram message format:**
```
Weekly digest — {date}
Active:    {SCORED} scored · {TAILORED} tailored · {PENDING_APPROVAL} pending approval
Applied:   {APPLIED} applied · {INTERVIEWING} interviewing · {OFFER} offer
Follow-ups due:  {n}
Ghosted (last 7d):  {n}
Expired (last 7d):  {n}
```

**Implementation notes:**
- Pull counts via `service.query_jobs(status_set, ...)` for each relevant status; compute
  follow-ups due using the same logic as the follow-up job's check query.
- Format and send via Telegram client.
- No LLM call in the default path. An optional `digest_narrative: bool` setting
  can enable a single LLM call to add a one-line summary sentence if desired.

**Tests:**
- Mock the injected `query_jobs` facade method returning fixture counts, and the Telegram client.
- Assert the Telegram message content matches the expected format.
- Assert no LLM call in the default (template) path.
- Assert `digest_narrative = true` triggers exactly one LLM call.

**Definition of done:**
- Digest callable as a plain function.
- Telegram push tested with mocked client.
- Scheduler registers it for Monday after the daily jobs complete.

---

## Build Order

| Step | Work Package | Depends on |
|---|---|---|
| 1 | WP-S1 — Scheduler bootstrap | Backend API lifespan hook |
| 2 | WP-S2 — Scrape job | Adapters (WP-A complete), scoring function, `JobService.ingest_job` |
| 3 | WP-S3 — Lifecycle job | Backend FSM (`transition_status`), Telegram client |
| 4 | WP-S4 — Follow-up job | `record_follow_up`, LiteLLM client, Telegram client |
| 5 | WP-S5 — Tailor job | Tailoring service, `select_top_scored`, `register_artifact`, `transition_status`, Telegram client |
| 6 | WP-S6 — Query regen | LiteLLM client, `profile.json`, file I/O |
| 7 | WP-S7 — Digest | `query_jobs`, Telegram client |

WP-S1 through WP-S3 form the deterministic backbone (zero LLM dependency) and
should ship together. WP-S4 through WP-S7 add the LLM-touching stages
incrementally in dependency order.

---

## Invariants

1. The scheduler never writes to the database directly. All writes go through the
   backend service layer as in-process function calls, never over HTTP.
2. Jobs that need no judgement (scrape, scoring, lifecycle, digest default) never
   call the LLM.
3. All time rules key off `status_changed_at`. Follow-up activity never resets
   the ghost clock.
4. Every scheduled function is a plain async function testable without the
   scheduler. The scheduler wiring is tested once in WP-S1.
5. Failure in one job or one record within a batch is caught, logged, and
   skipped — never propagated to the scheduler loop.

---

## Skeleton — Files & Functions

Layout follows the modular per-concern convention. All jobs live under a
`scheduler/` package; each file maps to one work package.

**Dependency ownership.** The scheduler owns none of the clients it calls. The
`TelegramClient` is owned by the Telegram layer (`telegram/client.py`); the
`JobService` facade and the LiteLLM client are owned by their respective layers.
All are constructed once in the FastAPI lifespan hook (WP-S1 / WP-T1) and injected
into each job. The scheduler depends on these interfaces; it never constructs them.

```
scheduler/
  __init__.py
  bootstrap.py        # WP-S1: lifespan registration, cron triggers, DI wiring
  jobs/
    __init__.py
    scrape.py         # WP-S2: scrape + inline score
    lifecycle.py      # WP-S3: three time rules
    follow_up.py      # WP-S4: deterministic check + LLM draft
    tailor.py         # WP-S5: top-N select + tailor + push
    query_regen.py    # WP-S6: profile -> search_queries.json
    digest.py         # WP-S7: weekly pipeline summary
```

Injected interfaces, by owning layer (none constructed by the scheduler):

| Interface | Owned by | Used by jobs |
|---|---|---|
| `TelegramClient` | `telegram/client.py` | lifecycle, follow_up, tailor, digest |
| `JobService` (in-process service facade) | backend layer | all jobs |
| `LLMClient` (LiteLLM) | agent/LLM layer | follow_up, tailor (via tailoring service), query_regen |
| `Scorer` Protocol (`async score(jd_text, candidate) -> int`, `name`) | scoring layer | scrape |
| tailoring service `tailor(jd, profile)` | tailoring layer | tailor |

`TelegramClient` send primitives used (chat_id is baked in at construction —
never passed by the scheduler):

| Job | Method |
|---|---|
| lifecycle (ghost notice) | `send_message(text)` |
| follow_up | `send_message_with_keyboard(text, keyboard)` |
| tailor | `send_document(text, pdf_bytes)` + `send_message_with_keyboard(...)` |
| digest | `send_message(text)` |

### `scheduler/bootstrap.py` — WP-S1

```python
def register_jobs(
    scheduler: AsyncIOScheduler,
    deps: SchedulerDeps,
    settings: Settings,
) -> None:
    """Register all six jobs on the scheduler with cron triggers from settings.
    Each job is bound to its injected dependencies via functools.partial."""
    ...

async def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Start the scheduler. Called from the FastAPI lifespan startup phase
    alongside start_bot()."""
    ...

async def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Graceful shutdown, called on lifespan teardown."""
    ...
```

`SchedulerDeps` is the injection bundle — a frozen dataclass holding the
`TelegramClient`, `JobService` facade, `LLMClient`, scorer, and tailoring service,
constructed once in the lifespan hook and shared across all jobs.

### `scheduler/jobs/scrape.py` — WP-S2

```python
async def run_scrape(
    adapters: list[JobSource],
    service: JobService,
    scorer: Scorer,        # injected instance; async score(jd_text, candidate) -> int is an embeddings-API network call; also exposes name
    settings: Settings,
    queries_path: Path,
    delay_s: float = 1.0,
) -> None:
    """Read the CURRENT search_queries.json via _load_queries(queries_path) on every
    run, so weekly query_regen updates take effect without an app restart. Missing
    file: log warning and return. Otherwise delegate to _fan_out."""
    ...

async def _fan_out(
    queries: list[str],
    adapters: list[JobSource],
    service: JobService,
    scorer: Scorer,
    settings: Settings,
    delay_s: float,
) -> None:
    """Testable core. For each query x adapter: fetch, then ingest + score each
    JobCreate via _ingest_and_score. Adapter and per-job failures are isolated
    (logged; the loop continues). Sleeps delay_s between adapter fetches."""
    ...

def _load_queries(path: Path) -> list[str] | None:
    """Read search_queries.json. Returns None (logged) if missing."""
    ...

async def _ingest_and_score(
    jobs: list[JobCreate], service: JobService, scorer: Scorer, settings: Settings
) -> None:
    """Ingest each job via service.ingest_job (backend dedups). For each new DISCOVERED record:
      score = await scorer.score(jd_text, profile)
      status = SCORED if score >= settings.score_threshold else REJECTED
    The gate lives here, not in the scorer — the scorer only returns the int.
    scorer.name is stamped onto the record for the per-cohort score audit trail."""
    ...
```

### `scheduler/jobs/lifecycle.py` — WP-S3

```python
async def run_lifecycle(
    service: JobService,
    telegram: TelegramClient,
    settings: Settings,
) -> None:
    """Apply three deterministic time rules on status_changed_at:
      PENDING_APPROVAL > pending_expiry_days -> EXPIRED   (silent)
      SCORED           > stale_after_days     -> REJECTED  (silent)
      APPLIED/INTERVIEWING > ghost_after_days -> GHOSTED   (+ telegram notice)
    Per-record transition_status failure is caught and logged; batch continues."""
    ...

async def _expire_pending(service: JobService, settings: Settings) -> int: ...

async def _reject_stale(service: JobService, settings: Settings) -> int: ...

async def _ghost_silent_applicants(
    service: JobService, telegram: TelegramClient, settings: Settings
) -> int:
    """Transition + send one ghost notice per record:
    'No response from {company} ({role}) — marked as ghosted.'"""
    ...
```

### `scheduler/jobs/follow_up.py` — WP-S4

```python
async def run_follow_up(
    service: JobService,
    llm: LLMClient,
    telegram: TelegramClient,
    settings: Settings,
) -> None:
    """Deterministic check first (zero tokens): APPLIED, status_changed_at past
    follow_up_after_days, follow_up_count = 0 (first nudge) or = 1 past interval
    (second nudge). For each match: draft via LLM, push with [Sent it] [Skip].
    LLM is called only for qualifying records."""
    ...

def _due_for_followup(jobs: list[Job], settings: Settings) -> list[Job]:
    """Pure predicate filter — status, age, follow_up_count. No I/O."""
    ...

async def _draft_and_push(
    job: Job, llm: LLMClient, telegram: TelegramClient
) -> None:
    """llm.draft_followup(role, company, applied_date) -> text, then
    telegram.send_message_with_keyboard(text, [[Sent it] [Skip]])."""
    ...
```

### `scheduler/jobs/tailor.py` — WP-S5

```python
async def run_tailor(
    service: JobService,
    tailoring: TailoringService,
    telegram: TelegramClient,
    settings: Settings,
) -> None:
    """Select top tailor_batch_size SCORED via service.select_top_scored
    (score DESC, posted_at DESC, id ASC),
    tailor each -> PDF -> register artifact -> PENDING_APPROVAL -> push with
    PDF + [Mark Applied] [Skip]. Guard violation or exception on one record:
    log, mark that record failed, continue the batch."""
    ...

async def _tailor_one(
    job: Job, tailoring: TailoringService, service: JobService, telegram: TelegramClient
) -> None:
    """tailor -> validate (schema + guards) -> render PDF ->
    service.register_artifact -> service.transition_status(PENDING_APPROVAL) ->
    send_document + send_message_with_keyboard. Guard breach raises; caught by caller."""
    ...
```

### `scheduler/jobs/query_regen.py` — WP-S6

```python
async def run_query_regen(
    llm: LLMClient,
    settings: Settings,
    profile_path: Path,
    queries_path: Path,
) -> None:
    """Read profile.json, one LLM call -> query list, write search_queries.json
    with backup-on-change. Identical content: no write (idempotent).
    Missing profile.json: log warning and return.
    Also the callable backing the agent's regenerate_queries tool."""
    ...

def _write_with_backup(path: Path, content: str) -> bool:
    """Write only if content differs from existing; back up the old file first.
    Returns True if written, False if unchanged."""
    ...
```

### `scheduler/jobs/digest.py` — WP-S7

```python
@dataclass(frozen=True)
class DigestCounts:
    """Pipeline state snapshot for the weekly digest."""
    scored: int
    tailored: int
    pending_approval: int
    applied: int
    interviewing: int
    offer: int
    follow_ups_due: int
    ghosted_last_7d: int
    expired_last_7d: int

async def collect_counts(service: JobService, settings: Settings) -> DigestCounts:
    """Pull active-state counts via service.query_jobs(status_set, ...); compute follow_ups_due
    with the same predicate as the follow-up job; count GHOSTED/EXPIRED in last 7d."""
    ...

def format_digest(counts: DigestCounts, today: date) -> str:
    """Pure: counts + date -> Telegram message body. Zero LLM, zero I/O."""
    ...

async def run_digest(
    service: JobService,
    telegram: TelegramClient,
    settings: Settings,
) -> None:
    """collect_counts -> format_digest -> telegram.send_message.
    Catches and logs its own exceptions; never propagates to the scheduler loop."""
    ...
```

**Test files** mirror the package, one per job module:

```
tests/scheduler/
  test_bootstrap.py     # WP-S1: scheduler start/stop, cron registration
  test_scrape.py        # WP-S2: fresh-queries load, fan-out, ingest, dedup, score transitions, adapter isolation
  test_lifecycle.py     # WP-S3: three rules, boundaries, ghost-notice-only
  test_follow_up.py     # WP-S4: predicate filter, LLM-only-on-match, ghost clock untouched
  test_tailor.py        # WP-S5: batch cap, ordering, guard-skip, batch isolation
  test_query_regen.py   # WP-S6: write, backup-on-change, idempotent no-write
  test_digest.py        # WP-S7: format assertion (pure), template path no-LLM
```