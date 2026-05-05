# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

An agentic job application tracker. Users interact in natural language; the system handles discovery, storage, scoring, and status tracking. Architecture is split into five strict layers — each communicates only through defined interfaces and knows nothing about the internal implementation of adjacent layers.

Full architecture and layer specs live in `.agent/overview.md`. Backend implementation spec is in `.agent/Backend.md`. TDD test map is in `.agent/backend_tdd_overview.md`.

## Commands

```bash
# Run all tests
pytest

# Unit tests only (fastest feedback loop)
pytest tests/unit/

# Single test
pytest tests/unit/test_state_machine.py::test_rejected_is_terminal -v

# With coverage
pytest --cov=app --cov-report=term-missing

# Run the API server
uvicorn app.main:app --reload
```

## Project Structure

```
app/
  main.py          # FastAPI app, middleware, lifespan hook (APScheduler mounts here)
  config.py        # pydantic-settings — all env vars including API keys go here, never hardcoded
  models/
    enums.py       # ApplicationStatus enum + VALID_TRANSITIONS dict + transition() + InvalidTransitionError
    job.py         # JobCreate / JobUpdate / JobResponse Pydantic schemas
  db/
    database.py    # get_db() dependency, create_tables() — overridden in tests via dependency_overrides
    repository.py  # ALL SQL lives here — routes never construct queries
  routes/
    jobs.py        # Thin route handlers: validate input, call repo, return response. No SQL, no logic.
tests/
  conftest.py      # async_client fixture — fresh tmp_path DB per test via app.dependency_overrides[get_db]
  unit/            # Pure function tests, no DB, no HTTP
  integration/     # Routes + real in-memory SQLite, one fresh DB per test
  e2e/             # Full lifecycle tests
```

## Architecture Rules

**Repository is the only file that knows the DB driver.** Routes call repo functions — never `db.execute()` directly. Swapping SQLite → PostgreSQL touches only `repository.py`.

**Route handlers contain zero business logic.** Pydantic validates input automatically; handlers call the repo and return. Status transition logic lives in `enums.py` and is enforced inside `repository.update_job_status()` before any DB write.

**The agent never writes to the DB directly.** It calls the backend HTTP API via tool calls. The backend validates and executes.

## State Machine

```
FOUND → APPLIED → SCREENING → INTERVIEW → OFFER
                                         → REJECTED
         → REJECTED (valid from any active stage)
```

`OFFER` and `REJECTED` are terminal — no transitions out. Self-transitions (e.g. `FOUND → FOUND`) are invalid. `transition(current, next)` raises `InvalidTransitionError` on any invalid move; routes catch this and return 422.

## Test Isolation (Critical)

Integration and e2e tests must use an isolated DB, never `./jobs.db`. `conftest.py` achieves this by:
1. Creating a fresh SQLite file in pytest's `tmp_path` (unique per test)
2. Registering `app.dependency_overrides[get_db] = override_get_db` pointing at that file
3. Clearing `app.dependency_overrides` on teardown

`pytest.ini` must have `asyncio_mode = auto` — without it, async tests are silently skipped and pytest reports zero failures even though nothing ran.

## Key Design Decisions

- **Fingerprint deduplication:** `SHA-256(company.lower() + "|" + role.lower() + "|" + url)` stored as a `UNIQUE` column. Duplicate inserts return the existing record silently (`created: False`) — no error raised.
- **Notes are sticky:** `UPDATE SET notes = COALESCE(?, notes)` — passing `notes=None` on a status patch preserves existing notes rather than clearing them.
- **`created` field on `JobResponse`:** Tells the caller whether the POST created a new record or returned an existing one. Both cases return 201.
- **Config via `pydantic-settings`:** `db_path`, `log_level` now; Tavily and Anthropic API keys added in later phases. `.env` file, never committed.

## Coverage Targets

- `app/models/enums.py` — 100%
- `app/db/repository.py` — 90%+
- `app/routes/jobs.py` — 90%+
- Overall `app/` — 80% minimum

## Build Order

Layers are built in dependency order — later layers call earlier ones:

1. **Week 1** — Backend API (current)
2. **Week 2** — Agent Brain (Anthropic API, tool calling, ReAct loop)
3. **Week 3** — Scraping Layer (Tavily API)
4. **Week 4** — Scoring & Deduplication (pure functions)
5. **Week 5** — Frontend (Streamlit) + Scheduling (APScheduler)
