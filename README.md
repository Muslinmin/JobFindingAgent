# JobFindingAgent

An agentic job application tracker. Interact in natural language — the system handles discovery, storage, scoring, and status tracking automatically.

---

## Roadmap to v1.0.0

This project is built in layers. Each layer is fully tested before the next begins. The checklist below tracks progress toward the first stable release.

- [x] **Week 1 — Backend API** — FastAPI, SQLite, repository pattern, CRUD routes, state machine, TDD
- [x] **Week 2 — Agent Brain** — LiteLLM, ReAct loop, tool calling, profile manager, `POST /chat`
- [x] **Week 3 — Scraping Layer** — Tavily integration, parser, `search_jobs` tool, rate limiting
- [x] **Week 4 — Scoring & Dedup** — Keyword scorer, SHA-256 fingerprinting, wired into both ingestion paths, e2e tested
- [ ] **Week 5 — Frontend** — Streamlit dashboard, APScheduler digest and scrape jobs

**Current version:** `0.4.0` (pre-release — Week 5 remaining before `0.5.0`)

---

## Requirements

- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- A [Tavily API key](https://tavily.com/)
- An API key for one of: Anthropic, OpenAI, or Google Gemini

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd JobFindingAgent
```

### 2. Create the conda environment

```bash
conda env create -f environment.yml
conda activate job-finder
```

### 3. Configure environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

```env
# Pick any supported model and paste its key
MODEL=gemini/gemini-2.0-flash-lite   # or gpt-4o-mini, claude-sonnet-4-6
MODEL_API_KEY=your-key-here

TAVILY_API_KEY=your-tavily-key
```

---

## Running

```bash
uvicorn app.main:app --reload --app-dir src
```

---

## Testing

```bash
# All non-live tests
pytest

# Unit tests only
pytest src/test/unit/

# Integration tests only
pytest src/test/integration/

# Live e2e tests (requires MODEL_API_KEY + TAVILY_API_KEY in .env)
pytest -m live -s
```

---

## Architecture

```
User (natural language)
        │
        ▼
  Streamlit Dashboard / CLI
        │ HTTP
        ▼
  FastAPI + SQLite          ← source of truth
     │          │
     ▼          ▼
 Agent Layer   Scoring & Dedup
 LiteLLM +     Keyword match (0.0–1.0)
 Tool Calling  SHA-256 fingerprint
     │
     ▼
 Scraping Layer
 Tavily Search API
```

| Layer | Tech |
|---|---|
| Backend API | FastAPI, aiosqlite, Pydantic, SQLite |
| Agent | LiteLLM (Anthropic / OpenAI / Gemini), ReAct loop |
| Scraper | Tavily API |
| Scoring | Pure Python, hashlib |
| Frontend | Streamlit *(coming in v1.0.0)* |
| Scheduling | APScheduler *(coming in v1.0.0)* |
