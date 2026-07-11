# Backend API Layer — Implementation Spec (v2)

> Source of truth for the backend layer of JobFindingAgent v2. Companion to
> `architecture_v2.md` (Component 3). Captures scope, requirements,
> specifications, and the work-package breakdown. Build/test order (step 5) is
> appended once decided. Written for independent execution.

**One-line responsibility:** Source of truth for job records and artifacts.
Every write from every component goes through this layer.

> **v1 reconciliation (decided):** Full v2 design on a fresh database — no
> migration. Existing v1 identifiers are kept to avoid churn: `ApplicationStatus`
> (not `Status`), `role` (not `title`), `InvalidTransitionError`, `transition()`.
> The state machine lives in `enums.py` (no separate `fsm.py`). Pydantic schemas
> live in `job.py`. The authoritative implementations are `enums.py` and `job.py`;
> where snippets below use the older identifiers, the code files win.

---

## 1. Scope Boundary

### Inside this layer
- FastAPI app skeleton + lifespan hook **mechanism** (the registration point — not the scheduled jobs themselves). The HTTP surface is now minimal (three thin routes, below); it is no longer the primary interface.
- Data model: the extended `jobs` table and the new `artifacts` table.
- Repository layer — the **only** place raw SQL and driver details live (the aiosqlite → asyncpg seam).
- Pydantic schemas + enums: `JobCreate`, `Job`, `ArtifactCreate`, `Artifact`, `Status`, `ArtifactKind`, `UserAction`, plus the thin route DTOs `ChatRequest`/`ChatResponse` (the latter carrying an `Attachment` list of base64 file bytes), `ActionRequest`, `FollowUpRequest` (request/response bodies for the three inbound routes).
- FSM enforcement — reject illegal transitions before any write.
- **The service layer is the real interface.** It is an in-process facade — a single object presenting one front door to the validated operations (`ingest_job`, `transition_status`, `register_artifact`, `record_follow_up`, and the read functions). "In-process" means callers run inside the same program and invoke a plain method with no network hop.
- The facade is constructed once at the composition root (`main.py`) and injected via dependency injection into its callers: the scheduler, the agent's tools, and the tailoring consumer. There is no self-HTTP; in-process callers never call the backend over the network.
- Exactly **three thin inbound HTTP routes** survive, all for the separate Telegram bot client (a separate device): `POST /chat` (free-form message → agent), `POST /jobs/{job_id}/action` (a button tap → `transition_status`, deterministic), and `POST /jobs/{job_id}/follow-up` (a button tap → `record_follow_up`, deterministic, record-only). Every other operation the old CRUD endpoints exposed is now reached in-process through the facade.



### Resolved boundary decisions
- **Fingerprint — external.** Belongs to the dedup layer; the method is unsettled (company + title collisions for large employers). The backend imports it behind a stable signature `fingerprint(job: JobCreate) -> str` and treats it as an injected dependency, so revising the algorithm changes **zero** backend code.

- **FSM — defined in the backend, shared read-only with the agent.** Rule-based. The agent may *read* legality (`can_transition`) to avoid proposing doomed moves, but enforcement is unconditional at the write path. The agent proposes; the backend disposes.


- **Two inbound routes, split by ambiguity — not one.** `POST /chat` is thin transport to the agent: the route is backend, the body is the agent, and the agent is the single interpreter of ambiguous user intent. `POST /jobs/{job_id}/action` is the deterministic path for a button tap, which is already unambiguous and so bypasses the agent entirely, calling the service facade directly. This sharpens the invariant rather than muddying it: `/chat` becomes the **only** place an LLM call can originate; `/action` is purely deterministic. Neither handler branches on intent — each has exactly one destination.

- **Outbound push stays off the HTTP surface.** Notifications (job cards, lifecycle nudges) go outbound from the scheduler directly through the notifications bot via the Telegram API. Only the *button reply* those cards generate comes back to the backend, and it arrives through `/jobs/{job_id}/action`. See §3e for the two-bot messaging model.

---

## 2. Requirements

### Functional
1. **Ingest a job idempotently.** Persist a new job; re-ingesting the same job (same fingerprint) does not duplicate — it stamps liveness (`seen_count`, `last_seen_at`) on the existing row. Uniqueness is the backend's responsibility.
2. **Persist the score it is handed.** Store the integer score; never compute or recompute it.
3. **Transition status under FSM rule.** Move a record only if the transition is legal; reject illegal ones before any write; stamp `status_changed_at` on every legal transition.
4. **Record follow-up activity without a state change.** Increment `follow_up_count`, stamp `last_follow_up_at`, leave `status` and `status_changed_at` untouched.
5. **Register an artifact against a job.**
6. **Serve the consumers' queries:** status-set filtering (active vs. terminal), age-based selection on `status_changed_at`, ranked-and-limited selection — `top_scored_for_tailoring`, returning `SCORED` jobs by score, then recency, then id — and `find_jobs`, a partial-match lookup on `company`/`title` (optional `status_set`) backing the agent's natural-language job resolution.


7. **Provide exactly three thin inbound routes** — `/chat` (delegates to the agent; the only LLM origin), `/jobs/{job_id}/action` (maps a `UserAction` to an FSM transition; deterministic, no agent), and `/jobs/{job_id}/follow-up` (calls `record_follow_up`; deterministic, record-only, no status change). All other operations are in-process facade calls, not endpoints.

### Non-functional (invariants honored)
1. Single write path; no consumer writes to the DB directly.
2. The backend validates and executes; it never reasons. No LLM call originates here. Every operation is deterministic and unit-testable.
3. Repository isolation — switching aiosqlite → asyncpg touches only `repository.py` / `database.py`.
4. The clock-separation invariant is enforced **in the write operations themselves** — a follow-up write is structurally incapable of moving `status_changed_at`, and the nudge write (`mark_follow_up_nudged`) touches only `follow_up_nudge_at`, so no lifecycle write can contaminate another's clock.
5. No illegal histories — even an agent proposing `REJECTED → OFFER` cannot produce that row.
6. The fingerprint is imported behind a stable signature; revising it changes no backend code.
7. Every external dependency (fingerprint, DB) is injected and mockable.
8. No authentication/authorization until 1.0.0 (single-user) — explicitly out of scope, not forgotten.

---

## 3. Specifications

### 3a. Data Model

**Governing principle:** thin typed core + a `metadata` JSON bag. A field gets a
typed column only if the backend *queries, sorts, or enforces on* it. Everything
portal-specific (salary, `skills[]`, `categories`, `positionLevels`, UEN,
district, `objectID`, expiry, `jobSource`) goes into `metadata`. Adding JobStreet
later adds no columns.

> v1 base columns are reconstructed from references; reconcile against the actual
> v1 schema. The v2 deltas (six lifecycle columns + `artifacts` table) are the
> only explicit additions.

#### `jobs` table

**Identity & dedup**
- `id` — INTEGER, PK.
- `fingerprint` — TEXT, NOT NULL, **UNIQUE**. The UNIQUE constraint *is* dedup enforcement at the DB level. Value supplied by the external fingerprint function.

**Content (normalized `JobCreate` core)**
- `company` — TEXT, NOT NULL.
- `title` — TEXT, NOT NULL.
- `description` — TEXT, NOT NULL (full JD; may be HTML).
- `url` — TEXT, NOT NULL (canonical listing URL). Stored, but **not** part of the fingerprint.
- `posted_at` — TEXT (ISO), nullable. Used as a ranking tie-break.
- `metadata` — TEXT (JSON). Portal-specific bag + source-native identity for traceability.

**Pipeline state**
- `status` — TEXT, NOT NULL, default `'DISCOVERED'`. Validated by a Python enum; **no DB `CHECK`** — adding a new state costs zero migration.
- `score` — INTEGER, **nullable**. NULL while `DISCOVERED`; 0–10000 once scored. Stored once, never recomputed.

**Lifecycle clocks & counters (v2 additions)**
- `status_changed_at` — TEXT (ISO, UTC), NOT NULL. *The* clock for follow-up/ghost/expiry. Set = `created_at` at insert; re-stamped on every legal transition.
- `follow_up_count` — INTEGER, NOT NULL, default 0.
- `last_follow_up_at` — TEXT (ISO, UTC), nullable. When the **user** followed up. Tracked separately so it can never touch `status_changed_at`.
- `follow_up_nudge_at` — TEXT (ISO, UTC), nullable. When the **scheduler pushed a follow-up reminder card**. Written only by the scheduler's push (via `mark_follow_up_nudged`), never by `record_follow_up`. Its sole purpose is to make a job qualify for a nudge exactly once: the detection read requires it to be NULL. Deliberately distinct from `last_follow_up_at` — "we reminded you" is not "you did it."
- `seen_count` — INTEGER, NOT NULL, default 1.
- `last_seen_at` — TEXT (ISO, UTC), NOT NULL. = `created_at` at insert; re-stamped on every duplicate ingest.

**Bookkeeping**
- `created_at` — TEXT (ISO, UTC), NOT NULL.
- `updated_at` — TEXT (ISO, UTC), NOT NULL (generic last-touch).

> **Five timestamps, five meanings:** `updated_at` (any change), `status_changed_at`
> (status only), `last_seen_at` (ingest sighting), `last_follow_up_at` (the user
> followed up), `follow_up_nudge_at` (the system pushed a reminder). Conflating any
> two is the bug the design warns about — especially the last two, which are "you
> did it" versus "we reminded you." They are separate columns precisely so each
> write touches one without the others.

#### `artifacts` table
- `id` — INTEGER, PK.
- `job_id` — INTEGER, NOT NULL, FK → `jobs(id)`, `ON DELETE RESTRICT`.
- `kind` — TEXT, NOT NULL: `cv_pdf` | `cover_letter` | `follow_up_email`. Python-enum validated.
- `path` — TEXT, NOT NULL (filesystem path; bytes live on disk, not in the row).
- `created_at` — TEXT (ISO, UTC), NOT NULL.

#### Conventions
- **Timestamps: TEXT ISO-8601, always UTC.** Lexicographic order = chronological order (age queries become string comparisons); UTC removes timezone ambiguity (SG is UTC+8 — mixing local and UTC would silently skew every ghost/expiry clock by 8 hours).
- **`status` and `kind` are plain TEXT validated by Python enums, no DB `CHECK`** — new values need no migration.
- **`score` nullable until scored.**
- **Artifacts: keep-all (history).** Re-tailoring inserts a new row; "the current CV" is `ORDER BY created_at DESC LIMIT 1` for that `kind`. Matches the project's audit-trail philosophy.
- **FK `RESTRICT`** is effectively moot: the design never hard-deletes a job (terminal states + filtering, not deletion). SQLite requires `PRAGMA foreign_keys = ON` per connection for the FK to be enforced at all.
- **Indexes:** the UNIQUE index on `fingerprint` is mandatory (it is the dedup mechanism). Composite indexes `(status, score)` and `(status, status_changed_at)` are deferred until row volume justifies them.

### 3b. State Machine

**States**
- *Active (pre-application):* `DISCOVERED` → `SCORED` → `TAILORED` → `PENDING_APPROVAL` → `APPLYING`\* → `APPLIED` (\* + `APPLY_FAILED`, both Phase 2)
- *Active (post-application):* `INTERVIEWING`, `OFFER`
- *Terminal:* `REJECTED`, `USER_SKIPPED`, `EXPIRED`, `ACCEPTED`, `DECLINED`
- *Semi-terminal:* `GHOSTED` (one resurrection edge)
- *Entry state:* `DISCOVERED` — set at insert, never transitioned *into*. The validator handles "new record" as a separate path from "transition."

**Transition table** (`auto` = deterministic system rule, no LLM; `user` = button/chat; `worker` = Phase 2 apply worker; `config` = `auto_apply`)

| From | To | Trigger |
|---|---|---|
| DISCOVERED | SCORED | auto: score ≥ threshold |
| DISCOVERED | REJECTED | auto: score < threshold |
| SCORED | TAILORED | auto: tailor batch |
| SCORED | REJECTED | auto: staleness (`stale_after_days`) |
| TAILORED | PENDING_APPROVAL | auto |
| TAILORED | APPLYING | config: `auto_apply` (P2) |
| PENDING_APPROVAL | APPLIED | user: Mark Applied |
| PENDING_APPROVAL | USER_SKIPPED | user: Skip |
| PENDING_APPROVAL | EXPIRED | auto: `pending_expiry_days` |
| PENDING_APPROVAL | APPLYING | user: Apply for me (P2) |
| APPLYING | APPLIED | worker ok (P2) |
| APPLYING | APPLY_FAILED | worker fail (P2) |
| APPLY_FAILED | APPLIED | user: applied by hand (P2) |
| APPLY_FAILED | USER_SKIPPED | user: gave up (P2) |
| APPLIED | INTERVIEWING | user |
| APPLIED | REJECTED | user |
| APPLIED | GHOSTED | auto: `ghost_after_days` |
| APPLIED | DECLINED | user: candidate withdraws |
| INTERVIEWING | INTERVIEWING | user: next round (re-stamps clock) |
| INTERVIEWING | OFFER | user |
| INTERVIEWING | REJECTED | user |
| INTERVIEWING | GHOSTED | auto: `ghost_after_days` |
| INTERVIEWING | DECLINED | user: candidate withdraws |
| OFFER | ACCEPTED | user |
| OFFER | DECLINED | user: candidate declines |
| OFFER | REJECTED | user: offer rescinded (rare) |
| GHOSTED | INTERVIEWING | user: resurrection |

Terminal states have no outgoing edges.

**`REJECTED` vs `DECLINED`:** `REJECTED` = the company said no (at any stage);
`DECLINED` = the candidate said no — either withdrawing from the process
(`APPLIED` / `INTERVIEWING`) or declining an offer. It is the post-application
mirror of `USER_SKIPPED` (the candidate's pre-application no). Kept distinct on
purpose.

**Representation & enforcement**
- One transition map in the backend is the single source of truth.
- `validate_transition(from, to)` lives in the **write path** — the unconditional enforcer; every status change (button, chat tool, scheduler, worker) passes through it. Illegal → rejected before any write. This is what makes illegal histories physically impossible.
- `can_transition(from, to)` is a read-only courtesy for the agent; it never gates anything.
- **Self-loop `INTERVIEWING → INTERVIEWING` is legal** and re-stamps `status_changed_at` (resets the ghost clock — a new round means the company is active). A naive "reject if from == to" guard would wrongly break this.
- **`auto_apply` is decided at the call site, not in the FSM.** Both `TAILORED → PENDING_APPROVAL` and `TAILORED → APPLYING` are legal; config picks which fires.
- **The FSM is structural only** — it does not encode *who* may trigger a move. Actor-authorization is deliberately omitted for now (single-user; each transition has one natural caller in practice).

### 3c. Upsert Contract 

The `ingest_job` facade method owns this contract. It is called in-process by the
scraper and has no HTTP route of its own. It first validates the incoming
`JobCreate` (invalid input raises a Pydantic `ValidationError` and writes nothing),
then computes the fingerprint via the injected fingerprint function, then takes one
of two paths.

**Fresh hit (no row with this fingerprint) → INSERT:**
- From `JobCreate`: `company`, `title`, `description`, `url`, `posted_at?`, `metadata?`.
- `fingerprint` ← computed.
- `status = DISCOVERED`, `score = NULL`.
- `seen_count = 1`, `follow_up_count = 0`, `last_follow_up_at = NULL`, `follow_up_nudge_at = NULL`.
- `created_at = updated_at = status_changed_at = last_seen_at = now (UTC)`.

**Duplicate hit (fingerprint exists) → update liveness only:**
- `seen_count = seen_count + 1`
- `last_seen_at = now`
- `updated_at = now`
- **Frozen:** `status`, `score`, `status_changed_at`, all content, `follow_up_count`, `created_at`.

> **`status_changed_at` must NOT move on a re-sighting.** If it did, a listing that
> keeps reappearing in daily scrapes would reset its ghost clock every day and
> never ghost. `last_seen_at` says "still live"; `status_changed_at` says "how long
> in this state." Separate columns, separate purposes. Same bug-class as the
> follow-up/ghost separation.

**Atomic implementation** (the statement lives in `repository.py`, the only place SQL sits) — one statement, no read-then-write race (also the seam where the future single-writer serialization lands):

```sql
INSERT INTO jobs (...) VALUES (...)
ON CONFLICT(fingerprint) DO UPDATE SET
    seen_count = seen_count + 1,
    last_seen_at = excluded.last_seen_at,
    updated_at  = excluded.updated_at;
```

**Idempotency claim (state exactly):** idempotent with respect to content and
status — repeated calls never alter `status`, `score`, content, or
`status_changed_at`. Intentionally **non-idempotent** on `seen_count` /
`last_seen_at`, which advance every call by design (the liveness signal).

**Boundary reaffirm:** the upsert never scores. It always lands on `DISCOVERED`
with `score = NULL`. Scoring is the next step, run by the scrape job, which reads
`DISCOVERED` rows and transitions them in-process via `transition_status`.

### 3d. Operations (service facade) & Inbound Routes

The operations below are **service-facade methods**, not HTTP endpoints. In-process
callers (scheduler, agent tools, tailoring consumer) invoke them directly through
the injected facade. Only two of them are additionally reachable over HTTP, and
only because the Telegram bot is a separate client on another device (`/chat` and
`/jobs/{job_id}/action`, at the end of this section). Everything else is
in-process only.

**Conventions (facade + routes):** methods return the full `Job` or `Artifact`
object, or raise. When surfaced over the two HTTP routes, the mapping is `200`
success, `422` invalid body / out-of-vocabulary action, `404` job not found, `409`
conflict (illegal transition, or operation invalid for current state). The same
error classes are raised by the facade for in-process callers; HTTP status codes
are just how the routes render them.

**`ingest_job(JobCreate) -> Job` — ingest (upsert)**
- In: `JobCreate { company, title, description, url, posted_at?, metadata? }`. Caller never supplies `status`, `score`, `fingerprint`, timestamps, or counters.
- Out: `Job`. (No `was_created` flag — the returned row's `seen_count` already encodes it: `1` = created this call, `>1` = duplicate hit.)
- Does: the 3c upsert. Always `DISCOVERED`. Never scores. Called in-process by the scraper.

**`transition_status(job_id, to_status: Status) -> Job` — transition (the FSM enforcement point)**
- In: `job_id`, `to_status`.
- Out: updated `Job`.
- Does: load job → `validate_transition(current, to_status)` → if legal, set status, stamp `status_changed_at = now`, `updated_at = now`; illegal → raises `InvalidTransitionError` (`409` at a route). Self-loop `INTERVIEWING→INTERVIEWING` allowed (re-stamps clock). **Every** status change converges here — the auto transitions the scheduler drives, the tailoring transitions, and the user button taps arriving via `/action`.

**`query_jobs(status_set, limit, offset) -> list[Job]` — general read**
- In: `status` (set; **default = active pipeline**, terminal only on explicit request), `limit`, `offset`.
- Out: `list[Job]`.
- Does: thin wrapper over the general repository read function. Backs the agent's `query_jobs` tool in-process. No HTTP endpoint — the agent calls the tool, the tool calls this method, all in-process. (See Read surfaces, Decision C.)

**`find_jobs(job_title, company=None, status_set=None, limit=50) -> list[Job]` — named lookup for natural-language resolution**
- In: `job_title` (**required — the primary key**; a case-insensitive *partial* match, so "backend engineer" finds "Senior Backend Engineer, Platform"). `company` (optional; when given, a case-insensitive partial match **AND**-combined with the title). `status_set` (optional; when given, restricts to those statuses; when **omitted, searches all statuses including terminal**, because a lookup may recall a job that has since been `REJECTED` or `DECLINED`). `limit` (a safety bound on rows returned, default 50 — not a semantic filter, just a guard against an unbounded list; parallels `query_jobs`).
- Out: `list[Job]`, ordered by recency (`status_changed_at` descending, then `id`) so the most recently active matches surface first.
- Does: thin wrapper over the **one** named repository lookup read whose `WHERE` owns the partial-match clauses. Matches on `company` and `title` **only, never `description`** — fuzzy matching on prose belongs to the agent reading rows, not a SQL `LIKE`. Backs the agent's job-lookup tool: the agent resolves a user's natural-language reference ("did GovTech get back to me?") by calling this to retrieve candidates, then matches precisely in-model and, if more than one row returns, disambiguates with the user. Pure read — never scores, never transitions. No HTTP endpoint; the agent calls the tool in-process. This is the named lookup counterpart to the coarse `query_jobs`, kept separate so the general status read never grows free-text search arguments (see Read surfaces, Decision C).

**`top_scored_for_tailoring(limit) -> list[Job]` — ranked read for tailoring**
- In: `limit` (the daily tailoring batch size; the value itself belongs to the tailoring consumer's config, not here).
- Out: `list[Job]` — only `SCORED` rows, ordered by `score` **descending**, then recency, then `id`. This exact tiebreak chain is the priority order the tailoring consumer processes in.
- Does: thin wrapper over the **one** named repository ranking read whose `ORDER BY` owns that deterministic ordering — a single named place, so the priority rule is never re-expressed anywhere else. Called in-process by the tailoring consumer. No HTTP endpoint. This is the tailoring entry of the specific, rule-bound named reads (see Read surfaces, Decision C). Pure read — it never scores and never transitions; the consumer reads this list, tailors each job, then calls `register_artifact` and `transition_status` per job.

**`register_artifact(job_id, ArtifactCreate) -> Artifact` — register artifact**
- In: `ArtifactCreate { kind, path }`.
- Out: `Artifact`.
- Does: 404 if no job; else always *insert* a new row (keep-all), `created_at = now`. **Does not change status** (Decision A). Called in-process by the tailoring consumer.

**`record_follow_up(job_id, note?) -> Job` — record follow-up (two-clocks made physical)**
- In: `job_id`, optional `note`.
- Out: updated `Job`.
- Does: increment `follow_up_count`, stamp `last_follow_up_at = now`, `updated_at = now`. **Cannot** touch `status` or `status_changed_at`. Requires `status == APPLIED`, else `409` (Decision B).

**`mark_follow_up_nudged(job_id) -> Job` — record that a reminder was pushed (scheduler-only)**
- In: `job_id`.
- Out: updated `Job`.
- Does: stamp `follow_up_nudge_at = now`, `updated_at = now`, and nothing else. **Cannot** touch `status`, `status_changed_at`, `follow_up_count`, or `last_follow_up_at`. Called in-process by the scheduler immediately after it successfully pushes a follow-up card, so the job drops out of the follow-up detection read and is never re-drafted. This is the nudge clock's only writer; it is deliberately separate from `record_follow_up` (system reminded vs. user acted). No HTTP route — the scheduler is in-process.

---

**Inbound HTTP routes (exactly three — the Telegram bot client's only doors in):**

**`POST /chat` — agent transport (thin)**
- In: `{ message }`.
- Out: `{ reply, attachments? }` — text plus, when the agent produced a file, its bytes (see the contract for the encoding).
- Does: hand to the agent, return its reply. Backend owns the route; the agent owns the body. Stateless per call — each call keeps nothing in memory from the previous one, which is why the agent rebuilds context from the conversation store (see §3f). The **only** place an LLM call originates. Because the agent never speaks unprompted (every utterance is a reply to an open `/chat` call), this response carries **everything** the agent produces — so the agent holds no Telegram client, and the bot, not the backend, forwards any attachment on to Telegram.

**`POST /jobs/{job_id}/action` — button tap (deterministic; mirror of `/chat`)**
- In: `{ action: UserAction }`.
- Out: updated `Job`.
- Does: the thin handler calls `transition_status` after mapping the action to a target status. It is deterministic — no agent, no LLM. An out-of-vocabulary action fails at parsing (`422`); an illegal or out-of-state tap fails FSM validation (`409`). This is the structured counterpart to `/chat`: a button press is already unambiguous, so it bypasses the single interpreter.

**`UserAction` — the button vocabulary (lives in `enums.py`).** A **restricted**
enum whose member string values match `Status` names exactly, but containing only
the states a user button may legitimately target. This is deliberately narrower
than `Status`: a tap can never request a system-only state such as `SCORED` or
`TAILORED` (those are automatic transitions the scheduler drives, never a button),
because those names are simply not in the enum, so they fail at `422` before any
lookup. The enum names a *target state*, not an *edge* — the button says "this job
is now `INTERVIEWING`," and the backend reads current status and validates the
`(current → target)` pair against the FSM. One target can cover several legal
source edges (`INTERVIEWING` covers advance-from-`APPLIED`, the next-round
self-loop, and resurrection-from-`GHOSTED`), all resolved server-side. The button
stays dumb; the FSM stays entirely in the backend.

Phase 1 vocabulary (from the `user`-triggered rows of the 3b transition table):
`APPLIED`, `USER_SKIPPED`, `INTERVIEWING`, `OFFER`, `ACCEPTED`, `DECLINED`,
`REJECTED`. Phase 2 adds `APPLYING` and the `APPLY_FAILED`-sourced actions
(deferred; the enum has a documented place to grow).

**`POST /jobs/{job_id}/follow-up` — record a follow-up (deterministic; record-only)**
- In: `{ note? }` (optional).
- Out: updated `Job`.
- Does: the thin handler calls `record_follow_up` and nothing else — increment `follow_up_count`, stamp `last_follow_up_at = now`. No agent, no language model. Requires `status == APPLIED`, else `409` (Decision B). This is a **separate route from `/action` on purpose**: recording a follow-up is deliberately *not* a status change and must never touch `status` or `status_changed_at` (it does not reset the ghost clock), so folding it into `/action` would force a branch inside a handler whose whole value is a single clean mapping. The user reaches this by tapping "Mark as followed up" on a follow-up card, after sending the email themselves from their own mail client.

> **Out-of-scope seam — the follow-up drafting service.** The email itself is
> written by a shared `FollowUpDraftingService`, a sibling of the tailoring service:
> constructed once at the composition root, injected into the scheduler, owning the
> LiteLLM call. It is **not** a method on `JobService` and is **not** specified in
> this document — the backend never reasons. The scheduler drafts (via this service)
> *before* it pushes the card, so the pushed card already carries the finished
> draft; this route only records that the user acted on it. The drafting service
> gets its own spec, as tailoring did.

**Route contracts (formal).** The prose above gives intent; this pins the wire
contract. Shared conventions apply to all three routes:

- **Method & encoding:** HTTP `POST`, JSON request body, JSON response body, `Content-Type: application/json` both ways.
- **Auth:** none in Phase 1 (out of scope until 1.0.0), so these routes must stay bound to a local/trusted network reachable only by the bot client — there is no other guard in front of the write path.
- **`job_id` path parameter:** integer, matches `jobs.id`; a non-integer value fails path validation with `422`.
- **`Job` response body:** the full `jobs` row exactly as defined in 3a — every typed column, all timestamps ISO-8601 UTC. Not a projection.
- **Error body:** `{ "detail": <string> }` (FastAPI default). The exception-to-status mapping is centralized in the app's exception handlers (WP-E), so route handlers stay thin and never build these by hand.

**Contract 1 — `POST /chat`**
- Path params: none.
- Request: `ChatRequest { message: string (required, non-empty) }`. Context is rebuilt by the agent from the conversation store, so the client sends only the message; there is no session field to pass.
- Success: `200` → `ChatResponse { reply: string, attachments: Attachment[] = [] }`, where `Attachment { kind: ArtifactKind, filename: string, mime_type: string, content: string }` and `content` is the file's raw bytes **base64-encoded** (base64 = binary written as text so it survives inside JSON, which cannot hold raw bytes). `attachments` is empty on a text-only reply.
- **Why bytes, not a path:** the bot runs on a separate machine and cannot resolve a backend filesystem path, so the response must carry the bytes themselves. The bot decodes each attachment and streams it to Telegram; the backend still never talks to Telegram. Inline base64 is chosen because the artifacts are small PDFs, where the ~⅓ size inflation is negligible and one request/one response is simplest. (A reference-plus-download-route alternative is recorded in §7 for when artifacts outgrow inline encoding.)
- Errors: `422` missing/empty `message`; `500` if the agent raises (the route is thin transport and does not interpret agent failures).
- Calls: the agent. This is the only route that may originate an LLM call.

**Contract 2 — `POST /jobs/{job_id}/action`**
- Path params: `job_id: integer`.
- Request: `ActionRequest { action: UserAction (required) }`. `UserAction` is the restricted enum; any value outside it is rejected at parsing.
- Success: `200` → `Job` (the updated row, post-transition).
- Errors: `422` body invalid or `action` not a `UserAction` member; `404` no job with `job_id` (`JobNotFoundError`); `409` the mapped transition is illegal from the job's current state (`InvalidTransitionError`).
- Calls: `transition_status(job_id, target)` after mapping `action` → target `Status`.

**Contract 3 — `POST /jobs/{job_id}/follow-up`**
- Path params: `job_id: integer`.
- Request: `FollowUpRequest { note: string | null = null }`. The body may be empty (`{}`).
- Success: `200` → `Job` (the updated row: `follow_up_count` incremented, `last_follow_up_at` stamped, `status_changed_at` unchanged).
- Errors: `422` `note` present but not a string; `404` no job with `job_id` (`JobNotFoundError`); `409` job is not `APPLIED` (`InvalidStateError`, Decision B).
- Calls: `record_follow_up(job_id, note)`.

**Exception → status mapping (centralized, WP-E).** One table drives every route's
error rendering:

| Condition | Raised by | HTTP | Body `detail` |
|---|---|---|---|
| Malformed body / out-of-vocabulary `action` / bad path type | Pydantic (pre-handler) | `422` | validation message |
| No job with that `job_id` | `JobNotFoundError` | `404` | "job not found" |
| Illegal/out-of-state status transition | `InvalidTransitionError` | `409` | transition message |
| Follow-up on a non-`APPLIED` job | `InvalidStateError` | `409` | precondition message |
| Agent failure or any unhandled error | (uncaught) | `500` | generic |

`422` needs no handler (FastAPI raises it during request parsing, before the route
body runs); the `404` and the two `409`s are the handlers WP-E must register, since
without them those facade exceptions would escape as `500`.

**Decision A — artifact registration is decoupled from the status move.**
`register_artifact` only registers. The tailoring consumer then calls
`transition_status` separately (`SCORED → TAILORED → PENDING_APPROVAL`, or
`→ APPLYING` under `auto_apply`). One method, one responsibility — and it keeps
on-demand re-tailoring safe: regenerating a CV for an already-`APPLIED` job
registers a fresh artifact without attempting an illegal transition. Tailoring
sequence: `top_scored_for_tailoring(limit)` → LLM selects (validated JSON) → deterministic
render to PDF on disk → `register_artifact` → `transition_status` → notifications-bot push.

**Decision B — `record_follow_up` requires `status == APPLIED`.** A follow-up only
has meaning while awaiting a response after applying. The server-side precondition
guards against a race (job rejected/ghosted between the daily check and the
action) and keeps the operation consistent with "the backend validates."

### Read surfaces (Decision C)

Every DB read is a repository function (the only place SQL lives). Three shapes:
- **General-purpose:** "list jobs by status, paginated." Backs the agent's `query_jobs` tool, called in-process through the `query_jobs` facade method. There is no public HTTP read endpoint — the old `GET /jobs` is removed along with the rest of the CRUD surface.
- **Named lookup:** `find_jobs` (partial match on `company`/`title`, optional `status_set`, defaulting to all statuses). Backs the agent's job-lookup tool, so a user's natural-language reference resolves to a `job_id`: the agent retrieves candidates by this read, then matches precisely in-model. It matches identifiers (`company`, `title`) only, never `description` — prose matching stays in the model, not SQL. It is a *named* read on purpose, kept off `query_jobs` so the general status read never accretes free-text search arguments.
- **Specific, rule-bound, named functions:** `top_scored_for_tailoring` (top-N `SCORED`, ordered by score → recency → id, surfaced on the facade for the tailoring consumer); `PENDING_APPROVAL` older than expiry; `APPLIED`/`INTERVIEWING` older than ghost window; `APPLIED` older than the follow-up window with `follow_up_count = 0` **and `follow_up_nudge_at IS NULL`** (the null-check makes a job qualify for a reminder exactly once, so the scheduler never re-drafts and re-pushes the same nudge every cycle). Called in-process by the scheduled jobs and the tailoring consumer; each has a precise typed signature and is unit-tested directly.

Decision: do **not** build one over-configurable query for all of these. General stays
general; the lookup and each pipeline-critical query are their own named functions. The LLM never
calls HTTP — it calls a tool that calls the function in-process.

### 3e. Messaging Model (two bots)

Telegram interaction is split across **two separate bots**, each with its own
token (the secret string that authenticates a program as a particular bot). The
split is by **interaction mode, not by direction** — each bot handles both
directions of its own mode.

- **Chat bot — free-form conversation.** Inbound user messages go to `POST /chat`,
  which forwards them to the agent; the agent's reply is relayed back to the user.
  This bot holds no notification logic. Pure transport and routing.
- **Notifications bot — the job-card lifecycle.** The scheduler pushes job cards
  (approve/skip prompts) and lifecycle nudges (pending-approval expiries, ghost
  warnings) **outbound** through this bot via the Telegram API. Each card carries
  inline buttons; when the user taps one, the resulting button reply comes **back**
  to the backend through `POST /jobs/{job_id}/action`.

Two consequences worth stating plainly, because they are what this split buys:
- **The outbound push is the one thing that does not go through a backend HTTP
  route.** The scheduler is fire-and-forget: it sends the card and is done. Only
  the button *reply* returns, and it returns via `/action`. The scheduler never
  waits for or hears about that reply.
- **No push-coexistence machinery exists.** Because notifications and conversation
  live on two separate streams, a push can never interrupt a conversation. The
  "is the user mid-conversation, hold or deliver?" gate, any `last_activity_at`
  signal read by the scheduler, and any held-push queue are **deleted, not
  deferred**. The scheduler and the conversation store have zero contact.

> **Open (transport detail, does not block this spec):** whether the bot client
> polls Telegram for updates and forwards button taps to `/action`, or the
> notifications bot uses a webhook that delivers taps straight to the backend. The
> polling-and-forward shape keeps the bot as the single Telegram-facing client,
> which fits "the bot is a separate client" more cleanly. Settle in the Telegram
> layer spec.

### 3f. Conversation Store (second database — reference)

The agent's multi-turn memory lives in a **second SQLite database file**, separate
from the jobs database, with its own repository, in a dedicated `conversation/`
package. It is injected into the **agent alone**; the backend's jobs service and
the scheduler hold no reference to it. It exists because `/chat` is stateless per
call, so the persisted transcript is what carries context from one message to the
next.

This is **not specified here.** Its data model (a `sessions` table plus per-session
JSON Lines transcript files), file responsibilities, and method signatures are the
source-of-truth of **`conversation_store_v2.md`**. The only facts this backend spec
records are the two above: it is a second database file distinct from the jobs
database, and it is not a caller of, nor called by, the jobs service.

---

## 4. Components, Files & Work Packages

> v1 file layout is inferred; reconcile against the repo. "(extend)" = likely
> exists in v1; "(new)" = introduced in v2. Only `app/models/enums.py` is a
> confirmed path (from the `from app.models.enums import ...` import); the
> subfolder names below (`db/`, `services/`, `routers/`) are a proposed layout —
> match them to your actual repo.

### Folder & file layout
```
app/
├── __init__.py
├── main.py                  # app creation, router includes, lifespan DB-init   (WP-F)
├── config.py                # DB path + pragmas
├── models/
│   ├── __init__.py
│   ├── enums.py             # ApplicationStatus, ArtifactKind, UserAction, FSM   (WP-B)
│   └── job.py               # JobCreate, StatusUpdate, JobResponse, Artifact*    (WP-A)
├── db/
│   ├── __init__.py
│   ├── database.py          # connection, pragmas, DDL, schema init              (WP-A)
│   └── repository.py        # all SQL: upsert, status write, reads               (WP-C)
├── services/
│   ├── __init__.py
│   └── service.py           # validated ops: ingest_job, transition_status, …    (WP-D)
└── routers/
    ├── __init__.py
    └── routes.py            # 3 thin routes: /chat, /action, /follow-up          (WP-E)

tests/
├── __init__.py
├── conftest.py              # fixtures: temp DB, client, mock_repository, mock_fingerprint
├── test_enums.py            # WP-B  (FSM — done, 33 passing)
├── test_job.py              # WP-A  model validation
├── test_repository.py       # WP-C
├── test_service.py          # WP-D
├── test_routes.py           # WP-E
└── test_main.py             # WP-F  startup
```

### File set
- `enums.py` (extend) — the root of the dependency graph. Holds `ApplicationStatus`, `ArtifactKind`, `UserAction` (the restricted button-vocabulary enum whose values match a subset of `ApplicationStatus` names), **and** the state machine (`VALID_TRANSITIONS`, `transition`, `can_transition`, `legal_targets`, `InvalidTransitionError`). Both `job.py` and the service layer import it.
- `job.py` (extend) — Pydantic I/O models: `JobCreate`, `StatusUpdate`, `JobResponse`, `ArtifactCreate`, `ArtifactResponse` (enums imported from `enums.py`).
- `database.py` (extend) — connection handling, startup pragmas (`foreign_keys = ON`; WAL noted for 0.7.0), DDL for the extended `jobs` table, the `artifacts` table, and the unique `fingerprint` index.
- `repository.py` (extend) — all SQL: upsert, status write, artifact insert, follow-up update, general list query, named scheduler queries.
- `service.py` (new) — the injected facade of shared validated operations that both the three routes and all in-process callers invoke.
- `routes.py` (extend) — exactly three thin routes: `/chat` (→ agent), `/jobs/{job_id}/action` (→ `transition_status` via the facade, after mapping `UserAction` → target status), and `/jobs/{job_id}/follow-up` (→ `record_follow_up`). No CRUD endpoints.
- `main.py` (extend) — app creation, router includes, lifespan DB-init.
- `config.py` (minor) — DB path + pragmas. Pipeline thresholds belong to consumer layers.

### Work packages

**WP-A — Data model.** Realizes 3a (`job.py` models; `database.py` DDL). Unit-tested: model validation (required fields; `JobCreate` rejects `status`/`score` via `extra="forbid"`; timestamps serialize ISO-UTC) + a round-trip test that builds the schema in a temp SQLite and confirms both tables and the unique fingerprint index. No dependencies.

**WP-B — FSM.** Realizes 3b (the state-machine portion of `enums.py`). Pure logic, zero I/O — the cleanest unit. Unit-tested: every legal edge passes, every illegal edge rejected, `INTERVIEWING` self-loop allowed, terminal states have no exits. Built alongside the enums.

**WP-C — Repository.** Realizes 3c persistence + the named reads (`repository.py`). Atomic `ON CONFLICT` upsert, status write, artifact insert, follow-up update, nudge-marker update (`follow_up_nudge_at`), general list query, the `find_jobs` lookup read, the `top_scored_for_tailoring` ranking read, and the scheduler window queries. Unit-tested against a temp SQLite seeded with known rows: upsert inserts-then-bumps `seen_count`; follow-up update leaves `status_changed_at` untouched; the nudge-marker update sets only `follow_up_nudge_at`; the follow-up detection read excludes rows already nudged (`follow_up_nudge_at IS NOT NULL`); `find_jobs` returns rows on a case-insensitive partial `title` match, narrows on `company`, and searches all statuses when `status_set` is omitted; the ranking read returns only `SCORED` rows in score → recency → id order; each scheduler query returns the right subset/order. Fingerprint injected and mocked. Depends on WP-A.

**WP-D — Service facade.** Realizes 3d orchestration (`service.py`): `ingest_job` (fingerprint via injected fn → repository upsert), `transition_status` (read current → FSM validate → write), `query_jobs`, `find_jobs` (thin wrapper over the lookup read), `top_scored_for_tailoring` (thin wrapper over the ranking read), `register_artifact`, `record_follow_up`, `mark_follow_up_nudged` (stamps `follow_up_nudge_at` only). This is the injected in-process facade on which the three routes, the agent tools, the scheduler, and the tailoring consumer all converge — there is no self-HTTP. Unit-tested by mocking the repository + fingerprint and asserting call order + that an illegal transition is refused before any write. Depends on WP-B and WP-C.

**WP-E — Inbound routes.** Realizes the three thin routes of 3d (`routes.py`): `/chat` (forwards to the agent), `/jobs/{job_id}/action` (parses `UserAction`, maps it to a target status, calls `transition_status`), and `/jobs/{job_id}/follow-up` (calls `record_follow_up`; record-only). Deliverable includes the exception handlers that render facade errors as status codes — `InvalidTransitionError → 409`, follow-up-on-non-`APPLIED` → 409, job-not-found → 404 (an out-of-vocabulary action already yields `422` from Pydantic parsing, so it needs no handler); without them an illegal tap escapes as an unhandled `500`. Tested with mock-facade mapping tests (`/action`: 409 / 422 / 404; `/follow-up`: 409 on non-`APPLIED`) plus one real-database happy-path test per state-touching route (a legal `INTERVIEWING` tap returns `200` with the updated `Job`; a follow-up on an `APPLIED` job returns `200` with `follow_up_count` incremented and `status_changed_at` unchanged) proving the path wires together. `/chat` is tested with a mocked agent, asserting only the forwarding contract. Depends on WP-D.

**WP-F — App wiring.** App skeleton in `main.py`: create app, include routers, lifespan DB-init (scheduler registration sits in the hook, but the jobs themselves are the scheduling layer — only the mechanism is here). Startup integration test: boot the app, confirm schema created and routes respond. Depends on WP-E.

### Dependency order
`enums.py` is the root (both A and B import it). WP-A and WP-B are then the foundation (no further dependencies) → WP-C (needs A) → WP-D (needs B + C) → WP-E (needs D) → WP-F (wires last). Critical path: **enums → A → C → D → E → F**, with **B** parallel to A after enums and joining at D.

**Not work packages here:** the fingerprint (dedup layer; imported behind a stable
signature, mocked in tests) and the pipeline thresholds in config (consumed by
other layers, though the columns they act on are defined here).

---

## 5. Build & Test Order

Order respects the §4 dependency graph and the project's TDD discipline:
Red-Green-Refactor; roughly 70% unit / 25% integration / 5% E2E; all external
dependencies (fingerprint, LLM) mocked except `live`-marked tests; query
functions tested as plain functions, never through the scheduler.

**Sequence:** `enums.py` → **WP-B (FSM)** → **WP-A (data model)** → WP-C → WP-D
→ WP-E → WP-F. B is built before A by preference (purest unit — no I/O, no
fixtures, no mocking; locks the correctness core D depends on), though A and B
are interchangeable once `enums.py` exists.

**Step 0 — `enums.py`.** `Status` and `ArtifactKind`. No test of its own.

**WP-B — FSM** (pure, no DB). First red test:
```python
def test_legal_edge_allowed():
    assert can_transition(Status.DISCOVERED, Status.SCORED)

def test_illegal_edge_rejected():
    assert not can_transition(Status.REJECTED, Status.OFFER)

def test_interviewing_self_loop_allowed():
    assert can_transition(Status.INTERVIEWING, Status.INTERVIEWING)

def test_terminal_states_have_no_exits():
    for s in (Status.REJECTED, Status.USER_SKIPPED, Status.EXPIRED,
              Status.ACCEPTED, Status.DECLINED):
        assert legal_targets(s) == set()
```
Hand-pick representative legal/illegal edges plus structural properties; do not
loop the map back against itself.

**WP-A — Data model.** First red tests (models + schema):
```python
def test_jobcreate_refuses_caller_set_status():
    with pytest.raises(ValidationError):
        JobCreate(company="X", title="Y", description="Z",
                  url="http://e", status="DISCOVERED")  # extra='forbid'

async def test_schema_creates_tables_and_unique_fingerprint_index(tmp_db):
    await init_db(tmp_db)
    assert await table_exists(tmp_db, "jobs")
    assert await table_exists(tmp_db, "artifacts")
    assert await unique_index_on(tmp_db, "jobs", "fingerprint")
```

**WP-C — Repository** (real temp SQLite — do not mock the DB; fingerprint
injected). The frozen-clock assertions are the load-bearing ones:
```python
async def test_upsert_fresh_inserts_discovered(repo, empty_db):
    row = await repo.upsert_job(make_jobcreate(), fingerprint="abc", now=t1)
    assert row.status == Status.DISCOVERED
    assert row.seen_count == 1 and row.score is None

async def test_upsert_duplicate_bumps_seen_and_freezes_clock(repo, db_with_job):
    row = await repo.upsert_job(make_jobcreate(), fingerprint="abc", now=t2)
    assert row.seen_count == 2
    assert row.status_changed_at == t1        # must NOT move on re-sighting

async def test_follow_up_leaves_status_clock_untouched(repo, applied_job):
    row = await repo.record_follow_up(applied_job.id, now=t2)
    assert row.follow_up_count == 1 and row.last_follow_up_at == t2
    assert row.status_changed_at == applied_job.status_changed_at
```
Plus one test per named read: `top_scored_for_tailoring` ordering (only `SCORED`
rows, score → recency → id, respects `limit`) and each scheduler window query's
ghost/expiry threshold boundary.

**WP-D — Service** (mock repository + fingerprint). Load-bearing test: no write
on an illegal transition:
```python
async def test_transition_refuses_illegal_before_any_write(service, mock_repository):
    with pytest.raises(IllegalTransition):
        await service.transition_status(applied_job_id, Status.OFFER)
    mock_repository.write_status.assert_not_called()

async def test_ingest_computes_fingerprint_then_upserts(service, mock_repository, mock_fingerprint):
    await service.ingest_job(make_jobcreate())
    mock_fingerprint.assert_called_once()
    mock_repository.upsert_job.assert_called_once()
```

**WP-E — Routes** (httpx). The FSM's own rules are already exhaustively covered in
WP-B and refuse-before-write in WP-D, so WP-E does not re-test them — it verifies
the *route-to-status mapping* and, in one case, that the whole path wires together.
Two groups:

*Mapping tests — mock the facade, no database.* Mock `transition_status` to raise,
and assert the route renders the right status code. These cover the three mappings:
```python
async def test_action_illegal_maps_to_409(client, mock_service):
    mock_service.transition_status.side_effect = InvalidTransitionError(...)
    r = await client.post("/jobs/any-id/action", json={"action": "OFFER"})
    assert r.status_code == 409

async def test_action_out_of_vocabulary_returns_422(client):
    r = await client.post("/jobs/any-id/action", json={"action": "SCORED"})
    assert r.status_code == 422        # Pydantic rejects before the handler runs

async def test_action_not_found_maps_to_404(client, mock_service):
    mock_service.transition_status.side_effect = JobNotFoundError(...)
    r = await client.post("/jobs/unknown/action", json={"action": "INTERVIEWING"})
    assert r.status_code == 404

async def test_follow_up_on_non_applied_maps_to_409(client, mock_service):
    mock_service.record_follow_up.side_effect = InvalidStateError(...)
    r = await client.post("/jobs/any-id/follow-up", json={})
    assert r.status_code == 409
```

*Wiring tests — real app over a temp DB.* One happy-path test per state-touching
route proves the route genuinely reaches the facade, a real row is loaded and
written, and the result is serialized back — the class of bug the mocked tests
cannot catch (route silently not calling the facade, facade not injected):
```python
async def test_action_legal_transitions_and_returns_job(client, applied_job):
    r = await client.post(f"/jobs/{applied_job.id}/action",
                          json={"action": "INTERVIEWING"})
    assert r.status_code == 200 and r.json()["status"] == "INTERVIEWING"

async def test_follow_up_records_without_touching_status_clock(client, applied_job):
    r = await client.post(f"/jobs/{applied_job.id}/follow-up", json={})
    body = r.json()
    assert r.status_code == 200 and body["follow_up_count"] == 1
    assert body["status_changed_at"] == applied_job.status_changed_at  # ghost clock intact
```

The `/chat` route is tested with a **mocked agent**, asserting only the forwarding
contract (message passed through, agent's reply returned) — never a real LLM call
inside a route test.

**WP-F — App wiring** (startup integration). Boot the app through the lifespan
hook, confirm the schema exists and the three routes are mounted (`/chat`,
`/jobs/{id}/action`, and `/jobs/{id}/follow-up` respond rather than 404).

**E2E (minimal, ~5%):** one thin slice — `ingest_job` in-process, then a legal
`/action` sequence over HTTP, then read the row back via `query_jobs`. Reuses the
same real-app-over-temp-DB fixtures as the WP-E wiring test. Fuller cross-layer E2E
belongs at the overview level, not here.

---

## 6. Invariants

1. The scraper never reasons; the agent never writes to the DB directly. All writes go through the backend's single validated path.
2. The LLM proposes; the backend validates and executes (FSM, Pydantic, repository). No LLM call originates in this layer.
3. Every external dependency (fingerprint, DB) is injected and mocked in tests.
4. The clocks rule is physical: follow-up writes cannot move `status_changed_at`; re-sightings cannot move it either; and the nudge clock `follow_up_nudge_at` is written *only* by `mark_follow_up_nudged` (the scheduler's push), never by `record_follow_up` — "we reminded you" and "you followed up" are separate columns with separate writers.
5. Repository pattern isolation — a DB-driver switch touches only `repository.py` / `database.py`.
6. Timestamps are ISO-8601 UTC everywhere.
7. The service facade is the real interface; in-process callers invoke it directly through dependency injection and never call the backend over HTTP.
8. Exactly three inbound HTTP routes exist. `/chat` is the single origin of any LLM call; `/jobs/{job_id}/action` and `/jobs/{job_id}/follow-up` are both deterministic and never invoke the agent. `/action` changes status; `/follow-up` records a follow-up and never touches `status` or `status_changed_at`; the two stay separate routes. No CRUD endpoints are exposed.
9. `UserAction` is strictly narrower than `Status`: a button tap can only ever target a user-legal state, and can never request a system-only state (e.g. `SCORED`, `TAILORED`).
10. Outbound notifications leave via the notifications bot directly, off the HTTP surface; only the button reply returns, through `/action`. No push-coexistence gate, activity signal, or held-push queue exists — the scheduler and the conversation store never touch.

---

## 7. Deferred / Open

- **SQLite single-writer constraint** — named, not solved. One writer at a time across the whole file; concurrent writes (scrape burst vs. a user-triggered tool call) yield `SQLITE_BUSY`. Invisible at dev scale. Mitigations (0.7.0 reliability): WAL mode, `busy_timeout`, short write transactions, optionally a single writer connection/queue. The single validated write path pre-positions this fix to one place.
- **Button-tap transport** — whether the Telegram bot client polls and forwards taps to `/action`, or the notifications bot uses a webhook straight to the backend. Settled in the Telegram layer spec (see §3e).
- **Conversation store** — the second SQLite database and `conversation/` package are referenced here (§3f) but specified in `conversation_store_v2.md`.
- **Attachment delivery via a download route** — the `/chat` response returns attachment bytes inline as base64, which suits the small PDFs of Phase 1. If artifacts grow large, or another caller needs to fetch them, switch to a reference-plus-fetch shape: `ChatResponse` returns an artifact `id`, and a new streaming route `GET /artifacts/{artifact_id}` returns the raw bytes. This keeps the JSON small and enables real streaming, at the cost of a second round trip and a fourth route. Deferred; the inline form is the current contract.
- **Follow-up re-nudging** — the follow-up detection read nudges each job exactly once (`follow_up_nudge_at IS NULL`). If an ignored reminder should later be re-sent after a cooldown, swap that clause for "null or older than the cooldown window." Deferred as a documented option; not built now.
- **v1 file layout reconciliation** — confirm actual filenames/structure against §4.