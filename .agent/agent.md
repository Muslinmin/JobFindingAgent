# Job Application Tracker — Agent Layer Implementation Plan

---

## Overview

The agent layer is the orchestration brain of the system. It receives natural language input from the user, reasons about what action to take, executes the correct tool against the backend, and returns a response. It owns nothing except the loop — all business logic, DB writes, and validation live in the backend layer beneath it.

The agent also owns the user profile. Conversations are the mechanism by which the profile is shaped over time. Every session that modifies the profile produces a versioned backup, giving the user full rollback capability.

Stack: LiteLLM (model abstraction), Anthropic / OpenAI / Gemini (target models), FastAPI (transport), aiosqlite (shared DB connection from backend), Pydantic (request validation), loguru (logging).

---

## Project Structure

```
job_tracker/
├── agent/
│   ├── prompts/
│   │   └── system.md            # System prompt template — rendered at runtime
│   ├── agent.py                 # ReAct loop + prompt rendering
│   ├── llm_client.py            # LiteLLM wrapper — model abstraction
│   ├── tools.py                 # Tool definitions (OpenAI format) + executors
│   └── profile.py               # Profile read / write / backup
├── profile.json                 # Live user profile
├── profiles/
│   └── backups/                 # Versioned profile snapshots
│       └── profile_<timestamp>.json
├── app/
│   └── routes/
│       ├── jobs.py              # Existing CRUD routes
│       └── chat.py              # New — POST /chat
├── tests/
│   ├── unit/
│   │   ├── test_profile.py
│   │   ├── test_tools.py
│   │   ├── test_llm_client.py
│   │   └── test_prompt_rendering.py
│   └── integration/
│       ├── test_agent.py
│       └── test_chat_route.py
└── requirements.txt
```

---

## Design Decisions

### Model Abstraction via LiteLLM

Target models include Anthropic (`claude-sonnet-4-5`), OpenAI (`gpt-4o`), and Google Gemini (`gemini/gemini-1.5-pro`). Each provider uses a different tool calling format and a different response shape. Writing the ReAct loop against any one of them directly means a rewrite to switch.

LiteLLM solves this by using OpenAI's format as the universal standard. Tools are defined once. The loop is written once. Switching models is one line in `.env`.

The direct Anthropic SDK is not used. All model calls go through `LLMClient`, which wraps LiteLLM. Nothing in `agent.py` or `tools.py` imports a provider SDK.

### Tool Calling Format

All tool definitions use OpenAI's function calling schema. LiteLLM translates to the correct provider format at call time. This means tool definitions are portable across all three providers without modification.

### Agent as a FastAPI Module

The agent runs as a module within the FastAPI process, not as a separate service. A new route `POST /chat` receives a user message and conversation history, calls `agent.run()`, and returns the response. The agent calls the backend repository functions directly — no HTTP self-calls.

### Stateless Server — Client Owns History

The server holds no conversation state between requests. The client sends the full message history on every `POST /chat` call. In Week 2 this client is a test script or curl. In Week 5, Streamlit holds history in `st.session_state`. This design keeps the server simple and scales without session storage.

### Profile as Versioned JSON

The user profile is a flat JSON file (`profile.json`) that the agent reads at the start of every conversation and updates when the user shares information about themselves. On every write that produces a change, the previous profile is backed up to `profiles/backups/profile_<timestamp>.json` before the new version is written. No change means no backup and no write.

This gives the user a full audit trail of profile evolution and the ability to roll back to any previous version by copying a backup over `profile.json`.

### System Prompt as a File

The system prompt is not hardcoded in Python. It lives in `agent/prompts/system.md` and is loaded at runtime. The prompt is a template — it contains a `{profile}` placeholder that is replaced with the current profile JSON before every API call. This means prompt iteration requires no code changes.

### Injected LLMClient for Testability

`agent.run()` accepts an optional `llm` parameter. In production this defaults to a real `LLMClient`. In tests a mock is injected. This means integration tests never make real API calls and do not require API keys.

---

## Phase 1 — Profile Manager

### Goal

Own the profile lifecycle: read the current profile, write updates with a diff check, and produce a versioned backup whenever a real change occurs.

### Profile Schema

```json
{
  "target_roles": ["Data Engineer", "Backend Engineer"],
  "skills": ["Python", "FastAPI", "SQL", "aiosqlite"],
  "preferred_locations": ["Singapore"],
  "experience_years": 3,
  "preferred_sources": ["GovTech", "LinkedIn"],
  "salary_expectation_sgd": { "min": 5000, "max": 8000 },
  "notes": "Prefer startups. Avoid pure frontend roles.",
  "updated_at": "2026-05-06T14:23:00"
}
```

### Implementation

```python
# agent/profile.py

import json
import shutil
from datetime import datetime
from pathlib import Path

PROFILE_PATH = Path("profile.json")
BACKUP_DIR   = Path("profiles/backups")


def read_profile() -> dict:
    """Return current profile. Returns empty dict if no profile exists yet."""
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text())


def write_profile(updated: dict) -> None:
    """
    Write an updated profile.

    Rules:
    - If updated == current: no-op. No backup, no write.
    - If updated differs: back up current first, then write updated.
    - Always stamps updated_at on write.
    """
    current = read_profile()
    _strip_metadata(current)
    _strip_metadata(updated)

    if current == updated:
        return

    _backup(current)
    updated["updated_at"] = datetime.utcnow().isoformat()
    PROFILE_PATH.write_text(json.dumps(updated, indent=2))


def _backup(profile: dict) -> None:
    """Write a timestamped snapshot to the backups directory."""
    if not profile:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    dest = BACKUP_DIR / f"profile_{timestamp}.json"
    dest.write_text(json.dumps(profile, indent=2))


def _strip_metadata(profile: dict) -> None:
    """Remove updated_at before diffing so timestamps don't cause false positives."""
    profile.pop("updated_at", None)
```

### Behaviour Table

| Scenario | Backup created | Profile written |
|---|---|---|
| First ever write | No (nothing to back up) | Yes |
| Write with real change | Yes | Yes |
| Write with identical content | No | No |
| Read on missing file | — | Returns `{}` |

---

## Phase 2 — Tool Definitions and Executors

### Goal

Define what tools the model can call (the JSON schema it sees) and implement what actually runs when each tool is called (the executor functions). These two concerns live in the same file but are clearly separated.

### Tool Definitions

All definitions use OpenAI's function calling format. LiteLLM translates to the correct provider schema at runtime.

```python
# agent/tools.py

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "log_job",
            "description": (
                "Log a new job application to the tracker. "
                "Only call this after the user has explicitly confirmed they want to log it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name"},
                    "role":    {"type": "string", "description": "Job title or role"},
                    "url":     {"type": "string", "description": "Direct URL to the listing"},
                    "source":  {"type": "string", "description": "Where the listing was found"},
                    "notes":   {"type": "string", "description": "Optional notes"}
                },
                "required": ["company", "role", "url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_status",
            "description": "Move a job application to a new status. Respects valid state transitions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer", "description": "ID of the job record"},
                    "status": {
                        "type": "string",
                        "enum": ["applied", "screening", "interview", "offer", "rejected"],
                        "description": "Target status"
                    },
                    "notes": {"type": "string", "description": "Optional notes on this update"}
                },
                "required": ["job_id", "status"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_jobs",
            "description": "Retrieve job applications from the tracker, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["found", "applied", "screening", "interview", "offer", "rejected"],
                        "description": "Filter by this status. Omit to return all."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": (
                "Merge new information into the user profile. "
                "Call this when the user shares preferences, skills, or personal details. "
                "Always confirm the change with the user before calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": "Key-value pairs to merge into the existing profile"
                    }
                },
                "required": ["updates"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": "Search for live job listings matching a query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search string, e.g. 'data engineer Singapore fintech'"
                    }
                },
                "required": ["query"]
            }
        }
    }
]
```

### Tool Executors

```python
# agent/tools.py (continued)

import json
from app.db import repository as repo
from app.models.job import JobCreate
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.routes.jobs import make_fingerprint
from agent.profile import read_profile, write_profile


async def execute_tool(tool_name: str, arguments: dict, db) -> str:
    """
    Dispatch a tool call by name. Returns a JSON string result in all cases.
    The ReAct loop feeds this string back to the model as a tool_result block.
    """
    handlers = {
        "log_job":        lambda: _log_job(arguments, db),
        "update_status":  lambda: _update_status(arguments, db),
        "query_jobs":     lambda: _query_jobs(arguments, db),
        "update_profile": lambda: _update_profile(arguments),
        "search_jobs":    lambda: _search_jobs(arguments),
    }
    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    result = handler()
    if hasattr(result, "__await__"):
        result = await result
    return result


async def _log_job(args: dict, db) -> str:
    job = JobCreate(**args)
    fp  = make_fingerprint(job)
    record, created = await repo.insert_job(db, job, fp)
    return json.dumps({"created": created, "job": record})


async def _update_status(args: dict, db) -> str:
    try:
        job = await repo.update_job_status(
            db,
            args["job_id"],
            ApplicationStatus(args["status"]),
            args.get("notes")
        )
        if not job:
            return json.dumps({"error": "Job not found"})
        return json.dumps(job)
    except InvalidTransitionError as e:
        return json.dumps({"error": str(e)})


async def _query_jobs(args: dict, db) -> str:
    jobs = await repo.get_all_jobs(db, status_filter=args.get("status"))
    return json.dumps(jobs)


def _update_profile(args: dict) -> str:
    current = read_profile()
    updated = {**current, **args["updates"]}
    write_profile(updated)
    return json.dumps({"status": "profile updated", "profile": updated})


def _search_jobs(args: dict) -> str:
    # Stubbed — scraping layer implemented in Week 3
    return json.dumps({
        "results": [],
        "note": "Search not yet available. Scraping layer coming in Week 3."
    })
```

### Tool Behaviour Table

| Tool | Calls | Returns on error |
|---|---|---|
| `log_job` | `repo.insert_job` | Never errors — idempotent upsert |
| `update_status` | `repo.update_job_status` | `{"error": "..."}` string — loop continues |
| `query_jobs` | `repo.get_all_jobs` | Empty list |
| `update_profile` | `profile.write_profile` | Always succeeds |
| `search_jobs` | Scraping layer (stubbed) | Stub result with note |

All executors return a JSON string. Errors are returned as `{"error": "..."}` rather than raising exceptions — the model sees the error and decides how to respond to the user.

---

## Phase 3 — LLM Client

### Goal

Wrap LiteLLM in a single class so that the rest of the agent never imports a provider SDK directly, and so that tests can inject a mock without patching global state.

```python
# agent/llm_client.py

from litellm import completion
from app.config import settings


class LLMClient:
    def __init__(self, model: str | None = None):
        self.model = model or settings.model

    def chat(self, messages: list, tools: list) -> object:
        """
        Call the configured model with message history and tool definitions.
        Returns a LiteLLM response object in OpenAI format regardless of provider.
        """
        return completion(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
```

### Model Switching

Change one value in `.env`. No code changes required.

```bash
# .env

MODEL=claude-sonnet-4-5        # Anthropic
# MODEL=gpt-4o                 # OpenAI
# MODEL=gemini/gemini-1.5-pro  # Google Gemini

ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

LiteLLM reads the correct key based on the model prefix. Unused keys are ignored.

### Config Additions

```python
# app/config.py

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Existing
    db_path:   str = "./jobs.db"
    log_level: str = "INFO"

    # Agent
    model:             str = "claude-sonnet-4-5"
    anthropic_api_key: str = ""
    openai_api_key:    str = ""
    gemini_api_key:    str = ""

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## Phase 4 — System Prompt

### Goal

Define agent behaviour as a markdown template. The `{profile}` placeholder is replaced at runtime with the current profile JSON. Prompt changes never require touching Python files.

```markdown
<!-- agent/prompts/system.md -->

## Role
You are a job search assistant. You have two responsibilities:

1. Shape the user's profile through conversation.
2. Help them discover, log, and track job applications.

## Current User Profile
{profile}

## Profile Behaviour
- Read the profile at the start of every conversation.
- When the user shares information about themselves — skills, preferences,
  salary expectations, locations — call update_profile to capture it.
- Before calling update_profile, state what you are about to change and
  ask the user to confirm. Never silently overwrite a field.
- If you detect a gap in the profile (missing target roles, no location set),
  ask about it naturally. Do not interrupt the flow with a form — weave it in.

## Job Management Behaviour
- Never log a job without the user's explicit confirmation.
- When searching, filter results against the profile's target_roles and
  preferred_locations wherever possible.
- When presenting search results, always state how each result matches the profile.
- Use the status enum values exactly: found, applied, screening, interview,
  offer, rejected.

## General Behaviour
- Be concise. The user is busy.
- If a tool returns an error, explain it plainly and suggest what to do next.
- Never invent job listings. Only report what search_jobs returns.
- Do not call search_jobs unless the user explicitly asks you to search.
```

---

## Phase 5 — ReAct Loop

### Goal

Implement the agent loop that renders the prompt, calls the model, executes tools, feeds results back, and repeats until the model returns a final text response.

```python
# agent/agent.py

import json
from pathlib import Path
from loguru import logger

from agent.llm_client import LLMClient
from agent.tools import TOOL_DEFINITIONS, execute_tool
from agent.profile import read_profile

SYSTEM_PROMPT_PATH = Path("agent/prompts/system.md")


def _render_system_prompt() -> str:
    template = SYSTEM_PROMPT_PATH.read_text()
    profile  = read_profile()
    return template.replace("{profile}", json.dumps(profile, indent=2))


async def run(
    messages: list,
    db,
    llm: LLMClient | None = None
) -> str:
    """
    Run the ReAct loop for one user turn.

    Args:
        messages: Full conversation history from the client, excluding system prompt.
        db:       Active aiosqlite connection (injected by FastAPI dependency).
        llm:      LLMClient instance. Defaults to production client. Pass a mock in tests.

    Returns:
        The model's final natural language response as a string.
    """
    if llm is None:
        llm = LLMClient()

    system_prompt = _render_system_prompt()
    history = [{"role": "system", "content": system_prompt}] + messages

    iteration = 0
    max_iterations = 10  # guard against infinite loops

    while iteration < max_iterations:
        iteration += 1
        logger.debug(f"Agent loop iteration {iteration}")

        response = llm.chat(history, TOOL_DEFINITIONS)
        message  = response.choices[0].message

        # No tool calls — model has produced its final response
        if not message.tool_calls:
            logger.debug("Agent loop complete — no tool calls in response")
            return message.content

        logger.debug(f"Tool calls requested: {[c.function.name for c in message.tool_calls]}")

        # Append the assistant's tool-calling message to history
        history.append(message)

        # Execute each tool and append its result
        for call in message.tool_calls:
            result = await execute_tool(
                tool_name=call.function.name,
                arguments=json.loads(call.function.arguments),
                db=db
            )
            logger.debug(f"Tool '{call.function.name}' result: {result}")
            history.append({
                "role":         "tool",
                "tool_call_id": call.id,
                "content":      result
            })

    logger.warning("Agent loop hit max iterations — returning fallback")
    return "I ran into an issue completing that request. Please try again."
```

### Loop Diagram

```
client sends messages (full history)
              │
              ▼
      render system prompt
      inject current profile
              │
              ▼
      ┌── call LLMClient.chat ──┐
      │                         │
      │   tool_calls present?   │
      │         │               │
      │        YES              │
      │         │               │
      │   execute each tool     │
      │   append tool results   │
      │         │               │
      └─────────┘               │
                                │
                   NO tool_calls│
                                │
                                ▼
                    return message.content
```

---

## Phase 6 — FastAPI Integration

### Goal

Expose `POST /chat` as the entry point for the agent. Wire it into the existing FastAPI app.

```python
# app/routes/chat.py

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from agent.agent import run
from app.db.database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    messages: list          # Full conversation history — client is responsible for this
    session_id: str | None = None   # Reserved for future use


class ChatResponse(BaseModel):
    reply: str


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, db=Depends(get_db)):
    reply = await run(payload.messages, db)
    return {"reply": reply}
```

Register the router in `main.py`:

```python
# app/main.py (addition)

from app.routes.chat import router as chat_router
app.include_router(chat_router)
```

### Endpoint

| Method | Path | Description |
|---|---|---|
| `POST` | `/chat` | Send a message + history, receive agent reply |

### Request Shape

```json
{
  "messages": [
    {"role": "user", "content": "Find me data engineer roles in Singapore"},
    {"role": "assistant", "content": "I found 3 roles matching your profile..."},
    {"role": "user", "content": "Log the first one"}
  ],
  "session_id": "optional-string"
}
```

---

## TDD Overview for the Agent Layer

---

### Philosophy

Same as the backend: tests are specifications. Write the test that describes correct behaviour, then write the code that satisfies it. The LLMClient is always injected as a mock in tests — no real API calls, no API keys required.

---

### Unit Tests (~70%)

**`tests/unit/test_profile.py`**

```python
import pytest
from agent.profile import read_profile, write_profile

def test_read_returns_empty_dict_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert read_profile() == {}

def test_write_creates_profile_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    assert (tmp_path / "profile.json").exists()

def test_write_stamps_updated_at(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    profile = read_profile()
    assert "updated_at" in profile

def test_write_creates_backup_on_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 1

def test_write_skips_backup_when_no_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backups = list(backup_dir.iterdir()) if backup_dir.exists() else []
    assert len(backups) == 0

def test_multiple_changes_produce_multiple_backups(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    write_profile({"skills": ["Python", "SQL", "FastAPI"]})
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 2
```

---

**`tests/unit/test_tools.py`**

```python
import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from agent.tools import execute_tool

@pytest.mark.asyncio
async def test_log_job_returns_created_true_on_new_insert():
    mock_record = {"id": 1, "company": "GovTech", "role": "Engineer",
                   "url": "https://careers.gov.sg/1", "status": "found",
                   "source": "manual", "notes": None, "date_logged": "2026-05-06"}
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(mock_record, True)):
        result = await execute_tool("log_job", {
            "company": "GovTech",
            "role": "Engineer",
            "url": "https://careers.gov.sg/1"
        }, db=MagicMock())
    assert json.loads(result)["created"] == True

@pytest.mark.asyncio
async def test_log_job_returns_created_false_on_duplicate():
    mock_record = {"id": 1, "company": "GovTech", "role": "Engineer",
                   "url": "https://careers.gov.sg/1", "status": "found",
                   "source": "manual", "notes": None, "date_logged": "2026-05-06"}
    with patch("agent.tools.repo.insert_job", new_callable=AsyncMock,
               return_value=(mock_record, False)):
        result = await execute_tool("log_job", {
            "company": "GovTech",
            "role": "Engineer",
            "url": "https://careers.gov.sg/1"
        }, db=MagicMock())
    assert json.loads(result)["created"] == False

@pytest.mark.asyncio
async def test_update_status_returns_error_on_invalid_transition():
    from app.models.enums import InvalidTransitionError
    with patch("agent.tools.repo.update_job_status",
               new_callable=AsyncMock,
               side_effect=InvalidTransitionError("Cannot transition")):
        result = await execute_tool("update_status",
                                    {"job_id": 1, "status": "offer"},
                                    db=MagicMock())
    assert "error" in json.loads(result)

@pytest.mark.asyncio
async def test_update_status_returns_error_on_missing_job():
    with patch("agent.tools.repo.update_job_status",
               new_callable=AsyncMock, return_value=None):
        result = await execute_tool("update_status",
                                    {"job_id": 9999, "status": "applied"},
                                    db=MagicMock())
    assert json.loads(result)["error"] == "Job not found"

@pytest.mark.asyncio
async def test_query_jobs_returns_list():
    with patch("agent.tools.repo.get_all_jobs",
               new_callable=AsyncMock, return_value=[{"id": 1}]):
        result = await execute_tool("query_jobs", {}, db=MagicMock())
    assert isinstance(json.loads(result), list)

@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    result = await execute_tool("nonexistent_tool", {}, db=MagicMock())
    assert "Unknown tool" in json.loads(result)["error"]

@pytest.mark.asyncio
async def test_search_jobs_returns_stub_while_unimplemented():
    result = await execute_tool("search_jobs", {"query": "Python jobs"}, db=MagicMock())
    parsed = json.loads(result)
    assert parsed["results"] == []
    assert "note" in parsed
```

---

**`tests/unit/test_llm_client.py`**

```python
from unittest.mock import patch, MagicMock
from agent.llm_client import LLMClient
from app.config import settings

def _mock_response(content="hello", tool_calls=None):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=tool_calls)
    )])

def test_llm_client_calls_litellm_completion():
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        client = LLMClient(model="claude-sonnet-4-5")
        client.chat([{"role": "user", "content": "hi"}], [])
        mock_completion.assert_called_once()

def test_llm_client_uses_settings_model_by_default():
    client = LLMClient()
    assert client.model == settings.model

def test_llm_client_accepts_model_override():
    client = LLMClient(model="gpt-4o")
    assert client.model == "gpt-4o"

def test_llm_client_passes_messages_and_tools():
    messages = [{"role": "user", "content": "hi"}]
    tools    = [{"type": "function", "function": {"name": "test"}}]
    with patch("agent.llm_client.completion",
               return_value=_mock_response()) as mock_completion:
        LLMClient(model="claude-sonnet-4-5").chat(messages, tools)
        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["messages"] == messages
        assert call_kwargs["tools"]    == tools
```

---

**`tests/unit/test_prompt_rendering.py`**

```python
import json
import pytest
from unittest.mock import patch
from agent.agent import _render_system_prompt

def test_profile_injected_into_prompt():
    profile = {"skills": ["Python"], "target_roles": ["Data Engineer"]}
    with patch("agent.agent.read_profile", return_value=profile):
        rendered = _render_system_prompt()
    assert json.dumps(profile, indent=2) in rendered

def test_empty_profile_renders_without_error():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert "{profile}" not in rendered

def test_prompt_template_placeholder_is_replaced():
    with patch("agent.agent.read_profile", return_value={"skills": []}):
        rendered = _render_system_prompt()
    assert "{profile}" not in rendered
```

---

### Integration Tests (~25%)

**`tests/integration/test_agent.py`**

```python
import pytest
import json
from unittest.mock import MagicMock, patch, AsyncMock
from agent.agent import run

def _make_llm(responses):
    """Build a mock LLMClient that returns responses in sequence."""
    mock = MagicMock()
    mock.chat.side_effect = responses
    return mock

def _text_response(content):
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=content, tool_calls=None)
    )])

def _tool_response(tool_name, arguments, call_id="call_1"):
    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(arguments)
    return MagicMock(choices=[MagicMock(
        message=MagicMock(content=None, tool_calls=[tool_call])
    )])

@pytest.mark.asyncio
async def test_returns_text_when_no_tool_called():
    llm = _make_llm([_text_response("You have 3 applications.")])
    with patch("agent.agent.read_profile", return_value={}):
        reply = await run(
            messages=[{"role": "user", "content": "how many jobs?"}],
            db=MagicMock(),
            llm=llm
        )
    assert reply == "You have 3 applications."

@pytest.mark.asyncio
async def test_executes_tool_then_returns_response():
    responses = [
        _tool_response("query_jobs", {}),
        _text_response("Here are your jobs.")
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs",
               new_callable=AsyncMock, return_value=[]):
        reply = await run(
            messages=[{"role": "user", "content": "list my jobs"}],
            db=MagicMock(),
            llm=llm
        )
    assert reply == "Here are your jobs."
    assert llm.chat.call_count == 2

@pytest.mark.asyncio
async def test_loop_stops_after_max_iterations():
    # Model keeps requesting tools indefinitely
    tool_resp = _tool_response("query_jobs", {})
    llm = MagicMock()
    llm.chat.return_value = tool_resp
    with patch("agent.agent.read_profile", return_value={}), \
         patch("agent.tools.repo.get_all_jobs",
               new_callable=AsyncMock, return_value=[]):
        reply = await run(
            messages=[{"role": "user", "content": "..."}],
            db=MagicMock(),
            llm=llm
        )
    assert "issue" in reply.lower()
    assert llm.chat.call_count == 10

@pytest.mark.asyncio
async def test_profile_update_triggers_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Seed an existing profile so there is something to back up
    import json
    (tmp_path / "profile.json").write_text(json.dumps({"skills": ["Python"]}))

    responses = [
        _tool_response("update_profile", {"updates": {"skills": ["Python", "SQL"]}}),
        _text_response("Profile updated.")
    ]
    llm = _make_llm(responses)
    with patch("agent.agent.read_profile",
               return_value={"skills": ["Python"]}):
        await run(
            messages=[{"role": "user", "content": "add SQL to my skills"}],
            db=MagicMock(),
            llm=llm
        )
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 1
```

---

**`tests/integration/test_chat_route.py`**

```python
import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient
from app.main import app
from app.db.database import create_tables

@pytest.fixture
async def async_client():
    await create_tables()
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client

@pytest.mark.asyncio
async def test_chat_returns_200_with_reply(async_client):
    with patch("app.routes.chat.run",
               new_callable=AsyncMock,
               return_value="You have 0 applications."):
        response = await async_client.post("/chat", json={
            "messages": [{"role": "user", "content": "how many jobs do I have?"}]
        })
    assert response.status_code == 200
    assert response.json()["reply"] == "You have 0 applications."

@pytest.mark.asyncio
async def test_chat_passes_full_message_history(async_client):
    history = [
        {"role": "user",      "content": "find me jobs"},
        {"role": "assistant", "content": "I found 3 roles..."},
        {"role": "user",      "content": "log the first one"}
    ]
    with patch("app.routes.chat.run",
               new_callable=AsyncMock,
               return_value="Logged.") as mock_run:
        await async_client.post("/chat", json={"messages": history})
        passed_messages = mock_run.call_args.args[0]
    assert len(passed_messages) == 3
```

---

### Running the Tests

```bash
# All agent tests
pytest tests/unit/test_profile.py tests/unit/test_tools.py \
       tests/unit/test_llm_client.py tests/unit/test_prompt_rendering.py \
       tests/integration/test_agent.py tests/integration/test_chat_route.py

# Unit only — fastest feedback loop
pytest tests/unit/

# With coverage
pytest --cov=agent --cov=app/routes/chat.py --cov-report=term-missing
```

### Coverage Targets

| File | Target | Reason |
|---|---|---|
| `agent/profile.py` | 100% | Pure file I/O — no excuses |
| `agent/tools.py` | 90%+ | Each executor and error path covered |
| `agent/llm_client.py` | 100% | One method |
| `agent/agent.py` | 85%+ | Loop paths, max-iteration guard covered |
| `app/routes/chat.py` | 90%+ | Route handler covered |

---

## Key Dependencies (additions to `requirements.txt`)

```
litellm
```

All other dependencies (`fastapi`, `aiosqlite`, `pydantic`, `loguru`, `httpx`, `pytest-asyncio`) are already present from the backend layer.

---

## Error Handling Strategy

| Failure Mode | Behaviour |
|---|---|
| Tool returns error JSON | Returned to model as tool_result — model explains to user |
| Invalid status transition | Caught in executor, returned as `{"error": "..."}` |
| Model hits max iterations | Loop exits, fallback message returned to user |
| LiteLLM / provider failure | Exception propagates to FastAPI → 500 response, logged |
| Profile write failure | Exception propagates — profile remains unchanged |
| Missing `profile.json` | `read_profile()` returns `{}` — agent starts fresh |

---

## Implementation Checklist

- [x] **Phase 1 — Profile Manager** — `agent/profile.py` + `tests/unit/test_profile.py`
- [ ] **Phase 2 — Tool Definitions and Executors** — `agent/tools.py` + `tests/unit/test_tools.py`
- [ ] **Phase 3 — LLM Client** — `agent/llm_client.py` + `tests/unit/test_llm_client.py`
- [ ] **Phase 4 — System Prompt** — `agent/prompts/system.md` + `tests/unit/test_prompt_rendering.py`
- [ ] **Phase 5 — ReAct Loop** — `agent/agent.py` + `tests/integration/test_agent.py`
- [ ] **Phase 6 — FastAPI Integration** — `app/routes/chat.py` + `tests/integration/test_chat_route.py`
