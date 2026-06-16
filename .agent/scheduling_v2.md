# Scheduling Layer v2 — Source of Truth

---

## Responsibility

The scheduler is the pipeline's heartbeat. It drives every automated stage on a
deterministic clock, calling the LLM only at the stages that genuinely need
judgement. It is **not** the agent — the agent is reactive (wakes on demand via
`POST /chat`). The scheduler is proactive (wakes on a clock).

All writes go through the backend API. The scheduler never writes to the DB directly.

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
| `scrape` | daily | no | queries → adapters → `POST /jobs` → score → SCORED \| REJECTED |
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

**5. LLM is only touched by three jobs.**
`tailor`, `follow_up` (drafting step only), and `query_regen`. Scrape, scoring,
and lifecycle are zero-token by design. This is the discipline that makes the
pipeline cheap, deterministic, and testable at low cost.

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
adapters for each query, posts each normalized `JobCreate` to `POST /jobs`, then
immediately scores each new `DISCOVERED` record and transitions it to `SCORED` or
`REJECTED`.

**Scoring is inline with the scrape job.** There is no value in a `DISCOVERED`
record sitting unscored between runs. `DISCOVERED` is a transient state — a job
should never be in `DISCOVERED` at the start of a new scrape cycle.

**Implementation notes:**
- Load `search_queries.json` at job start; if missing, log a warning and skip the
  run (query_regen will fix this).
- For each query, call each adapter's `.fetch(query)` concurrently (asyncio
  gather) with a configurable `delay_s` between adapter calls for politeness.
- Each adapter wraps its own exception: failure returns `[]` and logs; never
  raises to the job runner.
- For each returned `JobCreate`, call `POST /jobs`. The backend handles dedup
  (fingerprint match → upsert `seen_count`/`last_seen_at`, no new record).
- For each newly created record (status = `DISCOVERED`), call the scorer:
  `score(jd_text, profile) -> int`. Score ≥ `score_threshold` →
  `PATCH /jobs/{id}/status` to `SCORED`; below → `REJECTED`.
- Inject adapters, HTTP client, scorer function, and settings — never
  instantiated inside the job function.

**Tests:**
- Mock adapters returning fixture `JobCreate` lists; assert each result is POSTed
  exactly once.
- Assert a fingerprint-matched record does not result in a second `POST /jobs`
  creating a new row (mock the backend returning the upsert response).
- Assert scorer is called for each new `DISCOVERED` record.
- Assert a score ≥ threshold transitions to `SCORED`; below threshold to
  `REJECTED`.
- Assert a single adapter exception does not abort the others (remaining adapters
  still called).
- Assert missing `search_queries.json` logs a warning and returns without
  raising.

**Definition of done:**
- Job callable as a plain function with injected dependencies.
- All failure paths (missing queries file, adapter exception, score below
  threshold) covered by unit tests.
- Scheduler registers it for `scrape_hour` daily.

---

### WP-S3 — Lifecycle Job

**Scope:** Daily job. Three deterministic time rules, zero LLM calls.

```
PENDING_APPROVAL  older than pending_expiry_days  → EXPIRED
SCORED            older than stale_after_days      → REJECTED
APPLIED / INTERVIEWING  older than ghost_after_days → GHOSTED  (+ Telegram note)
```

All comparisons key off `status_changed_at`. Each transition is a
`PATCH /jobs/{id}/status` call through the backend API — never a direct DB write.

For each `GHOSTED` transition, send a one-line Telegram notification:
`"No response from {company} ({role}) — marked as ghosted."`

`EXPIRED` and `REJECTED` transitions are silent (no Telegram push).

**Implementation notes:**
- Query each bucket via `GET /jobs?status=...`; filter in Python on
  `status_changed_at` age vs the relevant threshold.
- Process all three buckets in a single job invocation; log counts at the end
  (`n_expired`, `n_stale_rejected`, `n_ghosted`).
- Telegram client injected; not called for EXPIRED or REJECTED transitions.

**Tests:**
- Mock the backend API and Telegram client.
- Inject records at controlled `status_changed_at` values (exactly at threshold
  = no transition; one day over = transition fires).
- Assert each rule fires correctly at the boundary.
- Assert Telegram is called exactly once per `GHOSTED` transition.
- Assert Telegram is never called for `EXPIRED` or `REJECTED` transitions.
- Assert one record's `PATCH` failure is caught and logged without aborting the
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

`[Sent it]` → `POST /jobs/{id}/follow-up` (increments `follow_up_count`, stamps
`last_follow_up_at`; status stays `APPLIED`).

**Ghost clock is NOT reset by follow-up activity.** The ghost clock keys off
`status_changed_at`; follow-up activity is tracked in `follow_up_count` /
`last_follow_up_at` separately.

**Optional second nudge:** `follow_up_count = 1 AND last_follow_up_at older than
follow_up_after_days` → same draft + push flow.

**Implementation notes:**
- Query via `GET /jobs?status=APPLIED`; filter in Python on `status_changed_at`
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
- Assert `POST /jobs/{id}/follow-up` is called when `[Sent it]` fires.
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

**Selection query (deterministic):**
```sql
SELECT ... WHERE status = 'SCORED'
ORDER BY score DESC,      -- exact integer comparison
         posted_at DESC,  -- tie-break 1: fresher listing has more runway
         id ASC           -- tie-break 2: total determinism
LIMIT :tailor_batch_size
```

**Guard violation** (from tailoring service) → log the failure, mark the
individual record failed, continue to the next record. One failure never aborts
the batch.

**Telegram push per successfully tailored job:** role, company, score, listing
URL, tailored CV PDF attached, inline keyboard `[Mark Applied]` `[Skip]`
(Phase 1); `[Apply for me]` `[Skip]` (Phase 2).

**Implementation notes:**
- Call `GET /jobs?status=SCORED&limit=tailor_batch_size&order_by=score,posted_at,id`
  (or equivalent) to get the batch; ordering is enforced server-side.
- For each record: call `tailoring_service.tailor(jd, profile)` → validate
  schema and guards → `render(tailored, profile)` → PDF bytes →
  `POST /jobs/{id}/artifacts` → `PATCH /jobs/{id}/status` to
  `PENDING_APPROVAL` → Telegram push with PDF.
- Tailoring service, Telegram client, and settings injected.

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
- Load `profile.json`; if missing, log a warning and skip (no profile = no
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
- Assert missing `profile.json` logs a warning without raising.
- All file I/O uses `tmp_path`.

**Definition of done:**
- LLM mocked in all non-`live` tests.
- File I/O tested with `tmp_path`.
- Callable as a plain function and triggerable by the agent.
- Scheduler registers it for Monday before `scrape_hour`.

---

### WP-S7 — Digest Job

**Scope:** Weekly (Monday, after all daily jobs complete). Pulls pipeline state
counts from the backend API and pushes a structured summary to Telegram. LLM is
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
- Pull counts via `GET /jobs?status=...` for each relevant status; compute
  follow-ups due using the same logic as the follow-up job's check query.
- Format and send via Telegram client.
- No LLM call in the default path. An optional `digest_narrative: bool` setting
  can enable a single LLM call to add a one-line summary sentence if desired.

**Tests:**
- Mock backend API returning fixture counts and Telegram client.
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
| 2 | WP-S2 — Scrape job | Adapters (WP-A complete), scoring function, `POST /jobs` |
| 3 | WP-S3 — Lifecycle job | Backend FSM (`PATCH /jobs/{id}/status`), Telegram client |
| 4 | WP-S4 — Follow-up job | `POST /jobs/{id}/follow-up`, LiteLLM client, Telegram client |
| 5 | WP-S5 — Tailor job | Tailoring service, `POST /jobs/{id}/artifacts`, Telegram client |
| 6 | WP-S6 — Query regen | LiteLLM client, `profile.json`, file I/O |
| 7 | WP-S7 — Digest | `GET /jobs?status=...`, Telegram client |

WP-S1 through WP-S3 form the deterministic backbone (zero LLM dependency) and
should ship together. WP-S4 through WP-S7 add the LLM-touching stages
incrementally in dependency order.

---

## Invariants

1. The scheduler never writes to the DB directly. All writes go through the
   backend API.
2. Jobs that need no judgement (scrape, scoring, lifecycle, digest default) never
   call the LLM.
3. All time rules key off `status_changed_at`. Follow-up activity never resets
   the ghost clock.
4. Every scheduled function is a plain async function testable without the
   scheduler. The scheduler wiring is tested once in WP-S1.
5. Failure in one job or one record within a batch is caught, logged, and
   skipped — never propagated to the scheduler loop.
