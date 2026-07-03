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

**Ownership split (do not blur):** session *lifecycle* + tables = backend;
turn *storage* (`conversations`) = repository; conversation *assembly* =
agent; tool *work* = services.

---

## 2. File layout

```
agent/
├── __init__.py
├── loop.py        # ReAct driver: iterate, bounds, stop conditions, error routing
├── context.py     # ConversationContext: build_context() / record(); compaction seam
├── prompt.py      # system-prompt composition: static spine + per-turn injection
├── schemas.py     # the 9 function-calling schemas (LLM-facing tool definitions)
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
The description is where "resolve via `query_jobs` first" guidance lives. No
execution. Exposes `TOOL_SCHEMAS: list[dict]`.

**`handlers.py`** — one handler per tool. Parses LLM-emitted args, calls the
service, marshals the result. `kind` is hardcoded here per tool
(`tailor_resume`→`resume`, `draft_followup`→`follow_up_email`,
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
def profile_summary(profile: Profile) -> str: ...  # tiered projection (see §6)
```
Assembles: static spine (`system.md`) + `profile_summary()` + assembled turns.
`profile_summary()` may instead live in the **profile module** (preferred — it
is a projection of the source of truth); `prompt.py` then imports it.

**`loop.py`** — the ReAct driver.
```python
def run(session_id: str, user_text: str, llm: LLMClient) -> str: ...
```
Stateless per call: `record` user turn → `compose` → iterate → `record`
assistant turn → return reply text. Bounds and stop conditions per §5.

---

## 3. Tool I/O contracts

Each tool = LLM-facing **schema** (agent layer) over a caller-agnostic
**service** (shared with the scheduler). Mutating tools take an already-
resolved `job_id` — never a fuzzy hint (resolution lives in the loop).

| Tool | Schema params | Service call | Returns to LLM | Expected failures |
|---|---|---|---|---|
| `query_jobs` | `status[]?, text?, include_terminal=False, limit=10` | `query_jobs(filters)` | `[{id, role, company, status, score, posted_at, applied_at}]` (≤limit) | none (empty list is valid) |
| `search_jobs` | `query, sources[]?, limit=20` | shared scrape/ingest → `POST /jobs` | `{ok, results:[{id,role,company,score,status,is_new}], failed_sources[]}` | — |
| `update_status` | `job_id, new_status, note?` | `update_status(...)` (stamps `status_changed_at`, enforces FSM) | `{ok, job_id, role, company, old_status, new_status}` | `not_found`; `illegal_transition{from,to,allowed}` |
| `tailor_resume` | `job_id` | `tailor(job_id)` | `{ok, job_id, artifact_id}` | `guard_violation{guard}` |
| `draft_followup` | `job_id` | `draft_followup(role,company,applied_date)` | `{ok, job_id, artifact_id, text}` | — |
| `draft_cover_letter` | `job_id` | `draft_cover_letter(jd,profile)` | `{ok, job_id, artifact_id, text}` | — |
| `regenerate_queries` | *(none)* | `regenerate_queries(profile)` | `{ok, count, queries_preview}` | `profile_empty` |
| `log_job` | `role, company, url?, description?, source="manual"` | `POST /jobs` path | `{ok, id, status, is_duplicate}` | — |
| `update_profile` | `patch, confirmed=False` | `update_profile(patch)` (diff + backup) | unconfirmed: `{ok, changed:false, pending_confirmation:true, diff}`; confirmed: `{ok, changed, summary}` | `invalid_patch{detail}` |

### Cross-cutting rules

- **Resolution is centralised.** Mutating tools are id-only; the LLM resolves
  via `query_jobs` first. The entire 0/1/N ambiguity surface lives in that one
  flow, not smeared across nine tools.
- **Errors are data, mostly.** Expected failures → structured `{ok:false}` the
  LLM narrates and the loop re-enters. Unexpected failures (service threw, DB
  down) bubble up, abort the loop, surface to Telegram as a generic error.
- **Services do work; callers own FSM moves.** No service moves the FSM.
  Transition A (post-tailor SCORED→PENDING_APPROVAL) is owned by the *tailor
  caller* — the scheduler advances it; the agent's `tailor_resume` does **not**
  transition (it reports the artifact). Transition B (→APPLIED) is owned by the
  *send event* — the Telegram `[Sent it]` button or the Phase-2 worker.
- **`search_jobs` ingests, not previews.** Found jobs enter the pipeline
  (dedup → DISCOVERED → scored). Over-broad recall is absorbed by the scorer.
- **Drafts persist artifacts only.** `draft_*` never touch status or
  `follow_up_count`. Drafting ≠ sending.
- **`update_profile` is the only source-of-truth mutator** and the only
  two-phase write: first call returns the diff and writes nothing; `confirmed=
  True` commits.

### Artifact identity

Artifacts are unique on **`(job_id, kind)`** (`kind ∈ {resume,
follow_up_email, cover_letter}`). Re-producing **overwrites** the existing row;
the prior file is backed up (timestamped/`.bak`, outside the registry).
Filename is a human-readable slug derived from `(company, title, kind)` with a
short `job_id` hash only as a collision suffix. One current artifact per kind
per job; no side-by-side versions (add `version` to the key if ever needed).
Return shape gains `replaced: bool`.

---

## 4. ReAct loop mechanics

`max_iterations = 5` (deepest realistic chain is `query_jobs → act` ≈ 2;
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
- **Assembled turns** — ephemeral; v1 loads the whole current session
  (bounded by the idle-timeout flush). Compaction seam in `context.py`.

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

## 6. Reference resolution

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
| RR-1 | eval+unit | One PUB job. "got an interview with PUB" | `query_jobs("PUB")`→1→`update_status(job_42, INTERVIEWING)`. Unit: service called with right id+state; `status_changed_at` stamped. |
| RR-2 | eval | No PUB job. "got an interview with PUB" | `[]`→ no mutation; reply offers to search/log. Assert `update_status` never called; no invented id. |
| RR-3 | eval | 3 GovTech jobs. "rejected by GovTech" | 3 rows → no mutation; reply lists all three by distinguishing label, asks which. |
| RR-4 | eval+unit | Continues RR-3. "the data analyst one" | resolves to `job_60` from session context (ideally no re-query) → `update_status(job_60, REJECTED)`. Tests context carries prior result forward. |
| RR-5 | eval+unit | Prior `query_jobs` returned 5 rows in deterministic order. "tailor my CV for the third one" | "third" → row 3's id → `tailor_resume(id)`. Tests context memory **and** the deterministic tiebreak (score desc, posted_at desc, id asc). |
| RR-6a | eval | One job salient. "draft a follow-up for that one" | `draft_followup(that_id)`. |
| RR-6b | eval | 3 jobs listed earlier this session. "draft a follow-up for that one" | **lists & asks**, no mutation. Proves clarify-over-assume. |
| RR-6c | eval | Referent not in current context. "that one" | "which job do you mean?"; no invented id. |
| RR-7 | unit | Resolved `job_42` deleted/expired. `update_status(job_42, OFFER)` | service → `{ok:false, not_found}` → loop re-enters → "that job no longer exists". |
| RR-8 | unit | `job_42` is REJECTED. "got an offer from PUB" | resolves (1 match) → `update_status(job_42, OFFER)` → `{ok:false, illegal_transition, from:REJECTED, allowed[]}` → reply relays `allowed`, asks if something changed. **Reply anchored to the service verdict, not the model's own FSM reasoning.** Proves resolution and FSM enforcement are separate. |

---

## 7. Work packages & build order

Dependency order (each is independently testable):

- **WP-A1 — `errors.py`.** Frozen `{ok:false}` shapes. *DoD:* every error
  variant constructible; `illegal_transition` carries `from/to/allowed`.
- **WP-A2 — `schemas.py`.** The 9 function-calling schemas. *DoD:* schemas
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

## 8. Open / deferred

- Compaction (summarise-on-eviction) inside `context.py` — seam only in v1.
- `target_tracks` weighting (Option B) — flat list in v1.
- `POST /session/end` external dependency (shared with `telegram_v2.md`).
