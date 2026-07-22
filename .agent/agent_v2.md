# Agent Layer — Status & Usage

> **Status: built (WP-A1…A9).** `POST /chat` resolves a session, composes
> context, drives the ReAct loop against a real tool-calling model, and
> records both turns under a per-session lock and a turn deadline. The RR
> eval set passes against the live model.
>
> This file used to be the build spec (full rationale, per-tool design
> notes, work-package DoDs). That plan has been executed, so this is now a
> usage reference plus the remaining work. The original spec is in git
> history if the reasoning is ever needed again.
>
> **Six of ten tools are wired.** The other four raise `ToolNotWiredError`
> — see §7. Companion docs: `backend_convo_store.md` (the store this layer
> binds to), `concurrencyFor_agentV2.md` (timeout ladder, locking).

**One-line responsibility:** turn a human's free-text message into
validated service calls, and resolve which records that message refers to.
It is woken, runs, and returns.

---

## 1. What's implemented

`src/agent/` is the layer's entire code surface:

| File | Responsibility |
|---|---|
| `errors.py` | The frozen `{ok:false}` shapes the LLM narrates. 7 variants + a factory each. No logic. |
| `schemas.py` | `TOOL_SCHEMAS` — the 10 function-calling definitions, OpenAI/LiteLLM shape. No execution. |
| `context.py` | `ConversationContext` (`resolve_session` / `record` / `build_context`) over the store, plus the `is_idle` policy predicate and the `_compact` seam. |
| `prompt.py` | `compose()` = system message (spine + profile summary) then turns. `system_message()` is separable for tests. |
| `handlers.py` | `AgentDeps` + `ToolDispatcher.dispatch(name, args)`. One handler per tool: parse args → call service → marshal result. The boundary seam. |
| `loop.py` | `Agent.run(session_id, user_text)` — the ReAct driver, bounds and stop conditions. |
| `llm_client.py` | `TaskLLMClient` (single-shot, fail-fast; scheduler) and `AgentLLMClient` (tool-calling, retrying; this layer). |
| `prompts/system.md` | The static spine — role, truthfulness, resolve-then-act, FSM deference, the confirmation marker. |

Wired in at the composition root (`app/main.py::lifespan`), routed by
`app/routes/chat.py`. `profile_summary()` lives in `profile/projections.py`
— it is a projection of the source of truth, so it belongs to the profile
module, and `prompt.py` imports it.

**Tests:** `test_agent_errors.py`, `test_agent_schemas.py`,
`test_agent_context.py`, `test_agent_prompt.py`, `test_agent_handlers.py`,
`test_agent_loop.py`, `test_llm_client.py`,
`test_agent_reference_resolution.py` (unit RR rows) in `src/test/unit/`;
`test_chat_route.py` and `test_agent_reference_resolution_live.py` (eval
RR rows, `-m live`) in `src/test/integration/`.

---

## 2. Scope & boundary (still binding)

**Owns, and only this:** the ReAct loop behind `POST /chat`; reference
resolution (phrase → `job_id` / `skill_id`); conversation *assembly*;
per-tool LLM binding.

**Never:**
- Writes the DB directly — every durable change goes through a service.
- Contains the *work* behind a tool — tailoring, scrape, drafting, query
  regen are services it *calls*.
- Owns a loop or heartbeat — the scheduler drives the pipeline; the agent
  wakes on a call.
- Holds an authoritative fact in conversation history — history is
  resolution-only; every durable fact lives in a DB column.
- Enforces the FSM — it *proposes* `update_status`; the backend rejects
  illegal transitions.
- Decides push timing or renders for Telegram.

**Single runtime caller:** a free-text turn via `POST /chat`, relayed by
Telegram. Button callbacks carry their own `job_id` and bypass the agent
straight to the backend. The scheduler never enters the loop and holds no
reference to the conversation store.

**Ownership split (do not blur):** session *policy* (continue-vs-new) =
agent; session *storage* = the conversation store; conversation *assembly*
= agent; tool *work* = services.

---

## 3. How to use

### Construction (composition root)

Bottom-up, no circular dependency. Already done in `main.py::lifespan`;
reproduced here because a test or a second entry point needs the same shape.

```python
conversation_db = await connect(settings.conversation_db_path)
await initialise_schema(conversation_db)
store = ConversationStore(
    ConversationRepository(conversation_db),
    TranscriptStore(Path(settings.transcript_base_dir)),
    lambda: datetime.now(timezone.utc).isoformat(),
)

context = ConversationContext(store, settings.session_idle_minutes)
agent = Agent(
    llm=AgentLLMClient(),
    dispatcher=ToolDispatcher(AgentDeps(
        job_service=..., scorer=..., llm=TaskLLMClient(), settings=settings,
        profile_path=Path(settings.profile_path),
        queries_path=Path(settings.search_queries_path),
    )),
    context=context,
    profile_path=Path(settings.profile_path),
)

app.state.session_locks = {}                     # per-session turn locks
app.state.session_resolution_lock = asyncio.Lock()  # see §5
```

`AgentDeps` is a dataclass of concrete collaborators rather than a service
locator, so a test constructs exactly the mocks a handler touches and a
missing dependency is a construction error, not a runtime surprise.

### Per-call flow (`POST /chat`)

```python
async with app.state.session_resolution_lock:
    session = await context.resolve_session()     # continue-vs-new
    turn_lock = _lock_for(request, session.id)

async with turn_lock:
    reply = await asyncio.wait_for(
        agent.run(session.id, message),
        timeout=settings.agent_turn_deadline_s,
    )
```

`agent.run` records the user turn, composes, iterates, records the
assistant turn, and returns the reply text. It owns no timeout of its own —
the deadline belongs to the request, not to the reasoning.

### Settings this layer reads

`session_idle_minutes` (30), `conversation_db_path`, `transcript_base_dir`,
`agent_turn_deadline_s` (180), `llm_call_timeout_s` (60), `llm_max_retries`
(3), `llm_retry_wait_s` (5), `llm_reasoning_effort` (`"none"`, §6 inv. 7),
`profile_path`, `search_queries_path`, `score_threshold`. The ladder invariant `llm_call < agent_turn <
backend_read` is asserted in `test_config.py`, not merely assumed.

---

## 4. Tool contracts

Each tool is an LLM-facing **schema** over a caller-agnostic **service**.
Mutating tools take an already-resolved `job_id`, never a fuzzy hint.

| Tool | Params | Returns | Expected failures |
|---|---|---|---|
| `find_jobs` | `job_title?, company?, status_set[]?, limit=50` | `[{id, role, company, status, score, status_changed_at}]` | none (empty list is valid) |
| `score_job` | `description` | `{ok, score}` | `embedding_unavailable` |
| `score_ingest` | `company, title, description, url?, posted_at?` | `{ok, id, status, score, seen_count, was_scored}` | `embedding_unavailable` |
| `update_status` | `job_id, new_status, note?` | `{ok, job_id, role, company, old_status, new_status}` | `not_found`, `illegal_transition{from,to,allowed}` |
| `regenerate_queries` | *(none)* | `{ok, count, queries_preview}` | `profile_empty` |
| `update_profile` | `op, …per-op fields, confirmed=False` | unconfirmed: `{ok, changed:false, pending_confirmation:true, diff}`; confirmed: `{ok, changed, summary, queries_stale}` | `invalid_patch{detail}` |
| `search_jobs` | `query, sources[]?, limit=20` | — | **unwired** (§7) |
| `tailor_resume` | `job_id` | — | **unwired** (§7) |
| `draft_followup` | `job_id` | — | **unwired** (§7) |
| `draft_cover_letter` | `job_id` | — | **unwired** (§7) |

### Rules that govern them

- **Resolution is centralised.** Mutating tools are id-only; the model
  resolves via `find_jobs` first. The entire 0/1/N ambiguity surface lives
  in that one flow rather than smeared across ten tools.
- **Errors are data, mostly.** Expected failures become `{ok:false}` the
  model narrates and the loop re-enters. An unexpected exception is *not*
  data: it aborts the turn and never becomes a tool result the model gets
  to narrate as a normal outcome.
- **The agent proposes FSM moves; the backend adjudicates.** On
  `illegal_transition` the reply is anchored to the service's verdict and
  its `allowed` list, not to the model's own reasoning about the state
  machine. It does not retry a refusal.
- **`find_jobs` defaults differ by call shape.** A named lookup (title
  and/or company) searches **all** statuses including terminal ones — you
  may be recalling a job since rejected. A bare listing hides terminal
  statuses, because "what am I working on" should not surface rejected
  jobs. Order is `status_changed_at DESC, id ASC`, which is what makes
  "the third one" deterministic.
- **`score_ingest` never re-scores.** An existing row that already carries
  a score returns it untouched — recomputing spends embedding credits to
  recreate a value the system promised to keep (`scoring_v2.md`).
- **`update_profile` is the only source-of-truth mutator** and the only
  two-phase write.

### `update_profile` takes a typed op, not a patch

A deliberate deviation from this file's original `patch` wording. The
mutator (`profile/mutate.py`, the sole permitted writer of `profile.json`)
takes a discriminated-union `ProfileOp`, and `profile/schema.py` §Mutation
says why: a patch dict cannot express whether a list write **appends or
replaces**, so "add robotics QA to my target tracks" could silently
destroy the other tracks. The tool schema exposes `op` plus its per-op
fields, with the op enum derived from the union so a new op cannot be
added without appearing in the schema.

`edit_bullets` and `tag_skill` are full replacements — the schema
description says so explicitly, because a model that assumes they append
silently drops whatever it did not resend.

---

## 5. Loop, context, and session mechanics

### Stop conditions (priority order)

1. **Final answer** — text with no tool call. Success.
2. **Max iterations** (`MAX_ITERATIONS = 5`) — best-effort reply, *not* an
   error. The user should see how far it got.
3. **No progress** — same tool, same raw args, twice in a row. The drift guard.
4. **Consecutive exceptions** (2) — abort with a generic error.

The counter for (4) is cleared **only by a successful tool call**, never by
a successful LLM call. Every tool failure is followed by a successful LLM
call — that is how the loop asks what to do next — so clearing it there
would mean two consecutive tool failures could never accumulate and the
condition would never fire.

**A crashed tool rewinds the round.** The exception never becomes a tool
message (invariant 2), which would otherwise strand the assistant
`tool_calls` that asked for it and make the next request a 400 (invariant
9) — so the assistant message goes too, and the model sees the
conversation as it stood before it acted. It therefore has no way to know
the call failed and will normally propose it again; that repeat is caught
ahead of the drift guard and returns the *error* reply rather than the
no-progress one, because "something broke" and "tell me which job you
meant" are not the same thing to a user. This is the live path today: the
four unwired tools in §7 all raise.

### Context composition

Three tiers: the **static spine** (`system.md`, identical every turn, read
once at import); the **profile summary** (reference tier — identity
verbatim, index as `id: label`, bodies dropped, contact excluded; the
*full* profile is loaded just-in-time by the tailoring service and never
carried in the loop); and the **assembled turns** (whole current session in
v1, `_compact` is the seam).

### Sessions

A session is a bounded window of turns, holding no authoritative facts.
There is **no end event** — a stale session is never reused, so it never
needs closing. `resolve_session` continues the latest session unless it is
absent or idle past `session_idle_minutes`.

**Resolution must be serialised by the caller.** `get_latest_session` +
`start_session` is a read-then-maybe-write: two simultaneous first messages
otherwise both see "no session", both create one, and the conversation
splits across two transcripts — which also defeats the per-session lock,
since the two turns then hold *different* lock objects. Hence the global
`session_resolution_lock`, held only for a read and at most one insert; the
slow part of the turn stays under the per-session lock, so unrelated
conversations still run concurrently.

That same "never reused" property is what expires a stale `PENDING_ACTION`
marker: once a new session starts, the old turns are out of loaded history.

---

## 6. Invariants (still binding)

1. Every durable change goes through a service. The agent holds no DB
   connection and no repository.
2. Expected failures are `{ok:false}` data; unexpected exceptions raise and
   abort the turn. Never launder one into the other.
3. The agent proposes FSM moves and relays the backend's verdict. It does
   not pre-filter illegal moves — two FSMs would drift.
4. History is resolution-only. What gets recorded is turn text, never a
   structured fact the system would later trust.
5. `AgentLLMClient` re-raises `asyncio.CancelledError` rather than retrying
   — a turn the deadline abandoned must not keep sleeping.
6. Every function touching `context.py`, a service, or the LLM is
   `async def`; backoff is `await asyncio.sleep`, never `time.sleep`. This
   app runs the API, both bots, and the scheduler on one event loop.
7. Reasoning models reject function tools on `/v1/chat/completions`, so
   `AgentLLMClient` sends `reasoning_effort=settings.llm_reasoning_effort`
   (`"none"`) with `drop_params=True` (a no-op for providers without that
   knob). Live-verified against `gpt-5.6-luna`, which 400s otherwise;
   BerriAI/litellm#33221. It is a **setting, not a literal**, because
   `drop_params` removes unsupported parameter *names* and not unsupported
   *values* — a reasoning model whose enum starts at `"minimal"` still 400s
   on `"none"`, and `LLM_REASONING_EFFORT=""` omits the parameter.
8. Requests carry `parallel_tool_calls=False`, and it is sent **only when
   `tools` is non-empty** (OpenAI rejects it on a request with no tools).
   The provider default is `true`; one call per iteration is what the loop's
   drift guard and its rewind-on-crash rule both assume.
9. Every `tool_calls` entry the loop appends is answered by a matching
   `tool` message before the next request, or the whole round is removed.
   There is no third option: an unanswered `tool_call_id` is a 400.

---

## 7. Remaining work — for whoever picks this up next

### WP-A10 — the four unwired services

Not agent-layer packages; the agent is only what surfaces the gap. Each
tool currently raises `ToolNotWiredError` from `handlers.py`. That is
deliberate: a structured `{ok:false}` would invite the model to narrate a
missing service as a normal refusal ("your resume couldn't be tailored
right now"), which is a lie. Wire the service, then replace the stub.

- **`tailor_resume`** — *cheapest, highest value.* `tailoring.tailor()`
  already works end to end; only the artifact wrapper is missing. Needed:
  read the job, look in its artifact directory, back up any existing file
  to a timestamped `.bak` name, write the new one, call
  `register_artifact(job_id, {kind: "cv_pdf", path})` (append-only — the
  DB keeps full history; the single-live-file property lives in the
  *directory*, not a uniqueness rule), return
  `{ok, job_id, artifact_id, kind, replaced}`. Filename is a slug from
  `(company, title, kind)` with a short `job_id` hash only as a collision
  suffix. Producing the file must **not** move the FSM.
- **`search_jobs`** — needs a shared scrape+ingest service extracted from
  `scheduler/jobs/scrape.py`, where the logic currently lives as
  `_fan_out` / `_ingest_and_score`, entangled with the batch loop and its
  per-record error isolation. It ingests, it does not preview: every
  result enters the pipeline and over-broad recall is absorbed by the
  scorer.
- **`draft_followup`** — needs the drafting half of
  `scheduler/jobs/follow_up.py` split from its Telegram push.
  `_assemble_followup_prompt(role, company, applied_date)` is already the
  reusable core. Drafting is not sending: it must never touch `status` or
  `follow_up_count`.
- **`draft_cover_letter`** — no service exists at all. Specced in
  `architecture_v2.md` §773 / §839: JD + profile → 250–400 words (hook,
  direct-match body, gap handling, call to action), persisted as a
  plain-text `cover_letter` artifact, with a `prompts/cover_letter.md`
  loaded by that **service**, not by the loop.

### Two loose ends the tool wiring will expose

- **Nothing strips the `PENDING_ACTION` marker.** `system.md` instructs
  the model to emit `<<<PENDING_ACTION {...}>>>` at the end of a turn that
  proposes a two-turn confirmation, and to replay the stored payload
  verbatim if the user agrees (the model does the replay itself by reading
  it back out of history — no code needed for that half). But nothing
  strips it before display, so it would currently reach the user in
  Telegram. Strip it bot-side in `telegram_bot/chat/handlers.py`, keeping
  the marker in the *stored* turn — that is what the next turn reads.
- **`attachments` is never populated.** `ChatResponse` carries the field
  and the bot already base64-decodes it into a Telegram document
  (`telegram_bot/chat/handlers.py`), but no handler produces one yet. It
  becomes live with `tailor_resume`: the handler reads the PDF bytes from
  the `ArtifactResult` path so they ride out in the single response. The
  raw bytes never go to the LLM — only the narration shape does.

### Deferred (no present reason to build)

- Compaction (trim / summarise-on-eviction) inside `context.py` — seam
  only in v1; `_compact` is identity.
- `target_tracks` weighting — flat list in v1.
- `WP-C6`, a Telegram-side typing indicator, tracked in
  `concurrencyFor_agentV2.md`. It matters more now that the loop is real:
  a five-iteration turn against a reasoning model is visibly slow.
