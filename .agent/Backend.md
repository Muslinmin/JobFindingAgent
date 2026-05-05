# Job Application Tracker — Backend Implementation Plan

---

## Overview

The backend is responsible for three things only: persisting job application data, exposing a clean HTTP API for reads and writes, and enforcing business rules at the data layer. It does not know about agents, scrapers, or scoring — those are separate concerns that call into this layer.

Stack: FastAPI (transport), SQLite via `aiosqlite` (persistence), Pydantic (validation), `loguru` (logging), `pydantic-settings` (config).

---

## Project Structure

```
job_tracker/
├── app/
│   ├── main.py              # FastAPI app entry point, middleware, lifespan
│   ├── config.py            # Environment config via pydantic-settings
│   ├── models/
│   │   ├── job.py           # Pydantic request/response schemas
│   │   └── enums.py         # ApplicationStatus enum + valid transitions
│   ├── db/
│   │   ├── database.py      # DB connection, table creation, lifespan hook
│   │   └── repository.py    # All SQL queries (Repository Pattern)
│   └── routes/
│       └── jobs.py          # CRUD API route handlers
├── tests/
│   ├── unit/
│   │   └── test_state_machine.py
│   ├── integration/
│   │   └── test_routes.py
│   └── e2e/
│       └── test_crud_pipeline.py
├── conftest.py
├── pytest.ini
├── .env
├── .env.example
├── Dockerfile
└── requirements.txt
```

---

## Phase 1 — Data Models

### Goal
Define the shape of a job application record and enforce valid status transitions at the model layer, before any DB operation is attempted.

### ApplicationStatus State Machine

Status flows in one direction only. Invalid transitions are rejected before any DB write occurs.

```
FOUND → APPLIED → SCREENING → INTERVIEW → OFFER
                                         → REJECTED
        → REJECTED (valid at any active stage)
```

```python
# app/models/enums.py

from enum import Enum

class ApplicationStatus(str, Enum):
    FOUND      = "found"
    APPLIED    = "applied"
    SCREENING  = "screening"
    INTERVIEW  = "interview"
    OFFER      = "offer"
    REJECTED   = "rejected"

VALID_TRANSITIONS = {
    ApplicationStatus.FOUND:      [ApplicationStatus.APPLIED, ApplicationStatus.REJECTED],
    ApplicationStatus.APPLIED:    [ApplicationStatus.SCREENING, ApplicationStatus.REJECTED],
    ApplicationStatus.SCREENING:  [ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED],
    ApplicationStatus.INTERVIEW:  [ApplicationStatus.OFFER, ApplicationStatus.REJECTED],
    ApplicationStatus.OFFER:      [],
    ApplicationStatus.REJECTED:   [],
}

class InvalidTransitionError(Exception):
    pass

def transition(current: ApplicationStatus, next: ApplicationStatus) -> ApplicationStatus:
    if next not in VALID_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"Cannot transition from '{current}' to '{next}'"
        )
    return next
```

### Pydantic Schemas

```python
# app/models/job.py

from pydantic import BaseModel, HttpUrl
from datetime import date
from app.models.enums import ApplicationStatus

class JobCreate(BaseModel):
    company: str
    role: str
    url: HttpUrl
    source: str = "manual"
    notes: str | None = None

class JobUpdate(BaseModel):
    status: ApplicationStatus
    notes: str | None = None

class JobResponse(BaseModel):
    id: int
    company: str
    role: str
    url: str
    status: ApplicationStatus
    source: str
    notes: str | None
    date_logged: date
    created: bool       # True = new insert, False = already existed (idempotent upsert)
```

The `created` field on the response tells the caller whether this was a new record or an existing one returned by the idempotency check — without raising an error either way.

---

## Phase 2 — Database Layer

### Goal
Set up an async SQLite connection, define the schema, and isolate all SQL behind a repository so routes never write raw queries.

### Schema

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT    UNIQUE NOT NULL,
    company     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'found',
    source      TEXT    NOT NULL DEFAULT 'manual',
    notes       TEXT,
    date_logged TEXT    NOT NULL
);
```

The `fingerprint` column is a SHA-256 hash of `(company + role + url)`, normalised to lowercase. It carries a `UNIQUE` constraint — the DB itself prevents duplicates at the storage level.

### DB Connection

```python
# app/db/database.py

import aiosqlite
from app.config import settings

async def get_db():
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db
# @TODO Agent take note : # CRITICAL: Use an isolated in-memory DB (:memory:) for all tests[cite: 44, 46, 62]. 
# Using ./jobs.db causes "state bleeding"—persistent changes from one test 
# will contaminate the next, leading to flaky failures[cite: 36, 40, 53]. 
# Every test must start with a blank slate to ensure reliability and speed[cite: 41, 57, 68]. 
# Avoid hitting the physical disk to prevent data pollution and ensure consistency 
# across environments[cite: 54, 55, 60]. If you don't override this in 
# conftest.py, you're testing on "dirty" data[cite: 36, 52].

async def create_tables():
    
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT    UNIQUE NOT NULL,
                company     TEXT    NOT NULL,
                role        TEXT    NOT NULL,
                url         TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'found',
                source      TEXT    NOT NULL DEFAULT 'manual',
                notes       TEXT,
                date_logged TEXT    NOT NULL
            )
        """)
        await db.commit()
```

### Repository Pattern

All SQL lives in `repository.py`. Routes call these functions — they never construct queries themselves. This means the DB driver can be swapped (SQLite → PostgreSQL) by changing one file, without touching any route handler.

```python
# app/db/repository.py

import aiosqlite
from datetime import date
from app.models.enums import ApplicationStatus, transition

async def insert_job(db, job, fingerprint: str):
    today = date.today().isoformat()
    try:
        cursor = await db.execute(
            """
            INSERT INTO jobs (fingerprint, company, role, url, source, notes, date_logged)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, job.company, job.role, str(job.url), job.source, job.notes, today)
        )
        await db.commit()
        return await get_job_by_id(db, cursor.lastrowid), True

    except aiosqlite.IntegrityError:
        existing = await get_job_by_fingerprint(db, fingerprint)
        return existing, False

async def get_all_jobs(db, status_filter: str | None = None) -> list[dict]:
    if status_filter:
        cursor = await db.execute("SELECT * FROM jobs WHERE status = ?", (status_filter,))
    else:
        cursor = await db.execute("SELECT * FROM jobs")
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_job_by_id(db, job_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None

async def get_job_by_fingerprint(db, fingerprint: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_job_status(db, job_id: int, new_status: ApplicationStatus, notes: str | None = None) -> dict | None:
    job = await get_job_by_id(db, job_id)
    if not job:
        return None
    transition(ApplicationStatus(job["status"]), new_status)  # raises InvalidTransitionError if invalid @TODO  check are we going to handle this in function or outside
    await db.execute(
        "UPDATE jobs SET status = ?, notes = COALESCE(?, notes) WHERE id = ?",
        (new_status.value, notes, job_id)
    )
    await db.commit()
    return await get_job_by_id(db, job_id)

async def delete_job(db, job_id: int) -> bool:
    cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0
```

---

## Phase 3 — API Routes

### Goal
Expose validated HTTP endpoints. Route handlers are thin — they validate input (Pydantic does this automatically), call the repository, and return the response. No SQL, no business logic inside the handler.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Create a job record (idempotent) |
| `GET` | `/jobs` | List all jobs, optional `?status=` filter |
| `GET` | `/jobs/{id}` | Get a single job by ID |
| `PATCH` | `/jobs/{id}/status` | Update status with transition validation |
| `DELETE` | `/jobs/{id}` | Remove a job record |

```python
# app/routes/jobs.py

import hashlib
from fastapi import APIRouter, Depends, HTTPException, Query
from app.models.job import JobCreate, JobUpdate, JobResponse
from app.db.database import get_db
from app.db import repository as repo
from app.models.enums import InvalidTransitionError

router = APIRouter(prefix="/jobs", tags=["jobs"])

def make_fingerprint(job: JobCreate) -> str:
    raw = f"{job.company.lower().strip()}|{job.role.lower().strip()}|{str(job.url)}"
    return hashlib.sha256(raw.encode()).hexdigest()




@router.post("", status_code=201, response_model=JobResponse)
async def create_job(payload: JobCreate, db=Depends(get_db)):
    fp = make_fingerprint(payload)
    job, created = await repo.insert_job(db, payload, fp)
    return {**job, "created": created}

@router.get("", response_model=list[JobResponse])
async def list_jobs(status: str | None = Query(None), db=Depends(get_db)):
    jobs = await repo.get_all_jobs(db, status_filter=status)
    return [{**j, "created": False} for j in jobs]

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, db=Depends(get_db)):
    job = await repo.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {**job, "created": False}

@router.patch("/{job_id}/status", response_model=JobResponse)
async def update_status(job_id: int, payload: JobUpdate, db=Depends(get_db)):
    try:
        job = await repo.update_job_status(db, job_id, payload.status, payload.notes)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {**job, "created": False}

@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: int, db=Depends(get_db)):
    deleted = await repo.delete_job(db, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
```

---

## Phase 4 — Config, Logging & Error Handling

### Environment Config

```python
# app/config.py

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    db_path: str = "./jobs.db"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"

settings = Settings()
```

Secrets for later phases (API keys) are added here as fields. They are never hardcoded anywhere in the codebase.

### Structured Logging

```python
# app/main.py

from loguru import logger
from app.config import settings

logger.add(
    "logs/app.log",
    rotation="10 MB",
    retention="7 days",
    level=settings.log_level,
    format="{time} | {level} | {module} | {message}"
)
```

### Error Handling Strategy

| Failure Mode | HTTP Response | Behaviour |
|---|---|---|
| Duplicate job insert | `200 OK` | Returns existing row, `created: false` |
| Invalid status transition | `422 Unprocessable Entity` | Message states current → attempted transition |
| Job ID not found | `404 Not Found` | Standard not found |
| DB connection failure | `503 Service Unavailable` | Log + let caller retry |

---

## Key Dependencies

```
fastapi
uvicorn[standard]
aiosqlite
pydantic
pydantic-settings
loguru
httpx
python-dotenv
```

---

---

# TDD Overview for the Backend

---

## Philosophy

A test is a specification, not a verification. You write what correct behaviour looks like before writing the code that produces it. The implementation is an answer to the test.

```
Red      → Write a failing test describing the behaviour you want
Green    → Write the minimum code to make it pass
Refactor → Clean up the implementation — the test catches any regression
```

---

## Test Categories

### Unit Tests (~70%)

Test one function in complete isolation. No DB, no HTTP, no network. Millisecond feedback.

**What gets unit tested:** `transition()`, `make_fingerprint()`, Pydantic schema validation edge cases.

```python
# tests/unit/test_state_machine.py

import pytest
from app.models.enums import ApplicationStatus, transition, InvalidTransitionError

def test_applied_can_move_to_screening():
    assert transition(ApplicationStatus.APPLIED, ApplicationStatus.SCREENING) == ApplicationStatus.SCREENING

def test_cannot_skip_from_found_to_offer():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.FOUND, ApplicationStatus.OFFER)

def test_rejected_is_terminal():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW)

def test_offer_is_terminal():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.OFFER, ApplicationStatus.SCREENING)

def test_can_reject_from_any_active_stage():
    active = [ApplicationStatus.APPLIED, ApplicationStatus.SCREENING, ApplicationStatus.INTERVIEW]
    for status in active:
        assert transition(status, ApplicationStatus.REJECTED) == ApplicationStatus.REJECTED
```

---

### Integration Tests (~25%)

Test routes + real in-memory SQLite DB. One fresh DB per test — no shared state between tests.

```python
# tests/integration/test_routes.py

import pytest
from httpx import AsyncClient
from app.main import app

@pytest.mark.asyncio
async def test_create_job_returns_201(async_client):
    response = await async_client.post("/jobs", json={
        "company": "GovTech",
        "role": "Data Engineer",
        "url": "https://careers.gov.sg/123"
    })
    assert response.status_code == 201
    assert response.json()["company"] == "GovTech"
    assert response.json()["created"] == True

@pytest.mark.asyncio
async def test_duplicate_post_returns_200_not_201(async_client):
    payload = {"company": "GovTech", "role": "Data Engineer", "url": "https://careers.gov.sg/123"}
    await async_client.post("/jobs", json=payload)
    response = await async_client.post("/jobs", json=payload)
    assert response.status_code == 200
    assert response.json()["created"] == False

@pytest.mark.asyncio
async def test_invalid_transition_returns_422(async_client):
    create = await async_client.post("/jobs", json={
        "company": "DBS", "role": "Analyst", "url": "https://dbs.com/1"
    })
    job_id = create.json()["id"]
    response = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "offer"})
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_status_filter_returns_correct_subset(async_client):
    await async_client.post("/jobs", json={"company": "A", "role": "R1", "url": "https://a.com/1"})
    await async_client.post("/jobs", json={"company": "B", "role": "R2", "url": "https://b.com/2"})
    response = await async_client.get("/jobs?status=found")
    assert len(response.json()) == 2

@pytest.mark.asyncio
async def test_get_nonexistent_job_returns_404(async_client):
    response = await async_client.get("/jobs/9999")
    assert response.status_code == 404

@pytest.mark.asyncio
async def test_delete_job_removes_record(async_client):
    create = await async_client.post("/jobs", json={
        "company": "ST Eng", "role": "SWE", "url": "https://stengg.com/1"
    })
    job_id = create.json()["id"]
    await async_client.delete(f"/jobs/{job_id}")
    response = await async_client.get(f"/jobs/{job_id}")
    assert response.status_code == 404
```

---

### End-to-End Tests (~5%)

Test the full CRUD lifecycle as a user would experience it — create, read, update, delete in sequence.

```python
# tests/e2e/test_crud_pipeline.py

@pytest.mark.asyncio
async def test_full_crud_lifecycle(async_client):
    # Create
    create = await async_client.post("/jobs", json={
        "company": "GovTech", "role": "Data Engineer", "url": "https://careers.gov.sg/1"
    })
    assert create.status_code == 201
    job_id = create.json()["id"]

    # Read
    get = await async_client.get(f"/jobs/{job_id}")
    assert get.json()["status"] == "found"

    # Update — valid transition
    patch = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "applied"})
    assert patch.status_code == 200
    assert patch.json()["status"] == "applied"

    # Delete
    delete = await async_client.delete(f"/jobs/{job_id}")
    assert delete.status_code == 204

    # Confirm gone
    gone = await async_client.get(f"/jobs/{job_id}")
    assert gone.status_code == 404
```

---

## Shared Fixtures (`conftest.py`)

```python
# conftest.py

import pytest
from httpx import AsyncClient
from app.main import app
from app.db.database import create_tables

@pytest.fixture
async def async_client():
    """
    Fresh FastAPI app with a fresh in-memory SQLite DB for every test.
    Tests are fully isolated — no shared state.
    """
    await create_tables()
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client
```

---

## Running the Tests

```bash
# All tests
pytest

# Unit only (fastest feedback loop during development)
pytest tests/unit/

# With coverage report
pytest --cov=app --cov-report=term-missing

# Specific test
pytest tests/unit/test_state_machine.py::test_rejected_is_terminal -v
```

**Coverage targets:**
- `app/models/enums.py` — 100% (pure logic, no excuses)
- `app/db/repository.py` — 90%+
- `app/routes/jobs.py` — 90%+
- Overall `app/` — 80% minimum