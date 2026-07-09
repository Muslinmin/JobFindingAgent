# Conversation Store & Messaging Model — Implementation Spec (v2)

> Source of truth for the conversation persistence concern of JobFindingAgent v2
> and for the two-bot messaging split that shapes it. Companion to `backend_v2.md`
> (which this spec adds a second database and a reduced `/chat` surface to) and to
> the forthcoming agent and scheduler specs (which are the only callers touched
> here). Captures scope, requirements, specifications, file responsibilities, the
> exact method signatures the agent calls, and the work-package and build order.
> Written for independent execution.

**One-line responsibility:** Persist each conversation's turns so the agent can
rebuild multi-turn context under a stateless `/chat` handler. This layer stores
and returns; it never reasons and never decides.

---

## 0. Context — the two decisions this spec settles

Two decisions were made together, because the first is what shrank the second.

**Decision one — two bots, not one.** Telegram interaction is split across two
separate bots, each with its own token (a token being the secret string that
authenticates a program as a particular bot). A **chat bot** handles inbound user
messages and forwards them to `/chat`. A **notifications bot** is outbound only:
the scheduler pushes job cards and lifecycle nudges through it directly via the
Telegram API, never through `/chat` and never through the backend's HTTP surface.
Because notifications and conversation now live on two separate message streams,
they can no longer collide on one surface.

**Decision two — a conversation store owned by the agent alone.** A separate
SQLite database file holds session metadata; JSON Lines transcript files on disk
hold the turns; the whole store is injected into the agent and read and written by
the agent only. The store exists for one reason: multi-turn coherence. Because
`/chat` is stateless per call — each HTTP call is a fresh invocation that keeps
nothing in memory from the previous one — the agent has no in-process memory of
earlier turns, so the persisted history is the only thing carrying context from
one message to the next.

### What decision one deleted

The two-bot split removed an entire stateful subsystem that a single shared
message surface would have required. The following are **deleted, not deferred**:

- The push-coexistence gate (the "is the user mid-conversation, hold or deliver?"
  decision).
- The `last_activity_at` signal the scheduler was going to read to make that
  decision.
- The held-push queue for notifications withheld during a conversation.
- Any contact whatsoever between the scheduler and this store. The scheduler and
  the conversation store are now total strangers.

Consequences for prior planning: **Component 8** collapses from "Push Delivery and
Conversation Coexistence" down to plain push delivery (the scheduler formats a
message and sends it through the notifications bot). The seventh scheduler job
penciled in for session-flush and idle-timeout is **no longer needed**, because
the only surviving use of a session is the agent bounding its own context, checked
lazily when a message arrives rather than as a standing scheduled job.

---

## 1. Scope Boundary

### Inside this layer
- A **second SQLite database file**, separate from the jobs database, holding one
  `sessions` table of session metadata.
- A folder of **JSON Lines transcript files** on disk, one file per session,
  holding the turns. (JSON Lines means each line of a file is one complete JSON
  object — here, one turn — so appending a turn appends a single line without
  reading or rewriting the whole file.)
- A repository module holding all SQL over the `sessions` table (the
  aiosqlite → asyncpg seam for this concern).
- A transcript-store module holding all JSON Lines file input and output.
- A facade — `ConversationStore` — that composes the repository and the transcript
  store and is the single object injected into the agent.
- Pydantic models: `Role`, `Turn`, `Session`.

### Outside this layer
- **Prompt building.** The agent reads history from this store and assembles the
  prompt itself. This store returns turns; it does not format them.
- **The continue-versus-new-session decision.** The store reports the latest
  session and creates new ones; the agent decides whether an incoming message
  continues the latest session or starts a fresh one. The idle check and the
  `session_idle_minutes` value live in the agent and its config, consistent with
  the project rule that a decision belongs to the caller, not the service.
- **Notification delivery.** Owned by the scheduler and the notifications bot.
  This store has no notion of pushes.
- **Message transport.** The chat bot and the `/chat` route move messages; this
  store never touches transport.

### Resolved boundary decisions
- **Session identifier is a UUID string, not an integer.** A UUID is generated
  before any write, so the transcript file path is known before the row is
  inserted; this lets session creation be a single insert rather than an insert
  followed by an update to fill in the path. It also fits what a session is — an
  opaque handle for one conversation thread, never ranked or ordered by id the way
  jobs are ranked by score.
- **Dedicated `conversation/` package, not the shared `models/`, `db/`,
  `services/` folders.** This concern has its own database and exactly one caller,
  so a single folder holding every file for it makes the boundary physical. The
  internal file names still mirror the jobs concern, so the naming convention is
  preserved inside the folder.
- **Two storage modules, not one.** The repository holds raw SQL (the driver
  seam); the transcript store holds filesystem I/O. They are kept separate so a
  database-driver change touches only the repository, and file I/O never leaks
  into the SQL-only module. The facade orchestrates both so the agent never sees
  the split.

---

## 2. Requirements

### Functional
1. **Start a session.** Generate a UUID identifier, derive its transcript path,
   insert a row with `started_at = last_activity_at = now`, and return it.
2. **Report the latest session.** Return the most recently started session, or
   nothing if none exists, so the agent can decide reuse versus a fresh session.
3. **Append a turn.** Stamp the turn's `created_at`, append it as one JSON line to
   the session's transcript file, and re-stamp that session's `last_activity_at`.
   One event produces exactly one write to each store.
4. **Load history.** Return every turn of a session, oldest first, for the agent
   to build a prompt from.

### Non-functional (invariants honored)
1. **Single caller.** Only the agent reads or writes this store. The scheduler and
   the jobs service have no reference to it.
2. **This layer never reasons.** No LLM call originates here. Every operation is
   deterministic and unit-testable.
3. **Repository isolation.** Switching aiosqlite → asyncpg touches only the
   conversation `repository.py` / `database.py`.
4. **Path is derived from the identifier.** Appending or loading a transcript never
   needs a database lookup to find the file, so the only database writes in the
   whole flow are the row creation and the activity stamp.
5. **Clock is injected.** The facade owns an injected clock (a small function
   returning the current time as an ISO-8601 UTC string) and passes the timestamp
   down; the repository stays a pure function of its inputs and is testable with a
   frozen clock, matching the jobs repository pattern.
6. **Timestamps are ISO-8601 UTC everywhere**, consistent with the jobs database.

---

## 3. Specifications

### 3a. Data Model

Two shapes live in two places. A `Turn` is one line in a transcript file; a
`Session` is one row in the database; the identifier links them.

**`sessions` table (conversations SQLite file)**
- `id` — TEXT, PK. UUID string; an opaque handle for one conversation thread.
- `started_at` — TEXT (ISO, UTC), NOT NULL.
- `last_activity_at` — TEXT (ISO, UTC), NOT NULL. Re-stamped on every appended turn.
- `transcript_path` — TEXT, NOT NULL. Filesystem path to this session's JSON Lines
  file. Derivable from `id`, stored for directness and traceability (mirrors the
  way the jobs `artifacts` table stores a `path` while bytes live on disk).

**Transcript file (one JSON Lines file per session)**
Each line is one `Turn` object:
- `role` — `USER` | `ASSISTANT` (Python-enum validated).
- `content` — the message text.
- `created_at` — ISO-8601 UTC.

**Conventions**
- **Session metadata in the database, turns on disk**, linked by `id`. The path is
  derived from `id` (e.g. `base_directory/{id}.jsonl`).
- **JSON Lines over a single JSON array**, so appending a turn appends one line
  rather than forcing a read-parse-append-rewrite of the whole file. This matches
  how append-only the rest of the design already is (keep-all artifacts, the
  append-only sightings on jobs).
- **Timestamps: TEXT ISO-8601, always UTC**, identical to the jobs database rule.

### 3b. Messaging Model (two bots)

- **Chat bot** — inbound only. Runs on a separate device (the front end). Forwards
  each user message to `/chat` and returns the agent's reply to the user. Holds no
  notification logic; pure transport and routing.
- **Notifications bot** — outbound only. The scheduler holds its token and pushes
  job cards (approve/skip prompts) and lifecycle nudges (pending-approval
  expiries, ghost warnings) through it directly via the Telegram API.
- **The two never entangle.** Inbound conversation and outbound notifications are
  separate message streams, so a push can never interrupt a conversation and the
  scheduler needs no activity signal. This is what deleted the coexistence gate.

`/chat` remains the single surviving HTTP endpoint on the backend, owned by the
backend's transport layer (the FastAPI app). It is thin: it forwards the incoming
message to the agent and returns the reply. The route is transport; the agent owns
the body.

### 3c. File Responsibilities & Signatures

Folder layout:

```
app/
└── conversation/
    ├── __init__.py
    ├── models.py            # Role, Turn, Session
    ├── database.py          # connection + schema init for the conversations SQLite file
    ├── repository.py        # ConversationRepository — SQL over the sessions table only
    ├── transcript_store.py  # TranscriptStore — JSON Lines file I/O only
    └── store.py             # ConversationStore — the facade injected into the agent
```

**`models.py` — the two data shapes**
```python
from enum import Enum
from pydantic import BaseModel

class Role(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"

class Turn(BaseModel):
    """One line in a session's JSON Lines transcript file."""
    role: Role
    content: str
    created_at: str          # ISO-8601 UTC

class Session(BaseModel):
    """One row in the conversations SQLite database."""
    id: str                  # UUID string; opaque handle for one conversation thread
    started_at: str          # ISO-8601 UTC
    last_activity_at: str    # ISO-8601 UTC; re-stamped on every appended turn
    transcript_path: str     # filesystem path to this session's JSON Lines file
```

**`database.py` — connection and schema for the separate file**
The conversations equivalent of the jobs `database.py`. Opens the second SQLite
file and creates its one table. Shared pragma helpers from the jobs side may be
reused; only the file path and the DDL differ.
```python
import aiosqlite

async def connect(database_path: str) -> aiosqlite.Connection:
    """Open a connection to the conversations database and apply pragmas."""
    ...

async def initialise_schema(connection: aiosqlite.Connection) -> None:
    """Create the sessions table if it does not already exist."""
    ...
```

**`repository.py` — SQL over session rows only**
Holds every SQL statement touching the `sessions` table and nothing else; never
reads or writes a transcript file. `now` is passed in by the caller (the
frozen-clock testing pattern used by the jobs repository).
```python
import aiosqlite
from app.conversation.models import Session

class ConversationRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        ...

    async def create_session(
        self,
        session_id: str,
        transcript_path: str,
        now: str,                 # ISO-8601 UTC, injected by the caller
    ) -> Session:
        """Insert a new session row with started_at = last_activity_at = now."""
        ...

    async def get_session(self, session_id: str) -> Session | None:
        """Fetch one session row by id, or None if it does not exist."""
        ...

    async def get_latest_session(self) -> Session | None:
        """Fetch the most recently started session, or None if there are none."""
        ...

    async def update_last_activity(self, session_id: str, now: str) -> None:
        """Stamp last_activity_at = now for one session. Touches nothing else."""
        ...
```

**`transcript_store.py` — JSON Lines file I/O only**
Owns the folder of transcript files and knows nothing about SQL. The path for a
session is derived deterministically from its id, so appending or reading a turn
requires no database lookup first.
```python
from app.conversation.models import Turn

class TranscriptStore:
    def __init__(self, base_directory: str) -> None:
        """base_directory is the folder holding every session's transcript file."""
        ...

    def transcript_path_for(self, session_id: str) -> str:
        """Deterministic path, e.g. base_directory/{session_id}.jsonl"""
        ...

    async def append_turn(self, transcript_path: str, turn: Turn) -> None:
        """Append one turn as a single JSON line. Never rewrites the whole file."""
        ...

    async def read_turns(self, transcript_path: str) -> list[Turn]:
        """Read and parse every line into Turn objects, oldest first."""
        ...
```

The file methods are `async` to match the codebase. Underlying file operations are
ordinarily blocking, so the implementation would use an async file library such as
`aiofiles` or run the blocking calls in a worker thread; at these transcript sizes
plain blocking I/O is also acceptable, but the async signature keeps the option
open without changing callers later.

**`store.py` — the facade the agent holds**
The one object injected into the agent. Holds the repository and the transcript
store, and owns an injected clock. Injecting the clock lets a test freeze time and
keeps the agent's calls clean, since the agent never produces a timestamp itself.
```python
from collections.abc import Callable
from app.conversation.models import Role, Turn, Session
from app.conversation.repository import ConversationRepository
from app.conversation.transcript_store import TranscriptStore

class ConversationStore:
    def __init__(
        self,
        repository: ConversationRepository,
        transcript_store: TranscriptStore,
        clock: Callable[[], str],     # returns ISO-8601 UTC 'now'; injected for testability
    ) -> None:
        ...

    async def start_session(self) -> Session:
        """Generate a session id, derive its transcript path, create the row, return it."""
        ...

    async def get_latest_session(self) -> Session | None:
        """Return the most recent session so the agent can decide reuse versus new."""
        ...

    async def append_turn(self, session_id: str, role: Role, content: str) -> Turn:
        """Stamp created_at, append the turn to the transcript file, and re-stamp the
        session's last_activity_at. One event, one write to each store."""
        ...

    async def load_history(self, session_id: str) -> list[Turn]:
        """Return every turn of the session, oldest first, for prompt building."""
        ...
```

### 3d. Agent Usage (verification only — not part of this layer)

This sketch lives in the agent, not the store. It is included only to confirm the
four facade methods are sufficient and to show where the one decision sits.
```python
# inside the agent, once per /chat call:
latest = await conversation_store.get_latest_session()
if latest is None or is_idle(latest.last_activity_at, session_idle_minutes):
    session = await conversation_store.start_session()
else:
    session = latest

await conversation_store.append_turn(session.id, Role.USER, incoming_message)
history = await conversation_store.load_history(session.id)
prompt  = build_prompt(history)                 # the agent's own concern
reply   = await llm.complete(prompt)
await conversation_store.append_turn(session.id, Role.ASSISTANT, reply)
return reply
```

`is_idle` and `session_idle_minutes` live in the agent and its config, not the
store. The store only reports the latest session and creates new ones; the agent
decides continue-versus-new. This is the entire remnant of the session concept
after the two-bot decision — a way for the agent to bound its own context, checked
lazily when a message arrives, with no scheduler job involved.

---

## 4. Components, Files & Work Packages

### Composition root wiring (`main.py`)
Built bottom-up; each step needs only earlier steps, so there is no circular
dependency. The store is handed to the agent alone.
```python
conversation_connection = await connect(conversation_db_path)
await initialise_schema(conversation_connection)

conversation_repository = ConversationRepository(conversation_connection)
transcript_store        = TranscriptStore(transcript_base_directory)
conversation_store      = ConversationStore(conversation_repository, transcript_store, clock)

agent = Agent(job_service=job_service, conversation_store=conversation_store, ...)
# scheduler receives NO reference to conversation_store
```

### Work packages
- **CW-A — Models.** `models.py`: `Role`, `Turn`, `Session`. Pure Pydantic; no
  I/O. Unit-tested: role validation; ISO-UTC serialization. No dependencies.
- **CW-B — Database & schema.** `database.py`: connect + `initialise_schema` for
  the separate SQLite file. Unit-tested: schema init creates the `sessions` table
  in a temp file. Depends on nothing but the driver.
- **CW-C — Repository.** `repository.py`: `create_session`, `get_session`,
  `get_latest_session`, `update_last_activity`. Real temp SQLite, injected `now`.
  Load-bearing tests: `create_session` sets `started_at == last_activity_at == now`;
  `update_last_activity` moves only that column; `get_latest_session` returns the
  newest by `started_at`. Depends on CW-A, CW-B.
- **CW-D — Transcript store.** `transcript_store.py`: `transcript_path_for`,
  `append_turn`, `read_turns`. Real temp directory. Load-bearing tests: append
  writes exactly one line; two appends then `read_turns` returns two turns
  oldest-first; path is derived from id. Depends on CW-A.
- **CW-E — Facade.** `store.py`: `start_session`, `get_latest_session`,
  `append_turn`, `load_history`, with an injected clock. Unit-tested by mocking the
  repository and transcript store: `append_turn` calls the transcript store once
  and `update_last_activity` once, with the clock's timestamp threaded through.
  Depends on CW-C, CW-D.

### Dependency order
`models.py` is the root. CW-A and CW-B are the foundation → CW-C (needs A + B) and
CW-D (needs A) in parallel → CW-E (needs C + D). Critical path:
**models → {B, } → C → E**, with **D** parallel to C after models, joining at E.

**Not work packages here:** the agent's prompt building and idle check (agent
layer); notification delivery (scheduler + notifications bot); the `/chat` route
edit (recorded in `backend_v2.md`).

---

## 5. Build & Test Order

Respects the §4 dependency graph and the project's TDD discipline:
Red-Green-Refactor; unit-heavy; real temp SQLite and real temp directories for the
storage modules; the facade tested against mocked repository and transcript store.

**Sequence:** `models.py` → **CW-B (database)** → **CW-C (repository)** →
**CW-D (transcript store)** → **CW-E (facade)**. C and D are interchangeable once
models and the database exist.

**Step 0 — `models.py`.** `Role`, `Turn`, `Session`. First red tests:
```python
def test_turn_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Turn(role="SYSTEM", content="x", created_at=t1)

def test_session_round_trips_iso_utc_fields():
    s = Session(id="u1", started_at=t1, last_activity_at=t1,
                transcript_path="/tmp/u1.jsonl")
    assert s.started_at == t1
```

**CW-B — Database & schema.** First red test:
```python
async def test_schema_creates_sessions_table(tmp_conv_db):
    conn = await connect(tmp_conv_db)
    await initialise_schema(conn)
    assert await table_exists(conn, "sessions")
```

**CW-C — Repository** (real temp SQLite; injected `now`). Load-bearing:
```python
async def test_create_session_sets_both_clocks_to_now(repo):
    s = await repo.create_session("u1", "/tmp/u1.jsonl", now=t1)
    assert s.started_at == t1 and s.last_activity_at == t1

async def test_update_last_activity_moves_only_that_column(repo, session_at_t1):
    await repo.update_last_activity(session_at_t1.id, now=t2)
    s = await repo.get_session(session_at_t1.id)
    assert s.last_activity_at == t2 and s.started_at == t1

async def test_get_latest_session_returns_newest(repo):
    await repo.create_session("u1", "/tmp/u1.jsonl", now=t1)
    await repo.create_session("u2", "/tmp/u2.jsonl", now=t2)
    assert (await repo.get_latest_session()).id == "u2"
```

**CW-D — Transcript store** (real temp directory). Load-bearing:
```python
async def test_append_writes_single_line(store, tmp_path):
    p = store.transcript_path_for("u1")
    await store.append_turn(p, Turn(role=Role.USER, content="hi", created_at=t1))
    assert sum(1 for _ in open(p)) == 1

async def test_read_turns_returns_oldest_first(store):
    p = store.transcript_path_for("u1")
    await store.append_turn(p, Turn(role=Role.USER, content="a", created_at=t1))
    await store.append_turn(p, Turn(role=Role.ASSISTANT, content="b", created_at=t2))
    turns = await store.read_turns(p)
    assert [t.content for t in turns] == ["a", "b"]
```

**CW-E — Facade** (mock repository + transcript store; injected clock).
Load-bearing: one append event produces exactly one write to each store.
```python
async def test_append_turn_writes_transcript_and_stamps_activity(
        store, mock_repository, mock_transcript_store):
    await store.append_turn("u1", Role.USER, "hi")
    mock_transcript_store.append_turn.assert_called_once()
    mock_repository.update_last_activity.assert_called_once()

async def test_start_session_creates_row_with_derived_path(
        store, mock_repository, mock_transcript_store):
    await store.start_session()
    mock_repository.create_session.assert_called_once()
```

---

## 6. Invariants

1. Only the agent holds a reference to the conversation store. The scheduler and
   the jobs service do not.
2. This layer never reasons; no LLM call originates in it.
3. Every database read/write is a repository function; every file read/write is a
   transcript-store function. SQL and file I/O never mix in one module.
4. The transcript path is derived from the session id, so no database lookup is
   needed to locate a transcript.
5. The clock is injected into the facade; the repository is a pure function of its
   inputs and is tested with a frozen clock.
6. Timestamps are ISO-8601 UTC everywhere.
7. Inbound conversation (chat bot → `/chat` → agent) and outbound notifications
   (scheduler → notifications bot) are separate streams that never entangle.

---

## 7. Deferred / Open

- **Prune / delete of old sessions and transcripts** — named, not built. No
  present reason to remove old conversations; a trivial later addition to both the
  repository and the transcript store when a retention policy is wanted.
- **History windowing** — `load_history` / `read_turns` return the full transcript;
  bounding the prompt is the agent's concern and a slice handles it today. A
  `limit` parameter is an easy future refinement if transcripts grow long enough to
  matter.
- **SQLite single-writer constraint** — the conversations database being a
  separate file means its writes never contend with jobs writes, which is the main
  reason it is a separate file rather than extra tables in the jobs database. The
  same WAL / `busy_timeout` mitigations noted in `backend_v2.md` apply if ever
  needed.
- **`backend_v2.md` edits** — pending in a follow-up pass: section 3d loses all
  endpoints except `/chat`; the scope section adopts the service-facade framing;
  the second database and its package are referenced from the backend spec.