# Job Application Tracker — Scraping Layer Implementation Plan

---

## Overview

The scraping layer has one responsibility: discover job listings from the web and deliver them to the backend in a clean, validated, deduplicated form. It does not own storage, it does not own the agent loop, and it does not score jobs — those are separate concerns. It queries Tavily, normalises the raw response, and calls `repo.insert_job()`. Everything else is handled by layers that already exist.

Stack: Tavily Search API (external), `httpx` (HTTP client), pure Python (parser), `APScheduler` (scheduling), `pydantic-settings` (config).

---

## Progress & TODO

**Current phase:** Phase 4 — Live Validation

| Phase | Status | Notes |
|---|---|---|
| Phase 1 — Parser | [x] Done | `scraper/parser.py` + `test_parser.py` — 19/19 tests passing |
| Phase 2 — Tavily Client | [x] Done | `scraper/tavily_client.py` + `test_tavily_client.py` — 12/12 tests passing |
| Phase 3 — Wire `_search_jobs` | [x] Done | `_search_jobs` implemented + `test_search_tool_executor.py` — all tests passing |
| Phase 4 — Live Validation | [x] Done | `test_tavily_live.py` + `test_agent_search_live.py` — 4/4 passing. Pydantic serialization warning on `ChatCompletionMessageToolCall` (LiteLLM/Gemini type mismatch) — non-breaking, tests pass. Rate-limit guards (15s delay) added to all live test files. |
| Phase 5 — Scheduler | [ ] TODO | Wire APScheduler into FastAPI lifespan hook — daily scrape job |
| Phase 6 — E2E Test with Scheduler | [ ] TODO | Verify scheduler fires, calls scraper pipeline, and inserts records into DB |

**Blockers:**
- None

**Last updated:** 2026-05-06

---

## Project Structure

```
job_tracker/
├── scraper/
│   ├── __init__.py
│   ├── tavily_client.py     # HTTP call to Tavily API — returns raw results
│   └── parser.py            # Normalises raw Tavily results → JobCreate-shaped dicts
├── agent/
│   └── tools.py             # _search_jobs() stub replaced with real scraper call
├── app/
│   ├── config.py            # TAVILY_API_KEY added
│   └── main.py              # APScheduler wired into lifespan hook
├── test/
│   ├── unit/
│   │   ├── test_parser.py
│   │   ├── test_tavily_client.py
│   │   └── test_search_tool_executor.py
│   ├── integration/
│   │   ├── test_scraper_pipeline.py
│   │   └── test_agent_search_tool.py
│   └── e2e/
│       └── test_search_pipeline.py
└── requirements.txt
```

---

## Phase 1 — Parser

### Goal

Define what a valid parsed job record looks like and enforce it with tests before writing the implementation. The parser is a pure function — it takes a list of raw Tavily result dicts and returns a list of `JobCreate`-shaped dicts. No HTTP, no DB, no side effects. This makes it the easiest and most important file to test first.

### What a raw Tavily result looks like

```json
{
  "title": "Data Engineer — GovTech Singapore",
  "url": "https://careers.gov.sg/jobs/123",
  "content": "We are looking for a Data Engineer with experience in Python and SQL...",
  "score": 0.91
}
```

### Parsing Rules

| Raw field | Maps to | Rule |
|---|---|---|
| `title` | `role` | Used as-is |
| `url` | `url` | Required — drop result if missing |
| `content` | `notes` | Truncated to 500 characters |
| Hardcoded | `company` | Extracted from title if possible, else `"Unknown"` |
| Hardcoded | `source` | Always `"tavily"` |

### Implementation

```python
# scraper/parser.py

from app.models.job import JobCreate

MAX_NOTES_LENGTH = 500


def parse_results(raw_results: list[dict]) -> list[dict]:
    """
    Normalise a list of raw Tavily results into JobCreate-shaped dicts.

    Rules:
    - Results missing a url are silently dropped.
    - The content/snippet becomes the notes field, truncated to 500 chars.
    - source is always set to "tavily".
    - company is extracted from the title if a dash is present, else "Unknown".
    """
    parsed = []
    for result in raw_results:
        url = result.get("url")
        if not url:
            continue

        title = result.get("title", "")
        company, role = _split_title(title)
        notes = (result.get("content") or "")[:MAX_NOTES_LENGTH] or None

        parsed.append({
            "company": company,
            "role":    role,
            "url":     url,
            "source":  "tavily",
            "notes":   notes,
        })

    return parsed


def _split_title(title: str) -> tuple[str, str]:
    """
    Best-effort extraction of company and role from a job listing title.

    Handles formats like:
    - "Data Engineer — GovTech"     → role="Data Engineer", company="GovTech"
    - "GovTech | Data Engineer"     → role="Data Engineer", company="GovTech"
    - "Data Engineer"               → role="Data Engineer", company="Unknown"
    """
    for sep in [" — ", " | ", " - ", " at "]:
        if sep in title:
            parts = title.split(sep, 1)
            # Heuristic: shorter part is usually the company name
            if len(parts[0]) < len(parts[1]):
                return parts[0].strip(), parts[1].strip()
            return parts[1].strip(), parts[0].strip()
    return "Unknown", title.strip()
```

### TDD — Unit Tests

```python
# tests/unit/test_parser.py

import pytest
from scraper.parser import parse_results, _split_title


def _make_result(**kwargs):
    base = {
        "title": "Data Engineer — GovTech",
        "url":   "https://careers.gov.sg/1",
        "content": "We are looking for a data engineer."
    }
    return {**base, **kwargs}


def test_parse_single_result_returns_correct_shape():
    results = [_make_result()]
    parsed = parse_results(results)
    assert len(parsed) == 1
    record = parsed[0]
    assert record["url"]    == "https://careers.gov.sg/1"
    assert record["source"] == "tavily"
    assert "company" in record
    assert "role"    in record


def test_parse_empty_list_returns_empty_list():
    assert parse_results([]) == []


def test_parse_drops_results_missing_url():
    results = [_make_result(url=None), _make_result()]
    parsed = parse_results(results)
    assert len(parsed) == 1


def test_parse_sets_source_to_tavily():
    parsed = parse_results([_make_result()])
    assert parsed[0]["source"] == "tavily"


def test_parse_snippet_becomes_notes():
    parsed = parse_results([_make_result(content="Some job description.")])
    assert parsed[0]["notes"] == "Some job description."


def test_parse_truncates_long_content():
    long_content = "x" * 600
    parsed = parse_results([_make_result(content=long_content)])
    assert len(parsed[0]["notes"]) == 500


def test_parse_multiple_results_returns_correct_count():
    results = [
        _make_result(url="https://a.com/1"),
        _make_result(url="https://b.com/2"),
        _make_result(url="https://c.com/3"),
    ]
    assert len(parse_results(results)) == 3


def test_split_title_dash_separator():
    company, role = _split_title("Data Engineer — GovTech")
    assert company == "GovTech"
    assert role    == "Data Engineer"


def test_split_title_pipe_separator():
    company, role = _split_title("DBS | Backend Engineer")
    assert "DBS"             in (company, role)
    assert "Backend Engineer" in (company, role)


def test_split_title_no_separator_returns_unknown_company():
    company, role = _split_title("Backend Engineer")
    assert company == "Unknown"
    assert role    == "Backend Engineer"
```

---

## Phase 2 — Tavily Client

### Goal

Wrap the Tavily HTTP call in a single async function. The client reads the API key from config, builds the request, and returns the raw results list. On any failure — network error, bad status, missing key — it logs the error and returns an empty list. It never raises.

### Getting a Tavily API Key

1. Sign up at [tavily.com](https://tavily.com)
2. Copy your API key from the dashboard
3. Add it to `.env`:

```bash
TAVILY_API_KEY=tvly-...
```

### Config Addition

```python
# app/config.py

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Existing
    db_path:           str = "./jobs.db"
    log_level:         str = "INFO"

    # Agent (existing)
    model:             str = "claude-sonnet-4-5"
    anthropic_api_key: str = ""
    openai_api_key:    str = ""
    gemini_api_key:    str = ""

    # Scraper (new)
    tavily_api_key:    str = ""
    scrape_query:      str = "software engineer Singapore"
    scrape_max_results: int = 10

    class Config:
        env_file = ".env"

settings = Settings()
```

`scrape_query` is the default search term used by the scheduler. The agent can override it per-call. `scrape_max_results` caps the number of results returned per search to avoid blowing through Tavily quota.

### Implementation

```python
# scraper/tavily_client.py

import httpx
from loguru import logger
from app.config import settings

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def search(query: str, max_results: int | None = None) -> list[dict]:
    """
    Call the Tavily search API and return raw results.

    Returns an empty list on any failure — never raises.
    The caller (parser, executor) handles the empty case gracefully.
    """
    api_key = settings.tavily_api_key
    if not api_key:
        logger.warning("TAVILY_API_KEY is not set — returning empty results")
        return []

    payload = {
        "api_key":     api_key,
        "query":       query,
        "max_results": max_results or settings.scrape_max_results,
        "search_depth": "basic",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(TAVILY_SEARCH_URL, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])

    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily returned HTTP {e.response.status_code}: {e}")
        return []

    except httpx.RequestError as e:
        logger.error(f"Tavily request failed: {e}")
        return []

    except Exception as e:
        logger.error(f"Unexpected error calling Tavily: {e}")
        return []
```

### TDD — Unit Tests

```python
# tests/unit/test_tavily_client.py

import pytest
import httpx
from unittest.mock import patch, AsyncMock
from scraper.tavily_client import search


@pytest.mark.asyncio
async def test_returns_empty_list_if_api_key_missing():
    with patch("scraper.tavily_client.settings") as mock_settings:
        mock_settings.tavily_api_key    = ""
        mock_settings.scrape_max_results = 10
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_search_passes_query_in_payload():
    captured = {}

    async def mock_post(url, json=None, **kwargs):
        captured["payload"] = json
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = lambda: None
        return mock_resp

    with patch("scraper.tavily_client.settings") as s, \
         patch("httpx.AsyncClient") as mock_client_cls:
        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = mock_post
        mock_client_cls.return_value = mock_client
        await search("data engineer Singapore")

    assert captured["payload"]["query"] == "data engineer Singapore"


@pytest.mark.asyncio
async def test_returns_empty_list_on_http_error():
    with patch("scraper.tavily_client.settings") as s, \
         patch("httpx.AsyncClient") as mock_client_cls:
        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post.side_effect = httpx.RequestError("connection refused")
        mock_client_cls.return_value = mock_client
        result = await search("data engineer Singapore")

    assert result == []


@pytest.mark.asyncio
async def test_search_returns_results_from_response():
    fake_results = [
        {"title": "Data Engineer — GovTech", "url": "https://careers.gov.sg/1", "content": "..."},
    ]

    with patch("scraper.tavily_client.settings") as s, \
         patch("httpx.AsyncClient") as mock_client_cls:
        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_resp             = AsyncMock()
        mock_resp.json.return_value = {"results": fake_results}
        mock_resp.raise_for_status   = lambda: None
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value  = mock_client
        result = await search("data engineer Singapore")

    assert result == fake_results
```

---

## Phase 3 — Wire `_search_jobs` and Ingestion

### Goal

Replace the four-line stub in `agent/tools.py` with a real implementation. The executor calls `tavily_client.search()`, passes the raw results through `parser.parse_results()`, inserts each record via `repo.insert_job()`, and returns a JSON string summary back to the agent loop.

### Before (stub)

```python
# agent/tools.py — current stub

def _search_jobs(args: dict) -> str:
    return json.dumps({
        "results": [],
        "note": "Search not yet available. Scraping layer coming in Week 3."
    })
```

### After (real implementation)

```python
# agent/tools.py — updated executor

import json
from scraper.tavily_client import search as tavily_search
from scraper.parser import parse_results
from app.routes.jobs import make_fingerprint
from app.models.job import JobCreate
from app.db import repository as repo


async def _search_jobs(args: dict, db) -> str:
    """
    1. Call Tavily with the user's query.
    2. Parse raw results into JobCreate-shaped dicts.
    3. Insert each record via repo (idempotent — duplicates silently ignored).
    4. Return a JSON summary to the model.
    """
    query       = args.get("query", settings.scrape_query)
    raw_results = await tavily_search(query)
    parsed      = parse_results(raw_results)

    inserted = []
    for record in parsed:
        try:
            job_data = JobCreate(**record)
            fp       = make_fingerprint(job_data)
            job, created = await repo.insert_job(db, job_data, fp)
            inserted.append({"id": job["id"], "company": job["company"], "role": job["role"], "created": created})
        except Exception as e:
            logger.warning(f"Failed to insert job record: {e}")
            continue

    return json.dumps({
        "query":   query,
        "found":   len(parsed),
        "inserted": inserted,
        "count":   len(inserted),
    })
```

Note the signature change: `_search_jobs` now accepts `db` as a second argument, consistent with the other async executors (`_log_job`, `_update_status`, `_query_jobs`). Update the `handlers` dispatch table accordingly:

```python
handlers = {
    "log_job":        lambda: _log_job(arguments, db),
    "update_status":  lambda: _update_status(arguments, db),
    "query_jobs":     lambda: _query_jobs(arguments, db),
    "update_profile": lambda: _update_profile(arguments),
    "search_jobs":    lambda: _search_jobs(arguments, db),   # db added
}
```

### TDD — Unit Tests

```python
# tests/unit/test_search_tool_executor.py

import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from agent.tools import execute_tool


def _make_db():
    return MagicMock()


@pytest.mark.asyncio
async def test_search_jobs_returns_json_string():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("agent.tools.parse_results", return_value=[]):
        result = await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert "count" in parsed


@pytest.mark.asyncio
async def test_search_jobs_delegates_to_tavily_with_query():
    mock_search = AsyncMock(return_value=[])
    with patch("agent.tools.tavily_search", mock_search), \
         patch("agent.tools.parse_results", return_value=[]):
        await execute_tool("search_jobs", {"query": "backend engineer SG"}, _make_db())
    mock_search.assert_called_once_with("backend engineer SG")


@pytest.mark.asyncio
async def test_search_jobs_result_includes_found_and_count():
    fake_parsed = [{"company": "GovTech", "role": "Data Engineer",
                    "url": "https://careers.gov.sg/1", "source": "tavily", "notes": None}]
    fake_job    = {"id": 1, "company": "GovTech", "role": "Data Engineer",
                   "url": "https://careers.gov.sg/1", "status": "found",
                   "source": "tavily", "notes": None, "date_logged": "2026-01-01"}

    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=fake_parsed), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock, return_value=(fake_job, True)):
        result = await execute_tool("search_jobs", {"query": "data engineer"}, _make_db())

    data = json.loads(result)
    assert data["found"] == 1
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_search_jobs_returns_gracefully_when_tavily_returns_empty():
    with patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[]), \
         patch("agent.tools.parse_results", return_value=[]):
        result = await execute_tool("search_jobs", {"query": "anything"}, _make_db())
    data = json.loads(result)
    assert data["count"] == 0
    assert data["inserted"] == []
```

---

## Phase 4 — Scheduler

### Goal

Wire APScheduler into the FastAPI lifespan hook so the scraper runs automatically every 24 hours without any user interaction. The scheduled job calls the same scraper logic the agent calls — no duplication.

### Implementation

```python
# app/main.py (additions)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from contextlib import asynccontextmanager
from loguru import logger

from scraper.tavily_client import search as tavily_search
from scraper.parser import parse_results
from app.db.database import create_tables, get_db
from app.db import repository as repo
from app.models.job import JobCreate
from app.routes.jobs import make_fingerprint
from app.config import settings


async def _scheduled_scrape():
    """
    Runs on the APScheduler tick.
    Uses the configured scrape_query from settings.
    Shares the same scraper + ingestion logic as the agent tool.
    """
    logger.info(f"Scheduled scrape starting — query: '{settings.scrape_query}'")
    raw  = await tavily_search(settings.scrape_query)
    jobs = parse_results(raw)
    logger.info(f"Scheduled scrape: {len(jobs)} results from Tavily")

    inserted_count = 0
    async for db in get_db():
        for record in jobs:
            try:
                job_data     = JobCreate(**record)
                fp           = make_fingerprint(job_data)
                _, created   = await repo.insert_job(db, job_data, fp)
                if created:
                    inserted_count += 1
            except Exception as e:
                logger.warning(f"Scheduled scrape: failed to insert record: {e}")

    logger.info(f"Scheduled scrape complete — {inserted_count} new records inserted")


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app):
    await create_tables()
    scheduler.add_job(
        _scheduled_scrape,
        trigger=IntervalTrigger(hours=24),
        id="daily_scrape",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — daily scrape job registered")
    yield
    scheduler.shutdown()
    logger.info("APScheduler stopped")
```

---

## Phase 5 — Integration and E2E Tests

### Integration Tests

```python
# tests/integration/test_scraper_pipeline.py

import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient
from app.main import app
from app.db.database import create_tables


FAKE_TAVILY_RESULTS = [
    {"title": "Data Engineer — GovTech", "url": "https://careers.gov.sg/1",
     "content": "Looking for a data engineer."},
    {"title": "Backend Engineer | DBS",  "url": "https://dbs.com/jobs/2",
     "content": "Python backend role."},
]


@pytest.fixture
async def async_client():
    await create_tables()
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_search_and_ingest_inserts_jobs_to_db(async_client):
    with patch("scraper.tavily_client.httpx.AsyncClient") as mock_cls, \
         patch("scraper.tavily_client.settings") as s:
        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_resp             = AsyncMock()
        mock_resp.json.return_value   = {"results": FAKE_TAVILY_RESULTS}
        mock_resp.raise_for_status     = lambda: None
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_cls.return_value  = mock_client

        # Trigger via agent search_jobs
        with patch("app.routes.chat.run", new_callable=AsyncMock) as mock_run:
            # Directly call scraper pipeline instead
            from scraper.tavily_client import search
            from scraper.parser import parse_results
            raw    = await search("data engineer")
            parsed = parse_results(raw)

        assert len(parsed) == 2

    response = await async_client.post("/jobs", json={
        "company": parsed[0]["company"],
        "role":    parsed[0]["role"],
        "url":     parsed[0]["url"],
        "source":  parsed[0]["source"],
    })
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_duplicate_results_not_double_inserted(async_client):
    payload = {"company": "GovTech", "role": "Data Engineer",
               "url": "https://careers.gov.sg/1"}
    r1 = await async_client.post("/jobs", json=payload)
    r2 = await async_client.post("/jobs", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json()["created"] == False

    all_jobs = await async_client.get("/jobs")
    govtech_jobs = [j for j in all_jobs.json()
                    if j["url"] == "https://careers.gov.sg/1"]
    assert len(govtech_jobs) == 1


@pytest.mark.asyncio
async def test_tavily_unavailable_returns_empty_does_not_crash():
    with patch("scraper.tavily_client.settings") as s, \
         patch("scraper.tavily_client.httpx.AsyncClient") as mock_cls:
        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post.side_effect = Exception("connection refused")
        mock_cls.return_value  = mock_client

        from scraper.tavily_client import search
        result = await search("data engineer")

    assert result == []


@pytest.mark.asyncio
async def test_scheduler_job_is_registered():
    from app.main import scheduler
    job = scheduler.get_job("daily_scrape")
    assert job is not None
```

```python
# tests/integration/test_agent_search_tool.py

import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch
from agent.agent import run


def _tool_response(tool_name, arguments, call_id="call_1"):
    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function.name      = tool_name
    tool_call.function.arguments = json.dumps(arguments)
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=None, tool_calls=[tool_call])
    )])


def _text_response(content):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=None)
    )])


FAKE_PARSED = [
    {"company": "GovTech", "role": "Data Engineer",
     "url": "https://careers.gov.sg/1", "source": "tavily", "notes": None},
]

FAKE_JOB = {
    "id": 1, "company": "GovTech", "role": "Data Engineer",
    "url": "https://careers.gov.sg/1", "status": "found",
    "source": "tavily", "notes": None, "date_logged": "2026-01-01",
}


@pytest.mark.asyncio
async def test_agent_executes_search_jobs_and_returns_results():
    responses = [
        _tool_response("search_jobs", {"query": "data engineer Singapore"}),
        _text_response("I found 1 job: Data Engineer at GovTech."),
    ]
    llm = MagicMock()
    llm.chat.side_effect = responses

    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=FAKE_PARSED), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock, return_value=(FAKE_JOB, True)):

        reply = await run(
            messages=[{"role": "user", "content": "find me data engineer jobs in Singapore"}],
            db=MagicMock(),
            llm=llm,
        )

    assert "GovTech" in reply or "1 job" in reply.lower()


@pytest.mark.asyncio
async def test_search_results_count_in_tool_result():
    responses = [
        _tool_response("search_jobs", {"query": "backend engineer"}),
        _text_response("Found 1 result."),
    ]
    llm = MagicMock()
    llm.chat.side_effect = responses

    tool_result_seen = {}

    original_chat = llm.chat.side_effect

    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.tavily_search", new_callable=AsyncMock, return_value=[{}]), \
         patch("agent.tools.parse_results", return_value=FAKE_PARSED), \
         patch("agent.tools.repo.insert_job", new_callable=AsyncMock, return_value=(FAKE_JOB, True)):

        await run(
            messages=[{"role": "user", "content": "find backend jobs"}],
            db=MagicMock(),
            llm=llm,
        )

    # Second call to llm.chat should include tool_result in message history
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_results = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_results) == 1
    result_data = json.loads(tool_results[0]["content"])
    assert "count" in result_data
```

### E2E Test

```python
# tests/e2e/test_search_pipeline.py

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from httpx import AsyncClient
from app.main import app
from app.db.database import create_tables
import json


FAKE_TAVILY_RESPONSE = {
    "results": [
        {"title": "Data Engineer — GovTech",
         "url": "https://careers.gov.sg/1",
         "content": "Python, SQL, data pipelines."},
        {"title": "Backend Engineer | DBS",
         "url": "https://dbs.com/jobs/2",
         "content": "FastAPI, PostgreSQL."},
    ]
}


def _tool_response(tool_name, arguments):
    tool_call = MagicMock()
    tool_call.id = "call_e2e_1"
    tool_call.function.name      = tool_name
    tool_call.function.arguments = json.dumps(arguments)
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=None, tool_calls=[tool_call])
    )])


def _text_response(content):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=None)
    )])


@pytest.fixture
async def async_client():
    await create_tables()
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_full_search_to_db_pipeline(async_client):
    """
    User asks agent to find jobs.
    Agent calls search_jobs tool.
    Scraper calls (mocked) Tavily — returns 2 listings.
    Parser normalises them.
    Both are inserted via repo.insert_job.
    GET /jobs returns 2 records with status "found".
    """
    llm_responses = [
        _tool_response("search_jobs", {"query": "data engineer Singapore"}),
        _text_response("I found 2 jobs and logged them to your tracker."),
    ]
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = llm_responses

    with patch("scraper.tavily_client.settings") as s, \
         patch("scraper.tavily_client.httpx.AsyncClient") as mock_cls, \
         patch("agent.llm_client.LLMClient", return_value=mock_llm), \
         patch("agent.agent.read_profile", return_value={}):

        s.tavily_api_key     = "tvly-test"
        s.scrape_max_results  = 10
        mock_resp             = AsyncMock()
        mock_resp.json.return_value   = FAKE_TAVILY_RESPONSE
        mock_resp.raise_for_status     = lambda: None
        mock_client           = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_cls.return_value  = mock_client

        chat_response = await async_client.post("/chat", json={
            "messages": [{"role": "user",
                          "content": "find me data engineer jobs in Singapore"}]
        })

    assert chat_response.status_code == 200
    assert "found" in chat_response.json()["reply"].lower() or \
           "job" in chat_response.json()["reply"].lower()

    jobs_response = await async_client.get("/jobs")
    assert jobs_response.status_code == 200
    jobs = jobs_response.json()
    assert len(jobs) == 2
    assert all(j["status"] == "found" for j in jobs)
    urls = {j["url"] for j in jobs}
    assert "https://careers.gov.sg/1" in urls
    assert "https://dbs.com/jobs/2"   in urls
```

---

## Error Handling Strategy

| Failure Mode | Behaviour |
|---|---|
| `TAVILY_API_KEY` missing | `tavily_client.search()` logs a warning and returns `[]` immediately |
| Tavily HTTP error (4xx/5xx) | Logged, returns `[]` — pipeline continues with empty results |
| Tavily network timeout or connection refused | Logged, returns `[]` — never raises |
| Parser receives malformed result (no url) | Record silently dropped, rest of list processed |
| `repo.insert_job` fails for one record | Logged, loop continues to next record |
| Scheduler fires while Tavily is down | Returns `[]`, logs warning, DB untouched |

The scraping layer is entirely defensive. No failure in this layer should crash the application or surface an error to the user. The agent and scheduler both treat an empty result list as a valid — if undesirable — outcome.

---

## Running the Tests

```bash
# All scraper tests
pytest tests/unit/test_parser.py \
       tests/unit/test_tavily_client.py \
       tests/unit/test_search_tool_executor.py \
       tests/integration/test_scraper_pipeline.py \
       tests/integration/test_agent_search_tool.py \
       tests/e2e/test_search_pipeline.py

# Unit only — fastest feedback loop
pytest tests/unit/test_parser.py tests/unit/test_tavily_client.py \
       tests/unit/test_search_tool_executor.py

# With coverage
pytest --cov=scraper --cov=agent/tools.py --cov-report=term-missing
```

---

## Coverage Targets

| File | Target | Reason |
|---|---|---|
| `scraper/parser.py` | 100% | Pure logic — no I/O, no excuses |
| `scraper/tavily_client.py` | 90%+ | All error paths (missing key, HTTP error, network error) covered |
| `agent/tools.py` (`_search_jobs`) | 90%+ | Happy path + empty result + insert failure |
| `app/main.py` (scheduler) | 80%+ | Test the callable directly; scheduler wiring is shallow |
| `scraper/` overall | 90%+ | |

---

## Key Dependencies (additions to `requirements.txt`)

```
httpx
apscheduler
```

All other dependencies (`fastapi`, `aiosqlite`, `pydantic`, `pydantic-settings`, `loguru`, `pytest-asyncio`) are already present.

---

## Implementation Checklist

- [x] **Phase 1 — Parser** — `scraper/parser.py` + `test/unit/test_parser.py`
- [x] **Phase 2 — Tavily Client** — `scraper/tavily_client.py` + `test/unit/test_tavily_client.py` + config additions
- [x] **Phase 3 — Wire `_search_jobs`** — update `agent/tools.py` + `test/unit/test_search_tool_executor.py`
- [x] **Phase 4 — Live Validation** — `test/integration/test_tavily_live.py` + `test/e2e/test_agent_search_live.py` — 4/4 passing
- [ ] **Phase 5 — Scheduler** — `app/main.py` lifespan additions + scheduler registration test
- [ ] **Phase 6 — E2E Test with Scheduler** — verify scheduler fires, calls scraper pipeline, inserts records into DB
