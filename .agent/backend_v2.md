# Backend API Layer — Implementation Spec (v2)

> Source of truth for the backend layer of JobFindingAgent v2. Companion to
> `architecture_v2.md` (Component 3). Captures scope, requirements,
> specifications, and the work-package breakdown. Build/test order (step 5) is
> appended once decided. Written for independent execution.

**One-line responsibility:** Source of truth for job records and artifacts.
Every write from every component goes through this layer.

> **v1 reconciliation (decided):** Full v2 design on a fresh database — no
> migration. Existing v1 identifiers are kept to avoid churn: `ApplicationStatus`
> (not `Status`), `role` (not `title`), `InvalidTransitionError`, `transition()`.
> The state machine lives in `enums.py` (no separate `fsm.py`). Pydantic schemas
> live in `job.py`. The authoritative implementations are `enums.py` and `job.py`;
> where snippets below use the older identifiers, the code files win.

---

## 1. Scope Boundary

### Inside this layer
- FastAPI app skeleton + lifespan hook **mechanism** (the registration point — not the scheduled jobs themselves).
- Data model: the extended `jobs` table and the new `artifacts` table.
- Repository layer — the **only** place raw SQL and driver details live (the aiosqlite → asyncpg seam).
- Pydantic schemas + enums: `JobCreate`, `Job`, `ArtifactCreate`, `Artifact`, `Status`, `ArtifactKind`.
- FSM enforcement — reject illegal transitions before any write.
- The idempotent upsert flow in `POST /jobs`.
- Endpoints: `POST /jobs`, `PATCH /jobs/{id}/status`, `GET /jobs`, `POST /jobs/{id}/artifacts`, `POST /jobs/{id}/follow-up`, `POST /chat` (thin).

### Outside this layer (consumers that write/read *through* it)
- **Scorer** — a pure function; the backend persists the integer it returns and never computes or recomputes it.
- **Tailoring service**, **scraper adapters**, **agent reasoning**, **Telegram bot** — all write through the API.
- **Scheduled job functions** (scheduling layer) — only the lifespan hook *mechanism* lives here.

### Resolved boundary decisions
- **Fingerprint — external.** Belongs to the dedup layer; the method is unsettled (company + title collisions for large employers). The backend imports it behind a stable signature `fingerprint(job: JobCreate) -> str` and treats it as an injected dependency, so revising the algorithm changes **zero** backend code.
- **`POST /chat` — thin transport.** The route is backend; the body is the agent. There is exactly one interpreter of user intent (the agent). The handler delegates; it does **not** branch between "send to agent" and "send to backend."
- **FSM — defined in the backend, shared read-only with the agent.** Rule-based. The agent may *read* legality (`can_transition`) to avoid proposing doomed moves, but enforcement is unconditional at the write path. The agent proposes; the backend disposes.

---

## 2. Requirements

### Functional
1. **Ingest a job idempotently.** Persist a new job; re-ingesting the same job (same fingerprint) does not duplicate — it stamps liveness (`seen_count`, `last_seen_at`) on the existing row. Uniqueness is the backend's responsibility.
2. **Persist the score it is handed.** Store the integer score; never compute or recompute it.
3. **Transition status under FSM rule.** Move a record only if the transition is legal; reject illegal ones before any write; stamp `status_changed_at` on every legal transition.
4. **Record follow-up activity without a state change.** Increment `follow_up_count`, stamp `last_follow_up_at`, leave `status` and `status_changed_at` untouched.
5. **Register an artifact against a job.**
6. **Serve the consumers' queries:** status-set filtering (active vs. terminal), age-based selection on `status_changed_at`, and ranked-and-limited selection (top-N `SCORED` by score, then recency, then id).
7. **Expose one validated write path** to both in-process callers (agent tools, scheduler) and HTTP clients.
8. **Provide the `/chat` transport endpoint** — thin; delegates to the agent.

### Non-functional (invariants honored)
1. Single write path; no consumer writes to the DB directly.
2. The backend validates and executes; it never reasons. No LLM call originates here. Every operation is deterministic and unit-testable.
3. Repository isolation — switching aiosqlite → asyncpg touches only `repository.py` / `database.py`.
4. The two-clocks invariant is enforced **in the write operations themselves** — a follow-up write is structurally incapable of moving `status_changed_at`.
5. No illegal histories — even an agent proposing `REJECTED → OFFER` cannot produce that row.
6. The fingerprint is imported behind a stable signature; revising it changes no backend code.
7. Every external dependency (fingerprint, DB) is injected and mockable.
8. No authentication/authorization until 1.0.0 (single-user) — explicitly out of scope, not forgotten.

---

## 3. Specifications

### 3a. Data Model

**Governing principle:** thin typed core + a `metadata` JSON bag. A field gets a
typed column only if the backend *queries, sorts, or enforces on* it. Everything
portal-specific (salary, `skills[]`, `categories`, `positionLevels`, UEN,
district, `objectID`, expiry, `jobSource`) goes into `metadata`. Adding JobStreet
later adds no columns.

> v1 base columns are reconstructed from references; reconcile against the actual
> v1 schema. The v2 deltas (five lifecycle columns + `artifacts` table) are the
> only explicit additions.

#### `jobs` table

**Identity & dedup**
- `id` — INTEGER, PK.
- `fingerprint` — TEXT, NOT NULL, **UNIQUE**. The UNIQUE constraint *is* dedup enforcement at the DB level. Value supplied by the external fingerprint function.

**Content (normalized `JobCreate` core)**
- `company` — TEXT, NOT NULL.
- `title` — TEXT, NOT NULL.
- `description` — TEXT, NOT NULL (full JD; may be HTML).
- `url` — TEXT, NOT NULL (canonical listing URL). Stored, but **not** part of the fingerprint.
- `posted_at` — TEXT (ISO), nullable. Used as a ranking tie-break.
- `metadata` — TEXT (JSON). Portal-specific bag + source-native identity for traceability.

**Pipeline state**
- `status` — TEXT, NOT NULL, default `'DISCOVERED'`. Validated by a Python enum; **no DB `CHECK`** — adding a new state costs zero migration.
- `score` — INTEGER, **nullable**. NULL while `DISCOVERED`; 0–10000 once scored. Stored once, never recomputed.

**Lifecycle clocks & counters (v2 additions)**
- `status_changed_at` — TEXT (ISO, UTC), NOT NULL. *The* clock for follow-up/ghost/expiry. Set = `created_at` at insert; re-stamped on every legal transition.
- `follow_up_count` — INTEGER, NOT NULL, default 0.
- `last_follow_up_at` — TEXT (ISO, UTC), nullable. Tracked separately so it can never touch `status_changed_at`.
- `seen_count` — INTEGER, NOT NULL, default 1.
- `last_seen_at` — TEXT (ISO, UTC), NOT NULL. = `created_at` at insert; re-stamped on every duplicate ingest.

**Bookkeeping**
- `created_at` — TEXT (ISO, UTC), NOT NULL.
- `updated_at` — TEXT (ISO, UTC), NOT NULL (generic last-touch).

> **Four timestamps, four meanings:** `updated_at` (any change), `status_changed_at`
> (status only), `last_seen_at` (ingest sighting), `last_follow_up_at` (follow-up
> only). Conflating any two is the bug the design warns about. They are separate
> columns precisely so a follow-up write touches one without the other.

#### `artifacts` table
- `id` — INTEGER, PK.
- `job_id` — INTEGER, NOT NULL, FK → `jobs(id)`, `ON DELETE RESTRICT`.
- `kind` — TEXT, NOT NULL: `cv_pdf` | `cover_letter` | `follow_up_email`. Python-enum validated.
- `path` — TEXT, NOT NULL (filesystem path; bytes live on disk, not in the row).
- `created_at` — TEXT (ISO, UTC), NOT NULL.

#### Conventions
- **Timestamps: TEXT ISO-8601, always UTC.** Lexicographic order = chronological order (age queries become string comparisons); UTC removes timezone ambiguity (SG is UTC+8 — mixing local and UTC would silently skew every ghost/expiry clock by 8 hours).
- **`status` and `kind` are plain TEXT validated by Python enums, no DB `CHECK`** — new values need no migration.
- **`score` nullable until scored.**
- **Artifacts: keep-all (history).** Re-tailoring inserts a new row; "the current CV" is `ORDER BY created_at DESC LIMIT 1` for that `kind`. Matches the project's audit-trail philosophy.
- **FK `RESTRICT`** is effectively moot: the design never hard-deletes a job (terminal states + filtering, not deletion). SQLite requires `PRAGMA foreign_keys = ON` per connection for the FK to be enforced at all.
- **Indexes:** the UNIQUE index on `fingerprint` is mandatory (it is the dedup mechanism). Composite indexes `(status, score)` and `(status, status_changed_at)` are deferred until row volume justifies them.

### 3b. State Machine

**States**
- *Active (pre-application):* `DISCOVERED` → `SCORED` → `TAILORED` → `PENDING_APPROVAL` → `APPLYING`\* → `APPLIED` (\* + `APPLY_FAILED`, both Phase 2)
- *Active (post-application):* `INTERVIEWING`, `OFFER`
- *Terminal:* `REJECTED`, `USER_SKIPPED`, `EXPIRED`, `ACCEPTED`, `DECLINED`
- *Semi-terminal:* `GHOSTED` (one resurrection edge)
- *Entry state:* `DISCOVERED` — set at insert, never transitioned *into*. The validator handles "new record" as a separate path from "transition."

**Transition table** (`auto` = deterministic system rule, no LLM; `user` = button/chat; `worker` = Phase 2 apply worker; `config` = `auto_apply`)

| From | To | Trigger |
|---|---|---|
| DISCOVERED | SCORED | auto: score ≥ threshold |
| DISCOVERED | REJECTED | auto: score < threshold |
| SCORED | TAILORED | auto: tailor batch |
| SCORED | REJECTED | auto: staleness (`stale_after_days`) |
| TAILORED | PENDING_APPROVAL | auto |
| TAILORED | APPLYING | config: `auto_apply` (P2) |
| PENDING_APPROVAL | APPLIED | user: Mark Applied |
| PENDING_APPROVAL | USER_SKIPPED | user: Skip |
| PENDING_APPROVAL | EXPIRED | auto: `pending_expiry_days` |
| PENDING_APPROVAL | APPLYING | user: Apply for me (P2) |
| APPLYING | APPLIED | worker ok (P2) |
| APPLYING | APPLY_FAILED | worker fail (P2) |
| APPLY_FAILED | APPLIED | user: applied by hand (P2) |
| APPLY_FAILED | USER_SKIPPED | user: gave up (P2) |
| APPLIED | INTERVIEWING | user |
| APPLIED | REJECTED | user |
| APPLIED | GHOSTED | auto: `ghost_after_days` |
| APPLIED | DECLINED | user: candidate withdraws |
| INTERVIEWING | INTERVIEWING | user: next round (re-stamps clock) |
| INTERVIEWING | OFFER | user |
| INTERVIEWING | REJECTED | user |
| INTERVIEWING | GHOSTED | auto: `ghost_after_days` |
| INTERVIEWING | DECLINED | user: candidate withdraws |
| OFFER | ACCEPTED | user |
| OFFER | DECLINED | user: candidate declines |
| OFFER | REJECTED | user: offer rescinded (rare) |
| GHOSTED | INTERVIEWING | user: resurrection |

Terminal states have no outgoing edges.

**`REJECTED` vs `DECLINED`:** `REJECTED` = the company said no (at any stage);
`DECLINED` = the candidate said no — either withdrawing from the process
(`APPLIED` / `INTERVIEWING`) or declining an offer. It is the post-application
mirror of `USER_SKIPPED` (the candidate's pre-application no). Kept distinct on
purpose.

**Representation & enforcement**
- One transition map in the backend is the single source of truth.
- `validate_transition(from, to)` lives in the **write path** — the unconditional enforcer; every status change (button, chat tool, scheduler, worker) passes through it. Illegal → rejected before any write. This is what makes illegal histories physically impossible.
- `can_transition(from, to)` is a read-only courtesy for the agent; it never gates anything.
- **Self-loop `INTERVIEWING → INTERVIEWING` is legal** and re-stamps `status_changed_at` (resets the ghost clock — a new round means the company is active). A naive "reject if from == to" guard would wrongly break this.
- **`auto_apply` is decided at the call site, not in the FSM.** Both `TAILORED → PENDING_APPROVAL` and `TAILORED → APPLYING` are legal; config picks which fires.
- **The FSM is structural only** — it does not encode *who* may trigger a move. Actor-authorization is deliberately omitted for now (single-user; each transition has one natural caller in practice).

### 3c. Upsert Contract (`POST /jobs`)

After Pydantic validation (invalid → 422, no write) the backend computes the
fingerprint via the external function, then takes one of two paths.

**Fresh hit (no row with this fingerprint) → INSERT:**
- From `JobCreate`: `company`, `title`, `description`, `url`, `posted_at?`, `metadata?`.
- `fingerprint` ← computed.
- `status = DISCOVERED`, `score = NULL`.
- `seen_count = 1`, `follow_up_count = 0`, `last_follow_up_at = NULL`.
- `created_at = updated_at = status_changed_at = last_seen_at = now (UTC)`.

**Duplicate hit (fingerprint exists) → update liveness only:**
- `seen_count = seen_count + 1`
- `last_seen_at = now`
- `updated_at = now`
- **Frozen:** `status`, `score`, `status_changed_at`, all content, `follow_up_count`, `created_at`.

> **`status_changed_at` must NOT move on a re-sighting.** If it did, a listing that
> keeps reappearing in daily scrapes would reset its ghost clock every day and
> never ghost. `last_seen_at` says "still live"; `status_changed_at` says "how long
> in this state." Separate columns, separate purposes. Same bug-class as the
> follow-up/ghost separation.

**Atomic implementation** — one statement, no read-then-write race (also the seam where the future single-writer serialization lands):

```sql
INSERT INTO jobs (...) VALUES (...)
ON CONFLICT(fingerprint) DO UPDATE SET
    seen_count = seen_count + 1,
    last_seen_at = excluded.last_seen_at,
    updated_at  = excluded.updated_at;
```

**Idempotency claim (state exactly):** idempotent with respect to content and
status — repeated calls never alter `status`, `score`, content, or
`status_changed_at`. Intentionally **non-idempotent** on `seen_count` /
`last_seen_at`, which advance every call by design (the liveness signal).

**Boundary reaffirm:** the upsert never scores. It always lands on `DISCOVERED`
with `score = NULL`. Scoring is the next step, run by the scrape job, which reads
`DISCOVERED` rows and transitions them via the status endpoint.

### 3d. API Endpoints

**Conventions:** `200` success, `422` invalid body, `404` job not found,
`409` conflict (illegal transition, or operation invalid for current state).
A `Job` in any response is the full row; an `Artifact` is the full artifacts row.

**`POST /jobs` — ingest (upsert)**
- In: `JobCreate { company, title, description, url, posted_at?, metadata? }`. Caller never supplies `status`, `score`, `fingerprint`, timestamps, or counters.
- Out: `Job`. (No `was_created` flag — the returned row's `seen_count` already encodes it: `1` = created this call, `>1` = duplicate hit.)
- Does: the 3c upsert. Always `DISCOVERED`. Never scores.

**`PATCH /jobs/{id}/status` — transition (the FSM enforcement point)**
- In: `{ to_status: Status }`.
- Out: updated `Job`.
- Does: load job → `validate_transition(current, to_status)` → if legal, set status, stamp `status_changed_at = now`, `updated_at = now`; illegal → `409`. Self-loop `INTERVIEWING→INTERVIEWING` allowed (re-stamps clock). Every status change converges here.

**`GET /jobs` — query (general read; thin shell for external callers)**
- In: `status` (set; **default = active pipeline**, terminal only on explicit request), `limit`, `offset`.
- Out: `list[Job]`.
- Thin wrapper over the general repository read function. The agent and scheduler call repository functions in-process; this endpoint exists for out-of-process callers (external scripts, debugging, future frontend). Not load-bearing in Phase 1.

**`POST /jobs/{id}/artifacts` — register artifact**
- In: `ArtifactCreate { kind, path }`.
- Out: `Artifact`.
- Does: 404 if no job; else always *insert* a new row (keep-all), `created_at = now`. **Does not change status** (Decision A).

**`POST /jobs/{id}/follow-up` — record follow-up (two-clocks made physical)**
- In: none (optional `{ note? }`).
- Out: updated `Job`.
- Does: increment `follow_up_count`, stamp `last_follow_up_at = now`, `updated_at = now`. **Cannot** touch `status` or `status_changed_at`. Requires `status == APPLIED`, else `409` (Decision B).

**`POST /chat` — agent transport (thin)**
- In: `{ message, ...context }`.
- Out: `{ reply, ... }`.
- Does: hand to the agent, return its reply. Backend owns the route; the agent owns the body. Stateless per call.

**Decision A — artifact registration is decoupled from the status move.**
`POST .../artifacts` only registers. The tailoring service then calls
`PATCH .../status` separately (`SCORED → TAILORED → PENDING_APPROVAL`, or
`→ APPLYING` under `auto_apply`). One endpoint, one responsibility — and it keeps
on-demand re-tailoring safe: regenerating a CV for an already-`APPLIED` job
registers a fresh artifact without attempting an illegal transition. Tailoring
sequence: query top-N `SCORED` → LLM selects (validated JSON) → deterministic
render to PDF on disk → `POST .../artifacts` → `PATCH .../status` → Telegram push.

**Decision B — `follow-up` requires `status == APPLIED`.** A follow-up only has
meaning while awaiting a response after applying. The server-side precondition
guards against a race (job rejected/ghosted between the daily check and the
action) and keeps the endpoint consistent with "the backend validates."

### Read surfaces (Decision C)

Every DB read is a repository function (the only place SQL lives). Two shapes:
- **General-purpose:** "list jobs by status, paginated." Backs the agent's `query_jobs` tool (in-process) and the thin HTTP `GET /jobs` (for out-of-process callers).
- **Specific, rule-bound, named functions:** top-N `SCORED` for tailoring; `PENDING_APPROVAL` older than expiry; `APPLIED`/`INTERVIEWING` older than ghost window; `APPLIED` older than follow-up window with `follow_up_count = 0`. Called in-process by the scheduled jobs; each has a precise typed signature and is unit-tested directly.

Decision: do **not** build one over-configurable query for both. General stays
general; each pipeline-critical query is its own named function. The LLM never
calls HTTP — it calls a tool that calls the general function in-process.

---

## 4. Components, Files & Work Packages

> v1 file layout is inferred; reconcile against the repo. "(extend)" = likely
> exists in v1; "(new)" = introduced in v2. Only `app/models/enums.py` is a
> confirmed path (from the `from app.models.enums import ...` import); the
> subfolder names below (`db/`, `services/`, `routers/`) are a proposed layout —
> match them to your actual repo.

### Folder & file layout
```
app/
├── __init__.py
├── main.py                  # app creation, router includes, lifespan DB-init   (WP-F)
├── config.py                # DB path + pragmas
├── models/
│   ├── __init__.py
│   ├── enums.py             # ApplicationStatus, ArtifactKind, state machine     (WP-B)
│   └── job.py               # JobCreate, StatusUpdate, JobResponse, Artifact*    (WP-A)
├── db/
│   ├── __init__.py
│   ├── database.py          # connection, pragmas, DDL, schema init              (WP-A)
│   └── repository.py        # all SQL: upsert, status write, reads               (WP-C)
├── services/
│   ├── __init__.py
│   └── service.py           # validated ops: ingest_job, transition_status, …    (WP-D)
└── routers/
    ├── __init__.py
    └── routes.py            # FastAPI endpoints + /chat forwarder                (WP-E)

tests/
├── __init__.py
├── conftest.py              # fixtures: temp DB, client, mock_repository, mock_fingerprint
├── test_enums.py            # WP-B  (FSM — done, 33 passing)
├── test_job.py              # WP-A  model validation
├── test_repository.py       # WP-C
├── test_service.py          # WP-D
├── test_routes.py           # WP-E
└── test_main.py             # WP-F  startup
```

### File set
- `enums.py` (extend) — the root of the dependency graph. Holds `ApplicationStatus`, `ArtifactKind`, **and** the state machine (`VALID_TRANSITIONS`, `transition`, `can_transition`, `legal_targets`, `InvalidTransitionError`). Both `job.py` and the service layer import it.
- `job.py` (extend) — Pydantic I/O models: `JobCreate`, `StatusUpdate`, `JobResponse`, `ArtifactCreate`, `ArtifactResponse` (enums imported from `enums.py`).
- `database.py` (extend) — connection handling, startup pragmas (`foreign_keys = ON`; WAL noted for 0.7.0), DDL for the extended `jobs` table, the `artifacts` table, and the unique `fingerprint` index.
- `repository.py` (extend) — all SQL: upsert, status write, artifact insert, follow-up update, general list query, named scheduler queries.
- `service.py` (new) — shared validated operations both routes and in-process callers invoke.
- `routes.py` (extend) — FastAPI endpoints (thin over the service) + the thin `/chat` forwarder.
- `main.py` (extend) — app creation, router includes, lifespan DB-init.
- `config.py` (minor) — DB path + pragmas. Pipeline thresholds belong to consumer layers.

### Work packages

**WP-A — Data model.** Realizes 3a (`job.py` models; `database.py` DDL). Unit-tested: model validation (required fields; `JobCreate` rejects `status`/`score` via `extra="forbid"`; timestamps serialize ISO-UTC) + a round-trip test that builds the schema in a temp SQLite and confirms both tables and the unique fingerprint index. No dependencies.

**WP-B — FSM.** Realizes 3b (the state-machine portion of `enums.py`). Pure logic, zero I/O — the cleanest unit. Unit-tested: every legal edge passes, every illegal edge rejected, `INTERVIEWING` self-loop allowed, terminal states have no exits. Built alongside the enums.

**WP-C — Repository.** Realizes 3c persistence + the named reads (`repository.py`). Atomic `ON CONFLICT` upsert, status write, artifact insert, follow-up update, general list query, scheduler queries. Unit-tested against a temp SQLite seeded with known rows: upsert inserts-then-bumps `seen_count`; follow-up update leaves `status_changed_at` untouched; each scheduler query returns the right subset/order. Fingerprint injected and mocked. Depends on WP-A.

**WP-D — Service layer.** Realizes 3c/3d orchestration (`service.py`): `ingest_job` (fingerprint via injected fn → repository upsert), `transition_status` (read current → FSM validate → write), `register_artifact`, `record_follow_up`. The single validated path on which HTTP routes, agent tools, and scheduler converge. Unit-tested by mocking the repository + fingerprint and asserting call order + that an illegal transition is refused before any write. Depends on WP-B and WP-C.

**WP-E — API routes.** Realizes the HTTP surface of 3d (`routes.py`): thin wrappers over service operations + the `/chat` forwarder. Integration-tested with httpx over a temp DB: status-code conventions; illegal transition surfaces as 409. Depends on WP-D.

**WP-F — App wiring.** App skeleton in `main.py`: create app, include routers, lifespan DB-init (scheduler registration sits in the hook, but the jobs themselves are the scheduling layer — only the mechanism is here). Startup integration test: boot the app, confirm schema created and routes respond. Depends on WP-E.

### Dependency order
`enums.py` is the root (both A and B import it). WP-A and WP-B are then the foundation (no further dependencies) → WP-C (needs A) → WP-D (needs B + C) → WP-E (needs D) → WP-F (wires last). Critical path: **enums → A → C → D → E → F**, with **B** parallel to A after enums and joining at D.

**Not work packages here:** the fingerprint (dedup layer; imported behind a stable
signature, mocked in tests) and the pipeline thresholds in config (consumed by
other layers, though the columns they act on are defined here).

---

## 5. Build & Test Order

Order respects the §4 dependency graph and the project's TDD discipline:
Red-Green-Refactor; roughly 70% unit / 25% integration / 5% E2E; all external
dependencies (fingerprint, LLM) mocked except `live`-marked tests; query
functions tested as plain functions, never through the scheduler.

**Sequence:** `enums.py` → **WP-B (FSM)** → **WP-A (data model)** → WP-C → WP-D
→ WP-E → WP-F. B is built before A by preference (purest unit — no I/O, no
fixtures, no mocking; locks the correctness core D depends on), though A and B
are interchangeable once `enums.py` exists.

**Step 0 — `enums.py`.** `Status` and `ArtifactKind`. No test of its own.

**WP-B — FSM** (pure, no DB). First red test:
```python
def test_legal_edge_allowed():
    assert can_transition(Status.DISCOVERED, Status.SCORED)

def test_illegal_edge_rejected():
    assert not can_transition(Status.REJECTED, Status.OFFER)

def test_interviewing_self_loop_allowed():
    assert can_transition(Status.INTERVIEWING, Status.INTERVIEWING)

def test_terminal_states_have_no_exits():
    for s in (Status.REJECTED, Status.USER_SKIPPED, Status.EXPIRED,
              Status.ACCEPTED, Status.DECLINED):
        assert legal_targets(s) == set()
```
Hand-pick representative legal/illegal edges plus structural properties; do not
loop the map back against itself.

**WP-A — Data model.** First red tests (models + schema):
```python
def test_jobcreate_refuses_caller_set_status():
    with pytest.raises(ValidationError):
        JobCreate(company="X", title="Y", description="Z",
                  url="http://e", status="DISCOVERED")  # extra='forbid'

async def test_schema_creates_tables_and_unique_fingerprint_index(tmp_db):
    await init_db(tmp_db)
    assert await table_exists(tmp_db, "jobs")
    assert await table_exists(tmp_db, "artifacts")
    assert await unique_index_on(tmp_db, "jobs", "fingerprint")
```

**WP-C — Repository** (real temp SQLite — do not mock the DB; fingerprint
injected). The frozen-clock assertions are the load-bearing ones:
```python
async def test_upsert_fresh_inserts_discovered(repo, empty_db):
    row = await repo.upsert_job(make_jobcreate(), fingerprint="abc", now=t1)
    assert row.status == Status.DISCOVERED
    assert row.seen_count == 1 and row.score is None

async def test_upsert_duplicate_bumps_seen_and_freezes_clock(repo, db_with_job):
    row = await repo.upsert_job(make_jobcreate(), fingerprint="abc", now=t2)
    assert row.seen_count == 2
    assert row.status_changed_at == t1        # must NOT move on re-sighting

async def test_follow_up_leaves_status_clock_untouched(repo, applied_job):
    row = await repo.record_follow_up(applied_job.id, now=t2)
    assert row.follow_up_count == 1 and row.last_follow_up_at == t2
    assert row.status_changed_at == applied_job.status_changed_at
```
Plus one test per named scheduler query (tailoring select ordering; ghost/expiry
threshold boundary).

**WP-D — Service** (mock repository + fingerprint). Load-bearing test: no write
on an illegal transition:
```python
async def test_transition_refuses_illegal_before_any_write(service, mock_repository):
    with pytest.raises(IllegalTransition):
        await service.transition_status(applied_job_id, Status.OFFER)
    mock_repository.write_status.assert_not_called()

async def test_ingest_computes_fingerprint_then_upserts(service, mock_repository, mock_fingerprint):
    await service.ingest_job(make_jobcreate())
    mock_fingerprint.assert_called_once()
    mock_repository.upsert_job.assert_called_once()
```

**WP-E — Routes** (integration, httpx, temp DB):
```python
async def test_patch_illegal_status_returns_409(client, applied_job):
    r = await client.patch(f"/jobs/{applied_job.id}/status",
                           json={"to_status": "OFFER"})
    assert r.status_code == 409

async def test_follow_up_on_non_applied_returns_409(client, scored_job):
    r = await client.post(f"/jobs/{scored_job.id}/follow-up")
    assert r.status_code == 409
```

**WP-F — App wiring** (startup integration). Boot the app through the lifespan
hook, confirm the schema exists and `GET /jobs` answers `200`.

**E2E (minimal, ~5%):** one thin slice — `POST /jobs`, then `PATCH` through a
legal sequence, then `GET` it back. Fuller cross-layer E2E belongs at the
overview level, not here.

---

## 6. Invariants

1. The scraper never reasons; the agent never writes to the DB directly. All writes go through the backend's single validated path.
2. The LLM proposes; the backend validates and executes (FSM, Pydantic, repository). No LLM call originates in this layer.
3. Every external dependency (fingerprint, DB) is injected and mocked in tests.
4. The two-clocks rule is physical: follow-up writes cannot move `status_changed_at`; re-sightings cannot move it either.
5. Repository pattern isolation — a DB-driver switch touches only `repository.py` / `database.py`.
6. Timestamps are ISO-8601 UTC everywhere.

---

## 7. Deferred / Open

- **SQLite single-writer constraint** — named, not solved. One writer at a time across the whole file; concurrent writes (scrape burst vs. a user-triggered tool call) yield `SQLITE_BUSY`. Invisible at dev scale. Mitigations (0.7.0 reliability): WAL mode, `busy_timeout`, short write transactions, optionally a single writer connection/queue. The single validated write path pre-positions this fix to one place.
- **v1 file layout reconciliation** — confirm actual filenames/structure against §4.
