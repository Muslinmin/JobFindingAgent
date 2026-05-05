# TDD Overview — Job Application Tracker Backend

---

## How Tests Map to Source Files

```
SOURCE FILES                          TEST FILES
─────────────────────────────────────────────────────────────────

app/models/enums.py    ──────────►  tests/unit/test_state_machine.py
app/models/job.py      ──────────►  tests/unit/test_schemas.py
                                           │
app/db/database.py  ◄──────────────────── │ (fixture overrides get_db)
app/db/repository.py   ──────────►  tests/integration/test_routes.py
app/routes/jobs.py     ──────────►  tests/integration/test_routes.py
                                           │
                       ──────────►  tests/e2e/test_crud_pipeline.py
                                    (all layers together)

conftest.py  ◄───────────── shared fixtures used by integration + e2e
```

---

## File-by-File Breakdown

---

### `app/models/enums.py`

```
┌──────────────────────────────────────────────┐
│  enums.py                                    │
│                                              │
│  class ApplicationStatus(str, Enum)          │
│    FOUND / APPLIED / SCREENING               │
│    INTERVIEW / OFFER / REJECTED              │
│                                              │
│  VALID_TRANSITIONS: dict                     │
│    { status → [allowed next statuses] }      │
│                                              │
│  class InvalidTransitionError(Exception)     │
│                                              │
│  def transition(current, next) → status      │
│    raises InvalidTransitionError if invalid  │
└──────────────────────────────────────────────┘
```

**Test file:** `tests/unit/test_state_machine.py`

```
┌──────────────────────────────────────────────────────────────────┐
│  test_state_machine.py                                           │
│                                                                  │
│  test_valid_forward_transition()                                 │
│    transition(APPLIED, SCREENING) == SCREENING                   │
│                                                                  │
│  test_cannot_skip_stages()                                       │
│    transition(FOUND, OFFER) → raises InvalidTransitionError      │
│                                                                  │
│  test_offer_is_terminal()                                        │
│    transition(OFFER, SCREENING) → raises InvalidTransitionError  │
│                                                                  │
│  test_rejected_is_terminal()                                     │
│    transition(REJECTED, INTERVIEW) → raises InvalidTransitionError│
│                                                                  │
│  test_can_reject_from_any_active_stage()                         │
│    [APPLIED, SCREENING, INTERVIEW] → REJECTED all valid          │
│                                                                  │
│  test_cannot_transition_to_same_status()          ← easy to miss │
│    transition(FOUND, FOUND) → raises InvalidTransitionError      │
└──────────────────────────────────────────────────────────────────┘
```

**No fixtures needed. Pure function calls. Runs in milliseconds.**

---

### `app/models/job.py`

```
┌──────────────────────────────────────────────┐
│  job.py                                      │
│                                              │
│  class JobCreate(BaseModel)                  │
│    company: str                              │
│    role: str                                 │
│    url: HttpUrl                              │
│    source: str = "manual"                    │
│    notes: str | None = None                  │
│                                              │
│  class JobUpdate(BaseModel)                  │
│    status: ApplicationStatus                 │
│    notes: str | None = None                  │
│                                              │
│  class JobResponse(BaseModel)                │
│    id, company, role, url, status            │
│    source, notes, date_logged                │
│    created: bool                             │
└──────────────────────────────────────────────┘
```

**Test file:** `tests/unit/test_schemas.py`

```
┌──────────────────────────────────────────────────────────────────┐
│  test_schemas.py                                                 │
│                                                                  │
│  test_job_create_requires_company_role_url()                     │
│    JobCreate(company=..., role=..., url=...) → valid             │
│                                                                  │
│  test_job_create_rejects_invalid_url()                           │
│    JobCreate(url="not-a-url") → raises ValidationError           │
│                                                                  │
│  test_job_create_source_defaults_to_manual()                     │
│    JobCreate(...).source == "manual"                             │
│                                                                  │
│  test_job_create_notes_is_optional()                             │
│    JobCreate(..., notes=None) → valid                            │
│                                                                  │
│  test_job_update_rejects_invalid_status()                        │
│    JobUpdate(status="banana") → raises ValidationError           │
└──────────────────────────────────────────────────────────────────┘
```

**No fixtures needed. Pydantic validation is synchronous.**

---

### `app/db/database.py`

```
┌──────────────────────────────────────────────┐
│  database.py                                 │
│                                              │
│  async def get_db()                          │
│    yields aiosqlite connection               │
│    ← this is what Depends(get_db) calls      │
│                                              │
│  async def create_tables()                   │
│    CREATE TABLE IF NOT EXISTS jobs (...)     │
│    ← called in conftest fixture              │
└──────────────────────────────────────────────┘
```

**Not tested directly.** Instead it is *overridden* in `conftest.py` so every
integration and e2e test uses an isolated in-memory DB instead of `./jobs.db`.

```
┌──────────────────────────────────────────────────────────────────┐
│  conftest.py  — shared fixture used by integration + e2e         │
│                                                                  │
│  @pytest.fixture                                                 │
│  async def async_client(tmp_path):                               │
│    1. create fresh SQLite file in tmp_path (isolated per test)   │
│    2. run CREATE TABLE on it                                     │
│    3. define override_get_db() pointing to that file             │
│    4. app.dependency_overrides[get_db] = override_get_db         │
│    5. yield AsyncClient(app=app, base_url="http://test")         │
│    6. app.dependency_overrides.clear()   ← teardown              │
│                                                                  │
│  KEY POINT: tmp_path is a built-in pytest fixture that gives     │
│  each test its own temp directory. No two tests share a DB file. │
└──────────────────────────────────────────────────────────────────┘
```

---

### `app/db/repository.py`

```
┌──────────────────────────────────────────────┐
│  repository.py                               │
│                                              │
│  async def insert_job(db, job, fingerprint)  │
│    → (dict, bool)  ← (record, created)       │
│    catches IntegrityError → returns False    │
│                                              │
│  async def get_all_jobs(db, status_filter)   │
│    → list[dict]                              │
│                                              │
│  async def get_job_by_id(db, job_id)         │
│    → dict | None                             │
│                                              │
│  async def get_job_by_fingerprint(db, fp)    │
│    → dict | None                             │
│                                              │
│  async def update_job_status(db, id, status) │
│    calls transition() — can raise            │
│    → dict | None                             │
│                                              │
│  async def delete_job(db, job_id)            │
│    → bool                                    │
└──────────────────────────────────────────────┘
```

Repository functions are tested **indirectly** through the route integration tests.
If you want direct repo tests (optional, useful for complex SQL):

```
┌──────────────────────────────────────────────────────────────────┐
│  tests/integration/test_repository.py  (optional, direct)       │
│                                                                  │
│  test_insert_job_returns_true_on_first_insert(db)                │
│    _, created = await insert_job(db, job, fp)                    │
│    assert created == True                                        │
│                                                                  │
│  test_insert_job_returns_false_on_duplicate(db)                  │
│    insert twice with same fingerprint                            │
│    assert created == False on second call                        │
│                                                                  │
│  test_get_all_jobs_filters_by_status(db)                         │
│    insert found + applied, filter by "found" → only 1 returned  │
│                                                                  │
│  test_update_status_persists_change(db)                          │
│    insert → update → get_by_id → check status changed           │
│                                                                  │
│  test_delete_returns_false_for_missing_id(db)                    │
│    delete(9999) → False                                          │
└──────────────────────────────────────────────────────────────────┘
```

---

### `app/routes/jobs.py`

```
┌──────────────────────────────────────────────┐
│  jobs.py                                     │
│                                              │
│  def make_fingerprint(job) → str             │
│    SHA-256 of company|role|url (lowercased)  │
│                                              │
│  POST   /jobs              create_job()      │
│  GET    /jobs              list_jobs()       │
│  GET    /jobs/{id}         get_job()         │
│  PATCH  /jobs/{id}/status  update_status()   │
│  DELETE /jobs/{id}         delete_job()      │
└──────────────────────────────────────────────┘
```

**Test file:** `tests/integration/test_routes.py`

```
┌──────────────────────────────────────────────────────────────────┐
│  test_routes.py                                                  │
│                                                                  │
│  ── POST /jobs ──────────────────────────────────────────────    │
│  test_create_job_returns_201(async_client)                       │
│    response.status_code == 201                                   │
│    response.json()["created"] == True                            │
│                                                                  │
│  test_create_job_default_status_is_found(async_client)           │
│    response.json()["status"] == "found"                          │
│                                                                  │
│  ── GET /jobs ───────────────────────────────────────────────    │
│  test_list_jobs_returns_all(async_client)                        │
│    insert 3 → GET /jobs → len == 3                               │
│                                                                  │
│  test_status_filter_returns_correct_subset(async_client)         │
│    insert 2 found + 1 applied → GET /jobs?status=found → len==2  │
│                                                                  │
│  ── GET /jobs/{id} ──────────────────────────────────────────    │
│  test_get_job_by_id_returns_correct_record(async_client)         │
│    insert → get by id → check company/role match                 │
│                                                                  │
│  test_get_nonexistent_job_returns_404(async_client)              │
│    GET /jobs/9999 → 404                                          │
│                                                                  │
│  ── PATCH /jobs/{id}/status ─────────────────────────────────    │
│  test_valid_transition_returns_200(async_client)                 │
│    found → applied → 200, status == "applied"                    │
│                                                                  │
│  test_invalid_transition_returns_422(async_client)               │
│    found → offer (skip) → 422                                    │
│                                                                  │
│  test_patch_nonexistent_job_returns_404(async_client)            │
│    PATCH /jobs/9999/status → 404                                 │
│                                                                  │
│  ── DELETE /jobs/{id} ───────────────────────────────────────    │
│  test_delete_job_removes_record(async_client)                    │
│    insert → delete → GET by id → 404                            │
│                                                                  │
│  test_delete_nonexistent_job_returns_404(async_client)           │
│    DELETE /jobs/9999 → 404                                       │
└──────────────────────────────────────────────────────────────────┘
```

---

### `tests/e2e/test_crud_pipeline.py`

```
┌──────────────────────────────────────────────────────────────────┐
│  test_crud_pipeline.py                                           │
│                                                                  │
│  test_full_crud_lifecycle(async_client)                          │
│    POST  /jobs           → 201, status="found"                   │
│    GET   /jobs/{id}      → status="found"                        │
│    PATCH /jobs/{id}/status → 200, status="applied"               │
│    PATCH /jobs/{id}/status → 200, status="screening"             │
│    DELETE /jobs/{id}     → 204                                   │
│    GET   /jobs/{id}      → 404                                   │
│                                                                  │
│  test_rejection_from_any_stage(async_client)                     │
│    create → applied → screening → REJECTED → 200                 │
│    try to move from rejected → 422                               │
└──────────────────────────────────────────────────────────────────┘
```

---

## Execution Order When You Run `pytest`

```
pytest
  │
  ├── conftest.py loads → async_client fixture registered
  │
  ├── tests/unit/              ← no DB, no HTTP, pure functions
  │     test_state_machine.py  runs first, fastest
  │     test_schemas.py
  │
  ├── tests/integration/       ← each test gets a fresh tmp DB
  │     test_routes.py         fixture spins up / tears down per test
  │
  └── tests/e2e/               ← full lifecycle, one test at a time
        test_crud_pipeline.py
```

Each integration and e2e test gets its own fresh database because `async_client`
is a **function-scoped fixture** (the default). There is no shared state between tests.

---

## pytest.ini (required for async tests)

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

Without `asyncio_mode = auto`, async test functions are silently skipped
and pytest reports 0 failures even though nothing ran.

---

## TDD + Build Progress

Legend: each item follows **test file → source file**. Check the box when both the test is written and the implementation passes.

---

### Phase 1 — Data Models

#### `tests/unit/test_state_machine.py` → `app/models/enums.py`

- [ ] `ApplicationStatus` enum defined (6 values)
- [ ] `VALID_TRANSITIONS` dict defined
- [ ] `InvalidTransitionError` exception defined
- [ ] `transition()` function implemented
  - [ ] `test_valid_forward_transition` — `APPLIED → SCREENING` returns `SCREENING`
  - [ ] `test_cannot_skip_stages` — `FOUND → OFFER` raises
  - [ ] `test_offer_is_terminal` — `OFFER → SCREENING` raises
  - [ ] `test_rejected_is_terminal` — `REJECTED → INTERVIEW` raises
  - [ ] `test_can_reject_from_any_active_stage` — `APPLIED / SCREENING / INTERVIEW → REJECTED` all valid
  - [ ] `test_cannot_transition_to_same_status` — `FOUND → FOUND` raises

#### `tests/unit/test_schemas.py` → `app/models/job.py`

- [ ] `JobCreate` schema defined
- [ ] `JobUpdate` schema defined
- [ ] `JobResponse` schema defined
  - [ ] `test_job_create_requires_company_role_url`
  - [ ] `test_job_create_rejects_invalid_url`
  - [ ] `test_job_create_source_defaults_to_manual`
  - [ ] `test_job_create_notes_is_optional`
  - [ ] `test_job_update_rejects_invalid_status`

---

### Phase 2 — Database Layer

#### `app/db/database.py` (no direct tests — overridden via fixture)

- [ ] `get_db()` async generator implemented
- [ ] `create_tables()` implemented

#### `conftest.py` — shared fixture

- [ ] `async_client` fixture implemented
  - [ ] uses `tmp_path` for per-test DB isolation
  - [ ] overrides `get_db` via `app.dependency_overrides`
  - [ ] clears `dependency_overrides` on teardown
- [ ] `pytest.ini` created with `asyncio_mode = auto`

#### `tests/integration/test_repository.py` (optional) → `app/db/repository.py`

- [ ] `insert_job()` implemented
  - [ ] `test_insert_job_returns_true_on_first_insert`
  - [ ] `test_insert_job_returns_false_on_duplicate`
- [ ] `get_all_jobs()` implemented
  - [ ] `test_get_all_jobs_filters_by_status`
- [ ] `get_job_by_id()` implemented
- [ ] `get_job_by_fingerprint()` implemented
- [ ] `update_job_status()` implemented
  - [ ] `test_update_status_persists_change`
- [ ] `delete_job()` implemented
  - [ ] `test_delete_returns_false_for_missing_id`

---

### Phase 3 — API Routes

#### `tests/integration/test_routes.py` → `app/routes/jobs.py`

- [ ] `make_fingerprint()` implemented
- [ ] `POST /jobs`
  - [ ] `test_create_job_returns_201`
  - [ ] `test_create_job_default_status_is_found`
- [ ] `GET /jobs`
  - [ ] `test_list_jobs_returns_all`
  - [ ] `test_status_filter_returns_correct_subset`
- [ ] `GET /jobs/{id}`
  - [ ] `test_get_job_by_id_returns_correct_record`
  - [ ] `test_get_nonexistent_job_returns_404`
- [ ] `PATCH /jobs/{id}/status`
  - [ ] `test_valid_transition_returns_200`
  - [ ] `test_invalid_transition_returns_422`
  - [ ] `test_patch_nonexistent_job_returns_404`
- [ ] `DELETE /jobs/{id}`
  - [ ] `test_delete_job_removes_record`
  - [ ] `test_delete_nonexistent_job_returns_404`

---

### Phase 4 — Config, Logging & App Entry Point

- [ ] `app/config.py` — `Settings` with `db_path`, `log_level`
- [ ] `.env.example` created
- [ ] `app/main.py` — FastAPI app wired, loguru configured, router mounted

---

### Phase 5 — End-to-End

#### `tests/e2e/test_crud_pipeline.py`

- [ ] `test_full_crud_lifecycle` — create → read → patch → patch → delete → 404
- [ ] `test_rejection_from_any_stage` — create → applied → screening → rejected → patch attempt → 422

---

### Infra

- [ ] `requirements.txt` complete
- [ ] `Dockerfile` written
- [ ] `pytest --cov=app --cov-report=term-missing` passing with targets met
  - [ ] `app/models/enums.py` — 100%
  - [ ] `app/db/repository.py` — 90%+
  - [ ] `app/routes/jobs.py` — 90%+
  - [ ] overall `app/` — 80%+
