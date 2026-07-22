# Agent Layer — Implementation Summary

> **Status: built and live.** `POST /chat` resolves a session, composes
> context, drives the ReAct loop against a real tool-calling model, and
> records both turns under a per-session lock and a turn deadline. All ten
> tools are wired.
>
> This file was once the build spec, then a spec-plus-worklog. Both jobs
> are done, so it is now a summary: what exists, what the contracts are,
> and which rules must not be broken. The per-decision reasoning that used
> to live here is in the module docstrings of the code it governs — they
> are the maintained copy. Deeper history is in git.
>
> Companion docs: `backend_convo_store.md` (the store this binds to),
> `concurrencyFor_agentV2.md` (timeout ladder, locking).

**One-line responsibility:** turn a human's free-text message into
validated service calls, and resolve which records that message refers to.
It is woken, runs, and returns.

---

## 1. What exists

`src/agent/`:

| File | Responsibility |
|---|---|
| `errors.py` | The frozen `{ok:false}` shapes the LLM narrates. 7 variants + a factory each. No logic. |
| `schemas.py` | `TOOL_SCHEMAS` — the 10 function-calling definitions, OpenAI/LiteLLM shape. No execution. |
| `context.py` | `ConversationContext` (`resolve_session` / `record` / `build_context`), the `is_idle` predicate, the `_compact` seam. |
| `prompt.py` | `compose()` = system message (spine + profile summary) then turns. |
| `handlers.py` | `AgentDeps` + `ToolDispatcher.dispatch(name, args)`. One handler per tool. The boundary seam. |
| `loop.py` | `Agent.run(session_id, user_text) -> TurnResult` — the ReAct driver, stop conditions, attachment pop. |
| `llm_client.py` | `TaskLLMClient` (single-shot, fail-fast) and `AgentLLMClient` (tool-calling, retrying). |
| `prompts/system.md` | The static spine — role, truthfulness, resolve-then-act, FSM deference, the confirmation marker. |

Constructed in `app/main.py::lifespan`, routed by `app/routes/chat.py`.
`profile_summary()` lives in `profile/projections.py` — it projects the
source of truth, so it belongs to the profile module.

**Three shared collaborators live outside `src/agent/`** because a tool
needed them and a scheduler job already did the same work. Each is the
single copy; neither caller may fork it:

| Module | Also used by | Read its docstring for |
|---|---|---|
| `app/services/discovery.py` | `scheduler/jobs/scrape.py` | `IngestOutcome`'s four narrowing counts |
| `app/services/artifacts.py` | available to `scheduler/jobs/tailor.py` | backup-on-replace, why the rename precedes the insert |
| `src/drafting/` | `scheduler/jobs/follow_up.py` | the cover-letter guard and its known limit |

**Tests:** `test_agent_{errors,schemas,context,prompt,handlers,loop}.py`,
`test_llm_client.py`, `test_agent_reference_resolution.py`,
`test_services_discovery.py`, `test_services_artifacts.py`,
`test_drafting.py` in `src/test/unit/`; `test_chat_route.py` and
`test_agent_reference_resolution_live.py` (`-m live`) in
`src/test/integration/`.

---

## 2. Scope & boundary

**Owns, and only this:** the ReAct loop behind `POST /chat`; reference
resolution (phrase → `job_id` / `skill_id`); conversation *assembly*;
per-tool LLM binding.

**Never:**
- Writes the DB directly — every durable change goes through a service.
- Contains the *work* behind a tool — tailoring, scrape, drafting, query
  regen are services it *calls*.
- Owns a loop or heartbeat — the scheduler drives the pipeline.
- Holds an authoritative fact in conversation history — history is
  resolution-only; every durable fact lives in a DB column.
- Enforces the FSM — it *proposes*; the backend rejects illegal moves.
- Decides push timing or renders for Telegram.

**Single runtime caller:** a free-text turn via `POST /chat`, relayed by
Telegram. Button callbacks carry their own `job_id` and bypass the agent.
The scheduler never enters the loop.

**Ownership split (do not blur):** session *policy* = agent; session
*storage* = the conversation store; conversation *assembly* = agent; tool
*work* = services.

---

## 3. Tool contracts

Each tool is an LLM-facing **schema** over a caller-agnostic **service**.
Mutating tools take an already-resolved `job_id`, never a fuzzy hint.

| Tool | Params | Returns | Expected failures |
|---|---|---|---|
| `find_jobs` | `job_title?, company?, status_set[]?, limit=50` | `[{id, role, company, status, score, status_changed_at}]` | none (empty list is valid) |
| `search_jobs` | `query, sources[]?, limit=20` | `{ok, query, sources[], fetched, ingested, new, scored, jobs[]}` | `not_found` (no such source) |
| `score_job` | `description` | `{ok, score}` | `embedding_unavailable` |
| `score_ingest` | `company, title, description, url?, posted_at?` | `{ok, id, status, score, seen_count, was_scored}` | `embedding_unavailable` |
| `update_status` | `job_id, new_status, note?` | `{ok, job_id, role, company, old_status, new_status}` | `not_found`, `illegal_transition{from,to,allowed}` |
| `tailor_resume` | `job_id` | `{ok, job_id, artifact_id, kind, status, replaced}` + an out-of-band attachment | `not_found`, `guard_violation{guard}` |
| `draft_followup` | `job_id, note?` | `{ok, job_id, role, company, draft}` | `not_found`, `guard_violation{guard}` |
| `draft_cover_letter` | `job_id, note?` | `{ok, job_id, artifact_id, kind, filename, replaced, letter}` | `not_found`, `guard_violation{guard}` |
| `regenerate_queries` | *(none)* | `{ok, count, queries_preview}` | `profile_empty` |
| `update_profile` | `op, …per-op fields, confirmed=False` | unconfirmed: `{ok, changed:false, pending_confirmation:true, diff}`; confirmed: `{ok, changed, summary, queries_stale}` | `invalid_patch{detail}` |

### Rules that govern them

- **Resolution is centralised.** Mutating tools are id-only; the model
  resolves via `find_jobs` first, so the whole 0/1/N ambiguity surface
  lives in one flow rather than smeared across ten tools.
- **Errors are data, mostly.** Expected failures become `{ok:false}` the
  model narrates and the loop re-enters. An unexpected exception is *not*
  data: it aborts the turn.
- **The agent proposes FSM moves; the backend adjudicates.** On
  `illegal_transition` the reply is anchored to the service's verdict and
  its `allowed` list. It does not retry a refusal.
- **`find_jobs` defaults differ by call shape.** A named lookup searches
  **all** statuses including terminal ones — you may be recalling a job
  since rejected. A bare listing hides them. Order is
  `status_changed_at DESC, id ASC`, which makes "the third one"
  deterministic. Matching is on letters and digits only
  (`repository._loose`): title punctuation varies by source and gets
  rewritten in transit, and a false negative is a dead end where a false
  positive is just a disambiguation prompt.
- **`search_jobs` ingests, it does not preview.** Every result enters the
  pipeline and is scored; over-broad recall is the scorer's problem. An
  unconfigured source name is `not_found`, never a silent fall-back to all
  sources.
- **`score_ingest` never re-scores.** An existing row that carries a score
  returns it untouched (`scoring_v2.md`).
- **`tailor_resume` moves the job to TAILORED, and no further.** Once the
  file exists TAILORED is simply true; the scheduler's tailor job makes the
  same move, so both paths agree. PENDING_APPROVAL stays off-limits — that
  state claims the *user* has been asked. The move is proposed, not
  pre-checked: re-tailoring a job past SCORED keeps the artifact and leaves
  the status alone.
- **`TAILORED → APPLIED` is legal.** PENDING_APPROVAL has duration only in
  the scheduler's push flow, where the human is asleep; in conversation it
  is entered and left in the same breath. An addition, not a replacement —
  `SCORED → APPLIED` stays illegal, so applying still requires a resume.
- **The three document tools deliver differently**, by what the user can do
  with the thing in a chat window. `tailor_resume` → attachment only (a PDF
  is unreadable in a bubble). `draft_cover_letter` → text *and* file (you
  review it by reading it; 400 words is worth keeping).
  `draft_followup` → text only, no artifact.
- **`note` is the user's words, not the model's.** Both drafting tools take
  an optional instruction. It makes iteration possible and doubles as
  drift-guard relief — a redraft with a different `note` is not an
  identical repeated call.
- **`update_profile` is the only source-of-truth mutator** and the only
  two-phase write. It takes the mutator's typed `ProfileOp`, not a patch
  dict: a dict cannot express whether a list write appends or replaces, so
  "add robotics QA to my target tracks" could silently destroy the others.
  `edit_bullets` and `tag_skill` replace wholesale.

---

## 4. Loop, context, and sessions

### Stop conditions (priority order)

1. **Final answer** — text with no tool call. Success, and the only normal exit.
2. **Max iterations** (`MAX_ITERATIONS = 5`) — best-effort reply, not an error.
3. **No progress** — same tool, same raw args, twice running. The drift guard.
4. **Consecutive exceptions** (2) — abort with a generic error.

The counter for (4) clears **only on a successful tool call**, never on a
successful LLM call — every tool failure is followed by a successful LLM
call, so clearing it there would mean the condition never fires.

**A crashed tool rewinds the round.** The exception never becomes a tool
message (inv. 2), which would strand the assistant `tool_calls` and 400 the
next request (inv. 9) — so the assistant message goes too. The model then
cannot know the call failed and will re-propose it; that repeat is caught
ahead of the drift guard and returns the *error* reply, because "something
broke" and "tell me which job you meant" are different to a user.

**Attachments ride beside the reply, never through it.** A handler that
produces a file adds `ATTACHMENT_KEY`; `loop.py` pops it before
serialisation, so the model learns the artifact exists and never sees a
byte. It travels on `TurnResult(reply, attachments)` as a *path*;
`POST /chat` reads and base64s it. An unreadable file degrades the turn
rather than failing it. Attachments accumulate into a list passed *into*
`_iterate`, so a turn that hit the cap after producing a CV still delivers.

### Context composition

Three tiers: the **static spine** (`system.md`, read once at import); the
**profile summary** (identity verbatim, index as `id: label`, bodies
dropped — the *full* profile is loaded just-in-time by the tailoring and
drafting services, never carried in the loop); and the **assembled turns**
(whole current session; `_compact` is identity in v1).

### Sessions

A session is a bounded window of turns holding no authoritative facts.
There is **no end event** — a stale session is never reused, so it never
needs closing. `resolve_session` continues the latest unless it is absent
or idle past `session_idle_minutes` (30). That is the *only* boundary: no
`/new`, no topic detection, and a restart does not split one, since the
store is on disk.

**Resolution must be serialised by the caller.** `get_latest_session` +
`start_session` is a read-then-maybe-write: two simultaneous first messages
would both see "no session", both create one, and split the conversation
across two transcripts — which also defeats the per-session lock, since the
turns would hold different lock objects. Hence the global
`session_resolution_lock`, held only for a read and at most one insert.

Session rollover is also what expires a stale `PENDING_ACTION` marker.
Nothing expires one *within* a live session.

---

## 5. Invariants

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
   `async def`; backoff is `await asyncio.sleep`. This app runs the API,
   both bots, and the scheduler on one event loop.
7. Reasoning models reject function tools on `/v1/chat/completions`, so
   `AgentLLMClient` sends `reasoning_effort=settings.llm_reasoning_effort`
   (`"none"`) with `drop_params=True`. Live-verified against `gpt-5.6-luna`,
   which 400s otherwise; BerriAI/litellm#33221. A **setting, not a
   literal**: `drop_params` removes unsupported parameter *names*, not
   unsupported *values*, so a model whose enum starts at `"minimal"` still
   400s on `"none"` — and `LLM_REASONING_EFFORT=""` omits it entirely.
8. Requests carry `parallel_tool_calls=False`, sent **only when `tools` is
   non-empty** (OpenAI rejects it otherwise). One call per iteration is what
   the drift guard and the rewind-on-crash rule both assume.
9. Every `tool_calls` entry the loop appends is answered by a matching
   `tool` message before the next request, or the whole round is removed.
   An unanswered `tool_call_id` is a 400.
10. The model must never announce work instead of doing it. Its turn ends
    the moment it replies without a tool call, so "I'll generate that now"
    followed by no call strands the user waiting for something that will
    never happen. Enforced by prompt only (`system.md` § Truthfulness) —
    no code detects it.

---

## 6. Settings this layer reads

`session_idle_minutes` (30), `conversation_db_path`, `transcript_base_dir`,
`agent_turn_deadline_s` (180), `llm_call_timeout_s` (60), `llm_max_retries`
(3), `llm_retry_wait_s` (5), `llm_reasoning_effort` (`"none"`),
`profile_path`, `search_queries_path`, `score_threshold`,
`tailoring_template_path`, `tailoring_output_dir`.

The ladder invariant `llm_call < agent_turn < backend_read` is asserted in
`test_config.py`, not merely assumed.

---

## 7. Deferred

- **Compaction** inside `context.py` — `_compact` is identity, so the whole
  session is re-sent every turn and grows until the idle window resets it.
- **`TaskLLMClient` has no per-call timeout.** Only the 180s turn deadline
  bounds a tailoring or drafting call, and the cover-letter guard-retry loop
  can make three ~9k-token calls. Reachable in practice.
- **A `/new` command** to end a session on demand. `start_session` already
  supports it; only `resolve_session` would need to yield.
- **`target_tracks` weighting** — flat list in v1.
- **`WP-C6`**, a Telegram typing indicator, tracked in
  `concurrencyFor_agentV2.md`. Matters more now the loop is real.
