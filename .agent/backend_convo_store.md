# Conversation Store — Status & Usage

> Companion to `backend_v2.md` (second database, reduced `/chat` surface) and
> `agent_v2.md` §6 (session-management contract this layer fulfils). For the
> two-bot messaging rationale (why the push-coexistence gate disappears), see
> `architecture_v2.md`. Implementation is complete (work packages CW-A–CW-E).
> This file used to be the build spec (scope, requirements, work-package
> order); that plan has been executed, so this is now a usage reference. The
> original spec is still in git history if ever needed again.

**One-line responsibility:** Persist each conversation's turns so the agent
can rebuild multi-turn context under a stateless `/chat` handler. This layer
stores and returns; it never reasons and never decides.

---

## What's implemented

`src/app/conversation/` is the layer's entire code surface:

| File | Responsibility |
|---|---|
| `models.py` | `Role` (`USER`/`ASSISTANT`), `Turn` (one transcript line), `Session` (one `sessions` row). Pure Pydantic, no I/O. |
| `database.py` | `connect(database_path)`, `initialise_schema(connection)` — opens the second SQLite file (separate from `jobs.db`) and creates the one `sessions` table. |
| `repository.py` | `ConversationRepository` — every SQL statement over `sessions`, and nothing else. `create_session`, `get_session`, `get_latest_session`, `update_last_activity`. `now` is always caller-injected (frozen-clock testable). |
| `transcript_store.py` | `TranscriptStore` — JSON Lines file I/O only, no SQL. `transcript_path_for`, `append_turn`, `read_turns`. Path is deterministic from `session_id`; missing file reads back as `[]`. |
| `store.py` | `ConversationStore` — the facade the agent holds. Composes the repository and transcript store behind one injected clock; the agent never sees the split. |

Tests: `src/test/unit/test_conversation_models.py`,
`src/test/unit/test_conversation_store.py` (facade, mocked repository +
transcript store), `src/test/integration/test_conversation_database.py`,
`test_conversation_repository.py`, `test_conversation_transcript_store.py`
(all three against real temp SQLite files / real temp directories, no
mocks). 29 tests total, all passing.

**Not built here (by design), now built by the agent layer:** the
composition-root wiring (`main.py`, plus the `conversation_db_path` /
`transcript_base_dir` settings) and the `is_idle` / `session_idle_minutes`
policy landed with `agent_v2.md`'s WP-A3 and WP-A8. `is_idle` lives in
`agent/context.py`, the settings in `app/config.py`, and the store is
constructed in `lifespan` exactly as the wiring sketch below shows.

One thing the sketch below does **not** show, and that a caller needs:
`get_latest_session` + `start_session` is a read-then-maybe-write, so the
*caller* must serialise it. Two concurrent first messages otherwise both
read "no session" and both create one. `POST /chat` holds a global
resolution lock around that decision; see `concurrencyFor_agentV2.md`.

---

## Data model

Session metadata lives in the database; turns live on disk; `id` links them.

- **`sessions` table** (conversations SQLite file) — `id` (TEXT PK, UUID),
  `started_at`, `last_activity_at` (both TEXT ISO-8601 UTC), `transcript_path`
  (TEXT, derivable from `id` but stored for directness).
- **Transcript file** — one JSON Lines file per session, one `Turn` per line:
  `role`, `content`, `created_at`.

The UUID is generated before any write, so the transcript path is known
before the row is inserted — session creation is a single insert, not an
insert-then-update.

---

## How to use

The facade's four methods are the entire contract surface (`agent_v2.md`
§6 pins these down as what the agent binds to):

```python
from app.conversation.store import ConversationStore
from app.conversation.models import Role

async def start_session() -> Session
async def get_latest_session() -> Session | None
async def append_turn(session_id: str, role: Role, content: str) -> Turn
async def load_history(session_id: str) -> list[Turn]
```

**Composition root wiring** (bottom-up, no circular dependency):

```python
conversation_connection = await connect(conversation_db_path)
await initialise_schema(conversation_connection)

conversation_repository = ConversationRepository(conversation_connection)
transcript_store        = TranscriptStore(transcript_base_directory)
conversation_store      = ConversationStore(conversation_repository, transcript_store, clock)

agent = Agent(job_service=job_service, conversation_store=conversation_store, ...)
# scheduler receives NO reference to conversation_store — the two are total strangers
```

**Per-`/chat`-call usage** (continue-vs-new is the agent's decision, not the
store's — the store only reports the latest session and creates new ones):

```python
latest = await conversation_store.get_latest_session()
session = (
    await conversation_store.start_session()
    if latest is None or is_idle(latest.last_activity_at, session_idle_minutes)
    else latest
)

await conversation_store.append_turn(session.id, Role.USER, incoming_message)
history = await conversation_store.load_history(session.id)
prompt  = build_prompt(history)                 # the agent's own concern
reply   = await llm.complete(prompt)
await conversation_store.append_turn(session.id, Role.ASSISTANT, reply)
return reply
```

`is_idle` and `session_idle_minutes` must live in the agent and its config,
never in this store. There is no end-of-session event: a stale session is
simply never reused, so it never needs closing — it falls out of scope the
moment the next message opens a fresh one. That is also what expires a
`PENDING_ACTION` marker embedded in an old assistant turn (`agent_v2.md`
§3d) — once a new session starts, the old turns are no longer in the loaded
history.

---

## Invariants (still binding)

1. Only the agent holds a reference to this store. The scheduler and the
   jobs service do not, and must not.
2. This layer never reasons; no LLM call originates here.
3. Every DB read/write is a `repository.py` function; every file read/write
   is a `transcript_store.py` function. SQL and file I/O never mix in one
   module.
4. The transcript path is derived from the session id — no DB lookup is
   needed to locate a transcript, so a call to `append_turn`/`load_history`
   touches the repository (or not) independently of the file.
5. The clock is injected into the facade only; `repository.py` is a pure
   function of its inputs, testable with a frozen clock.
6. Timestamps are ISO-8601 UTC everywhere.

---

## Deferred / Open

- **Prune / delete of old sessions and transcripts** — no present reason to
  remove old conversations; a trivial later addition to both storage modules
  when a retention policy is wanted.
- **History windowing** — `load_history` returns the full transcript;
  bounding the prompt is the agent's concern. A `limit` parameter is an easy
  later refinement if transcripts grow long enough to matter.
- **SQLite single-writer constraint** — the conversations database being a
  separate file means its writes never contend with jobs writes (the main
  reason it's a separate file rather than extra tables in `jobs.db`). Same
  WAL / `busy_timeout` mitigations as `backend_v2.md` apply if ever needed.
