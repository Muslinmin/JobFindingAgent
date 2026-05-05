# Job Application Tracker — Architecture & Tech Stack Overview

---

## What This App Is

A job application tracker built as an agentic system. Instead of manually logging applications into a spreadsheet, you interact with the system in natural language and it handles discovery, storage, scoring, and status tracking automatically.

The system is designed in discrete layers. Each layer has a single responsibility and communicates with the layers around it through clean interfaces. No layer knows the internal implementation of another.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     User Interface                       │
│              Streamlit Dashboard / CLI                   │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────┐
│                   Backend API Layer                      │
│                FastAPI + Pydantic + SQLite               │
└──────────┬─────────────────────────────┬────────────────┘
           │                             │
┌──────────▼──────────┐     ┌────────────▼───────────────┐
│    Agent Layer       │     │      Scoring & Dedup        │
│  Anthropic API +     │     │   Keyword Match + SHA-256   │
│  Tool Calling        │     │   Fingerprinting            │
└──────────┬──────────┘     └────────────────────────────┘
           │
┌──────────▼──────────┐
│   Scraping Layer     │
│   Tavily Search API  │
└─────────────────────┘
```

---

## Tech Stack at a Glance

| Layer | Technology | Purpose |
|---|---|---|
| Backend API | FastAPI | HTTP transport, routing, validation |
| Database | SQLite → PostgreSQL | Persistent storage of job records |
| ORM / Queries | aiosqlite + raw SQL | Async DB access via repository pattern |
| Validation | Pydantic v2 | Schema enforcement on all I/O |
| Config | pydantic-settings | Environment variable management |
| Agent Brain | Anthropic API (claude-sonnet, gemini, gpt, etc.) | Natural language reasoning + tool calling |
| Search | Tavily API | Web search built for agent use |
| Scoring | Pure Python | Keyword-based job relevance scoring |
| Deduplication | hashlib SHA-256 | Fingerprint-based duplicate prevention |
| Logging | loguru | Structured, levelled application logs |
| Testing | pytest + pytest-asyncio + httpx | Unit, integration, and E2E test coverage |
| Containerisation | Docker | Consistent runtime across environments |
| Frontend | Streamlit | Dashboard for visualising application data |
| Scheduling | APScheduler | Periodic scraping and digest runs |

---

---

## Layer 1 — Backend API

**Responsibility:** Persist job application records and expose a validated HTTP interface for all reads and writes. This is the source of truth for the entire system.

**Tech:** FastAPI, aiosqlite, Pydantic, SQLite

**What it owns:**
- The `jobs` database table and its schema
- All SQL queries, isolated behind a Repository pattern
- Status transition logic via a finite state machine
- Idempotent upsert behaviour — duplicate inserts return the existing record, not an error
- Five CRUD endpoints: create, list, get, update status, delete

**Key design decisions:**
- Route handlers contain zero business logic — they validate input and delegate to the repository
- The `fingerprint` column (SHA-256 hash of company + role + url) enforces uniqueness at the DB level
- Status transitions are enforced at the model layer before any DB write is attempted
- The repository is the only file that knows the DB driver — swapping SQLite for PostgreSQL touches one file

**Interfaces it exposes to other layers:**
- `POST /jobs` — agent and scraper call this to log discovered jobs
- `PATCH /jobs/{id}/status` — agent calls this when user updates an application
- `GET /jobs` — frontend and agent call this to read current state

**Detailed doc:** `backend.md`

---

## Layer 2 — Agent Brain

**Responsibility:** Receive natural language input, reason about what action to take, and execute the correct tool. The agent is the orchestrator — it talks to the backend, the scraper, and the scorer, but owns none of their logic.

**Tech:** Anthropic API (`claude-sonnet-4-5`), tool calling / function calling

**What it owns:**
- The tool definitions (log job, update status, query jobs, search for jobs)
- The ReAct loop — reason, act, observe, repeat until done
- Conversation memory within a session
- Routing user intent to the correct tool with the correct arguments

**The tool loop:**

```
User message
     │
     ▼
LLM receives message + tool schema definitions
     │
     ▼
LLM returns tool_use block { tool_name, arguments }
     │
     ▼
Backend executes the tool → calls repository or scraper
     │
     ▼
Result returned to LLM as tool_result block
     │
     ▼
LLM produces final natural language response
     │  (loop repeats if another tool call is needed)
     ▼
stop_reason: "end_turn"
```

**Tools the agent can call:**

| Tool | What it does |
|---|---|
| `log_job` | Calls `POST /jobs` to persist a new application |
| `update_status` | Calls `PATCH /jobs/{id}/status` to move an application forward |
| `query_jobs` | Calls `GET /jobs` to retrieve and summarise current applications |
| `search_jobs` | Calls the scraping layer to discover new listings |

**Key design decisions:**
- The LLM proposes actions — the backend validates and executes them. The model never writes directly to the DB
- Tool definitions are strict JSON schemas — the model cannot pass unexpected argument shapes
- External APIs are always mocked in tests — the agent logic is tested against deterministic fake responses

**Detailed doc:** `agent.md` *(coming)*

---

## Layer 3 — Scraping & Ingestion

**Responsibility:** Discover job listings from external sources and feed them into the backend in a structured, deduplicated form.

**Tech:** Tavily API, Python `httpx`

**What it owns:**
- Querying Tavily with a search string and returning raw results
- Parsing and normalising raw results into `JobCreate`-shaped records
- Passing normalised records to the backend via `POST /jobs`
- Respecting rate limits and handling upstream failures gracefully

**Why Tavily and not direct scraping:**
Direct scraping of LinkedIn or Indeed violates their ToS and breaks frequently. Tavily is a search API purpose-built for agent use — it returns structured, clean results, handles JS-rendered pages, and has a stable interface.

**Key design decisions:**
- The scraper does not write to the DB directly — it calls the backend API, which handles deduplication and validation
- If Tavily is unavailable, the scraper logs the failure and returns an empty list — it does not crash the pipeline
- Rate limiting is handled with a configurable delay between requests

**Detailed doc:** `scraper.md` *(coming)*

---

## Layer 4 — Scoring & Deduplication

**Responsibility:** Score each job listing against the user's profile and generate a unique fingerprint for deduplication. These are pure functions with no I/O — they take data in and return a result.

**Tech:** Pure Python, `hashlib`

### Scoring

Takes a job description string and a list of resume keywords. Returns a normalised float between 0.0 and 1.0.

```
score = (number of resume keywords found in JD) / (total resume keywords)
```

This is intentionally simple. It can be upgraded to TF-IDF or embedding cosine similarity without changing the function signature — all existing tests continue to pass.

### Deduplication

Takes a job record and returns a SHA-256 hash of `(company + role + url)`, normalised to lowercase. This fingerprint is stored as a unique constraint in the DB. Any attempt to insert the same job twice returns the existing record silently.

**Key design decisions:**
- Both functions are pure — no database calls, no API calls, no side effects
- This makes them trivially unit testable with 100% branch coverage
- The scorer receives keywords from config (`settings.resume_keywords`) — no hardcoding

**Detailed doc:** `scoring.md` *(coming)*

---

## Layer 5 — Frontend & Scheduling

**Responsibility:** Give the user a visual interface to see their application pipeline and automate periodic scraping without manual triggers.

**Tech:** Streamlit (dashboard), APScheduler (scheduling)

### Streamlit Dashboard

A single-page app that calls the backend API and renders:
- A status breakdown bar chart (how many at each stage)
- A filterable table of all applications with company, role, score, date, and status
- A chat input bar that forwards messages to the agent and displays responses inline

### APScheduler

Runs two background jobs:
- **Scrape job** — runs every 24 hours, queries Tavily for configured search terms, feeds results to the backend
- **Digest job** — runs every Monday morning, queries all applications and emails a summary of stale ones (no movement in 14 days)

**Key design decisions:**
- The dashboard is read-only for the DB — all writes go through the agent or the API, not directly through Streamlit
- The scheduler runs in the same process as the FastAPI app via a lifespan hook, keeping the deployment simple for an entry-level build

**Detailed doc:** `frontend.md` *(coming)*

---

---

## Development Order

Each layer is built, tested, and stable before the next is started. Later layers depend on earlier ones — the agent is useless without a working backend.

```
Week 1   Backend API        Models, DB, repository, CRUD routes, TDD (DONE AND TESTED)
Week 2   Agent Brain        Tool definitions, ReAct loop, session memory
Week 3   Scraping Layer     Tavily integration, parsing, rate limiting
Week 4   Scoring & Dedup    Scoring function, fingerprinting, wired into ingestion
Week 5   Frontend           Streamlit dashboard, APScheduler digest and scrape jobs
```

---

## Documentation Map

| File | Covers |
|---|---|
| `overview.md` | This file — full stack and layer summaries |
| `backend.md` | FastAPI, SQLite, repository, CRUD routes, TDD |
| `agent.md` | Anthropic API, tool definitions, ReAct loop *(coming)* |
| `scraper.md` | Tavily integration, parsing, rate limiting *(coming)* |
| `scoring.md` | Scoring function, deduplication, fingerprinting *(coming)* |
| `frontend.md` | Streamlit dashboard, APScheduler *(coming)* |