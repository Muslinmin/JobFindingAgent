# Job Application Agent v2 — Architecture & Tech Stack

---

## What Changed From v1

| | v1 (current) | v2 (this design) |
|---|---|---|
| Discovery | Tavily search API (search-result URLs, weak JDs) | Per-portal scraper adapters (real listing URLs, full JDs) |
| Candidate data | `profile.json` shaped via chat | Canonical structured profile parsed once from CV; profile drives queries, scoring, and tailoring |
| Scoring | Keyword overlap vs static config keywords | Profile-derived keywords → embedding similarity (upgrade path) |
| Output | Job records in DB, queried on demand | Tailored CV PDF per job, pushed via Telegram |
| Autonomy | None | Phase 1: autonomous scrape + tailor. Phase 2: autonomous apply behind approval gate |
| Frontend | Telegram bot (pure transport) | Telegram bot (transport + approval gate + artifact delivery) |

Unchanged: FastAPI backend, repository pattern, SHA-256 fingerprint dedup
(mechanism kept; fingerprint *inputs* change — see § Deduplication),
LiteLLM ReAct agent, stateless `POST /chat`, APScheduler in lifespan hook,
TDD discipline, "scraper is dumb / reasoning lives in the agent /
all writes go through the backend API".

---

## Core Design Principle: Pipeline State Machine

Every job is a record moving through states. Autonomy is a config flag that
skips the approval state — not a rewrite.

```
Pre-application (the automated pipeline):

DISCOVERED → SCORED → TAILORED → PENDING_APPROVAL → APPLYING → APPLIED
                │                  │       │            │
                │                  │       │            └─ APPLY_FAILED → notify, manual
                │                  │       └─ USER_SKIPPED (explicit decline)
                │                  └─ EXPIRED (auto: no decision in pending_expiry_days)
                └─ REJECTED (score below threshold)

Post-application (human updates via Telegram + automatic time rules):

APPLIED ──► INTERVIEWING ──► OFFER ──► ACCEPTED
   │  │           │             │
   │  │           ├─► REJECTED ◄┘
   │  ├─► REJECTED│
   │  │           │
   │  └──► GHOSTED ◄──── (auto: status unchanged for ghost_after_days)
   │           │
   │           └─► INTERVIEWING   (resurrection — companies do reply late)
   │
   └─ day follow_up_after_days: follow-up draft pushed to Telegram
      (an action prompt, not a state change)
```

- Phase 1 ships everything up to `PENDING_APPROVAL`. The user applies manually
  via the link; Telegram buttons move the record to `APPLIED` or `USER_SKIPPED`.
- Phase 2 adds the application worker. `[Apply for me]` transitions to `APPLYING`.
- Full autonomy = `auto_apply: true` per source → `TAILORED` skips straight to `APPLYING`.
- Post-application transitions come from the user in natural language
  ("got an interview with PUB", "rejection email from GovTech") — the agent
  resolves the record via `query_jobs`, then calls `update_status`.
  `INTERVIEWING → INTERVIEWING` is legal (multiple rounds).
- **The two clocks are different — this matters.** Time rules key off
  `status_changed_at` (when the record entered its current status), NOT
  `updated_at`. Follow-ups are recorded separately (`follow_up_count`,
  `last_follow_up_at`) and **do not reset the ghost clock** — ghosting
  measures *company* silence, not user activity. Otherwise every follow-up
  would postpone ghosting indefinitely.
- **Follow-up ladder (the point of tracking).** Deterministic daily check:
  `status = APPLIED AND status_changed_at older than follow_up_after_days
  AND follow_up_count = 0` → LLM drafts a short follow-up email from role,
  company, and applied date → pushed to Telegram with the draft text and
  `[Sent it] [Skip]` buttons. `[Sent it]` increments `follow_up_count` and
  stamps `last_follow_up_at`; status stays `APPLIED`. Optional second nudge
  at 2× the interval if `follow_up_count = 1`. The *check* is deterministic
  and free; only the few jobs crossing the threshold each day cost an LLM
  call (drafting is also available on demand via the `draft_followup` tool).
- **Ghosting is deterministic, not an LLM decision.** Daily rule:
  `status IN (APPLIED, INTERVIEWING) AND status_changed_at older than
  ghost_after_days → GHOSTED` (default 35 days). Zero tokens, unit-testable,
  cannot hallucinate. Each auto-ghost sends a one-line Telegram note.
- **Expiry keeps the pending queue honest.** Daily rule: `PENDING_APPROVAL
  older than pending_expiry_days (14) → EXPIRED` — listings close; jobs the
  user never acted on must not clutter active queries forever. `EXPIRED`
  ("never decided") is semantically distinct from `USER_SKIPPED` ("declined").
- Full timeline for one job: day 0 applied → day 7 follow-up draft pushed →
  (optional day 14 second nudge) → day 35 ghosted. Any real movement
  (interview, rejection) resets `status_changed_at` and the ladder restarts
  where relevant.
- `query_jobs` defaults to the active pipeline (excludes `GHOSTED`,
  `REJECTED`, `USER_SKIPPED`, `EXPIRED`); terminal states remain queryable
  explicitly ("show me everything that ghosted me").

State transitions are enforced at the model layer (same FSM pattern as v1 status
transitions). Illegal transitions (e.g. `REJECTED → OFFER`) are rejected before
any DB write.

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Candidate Profile (source of truth)                                │
│ CV ──parse once──► profile.json                                    │
│ profile ──slow loop (LLM)──► search_queries.json                   │
└───────────────────────────────┬──────────────────────────────────┘
                                 │ read by services
        ── TWO TRIGGERS — they never call each other ──
                                 │
┌───────────────┐                │                ┌───────────────────┐
│ APScheduler   │                │                │ Telegram Bot (thin)│
│ (lifespan)    │                │                │ free-text ─► /chat │
│ clock; picks  │                │                │ buttons   ─► Backend│
│ which records │                │                │ renders only,      │
│ scrape·tailor │                │                │ no logic/history   │
│ follow-up·    │                │                └─────────┬─────────┘
│ lifecycle·    │                │              free-text only│ POST /chat
│ digest·flush  │                │                           ▼
└───────┬───────┘                │                ┌───────────────────┐
        │ drives services        │                │ Agent — ReAct loop │
        │ directly               │                │ reference resolution│
        │                        │                │ ConversationContext │
        │                        │                │ thin tool bindings  │
        │                        │                └─────────┬─────────┘
        │                        │       tool handlers call │ the SAME
        │                        │       services            │
        ▼                        ▼                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Shared Services — caller-agnostic; THE WORK LIVES HERE             │
│ scrape/ingest · score · tailor · follow-up draft · query regen ·   │
│ status transition       (only tailor / draft / regen call the LLM) │
└──────┬───────────────────────┬───────────────────────┬────────────┘
       │ writes                │ LLM call              │ fetch
       ▼                       ▼                       ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
│ Backend API +    │  │ LLM (leaf dependency) │  │ Scraper adapters │
│ repository       │  │ single, stateless     │  │ MCF · C@G ·      │
│ jobs·artifacts·  │  │ completions           │  │ JobStreet        │
│ FSM·dedup·       │  │ — also used directly  │  └──────────────────┘
│ conversations·   │  │   by the Agent loop   │
│ sessions·        │  └──────────────────────┘
│ pending_pushes   │
└──────────────────┘

Phase 2 — Application Worker: separate process, polls DB for APPLYING
records, drives ATS forms (Playwright + browser-use). LinkedIn excluded.
```

---

## Components

### 1. Candidate Profile — the single source of truth

**Responsibility:** Hold all candidate data in structured form. Every stage
that needs "who is the candidate" reads from here — never from a PDF.

- The CV is parsed **once** into `profile.json` (experiences, projects, skills,
  education, contact). The CV file itself is just a rendering of this data;
  tailored CVs are re-renderings of subsets of it.
- **The profile is a deliberate superset of everything true** — every project
  (large or small), every skill, including the ATS-variant surface forms of the
  *same* real competence (`PostgreSQL`/`Postgres`, `REST`/`RESTful`, `CI/CD`).
  This is what makes tailoring a *selection* problem rather than a *gap-closing*
  one (see § Tailoring Service): there is nothing legitimate to add at tailoring
  time, only true content to choose from.
- **Each experience/project carries `demonstrated_skills`** — the skills that
  *specific item* genuinely exercised, authored by the human (or parse-proposed,
  then reviewed). These authored associations are what let the tailoring LLM
  emphasise a real skill on the right item without fabricating one (see § Agent
  Brain → LaTeX output path).
- **`target_tracks`** — a flat, human-authored `list[str]` of the directions
  the candidate is exploring (e.g. `["robotics", "mechatronics", "backend/data
  engineering"]`). It is *intent*, not query strings: the slow loop expands it
  into `search_queries.json`. Index-tier in the agent's profile summary (see
  § Agent Brain). Weighting/priority per track is a noted future seam (Option B);
  v1 is an unweighted flat list.
- Chat-driven updates continue via the agent's `update_profile` tool
  (diff-checked, backup-on-change — unchanged from v1).
- `search_queries.json` is derived from the profile by the LLM (see slow loop)
  — seeded from **skills and `target_tracks`**. Versioned with
  backup-on-change, human-reviewable, vetoable.

### 2. Scraper Layer — adapter pattern

**Responsibility:** Fetch listings from portals and normalise to `JobCreate`.
No reasoning, no DB writes, no scoring. Failure in one adapter logs and
returns `[]` — never crashes the pipeline.

```python
class JobSource(Protocol):
    name: str
    async def fetch(self, query: str) -> list[JobCreate]: ...
```

**Confirmed scope: MyCareersFuture + JobStreet + Careers@Gov.** All three
endpoints were live-tested on 2026-06-10; findings below.

| Adapter | Method | Difficulty | Live-test result (2026-06-10) |
|---|---|---|---|
| MyCareersFuture | httpx → public JSON API | Easy | ✅ `GET api.mycareersfuture.gov.sg/v2/jobs` returned full JSON with **no auth and no bot challenge** |
| Careers@Gov | httpx → **Algolia search API** | Easy | ✅ Full records (title, complete JD, agency, dept) via one POST; requires Referer header (see findings) |
| JobStreet | httpx + HTML parsing (BeautifulSoup), or internal JSON endpoints | Medium | ✅ Search pages load on a plain GET and are server-rendered, but listing data lives in markup — needs a real parser |

#### MCF live findings

- `GET /v2/jobs` paginates the full firehose of newest listings (no filters
  needed to receive data). Keyword search on the real site goes through
  **`POST /v2/search`** with a JSON body — build the adapter against the
  POST search endpoint, not GET query params.
- Each record includes: full HTML `description` (the complete JD), salary
  `minimum`/`maximum`/`salaryType`, `skills[]` from the curated government
  taxonomy, `categories[]`, `employmentTypes[]`, **`positionLevels[]`
  (including "Fresh/entry level" — a free entry-level filter)**, company UEN
  + profile, district/region, posting/expiry dates, and a canonical
  `metadata.jobDetailsUrl`.
- This single source resolves the v1 Tavily limitations (search-result-page
  URLs, missing JD text) outright. Expect the adapter to be ~50 lines of
  httpx + a Pydantic response model.

#### JobStreet live findings

- A plain GET to `sg.jobstreet.com/{query}-jobs` returns a server-rendered
  page (no hard block at low volume): job counts, salary ranges,
  classifications, and recency are visible in the HTML.
- Titles, companies, and listing URLs sit in markup attributes — naive text
  extraction loses them. Use BeautifulSoup against the listing-card
  structure, or preferably the internal JSON endpoints the site's own
  frontend calls (inspect network tab).
- Maintenance expectation: SEEK redesigns will break the parser
  periodically. Pin selectors in one module; cover with fixture-based tests
  so breakage is caught by CI, not in production.

#### Careers@Gov live findings

- The portal (`jobs.careers.gov.sg`) is a custom Next.js app. Job **search
  runs on Algolia**, not Workday: app id `3OW7D8B4IZ`, index `job_index`,
  public search-only API key embedded in the frontend (visible in the
  site's own network requests).
- The key is **referer-restricted**: requests without
  `Referer: https://jobs.careers.gov.sg/` (+ matching `Origin`) get 403.
  With those headers, a plain httpx POST returns 200 (verified live).
- Omitting `attributesToRetrieve` returns **full records**: title, complete
  JD text, `employmentType`, `agency` + `agencyAbbr`, `department`,
  `jobSource`, `activityTimestamp`, `objectID`. `hitsPerPage` goes up to
  1000 — one request typically covers an entire query ("engineer" = 758
  hits, single page).
- Detail URL is constructible from `objectID` (last path segment →
  `jobs.careers.gov.sg/{slug}`). No second fetch needed for discovery.
- `jobSource` reveals two upstream systems: **HRP** (internal HR platform;
  Workday tenant `sggovterp.wd102.myworkdayjobs.com` handles applications)
  and **GREENHOUSE** (commercial ATS with a fully public JSON API at
  `boards-api.greenhouse.io`). Useful Phase 2 intelligence: these are the
  two application-form systems the apply worker will meet for gov roles.
- Operational notes: keep app id / api key / index in `.env` (the key is
  public but rotates on redeploys); send the Referer headers as a matter of
  course; throttle politely; fail soft to `[]` on 403 or key rotation.
- Reference implementation: `careers_gov_adapter.py` (Pydantic models match
  the verified payload).

#### Excluded sources (deliberate)

- **LinkedIn — excluded.** Live test returned `ROBOTS_DISALLOWED`:
  LinkedIn's robots.txt explicitly forbids automated access to the
  `jobs-guest` endpoints that scraping tutorials describe as "public."
  The obstacle is policy, not technique. Excluding it also removes the
  account-ban risk entirely.

Rate limiting per adapter via configurable delay (carried over from v1 Tavily
client). Each adapter is independently unit-testable with mocked HTTP.

**Implementation playbook:** `adapters.md` — the reconnaissance method,
the JobSource contract, test requirements, definition of done (including
how to update this document), and per-portal work orders. New adapters are
built by following that playbook; this section only records outcomes.

### 3. Backend API — extended, not rewritten

**Responsibility:** Source of truth for job records and artifacts. All writes
from every component go through it.

Schema changes:
- `jobs.status` extended to the full pipeline FSM above.
- New lifecycle columns on `jobs`: `status_changed_at` (stamped on every
  status transition — the clock for follow-up/ghost/expiry rules),
  `follow_up_count`, `last_follow_up_at` (follow-up activity, tracked
  separately so it never resets the ghost clock), `seen_count` and
  `last_seen_at` (stamped on duplicate ingest — liveness signal for the
  lifecycle job).
- New `artifacts` table: `(id, job_id FK, kind ['cv_pdf','cover_letter','follow_up_email'], path, created_at)`.
- New `conversations` table: `(chat_id, session_id, turn_index, role, content, created_at)` — conversation turns per session (see § 7). Reference-resolution only; never holds authoritative facts.
- New `sessions` table: `(session_id, chat_id, started_at, ended_at NULL)` — session lifecycle for `/start` / `/end` / idle-timeout.
- New `pending_pushes` table: `(id, chat_id, push_type, payload, created_at, delivered_at NULL)` — pushes held during an active conversation (see § 8).
- New endpoints: `POST /jobs/{id}/artifacts`, `GET /jobs?status=...` filter,
  `POST /jobs/{id}/follow-up` (increments count, stamps timestamp).

Repository pattern unchanged — SQLite now, PostgreSQL later touches one file.

### 4. Scoring & Deduplication — pure functions

**Scoring responsibility:** JD text vs candidate profile → **integer 0–10000**
("basis points": internal float similarity × 10,000, rounded). Decides
`DISCOVERED → SCORED` (≥ `score_threshold`) vs `REJECTED`.

- v2.0: keyword overlap, but keywords **derived from profile.json** instead of
  static config.
- v2.1: embedding cosine similarity (JD text vs full profile text) — this is
  what makes **adjacent roles** rank correctly despite low keyword overlap.
- **Signature: `score(jd_text, candidate) -> int` (0–10000).** Embeddings add a
  model dependency: inject the embedder so tests pass a mock (same seam
  pattern as `LLMClient` injection in the agent). The scorer runs **once, at
  discovery** (`score(jd, full_profile)`). Tailoring does not re-score — the
  three-tier structural controls are the correctness guarantee (see § Tailoring
  Service).
- **Discovery is a coarse filter by design.** `score_threshold` is deliberately
  **lenient and configurable** — "plausibly relevant, let it through." The daily
  budget (`tailor_batch_size`), not the threshold, is the real throttle.
- **Deferred (0.7.x):** once embeddings land (v2.1), mean-pooling the bloated
  superset into one vector can depress good-fit jobs in the top-N ranking even
  past a lenient gate; fix by ranking on profile *chunks* (top-k / max-pool)
  at 0.7.x.

Integer score contract:

- **One conversion point.** The scorer itself does `round(similarity * 10_000)`
  and returns the int. Floats never escape the function — the DB column
  (INTEGER), `score_threshold` (e.g. 7000 = 0.70), ORDER BY, and Telegram
  display all live in the same unit. This is the discipline that prevents
  the classic scaled-value bug (comparing 7312 against 0.7).
- **Why: an explicit noise floor.** Quantizing to 4 decimal places declares
  that differences below 0.0001 are noise, not signal. Embedding jitter
  (0.73120001 vs 0.73120000) collapses into a *true tie*, resolved by the
  deterministic tiebreak (recency, then id) — the ranking never manufactures
  an ordering out of float noise.
- **Score once, store, never recompute.** Stamped at ingest; the daily
  ranking only reads stored integers. Re-scoring would let embedding-model
  drift shuffle the pool ranking between runs.
- **NaN guard.** The one unit test that matters: empty keyword list (v2.0)
  or zero vector (v2.1) → score returns `0`, never NaN.

Daily tailor-pass selection (deterministic, hence testable):

```sql
SELECT ... WHERE status = 'SCORED'
ORDER BY score DESC,      -- exact integer comparison
         posted_at DESC,  -- tie-break 1: fresher listing has more runway
         id ASC           -- tie-break 2: total determinism
LIMIT :tailor_batch_size
```

**Deduplication responsibility:** decide whether an incoming record is a job
the system has already considered. **v2 change: the URL is removed from the
fingerprint.**

```
fingerprint = SHA-256( normalize(company) + normalize(title) )
normalize   = prefer UEN for company when the portal provides it (MCF does);
              else lowercase, strip punctuation, collapse whitespace
```

Why: dedup semantics follow *application* semantics — the question is "have
I already considered this company + role?", not "is this the same row?".
Three duplicate classes, one rule:

| Case | What changes | URL-based fingerprint (v1) | Content fingerprint (v2) |
|---|---|---|---|
| Weekly rescrape, same listing | nothing | ✅ caught | ✅ caught |
| **Repost** (listing expires, re-listed with new id/URL) | objectID + URL | ❌ slips through as "new" | ✅ caught — ignored, per design intent |
| **Cross-portal** (gov jobs are cross-posted C@G ↔ MCF) | everything but content | ❌ ingested twice | ✅ caught |

Notes:
- The description is deliberately **excluded** from the hash: cross-portal
  copies differ in HTML/whitespace and reposts carry minor edits, so a
  description-inclusive hash misses exactly the duplicates that matter.
- Same company + same title for a genuinely new opening months later is
  *correctly* treated as duplicate — the user should not apply twice to the
  same company + role regardless.
- Company-name aliasing across portals ("PUB, The National Water Agency" vs
  "PUB"): UEN solves it where available; otherwise a small alias map,
  added lazily when a real collision is observed — not over-engineered
  upfront.
- **Duplicates are signal, not waste:** on a duplicate hit, the upsert stamps
  `last_seen_at` and increments `seen_count` on the existing record instead
  of discarding silently. "Still being seen" ≈ listing still live; the
  lifecycle job can expire `SCORED` records faster once they stop appearing
  in scrapes.
- Both functions remain pure (no I/O); uniqueness is still enforced by the
  DB constraint on `fingerprint`; the idempotent upsert in `POST /jobs` is
  unchanged. Adapters never dedup (invariant 1).
- Source-native identity (`objectID`, MCF uuid, portal URL) is kept in
  `metadata` for traceability — it's an attribute of the record, not part
  of its identity.

### 5. Tailoring Service — LLM selects, code renders

**Responsibility:** JD + profile → tailored CV PDF. Triggered for every job
entering `TAILORED`.

**Tailoring is selection, not gap-closing** (see `tailoring.md` for the full
decision record). `profile.json` is a deliberate **superset of everything true**;
tailoring chooses a subset and reframes its phrasing. The only ethical source of
any keyword is the candidate's own history, so a "missing keyword" can only mean
*true content not yet selected* — never content to invent. This makes invariant 4
structurally enforced rather than merely prompt-enforced.

Two strictly separated steps:
1. **LLM (LiteLLM, structured output):** emits a `TailoredSelection` — a
   *projection* over the superset, not a copy of it (no identity fields). It
   selects which experiences/projects/skills and their order, and may reframe
   the text of selected items, guided by the incorporated skills (`resume-tailor`,
   `resume-ats-optimizer`, `resume-section-builder`; see § Agent Brain). Output is
   JSON validated against a Pydantic schema. Content lives in **three tiers** with
   decreasing rigidity (see § Agent Brain → LaTeX output path): identity (frozen),
   selection (by reference — emitted `ref_id`s must resolve to real entries), and
   text (constrained rewrite — full freedom over *phrasing*, none over *claims*).
2. **Renderer (deterministic):** Jinja2 `.tex` template + `tectonic`
   (alt: `latexmk`) → PDF. The template owns all LaTeX syntax; content strings
   are LaTeX-escaped before substitution. The LLM can never break layout because
   it never produces layout. (RenderCV — YAML → LaTeX PDF — remains a drop-in
   alternative.)

#### Single-pass tailoring call

Tailoring is **one LLM call, no iteration**. The correctness guarantee is
structural — the three-tier model and guards — not a scoring loop.

```
tailored = llm_tailor(jd, profile)     # one LLM call
validate_schema(tailored)              # Pydantic — TailoredSelection
validate_guards(tailored, profile)     # ref_id integrity + skill-subset + no-new-specifics
render(tailored, profile)              # identity + resolved selection → PDF
```

**Guard violation → log and fail cleanly.** If any guard fires, the attempt
is logged (loguru) and the job is marked failed. No retry, no partial render.
The failure surfaces through the existing pipeline notification path. A guard
breach means the LLM fabricated a claim; re-prompting the same input is
unlikely to fix a structural hallucination.

This means ATS optimization is **not a separate gate** and there is no
post-tailor re-score. The scorer runs once at discovery; the structural guards
are the tailoring-quality guarantee.

Artifacts are written to disk, registered via `POST /jobs/{id}/artifacts`.

Testing: mock the LLM, assert schema validity; snapshot-test the renderer.
Additional guards (see `tailoring.md` § invariants): every `ref_id` resolves to a
real profile entry; skill terms surfaced in tailored text ⊆ that item's
`demonstrated_skills`; no new numerals/named entities vs the source item. Guard
violation → logged, job marked failed, no partial render.

### 6. Telegram Bot — transport layer (thin)

**Core principle:** Telegram is transport only. It moves messages between the
user and the backend and renders what it is handed. It holds **no business
logic, no pipeline state, and no conversation history**. Every decision about
*what* to say, *which* job a reply refers to, or *when* a session begins lives
above it (agent + repository). If a behaviour requires a decision, it does not
belong in this layer.

#### What the Telegram layer IS responsible for

1. **Inbound transport.** Receive a user message (text or button callback),
   attach the `chat_id`, forward to the backend (`POST /chat` for text;
   `PATCH /jobs/{id}/status` or the relevant endpoint for button callbacks —
   the callback payload carries the `job_id`, so button actions need no
   conversation context and are always unambiguous).
2. **Outbound transport.** Send backend-produced messages to the chat:
   deliver text, attach PDF/document artifacts, render inline keyboards.
3. **Telegram-flavoured rendering only.** Turn already-decided content into
   Telegram markdown, button layouts, and document uploads. This is the one
   kind of "formatting" it owns — *rendering*, never *structuring*. It does
   not decide what goes into a digest or how a follow-up reads; it renders the
   finished string.
4. **Command surface.** Expose `/start` and `/end` (session boundaries, below)
   and map button taps to backend calls. Commands are forwarded, not
   interpreted — the session lifecycle itself is owned by the backend.

#### What the Telegram layer is NOT responsible for

- **Not** conversation history — it never stores or appends turns (see § 7).
- **Not** deciding which job a free-text reply refers to — the agent resolves
  references from session context.
- **Not** content structuring — digest contents, follow-up wording, push copy
  are produced upstream (template or agent) and handed down as finished text.
- **Not** pipeline state — job status is authoritative in the DB; Telegram
  reflects it, never holds it.

#### Push notifications (system-initiated messages)

These are produced by scheduled jobs and pushed through Telegram. Each carries
a self-labelling header (role + company) so it is interpretable even out of
conversational context.

- **Tailored job ready** (`PENDING_APPROVAL`): role, company, score, listing
  URL, tailored CV PDF, inline keyboard.
  - Phase 1 buttons: `[Mark Applied]` `[Skip]` → `PATCH /jobs/{id}/status`.
  - Phase 2 buttons: `[Apply for me]` `[Skip]`.
- **Follow-up draft** (`APPLIED` + `follow_up_after_days`, none sent yet):
  context line + LLM-drafted email text (copy-paste ready) + `[Sent it]`
  `[Skip]`. `[Sent it]` → `POST /jobs/{id}/follow-up`.
- **Auto-ghost notice** (informational): one line, no buttons.
- **Weekly digest** (informational): pipeline summary.

Push *delivery timing* relative to an active conversation is governed by § 8
(it is not a Telegram-layer decision — Telegram just sends what the delivery
rule releases to it).

### 7. Conversation Sessions & History

History exists for exactly one job: **resolving references** ("that one",
"the third", "make it more formal") across a handful of recent turns. It is
**not** a memory of the job search — every durable fact lives in the DB and is
queried live. This narrow mandate is what lets sessions be cleared freely
without losing anything real.

Three responsibilities, three homes (dependency arrows point downward only):

- **Storage — repository.** A `conversations` table keyed by
  `(chat_id, session_id, turn_index)`. `append_turn(...)`,
  `get_session_turns(session_id)`. Durable, so an app restart mid-session
  loses nothing. Storage knows nothing about windowing or the LLM.
- **Assembly — agent layer (`ConversationContext` module).** Owns
  `build_context(session_id) -> messages[]` (load the current session's turns,
  assemble into the LLM messages array) and `record(session_id, role, content)`
  (append a turn). The ReAct loop calls these; it never trims inline. Isolating
  the policy here means a future summarise-on-eviction upgrade touches one
  module. v1 policy is trivial: **load the whole current session** — `/end`
  keeps sessions short, so no within-session windowing is needed yet. The
  `ConversationContext` seam exists from day one; its policy stays dumb until a
  real marathon session forces an upgrade.
- **Reference-resolution — emergent.** No component of its own: with recent
  turns in the assembled context, the LLM resolves "that one" during normal
  inference.

**Sessions.** `/start` opens a session; `/end` closes it. **Auto-open** is the
default: a message with no open session implicitly starts one, so quick
one-shot queries ("anything ghosted?") need no ceremony. `/start` then means
"explicitly begin fresh"; `/end` means "I'm done — forget the references"
(facts already persisted to the DB are untouched). A session is also
considered closed by **idle-timeout** (no user turn for `session_idle_minutes`),
checked by the scheduler so a walked-away user's session self-heals.

The endpoint stays stateless: `ConversationContext` rehydrates the session from
the DB on every `POST /chat` call.

### 8. Push Delivery & Conversation Coexistence

The hard problem: a scheduled push can fire **while the user is mid-conversation
about a different job**. Injecting it into the thread interleaves two subjects
and makes the next "tailor that one" ambiguous. The rule:

> **Notification can always fire; injection into the chat thread waits for a
> clean moment.** An active conversation is never interrupted by content — only
> by a lightweight signal that content is waiting.

Two delivery modes, chosen by conversation state at push time:

- **Idle / no active conversation:** deliver the push in full immediately
  (implicit `/start` if no session is open). Normal path.
- **Active conversation:** do **not** inject the push. Enqueue it
  (repository-backed pending-push queue, keyed by `chat_id`, durable) and
  surface only a **single coalesced nudge** — one message that ticks up
  ("📥 1 item waiting" → "📥 2 items waiting", edited in place, never one
  notification per push). Flush the queue in full when the conversation ends.

"Conversation ends" = `/end` (express lane) **or** idle-timeout (safety net,
via a small periodic scheduler check: any chat idle past the threshold with
queued pushes → flush). Buttons are exempt from all of this: a button tap
carries its own `job_id` and is always unambiguous, so push *responses* via
buttons work regardless of conversation state — only free-text replies to a
push needed protecting, which this design provides.

**Uniform hold policy (v1):** every push type may be held during an active
conversation; none overrides. All current pushes (tailored CV, follow-up draft,
ghost notice, digest) tolerate a short delay. *Note for future push types:* if
a genuinely time-critical push is ever added, it must explicitly opt out of
holding — silence-inheriting the wrong behaviour is the trap to avoid.

Pending-push queue (repository): `(chat_id, push_type, payload, created_at,
delivered_at NULL)`. Durable across restarts. The flush is a step in the
session-end handler and in the idle-timeout scheduler job — Telegram only sends
what the flush releases.

### 9. Agent Brain — reasoning layer, NOT the orchestrator

**The agent is not the orchestrator — APScheduler is.** The scheduler is the
heartbeat: it drives the pipeline (scrape → score → tailor → lifecycle →
follow-up) on a deterministic clock, calling the LLM only at the few stages
that need judgement (tailoring selection, follow-up drafting, query
regeneration). The agent is the *reactive reasoning layer* the **human** calls
into — and only the human. It wakes on `POST /chat` (a free-text user turn
relayed by Telegram), runs the ReAct loop, and returns; it never owns the loop.
The scheduled LLM stages above do **not** enter this loop — each is a single,
stateless completion, so the scheduler reaches the model directly through a
shared service, never through the agent. "Calls the LLM" is not "calls the
agent": the model is a leaf dependency that the agent loop and the services use
independently.

Architecture unchanged: LiteLLM ReAct loop, stateless `POST /chat`, LLM
proposes → backend validates → repository executes. New/changed tools:

| Tool | Change |
|---|---|
| `search_jobs` | Now invokes scraper adapters directly (ad-hoc interactive search) |
| `tailor_resume` | New — trigger tailoring for a specific job on demand |
| `draft_followup` | New — draft a follow-up email for a specific job on demand ("draft a follow-up for the PUB role") |
| `draft_cover_letter` | New — generate a cover letter for a specific job (JD + profile → `kind='cover_letter'` artifact) |
| `regenerate_queries` | New — rebuild `search_queries.json` from current profile |
| `query_jobs` | Gains status-filter awareness (pipeline states) |
| `log_job`, `update_status`, `update_profile` | Unchanged |

**Every tool is a thin binding, not the work itself.** Each tool is two parts:
a function-calling *schema* (so the LLM can propose the call) and a *handler*
that parses the emitted arguments, calls the underlying service, and marshals
the result back into the context. The work lives in the service — and for any
capability the scheduler also drives (tailoring, scrape/ingest, follow-up
drafting, query regeneration, status transitions), the scheduled job and the
agent handler call the **same** service. The service signature carries no notion
of its caller, so the two paths never couple. The agent layer owns the schema
and the handler; it never owns the work, the session lifecycle (backend), or
turn storage (repository). Per-tool I/O contracts live in `agent_v2.md`.

#### Incorporated skills — the agent's domain expertise

Four skill playbooks are loaded into the resume/cover-letter stages as
**system-prompt domain knowledge for the LLM selection step** — heuristics and
guardrails, not callable functions. They inform *what the LLM selects and how
it phrases it*; they never touch the deterministic renderer. This preserves
invariants 2 and 4 (LLM selects/emphasises real content only; code validates
and renders).

| Skill | Feeds | What it contributes |
|---|---|---|
| `resume-tailor` | `tailor_resume` / tailoring pass (LLM step) | JD-vs-profile selection logic: reorder experience by relevance, rewrite the summary to mirror the role, lead with the most relevant bullets, integrate JD keywords truthfully. The core of the "select & emphasise only" step. |
| `resume-ats-optimizer` | scorer (keyword model) + gap hint + text-tier guard | Keyword taxonomy (hard / soft / industry), match-score logic, placement priority (summary → skills → bullets), ATS formatting rules. Not a separate gate: it sharpens the scorer, and its keyword model computes the gap — *in-profile keywords not yet selected* — fed into the next tailoring pass. Its keyword detector also powers the text-tier guard (surfaced skills ⊆ item's `demonstrated_skills`). See § Tailoring Service. |
| `resume-section-builder` | tailoring pass (structure) | Section composition & ordering by career stage. For this user: **entry-level / technical / recent-graduate** profile — prioritise Skills + Projects, Education carries weight, 3–5 achievement bullets. Shapes the JSON structure the LLM emits. |
| `cover-letter-generator` | `draft_cover_letter` | JD + profile → 250–400-word letter: hook, direct-match body, gap handling, call to action. Produces a distinct `cover_letter` artifact. |

These skills constrain **content selection only**. The pipeline is:

```
skills (domain knowledge in system prompt)
   ↓ guide
LLM selection step ──► structured JSON  ──► Pydantic validation
                                              ↓
                                        LaTeX template (owns all syntax)
                                              ↓
                                        compile ──► PDF artifact
```

#### LaTeX output path (the deliverable is a LaTeX-rendered PDF)

The end artifact is a **LaTeX-compiled CV PDF**, not HTML→PDF. To keep
rendering deterministic and uncrashable:

- **The LLM emits structured, escaped-safe content JSON — it does NOT write raw
  LaTeX.** A Jinja2 `.tex` template owns every bit of LaTeX syntax; the LLM only
  fills slots. This is the LaTeX equivalent of invariant 2: the LLM cannot break
  layout because it never produces layout.
- **Three tiers of template slot, decreasing rigidity — only two are LLM-touched.**
  - **Identity (frozen)** — name, email, phone, location, school, degree,
    graduation date, links. Substituted **directly from `profile.json`**; the LLM
    never sees or rewrites them. Constant across every tailored CV; only the
    profile (source of truth) can change them.
  - **Selection (by reference)** — which experiences/projects/skills, and their
    order. The LLM emits `ref_id`s plus ordering; code resolves them to the real
    strings. Guard: every `ref_id` must resolve to a real profile entry. The LLM
    chooses and orders; it cannot conjure an item.
  - **Text (constrained rewrite)** — professional summary and the phrasing of
    selected bullets. The LLM has full freedom over *how it says things*
    (grammar, tense, synonyms, connectives, mirroring the JD's wording) and zero
    freedom over *what it claims*. Two guards: (a) skill terms surfaced in the
    text must be a subset of that item's `demonstrated_skills`; (b) no new
    specifics — numerals, named entities — absent from the source item.

  `demonstrated_skills` is the linchpin: each experience/project is tagged (by
  the human, or parse-proposed then reviewed) with the skills it genuinely
  exercised. This blocks the "two true atoms → one false molecule" failure — "I
  have leadership" + "I did project X" must not become "I led a team on X" unless
  X is actually tagged with leadership. The association is authored, never
  invented. (Soft edge: the skill-subset check catches fabricated *attributions*,
  not every fabricated *specific*; "team of 5" is caught only by guard (b), which
  is prompt-enforced plus a source diff, not a formal proof.)

  The Pydantic schema encodes the split: identity passes through unmodified;
  selection + text are the LLM's output surface (`TailoredSelection`). A tailored
  CV is therefore `identity(profile) + resolved(selection)` rendered through one
  template.
- **Escaping is mandatory.** Content strings are passed through a LaTeX-escape
  pass (`& % $ # _ { } ~ ^ \`) before template substitution. An unescaped `&`
  in a company name or a `%` in a bullet silently breaks compilation otherwise.
- **Compile = `tectonic` (or `latexmk`), deterministic.** Template + escaped
  JSON → `.tex` → compile → PDF → register via `POST /jobs/{id}/artifacts`.
- **One template, entry-level.** A single entry-level / technical section order
  (Skills + Projects prioritised, Education weighted, 3–5 achievement bullets).
  No multi-template selection — that would reopen the "LLM affects layout"
  question. Trimmed deliberately to match the user's actual target roles.
- **Alternative (only if raw-LaTeX authoring is ever wanted):** let the LLM
  write LaTeX directly, but then a sanitization + compile-retry loop is required
  (compile, catch errors, feed back, re-emit). Higher token cost, nondeterministic,
  not recommended at this scope. Template-owns-syntax is the default.

#### Prompt files & cover letter

- **Skills live as separate prompt files**, not inlined into one mega-prompt —
  consistent with the existing `agent/prompts/system.md` + `{profile}` pattern.
  `prompts/tailoring.md` (carries `resume-tailor` + `resume-ats-optimizer` +
  `resume-section-builder` guidance) and `prompts/cover_letter.md` (carries
  `cover-letter-generator`). Loaded only by the stage that needs them, so the
  large playbook text isn't paid for on every chat call — only on tailoring /
  cover-letter calls.
- **Cover letter is plain text, not a compiled PDF.** It's a document-style
  written artifact (`kind='cover_letter'`, `.txt`/`.md` body) delivered as text
  via Telegram — no second LaTeX template. Still a real artifact record for the
  audit trail; just not rendered.
- **Tailoring is a single-pass call** — one LLM call, structural guards, no
  re-score. See § Tailoring Service.

Testing: mock the LLM and assert the JSON validates against the Pydantic
schema; snapshot-test the `.tex` template output; a single `live`-marked test
actually compiles a fixture to PDF to catch template/escaping regressions.

### 10. Application Worker — Phase 2

**Responsibility:** Take records in `APPLYING`, drive a browser through the
application form, transition to `APPLIED` or `APPLY_FAILED`.

- **Separate process** from the FastAPI app (browser automation is heavy and
  crash-prone). Polls the DB for `APPLYING` records — DB-as-queue, no Redis yet.
- Stack: Playwright for deterministic steps + browser-use (or Stagehand) for
  the AI-judgment steps (unfamiliar form fields, screening questions).
- Reality: MCF/JobStreet apply buttons usually redirect to external ATS
  (Workday, SuccessFactors, Greenhouse). The worker's real job is **generic
  ATS form-filling**, portal-agnostic.
- Every run writes a structured log (screenshots, actions taken) for audit.
- Hard exclusion: LinkedIn. Authenticated automation there risks the account.

### 11. Scheduling — two loops at two speeds

**Slow loop (LLM, occasional):** on profile change or weekly — agent
regenerates `search_queries.json` from the profile. One LLM call, auditable
output, human-vetoable. This is where adjacent-role reasoning lives.

**Fast loop (deterministic, daily):** APScheduler scrape job reads
`search_queries.json`, fans out across adapters, ingests via `POST /jobs`
(dedup happens here), scores, queues tailoring. **Zero LLM calls for
discovery/ingest** — keeps it cheap, deterministic, and testable as plain
functions (no scheduler in tests, same as v1).

Scheduled jobs (all plain functions, tested without the scheduler):

| Job | Cadence | LLM? | Does |
|---|---|---|---|
| scrape | daily | no | queries → adapters → `POST /jobs` → score → SCORED/REJECTED |
| tailor | daily (after scrape) | yes (budgeted) | top-`tailor_batch_size` SCORED by score → tailor → PDF → PENDING_APPROVAL → Telegram push |
| follow-up | daily | yes (small N) | `APPLIED` past `follow_up_after_days`, `follow_up_count = 0` → draft email → Telegram push with `[Sent it] [Skip]` |
| lifecycle | daily | **no** | three deterministic time rules on `status_changed_at`: `PENDING_APPROVAL > pending_expiry_days → EXPIRED`; `SCORED > stale_after_days → REJECTED`; `APPLIED/INTERVIEWING > ghost_after_days → GHOSTED` (+ Telegram note) |
| digest | weekly (Mon) | optional | pipeline summary: active states, follow-ups pending, recent ghosts/expiries |
| query regen | weekly / on profile change | yes (1 call) | profile → `search_queries.json` |
| session-flush | every few min | **no** | any chat idle past `session_idle_minutes` → close session + flush pending-push queue (see § 8) |
| apply (P2) | poll | per-form | `APPLYING` records → ATS form-fill → APPLIED/APPLY_FAILED |

Note the split: the **follow-up check** is deterministic and free; only
drafting the email (a handful of jobs/day at most) costs tokens. The
**lifecycle job** never touches the LLM at all.

---

## Throughput & Cost Control

The pipeline is a funnel, and every stage must be cheaper than the one after
it. Rate limiting alone is the wrong tool — the design itself bounds the
expensive stages.

```
~1000 discovered/day  ─ dedup (fingerprint, free) ──►  ~600 new
                      ─ score threshold (pure fn) ──►  ~40 SCORED
                      ─ tailor_batch_size (top-N) ──►  10 tailored/day
                      ─ approval gate (human)     ──►  what you actually apply to
```

Three throttles, three layers:

1. **Portal politeness (scrape side).** Configurable `delay_s` per adapter.
   Volume is naturally low: MCF and Careers@Gov return up to ~1000 results
   per request, so a daily run across ~8 queries × 3 portals is ~25 HTTP
   requests total.
2. **Provider limits (LLM side).** LiteLLM Router `rpm`/`tpm` settings +
   exponential-backoff retries absorb burstiness against Gemini free-tier
   caps. Mechanical, set once.
3. **Fan-out budget (pipeline side — the one that matters).** The tailoring
   pass processes the **top `tailor_batch_size` SCORED records by score, per
   day** (default 10). Everything else stays in `SCORED` — the status column
   *is* the queue; tomorrow's run takes the next batch. A staleness expiry
   (`SCORED` untouched for 14 days → `REJECTED`) stops the backlog growing
   unboundedly, since listings close anyway.

Why a budget and not a rate limit: one tailoring call ≈ 5k tokens (JD ~1.5k +
profile ~2k + output ~1k). Unbounded, 1000 jobs/day ≈ 5M tokens — past any
free tier, producing PDFs nobody reads. Budgeted at 10/day ≈ 50k tokens —
negligible. The true bottleneck is human review capacity (~5–10
applications/day); `tailor_batch_size` is sized to the human, and LLM cost
follows automatically.

Settings: `tailor_batch_size=10`, `score_threshold=7000` (basis points,
= 0.70, lenient by design), `save_debug_artifacts=false`,
`follow_up_after_days=7`, `pending_expiry_days=14`,
`stale_after_days=14`, `ghost_after_days=35`, `session_idle_minutes=30`,
per-adapter `delay_s`, LiteLLM `rpm`/`tpm`.

---

## Flow of Execution (Phase 1, steady state)

```
1. [weekly / on profile change]
   Agent reads profile.json ─► generates search_queries.json (LLM, 1 call)

2. [daily, APScheduler]
   for query in search_queries.json:
       for adapter in [MCF, CareersGov, JobStreet]:
           raw = adapter.fetch(query)          # no LLM
           POST /jobs (normalise → dedup → DISCOVERED)

3. [same run]
   for job in status=DISCOVERED:
       s = score(job.description, profile)     # pure fn, int 0–10000
       s >= score_threshold (7000) ? SCORED : REJECTED

4. [tailoring pass — budgeted]
   for job in top tailor_batch_size of status=SCORED (by score desc):
       tailored = tailor_once(jd, profile)         # one LLM call + guard validation
       pdf      = render(tailored, profile)         # deterministic: identity + resolved selection
       POST /jobs/{id}/artifacts ─► TAILORED ─► PENDING_APPROVAL
       on guard violation: log + fail cleanly, no render
   (remaining SCORED records wait for tomorrow's batch;
    SCORED untouched > stale_after_days ─► REJECTED)

5. [Telegram push]
   send(role, company, score, link, pdf, [Mark Applied] [Skip])

6. [user taps button]
   PATCH /jobs/{id}/status ─► APPLIED | USER_SKIPPED

7. [post-application, ongoing]
   user (Telegram chat): "got an interview with PUB" / "rejected by GovTech"
       ─► agent: query_jobs to resolve record ─► update_status
       ─► APPLIED → INTERVIEWING → OFFER → ACCEPTED | REJECTED
       (every transition stamps status_changed_at)

8. [follow-up job, daily — deterministic check, LLM only for drafting]
   for job where status=APPLIED
            AND status_changed_at older than follow_up_after_days
            AND follow_up_count = 0:
       draft = llm_draft_followup(role, company, applied_date)
       Telegram push: context + draft email + [Sent it] [Skip]
   [Sent it] ─► POST /jobs/{id}/follow-up
       (follow_up_count += 1; status stays APPLIED;
        ghost clock NOT reset — it runs on status_changed_at)

9. [lifecycle job, daily — no LLM, three rules on status_changed_at]
   PENDING_APPROVAL older than pending_expiry_days ─► EXPIRED
   SCORED          older than stale_after_days     ─► REJECTED
   APPLIED/INTERVIEWING older than ghost_after_days ─► GHOSTED + Telegram note
   (GHOSTED → INTERVIEWING allowed if the company resurfaces)

Phase 2 replaces step 6's manual apply:
6'. [Apply for me] ─► APPLYING ─► worker fills ATS form ─► APPLIED | APPLY_FAILED
    (auto_apply: true skips the approval push entirely)
```

---

## Tech Stack

| Layer | Technology | Status |
|---|---|---|
| Backend API | FastAPI, Pydantic v2, pydantic-settings | Keep |
| Database | SQLite (aiosqlite) → PostgreSQL later | Keep |
| Data access | Repository pattern, raw SQL | Keep |
| Agent | LiteLLM (`gemini/gemini-2.5-flash-lite`), ReAct loop | Keep + new tools |
| Scraping | httpx (MCF `POST /v2/search`; Careers@Gov Algolia) + httpx/BeautifulSoup (JobStreet) | **Replaces Tavily** |
| Scoring | Pure Python → embeddings (injected embedder) | Upgrade |
| Dedup | hashlib SHA-256 over normalized (company + title); `seen_count`/`last_seen_at` on duplicate hits | Changed inputs |
| Tailoring LLM | LiteLLM structured output → Pydantic-validated JSON | New |
| PDF rendering | Jinja2 `.tex` template + Tectonic/latexmk (alt: RenderCV) | New |
| Notifications/UI | python-telegram-bot (inline keyboards, document upload) | Extend |
| Apply automation | Playwright + browser-use, separate process | Phase 2 |
| Scheduling | APScheduler (FastAPI lifespan) | Keep |
| Logging | loguru | Keep |
| Testing | pytest, pytest-asyncio, httpx; mocked LLM/embedder/HTTP | Keep |
| Container | Docker | Keep |

---

## Build Order

| Step | Deliverable | Version |
|---|---|---|
| 1 | MCF adapter (`POST /v2/search`, Pydantic response model — fixes URL/JD quality immediately) | 0.6.0 |
| 2 | Careers@Gov adapter (Algolia, referer headers — see `careers_gov_adapter.py`) | 0.6.0 |
| 3 | FSM migration + artifacts table | 0.6.0 |
| 4 | Profile-derived keywords; CV → `profile.json` parse (superset; parse proposes `demonstrated_skills` per item, human-reviewed) | 0.6.0 |
| 5 | Tailoring service (LLM JSON + PDF renderer, TDD) | 0.6.x |
| 6 | Telegram approval flow (inline keyboards, PDF push) | 0.6.x |
| 7 | JobStreet adapter (HTML parser or internal JSON endpoints, fixture-based tests) | 0.7.0 |
| 8 | Embedding-based scoring (injected embedder) | 0.7.x |
| 9 | Application worker, approval-gated | 0.9.0 |
| 10 | `auto_apply` flag — full autonomy per source | 1.0.0 |

Note: MCF + Careers@Gov together cover the GovTech/GLC/public-service
targets with the highest data quality per line of code — both are clean
JSON adapters, live-verified. JobStreet broadens private-sector coverage
at the cost of parser maintenance. LinkedIn is excluded (robots.txt
disallows automated access — verified live).

---

## Invariants (carried over and extended)

1. The scraper never reasons. The agent never writes to the DB directly.
   All writes go through the backend API.
2. The LLM proposes (tool calls, content selection); code validates and
   executes (FSM, Pydantic schemas, renderer).
3. Every external dependency (LLM, embedder, HTTP, browser) is injected and
   mocked in tests; `live`-marked tests are the only exception.
4. The tailoring LLM may select and emphasise real profile content only —
   never invent experience. This is enforced **structurally**, not by prompt
   alone: selection is by reference (`ref_id`s resolve to real entries), and text
   rewrites are bounded by two deterministic guards (surfaced skills ⊆ the item's
   `demonstrated_skills`; no new specifics vs the source). The LLM controls
   phrasing, never claims.
5. No LinkedIn automation of any kind — its robots.txt disallows automated
   access (verified live, 2026-06-10), and authenticated automation risks
   the personal account.
6. A human approval gate sits before every application until `auto_apply`
   is deliberately enabled per source.
7. Every LLM-consuming stage is budgeted (`tailor_batch_size`, LiteLLM
   `rpm`/`tpm`); rules that need no judgement (dedup, thresholds, expiry,
   ghosting, the follow-up *check*) are deterministic code and never call
   the LLM. The LLM drafts content; clocks run on `status_changed_at`.
8. Telegram is transport only — it renders and relays, never decides. No
   business logic, no pipeline state, no conversation history in the
   Telegram layer.
9. The DB is the source of truth; the conversation transcript is disposable.
   Every durable fact lives in a column (reached via a tool like
   `update_status`), never solely in chat history. Wanting history to do more
   than reference-resolution is the signal a fact escaped into the transcript
   and belongs in the DB instead. This is what makes sessions safe to clear.
