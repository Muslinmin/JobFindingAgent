# Job Application Agent v2 — Architecture & Tech Stack

---

## What Changed From v1

| | v1 (current) | v2 (this design) |
|---|---|---|
| Discovery | Tavily search API (search-result URLs, weak JDs) | Per-portal scraper adapters (real listing URLs, full JDs) |
| Candidate data | `profile.json` shaped via chat | Canonical structured profile parsed once from CV; profile drives queries, scoring, and tailoring |
| Scoring | Keyword overlap vs static config keywords | Embedding cosine similarity (JD vs profile vector), from the start |
| Output | Job records in DB, queried on demand | Tailored CV PDF per job, pushed via Telegram |
| Autonomy | None | Phase 1: autonomous scrape + tailor. Phase 2: autonomous apply behind approval gate |
| Frontend | Telegram bot (pure transport) | Two Telegram bots (chat + notifications), still pure transport |
| Write path | HTTP endpoints | In-process `JobService` facade; only three thin HTTP routes survive |

Unchanged: FastAPI backend, repository pattern, SHA-256 fingerprint dedup
(mechanism kept; fingerprint *inputs* change — see § Deduplication),
LiteLLM ReAct agent, stateless `POST /chat`, APScheduler in lifespan hook,
TDD discipline, "scraper is dumb / reasoning lives in the agent /
all writes go through the backend's single validated path" (now the
in-process service facade, not HTTP endpoints).

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
  resolves the record via `find_jobs`, then calls `update_status`.
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
- `find_jobs` (the agent's single read tool) defaults its status filter by
  call shape: a **bare listing** (no title, no company — "what am I working
  on") defaults to the active pipeline and excludes terminal states
  (`GHOSTED`, `REJECTED`, `USER_SKIPPED`, `EXPIRED`), whereas a **named
  lookup** (a title and/or company was given) searches **all** statuses,
  since you may be recalling a job that has since been rejected or declined.
  Either default can be overridden with an explicit `status_set`
  ("show me everything that ghosted me").

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
┌───────────────┐                │            ┌───────────────────────┐
│ APScheduler   │                │            │ TWO Telegram bots (thin)│
│ (lifespan)    │                │            │ chat bot  ─► POST /chat │
│ clock; picks  │                │            │ notif bot ─► /action,   │
│ which records │                │            │             /follow-up  │
│ scrape·tailor │                │            │ separate channels →     │
│ follow-up·    │                │            │ no push-coexistence     │
│ lifecycle·    │                │            └───────────┬─────────────┘
│ digest        │                │           free-text only│ POST /chat
└───────┬───────┘                │                         ▼
        │ drives services        │            ┌───────────────────────┐
        │ directly               │            │ Agent — ReAct loop     │
        │                        │            │ reference resolution   │
        │                        │            │ owns conversation store│
        │                        │            │ (separate SQLite +     │
        │                        │            │  JSONL transcripts)    │
        │                        │            │ thin tool bindings     │
        │                        │            └───────────┬───────────┘
        │                        │  tool handlers call the │ SAME services
        │                        │  (in-process, no HTTP)  │
        ▼                        ▼                         ▼
┌──────────────────────────────────────────────────────────────────┐
│ Shared Services (JobService facade) — caller-agnostic; WORK HERE   │
│ scrape/ingest · score · tailor · follow-up draft · query regen ·   │
│ status transition       (only tailor / draft / regen call the LLM) │
└──────┬───────────────────────┬───────────────────────┬────────────┘
       │ writes                │ LLM / embed           │ fetch
       ▼                       ▼                       ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
│ Backend +        │  │ LLM (leaf dependency) │  │ Scraper adapters │
│ repository       │  │ single, stateless     │  │ MCF · C@G ·      │
│ jobs·artifacts·  │  │ completions           │  │ JobStreet        │
│ FSM·dedup        │  │ — also used directly  │  └──────────────────┘
│ (3 thin routes:  │  │   by the Agent loop   │
│  /chat, /action, │  └──────────────────────┘
│  /follow-up)     │   Conversation store (separate SQLite + JSONL)
└──────────────────┘   is owned by the Agent, NOT the backend or scheduler

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

**Implementation playbook:** `scraper_layer.md` — the work-package
breakdown (Careers@Gov, MCF, JobStreet), the JobSource contract, test
requirements, definition of done, and per-portal work orders. New adapters
are built by following that playbook; this section only records outcomes.

### 3. Backend API — service facade, thin HTTP surface

**Responsibility:** Source of truth for job records and artifacts. Every write
from every component goes through its single validated path. (Full spec:
`backend_v2.md`.)

**The service layer is the real interface, not HTTP.** The backend exposes a
`JobService` facade — an in-process object presenting one front door to the
validated operations (`ingest_job`, `transition_status`, `register_artifact`,
`record_follow_up`, `mark_follow_up_nudged`, and the read functions). It is
constructed once at the composition root (`main.py`) and injected via
dependency injection into its in-process callers — the scheduler, the agent's
tool handlers, and the tailoring consumer. There is **no self-HTTP**:
in-process callers invoke a plain method with no network hop.

**Exactly three thin inbound HTTP routes survive**, all for the separate
Telegram bot device (see § 6):
- `POST /chat` — free-text message → agent. The only place an LLM call can
  originate. Returns `{ reply, attachments? }` (attachments carry base64 file
  bytes, typically a tailored PDF).
- `POST /jobs/{job_id}/action` — a button tap → `transition_status`.
  Deterministic, bypasses the agent; body is `{ action: UserAction }`.
- `POST /jobs/{job_id}/follow-up` — a button tap → `record_follow_up`.
  Deterministic, record-only (never touches `status` or `status_changed_at`).

Every other operation the old CRUD endpoints exposed — artifact registration,
status filtering, ranked selection — is now an in-process facade call, not an
endpoint.

Schema changes (jobs database):
- `jobs.status` extended to the full pipeline FSM above.
- New lifecycle columns on `jobs`: `status_changed_at` (stamped on every
  status transition — the clock for follow-up/ghost/expiry rules),
  `follow_up_count`, `last_follow_up_at` (user follow-up activity, tracked
  separately so it never resets the ghost clock), `follow_up_nudge_at`
  (stamped only by the scheduler's `mark_follow_up_nudged` — "we reminded
  you", a distinct writer from "you followed up"), `seen_count` and
  `last_seen_at` (stamped on duplicate ingest — liveness signal for the
  lifecycle job).
- New `artifacts` table: `(id, job_id FK, kind ['cv_pdf','cover_letter','follow_up_email'], path, created_at)`.
  Registration is **append-only** — a new row on every call, never
  overwritten (see § 9, Artifact identity).

**Conversation persistence is NOT in this database.** Sessions and turns live
in a *separate* conversations database in a dedicated `conversation/` package,
owned by the agent alone (see § 7 and `conversation_store_v2.md`). The jobs
backend holds no `conversations`, `sessions`, or `pending_pushes` table — the
two-bot split (§ 6) deleted the held-push machinery entirely.

`UserAction` is strictly narrower than `Status`: a button tap can only target a
user-legal state (`APPLIED`, `USER_SKIPPED`, `INTERVIEWING`, `OFFER`,
`ACCEPTED`, `DECLINED`, `REJECTED`), never a system-only state like `SCORED` or
`TAILORED`.

Repository pattern unchanged — SQLite now, PostgreSQL later touches one file.

### 4. Scoring & Deduplication — pure functions

**Scoring responsibility:** JD text vs candidate profile → **integer 0–10000**
("basis points": internal float similarity × 10,000, rounded). Decides
`DISCOVERED → SCORED` (≥ `score_threshold`) vs `REJECTED`.

- **v2 scores with embeddings from the start** (single implementation:
  `EmbeddingScorer`). Cosine similarity between the JD's embedding vector and
  the candidate profile's vector — one number capturing semantic match. This
  is what makes **adjacent roles** rank correctly despite low literal keyword
  overlap. (Full spec: `scoring_v2.md`.)
- **Signature: `async def score(jd_text, candidate) -> int` (0–10000).**
  `async` because producing an embedding is a network call to the embeddings
  API (OpenAI). The embedder is injected so tests pass a mock (same seam
  pattern as `LLMClient` injection in the agent). The scorer runs **once, at
  discovery**. Tailoring does not re-score — the three-tier structural
  controls are the correctness guarantee (see § Tailoring Service).
- **The profile vector is cached on the `Scorer` instance**, keyed by a
  SHA-256 fingerprint of the profile text, so the profile is embedded once and
  reused across the whole scrape batch; a profile edit changes the fingerprint
  and invalidates the cache automatically.
- **Embedding-service failure raises — it never fabricates a zero.** On an API
  failure the scorer raises rather than returning a misleading `0`; the caller
  leaves the job at `DISCOVERED` so it is retried on the next run, instead of
  silently rejecting a good job on a transient network blip.
- **Discovery is a coarse filter by design.** `score_threshold` (7000) is
  deliberately **lenient and configurable** — "plausibly relevant, let it
  through." The daily budget (`tailor_batch_size`), not the threshold, is the
  real throttle.
- **Deferred (0.7.x):** mean-pooling the bloated profile superset into one
  vector can depress good-fit jobs in the top-N ranking even past a lenient
  gate; fix by ranking on profile *chunks* (top-k / max-pool) at 0.7.x.

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
- **NaN guard.** The one unit test that matters: a zero vector (empty or
  degenerate input) → score returns `0`, never NaN. (Note this is the
  *degenerate-input* zero; a *service failure* raises instead, per above.)

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
  DB constraint on `fingerprint`; the idempotent upsert lives inside the
  `ingest_job` facade call (in-process, no HTTP). Adapters never dedup
  (invariant 1).
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

Artifacts are written to disk, registered via the `register_artifact` facade
call (in-process, append-only — see § 9, Artifact identity).

Testing: mock the LLM, assert schema validity; snapshot-test the renderer.
Additional guards (see `tailoring.md` § invariants): every `ref_id` resolves to a
real profile entry; skill terms surfaced in tailored text ⊆ that item's
`demonstrated_skills`; no new numerals/named entities vs the source item. Guard
violation → logged, job marked failed, no partial render.

**Build plan:** `tailoring_build.md` is the build playbook for this service. It
holds the work-package breakdown (WP0–WP7: schemas → guards → LLM call →
orchestration → renderer → artifact registration → entry points, with the build
order `WP0 → {WP2,WP3,WP5} → {WP4,WP6} → WP7`) and the test-case catalog that
serves as the executable specification, where each `TC-*` case traces back to a
`tailoring.md` section or invariant and is tagged as a unit, integration, or
`live` test. A signature-only code skeleton (the `tailoring/` package plus
`agent/tools/tailor_resume.py`, each definition commented with the `TC-*` ids and
`tailoring.md` section it satisfies) is generated from that plan. This section
records the design; the service is built by following the plan. The split is the
same one used elsewhere: `tailoring.md` is the decision record, and
`tailoring_build.md` is the build playbook, mirroring how § Scraper Layer pairs
with `scraper_layer.md`.

### 6. Telegram Bot — transport layer (two bots)

**Core principle:** Telegram is transport only. It moves messages between the
user and the backend and renders what it is handed. It holds **no business
logic, no pipeline state, and no conversation history**. Every decision about
*what* to say, *which* job a reply refers to, or *when* a session begins lives
above it (agent + conversation store). If a behaviour requires a decision, it
does not belong in this layer. (Full spec: `telegram_v2.md`.)

**Two separate bots, each with its own token and its own polling loop** — this
is the structural change that deletes the old push-coexistence problem
(§ 8), because notifications and conversation now live on two physically
different channels and can never collide on one surface:

- **Chat bot** — the agent's channel. Receives free-text from the user, calls
  `POST /chat`, and delivers the agent's `reply` back, plus any attachment
  (typically a tailored PDF, base64-decoded from the response) as a document.
  Nothing else.
- **Notifications bot** — the scheduler's outbound channel. Pushes approval
  cards, follow-up cards, digests, and ghost notices, and receives the button
  taps those cards generate (`POST /jobs/{id}/action` and
  `POST /jobs/{id}/follow-up`). No free-text.

Both bots are fully stateless: after the two-bot split neither holds any
in-memory state or database.

#### What the Telegram layer IS responsible for

1. **Inbound transport.** Chat bot: free-text → `POST /chat`. Notifications
   bot: a button tap → `POST /jobs/{id}/action` (carrying an opaque
   `UserAction` string copied verbatim from the callback data) or
   `POST /jobs/{id}/follow-up` (empty body). The callback payload carries the
   `job_id`, so button actions need no conversation context and are always
   unambiguous.
2. **Outbound transport.** Send scheduler-produced messages through the
   notifications bot: text, PDF/document attachments, inline keyboards.
3. **Telegram-flavoured rendering only.** Turn already-decided content into
   Telegram markdown, button layouts, and document uploads — *rendering*,
   never *structuring*.
4. **Command surface.** The chat bot exposes only `/start`, which sends a
   static greeting locally and signals **nothing** to the backend. **There is
   no `/end` command** and no session endpoint — session boundaries are the
   agent's own lazy inference from `/chat` traffic (§ 7).

#### What the Telegram layer is NOT responsible for

- **Not** conversation history or sessions — it never stores or appends turns,
  and it sends no session-open/close/idle signals (see § 7).
- **Not** deciding which job a free-text reply refers to — the agent resolves
  references from session context.
- **Not** content structuring — digest contents, follow-up wording, push copy
  are produced upstream (template or agent) and handed down as finished text.
- **Not** the FSM — on a button tap the bot carries the opaque `UserAction`
  string into the request body; it never imports the enum, validates
  membership, or maps actions to edges. All server-side.
- **Not** pipeline state — job status is authoritative in the DB; Telegram
  reflects it, never holds it.

#### Push notifications (system-initiated messages)

Produced by scheduled jobs and pushed through the notifications bot. Each
carries a self-labelling header (role + company) so it is interpretable out of
conversational context.

- **Tailored job ready** (`PENDING_APPROVAL`): role, company, score, listing
  URL, tailored CV PDF, inline keyboard.
  - Phase 1 buttons: `[Mark Applied]` → `action: APPLIED`, `[Skip]` →
    `action: USER_SKIPPED`, both via `POST /jobs/{id}/action`.
  - Phase 2 buttons: `[Apply for me]` `[Skip]`.
- **Follow-up draft** (`APPLIED` + `follow_up_after_days`, none sent yet):
  context line + LLM-drafted email text (copy-paste ready) + `[Sent it]`
  `[Skip]`. `[Sent it]` → `POST /jobs/{id}/follow-up`.
- **Auto-ghost notice** (informational): one line, no buttons.
- **Weekly digest** (informational): pipeline summary.

Because the two bots are separate channels, a push never has to yield to an
in-progress chat — there is no hold, queue, or flush (§ 8).

### 7. Conversation Sessions & History

History exists for exactly one job: **resolving references** ("that one",
"the third", "make it more formal") across a handful of recent turns. It is
**not** a memory of the job search — every durable fact lives in the jobs DB
and is queried live. This narrow mandate is what lets a stale session simply
fall out of scope without losing anything real. (Full spec:
`conversation_store_v2.md`.)

**The conversation store is a separate concern owned by the agent alone**, not
part of the jobs backend. It is a dedicated `conversation/` package with:
- a **second SQLite database** holding one `sessions` table —
  `(id UUID, started_at, last_activity_at, transcript_path)`. No `chat_id`
  (single user), and crucially **no `ended_at`** — see below.
- **JSON Lines transcript files**, one file per session, holding the turns
  (each line is one `Turn` = `{role, content, created_at}`). Appending a turn
  appends a single line; the path is derived from the session id, so no DB
  lookup is needed to find it.
- a `ConversationStore` facade injected into the agent, exposing exactly four
  methods: `start_session`, `get_latest_session`, `append_turn`,
  `load_history`. The scheduler receives **no** reference to it.

Three responsibilities, three homes (dependency arrows point downward only):

- **Storage — conversation store.** Durable, so an app restart mid-session
  loses nothing. Storage knows nothing about windowing or the LLM.
- **Assembly — agent layer.** The agent loads a session's turns and assembles
  them into the LLM messages array; the ReAct loop never trims inline.
  Compaction (summarise-on-eviction) is a seam inside the agent's context
  module, a no-op in v1: policy is trivially **load the whole current
  session**, because idle bounding keeps sessions short.
- **Reference-resolution — emergent.** No component of its own: with recent
  turns in the assembled context, the LLM resolves "that one" during normal
  inference.

**Sessions — no end event.** A session is just a bounded window of recent turns
the agent reads to build context. There is no `/start` open, no `/end` close,
and no scheduled flush. Instead the agent decides **continue-vs-new lazily on
each `/chat` call**: it asks the store for the latest session, and if that
session's `last_activity_at` is older than `session_idle_minutes` (an **agent**
config value, not the store's and not the scheduler's), it starts a fresh
session; otherwise it continues the latest one. A stale session is never
reused, so it never has to be closed — it simply falls out of scope when the
next message opens a new one. This is also what expires a stale `PENDING_ACTION`
marker (§ 9): once a new session starts, the old turns are no longer in the
loaded history.

The endpoint stays stateless: the agent rehydrates the session from the store
on every `POST /chat` call.

### 8. Push Delivery (plain — no coexistence gate)

The two-bot split (§ 6) **deletes** what used to be the hard problem here.
Because notifications go out on the notifications bot and conversation happens
on the chat bot, a scheduled push can never interleave with an in-progress
chat on the same surface. There is therefore **no** push-coexistence gate, no
`last_activity_at` signal read by the scheduler, no held-push queue, no
coalesced nudge, and no flush step. All of that is **deleted, not deferred**.

What remains is plain push delivery: a scheduled job formats its message and
sends it straight through the notifications bot via the Telegram API. The only
thing that comes *back* to the backend is the button reply those cards
generate, which arrives through `POST /jobs/{id}/action` or
`POST /jobs/{id}/follow-up` — never through `/chat`. The scheduler and the
conversation store never touch.

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
proposes → backend validates → repository executes. Full spec (schemas, I/O
contracts, reference-resolution eval set): `agent_v2.md`.

**Ten tools.** Each is a thin LLM-facing binding over a caller-agnostic service:

| Tool | Purpose |
|---|---|
| `find_jobs` | **The single read tool.** With a `job_title` it is a named lookup (case-insensitive *partial* match, optionally narrowed by `company`); without one it is a status listing ("what's in my pipeline"). Recency-ordered (`status_changed_at` desc, then `id` asc) — that fixed order is what makes "the third one" deterministic. Replaces the former `query_jobs`. |
| `search_jobs` | Runs scraper adapters and **ingests** the results (dedup → DISCOVERED → scored); not a preview. |
| `score_job` | Assess-only: scores a pasted JD against the profile, writes nothing. |
| `score_ingest` | Record-and-score: upsert the job, then score it — but **skip re-scoring** when the existing row already carries a score (saves embedding credits). |
| `update_status` | Propose an FSM transition (stamps `status_changed_at`; the backend rejects illegal moves). |
| `tailor_resume` | Trigger tailoring for one job; produces + registers a `cv_pdf` artifact. Does **not** move the FSM (see two-turn confirmation). |
| `draft_followup` | Draft a follow-up email for one job (`follow_up_email` artifact). Drafting ≠ sending — never touches status or `follow_up_count`. |
| `draft_cover_letter` | Generate a cover letter for one job (`cover_letter` artifact, plain text). |
| `regenerate_queries` | Rebuild `search_queries.json` from the current profile (same service the scheduler's weekly `query_regen` calls). |
| `update_profile` | The only source-of-truth mutator and the only two-phase write: first call returns a diff and writes nothing; `confirmed=True` commits. |

**`log_job` is removed.** There is deliberately no record-only tool: every job
that enters the database must be scored, so a bare "just record this" request
is declined in favour of `score_ingest`. Mutating tools take an already-resolved
`job_id` — the entire 0/1/N ambiguity surface lives in the `find_jobs`
resolution flow, not smeared across the tools.

**Every tool is a thin binding, not the work itself** — a function-calling
*schema* plus a *handler* that parses args, calls the service, and marshals the
result. For any capability the scheduler also drives (tailoring, scrape/ingest,
follow-up drafting, query regeneration, status transitions), the scheduled job
and the agent handler call the **same** service, whose signature carries no
notion of its caller. The agent owns the schema and handler; it never owns the
work, session policy is its own but session *storage* is the conversation
store's (§ 7).

#### Agent mechanics (three that shape the FSM and transport)

- **Artifact identity — append-only in the DB, single-live-file on disk.**
  `register_artifact(job_id, {kind, path})` inserts a **new row on every call**
  and never overwrites, so the table keeps the full history (`kind ∈ {cv_pdf,
  follow_up_email, cover_letter}`). The "one current file per job" property is
  enforced on the **filesystem**: before writing a new tailored resume the tool
  backs up any existing file to a timestamped `.bak` name, then writes the new
  one in place. The return shape gains `replaced: bool`.
- **Two-turn confirmations.** Both `update_profile` (diff → commit) and
  tailoring (show PDF → transition) span two user turns. Because the agent is
  stateless per call, the pending action is stored as a compact machine-exact
  marker embedded in the agent's own assistant turn, delimited so the bot
  strips it before display: `<<<PENDING_ACTION {…}>>>`. On the next call the
  agent finds the most recent unresolved marker and, on an affirmation, replays
  the stored payload verbatim. The marker lives *inside the turn text*, not as a
  new field on the `Turn` model, so the agent never reaches into the
  conversation store's schema. A stale marker expires naturally when a new
  session starts (§ 7).
- **The SCORED→PENDING_APPROVAL move is the tailor caller's, not the tool's.**
  `tailor_resume` only produces and registers the artifact. The scheduler
  advances the FSM in the daily batch; the agent advances it on demand **only
  after the user accepts** — the second turn of the confirmation above.
- **`POST /chat` transport.** In: `{ message }`. Out: `{ reply, attachments? }`,
  where each attachment is `{ filename, mime_type, content_b64 }` (raw bytes as
  base64 so a binary can ride inside JSON). The chat bot decodes it and forwards
  it to Telegram as a document. Every file the agent makes must ride out in this
  single response — the agent holds no Telegram client.

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
| `resume-ats-optimizer` | discovery scorer (keyword model) + text-tier guard | Keyword taxonomy (hard / soft / industry), match-score logic, placement priority (summary → skills → bullets), ATS formatting rules. It plays two roles, both real under the single-pass design. First, it sharpens the discovery scorer, which is the early score that decides whether a job is worth tailoring for at all. Second, its keyword detector powers the text-tier guard, which checks that every skill surfaced in the tailored wording is one the candidate actually listed for that experience (surfaced skills ⊆ item's `demonstrated_skills`). It is not a separate gate, and there is no post-tailoring re-score. See § Tailoring Service. |
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
  JSON → `.tex` → compile → PDF → register via the `register_artifact` facade call.
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

**Fast loop (deterministic, daily):** the scrape job reads
`search_queries.json` (fresh each run, so weekly regeneration takes effect
without a restart), fans out across adapters, ingests **in-process** via the
injected `JobService` facade (`service.ingest_job(...)` — dedup happens here,
no self-HTTP), scores inline, and transitions to SCORED/REJECTED. **Zero LLM
calls for discovery/ingest** (scoring uses the embeddings API, not the chat
LLM) — cheap, deterministic, testable as plain functions (no scheduler in
tests). Full spec: `scheduling_v2.md`.

**Six scheduled jobs** (all plain async functions, tested without the
scheduler), run in this daily order so each stage sees the previous stage's
results: `query_regen` (Mon only, before scrape) → `scrape+score` →
`lifecycle` → `follow_up` → `tailor` → `digest` (Mon only, last).

| Job | Cadence | LLM? | Does |
|---|---|---|---|
| scrape | daily | no | queries → adapters → `ingest_job()` → score inline → SCORED/REJECTED |
| lifecycle | daily | **no** | three deterministic time rules on `status_changed_at`: `PENDING_APPROVAL > pending_expiry_days → EXPIRED`; `SCORED > stale_after_days → REJECTED`; `APPLIED/INTERVIEWING > ghost_after_days → GHOSTED` (+ ghost notice) |
| follow-up | daily | yes (small N) | `APPLIED` past `follow_up_after_days`, not yet nudged → draft email → push with `[Sent it] [Skip]`; stamps `follow_up_nudge_at` |
| tailor | daily (after scrape) | yes (budgeted) | top-`tailor_batch_size` SCORED by score → tailor → PDF → PENDING_APPROVAL → push |
| query_regen | weekly (Mon) + on-demand | yes (1 call) | profile → `search_queries.json` (backup-on-change; also backs the agent's `regenerate_queries` tool) |
| digest | weekly (Mon) | **no** (default) | pipeline summary: active states, follow-ups pending, recent ghosts/expiries |
| apply (P2) | poll | per-form | `APPLYING` records → ATS form-fill → APPLIED/APPLY_FAILED |

**There is no seventh session-flush job.** The two-bot split (§ 6, § 8)
removed the held-push queue and the idle-flush it existed to service; session
idle is now checked lazily by the agent on each `/chat` call (§ 7), not by the
scheduler.

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
   for query in search_queries.json:          # re-read fresh each run
       for adapter in [MCF, CareersGov, JobStreet]:
           raw = adapter.fetch(query)          # no LLM
           service.ingest_job(...)             # in-process; normalise → dedup → DISCOVERED

3. [same run]
   for job in status=DISCOVERED:
       s = await scorer.score(job.description, profile)   # embeddings, int 0–10000
       s >= score_threshold (7000) ? SCORED : REJECTED

4. [tailoring pass — budgeted]
   for job in top tailor_batch_size of status=SCORED (by score desc):
       tailored = tailor(jd, profile)              # one LLM call + guard validation
       pdf      = render(tailored, profile)         # deterministic: identity + resolved selection
       service.register_artifact(...) ─► service.transition_status(PENDING_APPROVAL)
       on guard violation: log + fail cleanly, no render
   (remaining SCORED records wait for tomorrow's batch;
    SCORED untouched > stale_after_days ─► REJECTED)

5. [notifications bot push]
   send(role, company, score, link, pdf, [Mark Applied] [Skip])

6. [user taps button]
   POST /jobs/{id}/action  {action: APPLIED | USER_SKIPPED}

7. [post-application, ongoing]
   user (chat bot → POST /chat): "got an interview with PUB" / "rejected by GovTech"
       ─► agent: find_jobs to resolve record ─► update_status
       ─► APPLIED → INTERVIEWING → OFFER → ACCEPTED | REJECTED
       (every transition stamps status_changed_at)

8. [follow-up job, daily — deterministic check, LLM only for drafting]
   for job where status=APPLIED
            AND status_changed_at older than follow_up_after_days
            AND not yet nudged (follow_up_nudge_at IS NULL):
       draft = llm_draft_followup(role, company, applied_date)
       notifications push: context + draft email + [Sent it] [Skip]
       mark_follow_up_nudged(...)               # stamps follow_up_nudge_at
   [Sent it] ─► POST /jobs/{id}/follow-up
       (follow_up_count += 1; status stays APPLIED;
        ghost clock NOT reset — it runs on status_changed_at)

9. [lifecycle job, daily — no LLM, three rules on status_changed_at]
   PENDING_APPROVAL older than pending_expiry_days ─► EXPIRED
   SCORED          older than stale_after_days     ─► REJECTED
   APPLIED/INTERVIEWING older than ghost_after_days ─► GHOSTED + Telegram note
   (GHOSTED → INTERVIEWING allowed if the company resurfaces)

Phase 2 replaces step 6's manual apply:
6'. [Apply for me] ─► POST /jobs/{id}/action {action: APPLYING}
    ─► worker fills ATS form ─► APPLIED | APPLY_FAILED
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
| Scoring | Embedding cosine similarity (injected async embedder; OpenAI), profile vector cached | New |
| Dedup | hashlib SHA-256 over normalized (company + title); `seen_count`/`last_seen_at` on duplicate hits | Changed inputs |
| Tailoring LLM | LiteLLM structured output → Pydantic-validated JSON | New |
| PDF rendering | Jinja2 `.tex` template + Tectonic/latexmk (alt: RenderCV) | New |
| Notifications/UI | python-telegram-bot, **two bots** (chat + notifications), inline keyboards, document upload | Extend |
| Conversation store | separate SQLite DB + JSON Lines transcripts, agent-owned | New |
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
| 4 | CV → `profile.json` parse (superset; parse proposes `demonstrated_skills` per item, human-reviewed) + embedding scorer (injected async embedder, cached profile vector) | 0.6.0 |
| 5 | Tailoring service (LLM JSON + PDF renderer, TDD) — build plan in `tailoring_build.md` (WP0–WP7, tests-as-spec) | 0.6.x |
| 6 | Two-bot Telegram layer + approval flow (inline keyboards, PDF push) | 0.6.x |
| 7 | JobStreet adapter (HTML parser or internal JSON endpoints, fixture-based tests) | 0.7.0 |
| 8 | Chunk-level scoring refinement (top-k / max-pool over profile chunks) | 0.7.x |
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
   All writes go through the backend's single validated path — the in-process
   `JobService` facade (no self-HTTP). Only three thin inbound routes exist
   (`/chat`, `/jobs/{id}/action`, `/jobs/{id}/follow-up`).
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
   business logic, no pipeline state, no conversation history in either bot.
   Two separate channels (chat + notifications) mean a push never has to yield
   to an in-progress chat, so there is no push-coexistence gate, held-push
   queue, or flush.
9. The jobs DB is the source of truth; the conversation transcript is
   disposable. Every durable fact lives in a column (reached via a tool like
   `update_status`), never solely in chat history. Wanting history to do more
   than reference-resolution is the signal a fact escaped into the transcript
   and belongs in the DB instead. A session is never explicitly closed — it
   simply falls out of scope when, on a later `/chat` call, the agent finds it
   idle and starts a fresh one; nothing real is lost because nothing real lived
   there.