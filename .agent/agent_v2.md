# Agent Layer — `agent_v2.md`

Source-of-truth spec for the reactive reasoning layer (the ReAct loop behind
`POST /chat`). Consolidates and supersedes the scattered notes in
`architecture_v2.md` §7–§9. Companion to `backend_v2.md`, `scraper_layer.md`,
`tailoring_build.md`, `scheduling_v2.md`, `telegram_v2.md`.

---

## 1. Scope & boundary

The agent layer is the reactive reasoning layer: it turns a human's free-text
turn into validated service calls and resolves which records that turn refers
to. It is woken, runs, and returns.

**Owns (only these four):**
1. The ReAct loop behind `POST /chat`.
2. Reference resolution — phrase → specific `job_id` / `skill_id`.
3. Conversation **assembly** policy (`ConversationContext`).
4. Per-tool LLM binding — function-calling schema + thin handler.

**Never:**
- Writes the DB directly — every durable change goes through a service.
- Contains the *work* behind a tool — tailoring, scrape, FSM transition,
  drafting, query regen are services it *calls*.
- Owns a loop/heartbeat — the scheduler drives the pipeline; the agent wakes
  on a call.
- Holds an authoritative fact in conversation history — history is
  resolution-only; every durable fact lives in a DB column.
- Enforces the FSM — it *proposes* `update_status`; the backend rejects
  illegal transitions.
- Decides push timing or renders for Telegram.
- Claims a monopoly on the LLM — a single completion inside a service is not
  "the agent". Scheduled stages call the LLM directly through services.

**Single runtime caller:** a free-text user turn via `POST /chat`, relayed by
Telegram. Button callbacks carry their own `job_id` and bypass the agent
straight to the backend. The scheduler never enters the loop.

**Ownership split (do not blur):** session *policy* (continue-vs-new, decided
lazily on each call) = agent; session *storage* + the `sessions` /
`conversations` tables = the conversation store (repository + transcript store);
conversation *assembly* = agent; tool *work* = services.

---

## 2. File layout

```
agent/
├── __init__.py
├── loop.py        # ReAct driver: iterate, bounds, stop conditions, error routing
├── context.py     # ConversationContext: build_context() / record(); compaction seam
├── prompt.py      # system-prompt composition: static spine + per-turn injection
├── schemas.py     # the 10 function-calling schemas (LLM-facing tool definitions)
├── handlers.py    # thin handlers: parse args → call service → marshal result
├── errors.py      # the {ok:false} result shapes (contract the LLM reasons against)
└── prompts/
    ├── system.md       # static spine (role, truthfulness rules, tool discipline)
    ├── tailoring.md    # loaded by the tailoring SERVICE, not the loop
    └── cover_letter.md # loaded by the cover-letter SERVICE, not the loop
```

The **boundary seam** is the line between `handlers.py` and everything it
imports: above = LLM binding + reasoning (this tree); below = services,
repository, backend client (not this tree). Services live one level up
(`services/…`) because the scheduler shares them.

### Per-file responsibility & key signatures

**`errors.py`** — frozen result shapes the LLM narrates. No logic.
```python
class ToolError(TypedDict):
    ok: Literal[False]
    error: str            # "not_found" | "ambiguous" | "illegal_transition"
                          # | "guard_violation" | "invalid_patch" | "profile_empty"
    # error-specific fields, e.g. illegal_transition carries: from, to, allowed[]
```

**`schemas.py`** — one schema per tool: `name`, `description`, typed params.
The description is where "resolve via `find_jobs` first" guidance lives. No
execution. Exposes `TOOL_SCHEMAS: list[dict]`.

**`handlers.py`** — one handler per tool. Parses LLM-emitted args, calls the
service, marshals the result. `kind` is hardcoded here per tool
(`tailor_resume`→`cv_pdf`, `draft_followup`→`follow_up_email`,
`draft_cover_letter`→`cover_letter`). Expected failures → return `{ok:false}`;
unexpected → raise. Exposes `dispatch(name, args) -> dict`.

**`context.py`** — assembly only.
```python
def build_context(session_id: str) -> list[Message]: ...  # v1: whole current session
def record(session_id: str, role: str, content: str) -> None: ...
```
Reads/writes turns via the repository. Compaction (trim/summarise-on-eviction)
is a **seam inside `build_context`**, no-op in v1.

**`prompt.py`** — composition.
```python
def compose(session_id: str, profile: Profile) -> list[Message]: ...
def profile_summary(profile: Profile) -> str: ...  # tiered projection (see §5)
```
Assembles: static spine (`system.md`) + `profile_summary()` + assembled turns.
`profile_summary()` may instead live in the **profile module** (preferred — it
is a projection of the source of truth); `prompt.py` then imports it.

**`loop.py`** — the ReAct driver.
```python
def run(session_id: str, user_text: str, llm: LLMClient) -> str: ...
```
Stateless per call: `record` user turn → `compose` → iterate → `record`
assistant turn → return reply text. Bounds and stop conditions per §4.

---

## 3. Tool I/O contracts

Each tool = LLM-facing **schema** (agent layer) over a caller-agnostic
**service** (shared with the scheduler). Mutating tools take an already-
resolved `job_id` — never a fuzzy hint (resolution lives in the loop).

| Tool | Schema params | Service call | Returns to LLM | Expected failures |
|---|---|---|---|---|
| `find_jobs` | `job_title?, company?, status_set[]?, limit=50` | `find_jobs(job_title, company, status_set, limit)` | `[{id, role, company, status, score, status_changed_at}]` (recency-ordered) | none (empty list is valid) |
| `search_jobs` | `query, sources[]?, limit=20` | shared scrape/ingest → `POST /jobs` | `{ok, results:[{id,role,company,score,status,is_new}], failed_sources[]}` | — |
| `score_job` | `description` | `score(description, profile)` | `{ok, score}` | `embedding_unavailable` |
| `score_ingest` | `company, title, description, url?, posted_at?` | `ingest_job(JobCreate)` → `score(...)` if new/unscored | `{ok, id, status, score, seen_count, was_scored}` | `embedding_unavailable` |
| `update_status` | `job_id, new_status, note?` | `update_status(...)` (stamps `status_changed_at`, enforces FSM) | `{ok, job_id, role, company, old_status, new_status}` | `not_found`; `illegal_transition{from,to,allowed}` |
| `tailor_resume` | `job_id` | `tailor(job_description, profile) -> ArtifactResult` | `{ok, job_id, artifact_id, kind:"cv_pdf", replaced}` (PDF bytes ride in the transport — see Agent transport) | `guard_violation{guard}` |
| `draft_followup` | `job_id` | `draft_followup(role,company,applied_date)` | `{ok, job_id, artifact_id, text}` | — |
| `draft_cover_letter` | `job_id` | `draft_cover_letter(jd,profile)` | `{ok, job_id, artifact_id, text}` | — |
| `regenerate_queries` | *(none)* | `regenerate_queries(profile)` | `{ok, count, queries_preview}` | `profile_empty` |
| `update_profile` | `patch, confirmed=False` | `update_profile(patch)` (diff + backup) | unconfirmed: `{ok, changed:false, pending_confirmation:true, diff}`; confirmed: `{ok, changed, summary}` | `invalid_patch{detail}` |

### Cross-cutting rules

- **Resolution is centralised.** Mutating tools are id-only; the LLM resolves
  via `find_jobs` first. The entire 0/1/N ambiguity surface lives in that one
  flow, not smeared across ten tools.
- **Errors are data, mostly.** Expected failures → structured `{ok:false}` the
  LLM narrates and the loop re-enters. Unexpected failures (service threw, DB
  down) bubble up, abort the loop, surface to Telegram as a generic error.
- **Services do work; the agent owns the FSM moves it triggers.** No service
  moves the FSM on its own. Transition A (post-tailor SCORED→PENDING_APPROVAL)
  is owned by the *tailor caller*: the scheduler advances it in the daily batch,
  and the agent advances it on demand once the user **accepts** the tailored
  resume — that is the second turn of the confirmation (see Two-turn
  confirmations). The `tailor_resume` tool itself only produces and registers
  the artifact; it does not transition. The agent proposes the move by calling
  `update_status`; if that returns `illegal_transition`, the agent surfaces a
  reply naming the `<job_title>` and `<job_id>` that could not be transitioned,
  **logs** the error, and does not retry blindly. Transition B (→APPLIED) is
  owned by the *send event* — the Telegram `[Sent it]` button or the Phase-2
  worker.
- **`search_jobs` ingests, not previews.** Found jobs enter the pipeline
  (dedup → DISCOVERED → scored). Over-broad recall is absorbed by the scorer.
- **Drafts persist artifacts only.** `draft_*` never touch status or
  `follow_up_count`. Drafting ≠ sending.
- **`update_profile` is the only source-of-truth mutator** and the only
  two-phase write: first call returns the diff and writes nothing; `confirmed=
  True` commits.

### Artifact identity

The database registration is **append-only** and the "one current file per
job" property is enforced on the **filesystem**, not by a database uniqueness
rule. `register_artifact(job_id, {kind, path})` inserts a new row on every call
and never overwrites (`kind ∈ {cv_pdf, follow_up_email, cover_letter}`), so the
table keeps the full history of what was produced and when.

The single-live-file property lives in the directory. Before writing a new
tailored resume, the tool reads the job row, looks in that job's artifact
directory, and checks whether a resume file already exists there. If one
exists, the tool first copies it to a non-colliding backup name that carries the
current timestamp (for example `…-2026-07-11T09-14-02Z.bak`) and only then
writes the newly tailored file in place. If no prior file exists, the tool
simply writes the new one. Filename is a human-readable slug derived from
`(company, title, kind)` with a short `job_id` hash only as a collision suffix.
The net effect is one live file per kind per job in the directory, plus
timestamped backups, plus a complete append-only registration log in the
database. Return shape gains `replaced: bool` (true when a prior file was backed
up before the write).

### Reading jobs (`find_jobs`)

`find_jobs` is the single read tool, and it does two jobs depending on whether a
title is supplied. With a `job_title` it is a **named lookup**: the title is
matched case-insensitively as a *partial* string, so "backend engineer" finds
"Senior Backend Engineer, Platform", optionally narrowed by a `company` (also
partial, combined with the title by logical AND). Without a `job_title` it is a
**status listing** that answers "what's in my pipeline right now" by returning
rows filtered only by `status_set`.

`status_set` is optional, and when it is omitted the default depends on the call
shape. A named lookup (a title and/or company was given) searches **all**
statuses including terminal ones, because you may be recalling a job that has
since been rejected or declined. A bare listing (no title and no company)
defaults to the **active pipeline** and excludes terminal statuses, because "what
am I working on" should not surface rejected jobs. In either case the agent may
pass an explicit `status_set` to override the default.

`find_jobs` matches on company and title only, never on the description, because
fuzzy matching over prose is the agent's job when it reads the returned rows, not
a database `LIKE` clause. Results come back ordered by recency
(`status_changed_at` descending, then `id` ascending) so the most recently active
matches surface first, and that fixed order is also what makes a positional
reference like "the third one" deterministic. If a named lookup returns more than
one row the agent disambiguates with the user rather than guessing. It is a pure
read: it never scores and never transitions.

### Manual entry & scoring

Two tools cover the "I found a job" cases, and which one the agent picks is
driven by whether the user wants the job **recorded** or merely **assessed**.
`score_job` is assess-only: it scores a pasted job description against the
profile and returns just the number, writing nothing to the database. Use it for
"how well does this job match me". `score_ingest` is the record-and-score path
for "score this job and put it in the database": it first calls
`ingest_job(JobCreate)`, which is an upsert (insert the job if new, otherwise
return the existing row), and the returned `seen_count` says which happened
(`1` means created just now, greater than `1` means it was already there). If the
job is new, or it already exists but carries no score yet, `score_ingest` then
calls `score(...)` and persists the result; if the existing row is already
scored, it skips the scoring call and returns the stored score, which saves the
embedding credits. There is deliberately **no** record-only tool: every job that
enters the database must be scored, so if the user asks only to record a job
without assessing it, the agent politely declines and offers to score-and-record
instead.

### Tailoring the resume

`tailor_resume` wraps the tailoring layer's single public entry,
`tailor(job_description, profile) -> ArtifactResult`, where `ArtifactResult`
carries a filesystem `path` and `kind = "cv_pdf"`. The handler passes the job's
description and the profile in, receives the `ArtifactResult`, backs up any prior
file and registers the new one per Artifact identity above, and then reads the
PDF bytes from the returned `path` so they can travel back in the reply. The
handler returns `{ok, job_id, artifact_id, kind:"cv_pdf", replaced}` to the LLM
for narration; the raw bytes do not go to the LLM but are attached to the
transport envelope described next. Producing the file does not move the FSM; the
SCORED→PENDING_APPROVAL move happens only after the user accepts, as the second
turn of a two-turn confirmation.

### Agent transport (`POST /chat`) and attachments

`POST /chat` is the thin transport around the loop. In: `{ message }`. Out:
`{ reply, attachments? }`, where `reply` is the text and `attachments` is present
only when the agent produced a file. Each attachment is a JSON object
`{ filename, mime_type, content_b64 }`, where `content_b64` is the file's raw
bytes encoded as base64, meaning the binary is rewritten as plain text so it can
sit safely inside a JSON body. The bot on the separate device decodes
`content_b64` back to bytes and forwards it to Telegram as a document. The route
is stateless per call and keeps nothing in memory between calls, which is why the
agent holds no Telegram client and why every file the agent makes must ride out
in this single response.

### Two-turn confirmations

Some actions span two user turns: `update_profile` shows a diff first and commits
only on `confirmed=True`, and tailoring shows the PDF first and transitions the
job only once the user accepts. Because the agent is stateless per call and
rebuilds context by re-reading the transcript, the pending action must be stored
somewhere the next call will see it. The agent does this by embedding a compact,
machine-exact marker inside its own assistant turn, delimited so the bot can
strip it before showing the prose to the user:

```
<<<PENDING_ACTION {"kind":"profile_update","payload":{…the exact patch…},
                   "proposed_at":"…"}>>>
```

For a profile update the payload is the exact patch; for a tailor acceptance it
is `{ "job_id": …, "artifact_id": …, "to_status": "PENDING_APPROVAL" }`. On the
next `/chat` call the agent loads history, finds the most recent unresolved
marker, and if the incoming message is an affirmation it replays that payload
verbatim — calling `update_profile` with `confirmed=True` and the stored patch,
or calling `update_status` with the stored job and target status — rather than
re-deriving the change from the English of its earlier message. If the user
instead changes course, the marker is simply abandoned, and once a session goes
idle and a new session starts the stale marker falls out of the loaded history on
its own. The marker lives inside the turn text rather than as a new field on the
`Turn` model, so this layer does not reach across into the conversation store's
schema.

---

## 4. ReAct loop mechanics

`max_iterations = 5` (deepest realistic chain is `find_jobs → act` ≈ 2;
`search → tailor top result` ≈ 3; 5 is headroom). No token budget at single-
user / flash-lite scale — the iteration cap is the proxy.

**Stop conditions, in priority order:**
1. **Final answer** — LLM emits text with no tool call → success, return reply.
2. **Max iterations** — hit cap → exit with a **best-effort** reply
   ("couldn't fully complete that — here's where I got"), not an error.
3. **No-progress** — same tool called with same args twice in a row → bail.
   (This is the drift guard.)
4. **Consecutive exceptions** — 2 unexpected exceptions → abort, generic error
   to Telegram.

Expected `{ok:false}` results **re-enter** the loop (the model disambiguates /
relays). Only unexpected exceptions abort.

---

## 5. Context composition

Three tiers, managed differently (persistent / reference / ephemeral):

- **Static spine** (`system.md`) — role, truthfulness + tool-use discipline,
  written at the "right altitude" (guide *when* to resolve-then-act; do **not**
  hardcode every utterance→tool mapping). Sectioned with markdown headers.
- **Profile summary** (`profile_summary()`) — reference tier, **compact**, not
  the full superset. Just-in-time: the full profile is loaded by the *tailoring
  service* when it runs, never carried in the loop.
- **Assembled turns** — ephemeral; v1 loads the whole current session (bounded
  by the idle check the agent applies lazily when a message arrives — there is
  no flush event). Compaction seam in `context.py`.

### Profile summary tiers (classification, not scoring)

Each profile field is tagged once at schema-design time (declare the tier on
the Pydantic schema, e.g. `Field(json_schema_extra={"tier": ...})`, so the
projection never drifts from the schema). `profile_summary()` is a mechanical
filter — no runtime judgment, no LLM:

| Tier | Fields | Projection |
|---|---|---|
| **identity** (scalar) | name, location, school, degree, graduation_date, years_of_experience, candidate_status | verbatim |
| **index** (collection) | skills, projects, experience, **`target_tracks`** | `{id, label}` per item (bodies dropped) |
| **body** | bullets, project write-ups, evidence/`demonstrated_skills` text | **dropped** — loaded just-in-time by the tailoring service |

**Excluded from the agent's context entirely** (render-only, no reasoning
value): email, phone, links. These remain CV fixed slots but never enter chat.

A char-budget backstop applies to identity scalars: a tagged scalar exceeding
the budget is treated as a body and excluded (stops an "objective" paragraph
leaking body-sized text into identity).

---

## 6. Session management

A *session* is a bounded window of turns the agent reads to build context for a
reply. It exists only so the agent can decide how far back in the transcript to
read; it holds no authoritative facts. There is **no end event**. The agent
never closes a session and holds no live session object between calls, because it
is stateless per `/chat` call. A session ends simply by not being reused: when
the next message arrives after the session has been idle past a threshold, the
agent starts a fresh session instead of continuing the old one. Policy
(continue-vs-new) is the agent's; mechanics (create, read, append) are the
store's.

### Store methods the agent calls (the contract surface)

These are the only four methods the agent depends on. Their bodies, and the full
`ConversationStore` facade that owns the repository, the transcript store, and an
injected clock, are defined in `conversation_store_v2.md`; this section fixes
only the surface the agent binds to. `Session`, `Turn`, and `Role` are imported
from the conversation store's models, not redefined here.

```python
async def start_session() -> Session
async def get_latest_session() -> Session | None
async def append_turn(session_id: str, role: Role, content: str) -> Turn
async def load_history(session_id: str) -> list[Turn]
```

`start_session` generates a session id, derives its transcript path from that id,
creates the row, and returns the `Session`. `get_latest_session` returns the most
recent session, or `None` when none exists, so the agent can make the
continue-vs-new call. `append_turn` stamps `created_at`, writes the turn to the
transcript file, and re-stamps the session's `last_activity_at`, which is the
timestamp the idle check reads. `load_history` returns every turn of a session,
oldest first, for prompt building. The store owns the clock, so the agent never
produces a timestamp itself; a test can freeze the clock to make these
deterministic.

### Agent-side policy (continue-vs-new)

The idle threshold and the idle predicate live in the **agent** and its config,
never in the store. `session_idle_minutes` is an agent config value. `is_idle`
compares the current time against a session's `last_activity_at` and reports
whether the gap exceeds that threshold.

```python
def is_idle(last_activity_at: str, idle_minutes: int) -> bool
```

The whole decision runs once per `/chat` call, lazily, when a message arrives:

```python
latest = await store.get_latest_session()
session = (
    await store.start_session()
    if latest is None or is_idle(latest.last_activity_at, session_idle_minutes)
    else latest
)

await store.append_turn(session.id, Role.USER, incoming_message)
history = await store.load_history(session.id)
prompt = build_prompt(history)          # the agent's own concern
reply = await run_loop(prompt)          # the ReAct driver, §4
await store.append_turn(session.id, Role.ASSISTANT, reply)
return reply
```

### Lifecycle consequence (why no end event is needed)

Because a stale session is never reused, it never has to be closed; it just falls
out of scope when the next message opens a new one. This is also what expires the
`PENDING_ACTION` marker from §3: once a new session starts, the old session's
turns are no longer in the loaded history, so an unresolved two-turn
confirmation from a prior, now-idle session cannot be replayed by accident.

---

## 7. Reference resolution

Converting "the thing you mentioned" → the row it refers to. Matches the
*phrase* against *job fields* (role/company/status) or *profile index*
(skill_id/label). It does **not** match profile-against-JD — that is tailoring,
a separate layer.

**Resolution predicate:** *salient* = exactly one candidate in the current,
uncompacted context. Zero or many → **clarify, never assume** (no recency-pick,
no position-pick from multiple candidates). A job compacted out of context is
no longer salient.

The 0/1/N decision (act / ask / list) is made by the **LLM**, hence the
eval/unit split in the tests below.

### Test group RR — reference resolution

`[eval]` = model judgment (non-deterministic, `-m live` set);
`[unit]` = plumbing (deterministic, mock LLM tool choice + service).

| ID | Kind | Scenario | Expected / assertion |
|---|---|---|---|
| RR-1 | eval+unit | One PUB job. "got an interview with PUB" | `find_jobs(company="PUB")`→1→`update_status(job_42, INTERVIEWING)`. Unit: service called with right id+state; `status_changed_at` stamped. |
| RR-2 | eval | No PUB job. "got an interview with PUB" | `[]`→ no mutation; reply offers to search/log. Assert `update_status` never called; no invented id. |
| RR-3 | eval | 3 GovTech jobs. "rejected by GovTech" | 3 rows → no mutation; reply lists all three by distinguishing label, asks which. |
| RR-4 | eval+unit | Continues RR-3. "the data analyst one" | resolves to `job_60` from session context (ideally no re-query) → `update_status(job_60, REJECTED)`. Tests context carries prior result forward. |
| RR-5 | eval+unit | Prior `find_jobs` returned 5 rows in deterministic order. "tailor my CV for the third one" | "third" → row 3's id → `tailor_resume(id)`. Tests context memory **and** the deterministic order (`status_changed_at` desc, then `id` asc). |
| RR-6a | eval | One job salient. "draft a follow-up for that one" | `draft_followup(that_id)`. |
| RR-6b | eval | 3 jobs listed earlier this session. "draft a follow-up for that one" | **lists & asks**, no mutation. Proves clarify-over-assume. |
| RR-6c | eval | Referent not in current context. "that one" | "which job do you mean?"; no invented id. |
| RR-7 | unit | Resolved `job_42` deleted/expired. `update_status(job_42, OFFER)` | service → `{ok:false, not_found}` → loop re-enters → "that job no longer exists". |
| RR-8 | unit | `job_42` is REJECTED. "got an offer from PUB" | resolves (1 match) → `update_status(job_42, OFFER)` → `{ok:false, illegal_transition, from:REJECTED, allowed[]}` → reply relays `allowed`, asks if something changed. **Reply anchored to the service verdict, not the model's own FSM reasoning.** Proves resolution and FSM enforcement are separate. |

---

## 8. Work packages & build order

Dependency order (each is independently testable):

- **WP-A1 — `errors.py`.** Frozen `{ok:false}` shapes. *DoD:* every error
  variant constructible; `illegal_transition` carries `from/to/allowed`.
- **WP-A2 — `schemas.py`.** The 10 function-calling schemas. *DoD:* schemas
  validate against the LLM provider's function spec; mutating tools expose
  `job_id`; `update_status.new_status` is the FSM enum.
- **WP-A3 — `context.py`.** `build_context` / `record` over the repository.
  *DoD:* round-trip a session; whole-session load; compaction seam present and
  no-op.
- **WP-A4 — `profile_summary()`** (profile module). Tiered projection. *DoD:*
  identity verbatim, index as id+label, bodies dropped, contact excluded,
  char-budget backstop. Pure function, unit-tested off a fixture profile.
- **WP-A5 — `prompt.py`.** `compose()` = spine + summary + turns. *DoD:*
  message list assembles in order; spine loaded from `system.md`.
- **WP-A6 — `handlers.py`.** `dispatch()` per tool. *DoD:* each handler calls
  the right service with marshalled args; `kind` hardcoded; expected→`{ok:false}`,
  unexpected→raise. Mocked services.
- **WP-A7 — `loop.py`.** ReAct driver + bounds. *DoD:* the 4 stop conditions
  fire correctly (incl. no-progress, best-effort on cap); errors-as-data
  re-enters; unexpected aborts. Mocked LLM.
- **WP-A8 — `POST /chat` wiring.** Stateless endpoint → `loop.run`. *DoD:*
  end-to-end with a mock LLM; turns recorded; reply returned.
- **WP-A9 — RR eval set.** RR-1…8 (`-m live` for eval rows, deterministic for
  unit rows). *DoD:* unit rows pass in CI; eval rows runnable against the live
  model.

**Definition of done (layer):** WP-A1…A9 complete; RR unit rows green in CI;
the boundary "nevers" (§1) each have a covering assertion; `agent_v2.md`
matches the implementation.

---

## 9. Open / deferred

- Compaction (summarise-on-eviction) inside `context.py` — seam only in v1.
- `target_tracks` weighting (Option B) — flat list in v1.