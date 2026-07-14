# Profile Layer — Planning Document (v2)

Status: planning complete, pre-implementation. Introduces `target_tracks`,
the tier-tagged schema, load-time invariant validation, and the typed-operation
mutator contract.
Scope: the profile layer only. How each consumer *uses* the projection it
receives is out of scope and belongs to that consumer's spec. The seams that
*are* documented here are the exact projections each consumer is entitled to —
see § Integration.

This document is the source of truth for the profile layer. It follows the same
five-step planning shape as the other layer specs: scope boundary, requirements,
input/output contract, work packages, build order.

---

## Purpose

The profile layer answers one question: **what is true about the candidate?**

It owns `profile.json` — the single source of truth — and hands out projections
of it to every other layer. It does not score, does not tailor, does not scrape,
and does not render. Its job is to guarantee that every layer downstream
receives a candidate profile that is **already known-good**, so that no
downstream failure is ever ambiguous about whether the profile was at fault.

The profile is a deliberate **superset**: it holds every experience, every
project, every skill — including ATS-variant surface forms of the same real
competence (`REST` / `RESTful`, `ROS` / `Robot Operating System`). Downstream
layers only ever *select* from it. Nothing downstream may introduce a fact that
is not already here.

---

## Step 1 — Scope Boundary

### What the layer is

Five things, and nothing else:

1. **The schema.** `Profile`, `ProfileItem`, `Education`, and the typed
   operations that mutate them. Every field carries a **tier tag** declared on
   the Pydantic model itself.
2. **Loading and validating.** `load_profile()` reads the file, parses it into a
   `Profile`, and enforces the structural invariants. It raises on violation.
3. **Projections.** Each consumer gets a different, purpose-built view:
   `profile_summary()` for the agent, `profile_to_text()` for the scorer,
   `index_view()` for query regeneration. The full object goes to tailoring.
4. **Mutation.** `update_profile()` is the **sole mutator** of `profile.json`.
   No other code path writes that file.
5. **Review helpers.** `orphan_skills()` surfaces skills tied to no item, for
   human review.

### What the layer is NOT (out of scope)

- **It does not generate search queries.** `llm_generate_queries()` lives in the
  scheduling layer (WP-S6). The profile layer supplies the *input* to it
  (`index_view()`) and knows nothing about `search_queries.json`.
- **It does not embed anything.** The scorer owns the embedding call and the
  fingerprint cache. The profile layer only supplies `profile_to_text()`.
- **It does not decide what a good target track is.** It can *advise* (Step 3),
  but it never blocks a write.
- **It does not parse a CV.** CV → profile is a one-off bootstrap step performed
  by a human with LLM assistance, and its output is human-reviewed before it
  becomes `profile.json`. It is not a runtime code path.
- **It does not manage artifacts.** Rendered CVs and cover letters belong to the
  backend's artifact registry.

### Deliberately excluded from this layer's design

- **Per-skill proficiency.** There is no depth field. `demonstrated_skills` is a
  *permission list*, not a rating. A future `proficiency` seam is named in
  § Deferred seams.
- **Alias selection policy.** The tailorer may surface any of a skill's
  `surfaces()`. *Which* one it picks for a given JD — exact-match the posting's
  spelling, else fall back to `label` — is a tailoring-layer decision, not a
  profile-layer one. The profile layer only guarantees the alternatives are
  linked. **Not built here.**
- **Per-track weighting.** `target_tracks` is a flat, unweighted list in v2.
- **Multi-profile support.** One candidate, one `profile.json`. The single-user
  scope makes this correct, not merely convenient.

---

## Step 2 — Requirements

1. **The profile is a superset, and selection happens downstream.** The layer
   never curates, hides, or drops content for being off-topic. Every truthful
   item lives here; the scorer, tailorer, and agent decide what to do with it.

2. **The base bullet text must be literally true.** `ProfileItem.bullets` is the
   frozen factual base. Every tailored rewrite is a constrained rephrasing of
   these lines, so an overclaim here propagates into **every CV the system will
   ever generate**. This is not a stylistic preference — it is the reason the
   whole superset-plus-guards design is sound. *(This requirement is not
   hypothetical: a bullet describing "XR interactive components on an extended
   reality platform" was found to describe work that was actually a UI inside a
   VR application. It was rewritten. The base text is the only place such an
   error can be corrected.)*

3. **`demonstrated_skills` is a permission list, not an endorsement of depth.**
   A skill may be surfaced on an item **only** if it appears in that item's
   `demonstrated_skills`. This is what blocks false recombination — "I have
   leadership" plus "I did project X" becoming "I led team X". It is
   human-authored, or parse-proposed and then human-reviewed, but **never
   inferred at tailoring time**.

   The corollary is easy to get wrong: **removing a skill from an item to signal
   modest depth is a bug, not restraint.** If a bullet says "exposing Flask REST
   APIs" but the item is not tagged with `Flask`, the text guard will reject a
   tailored rewrite that preserves the candidate's own true sentence. Depth is
   communicated through `summary_seed` and skill ordering, never by deleting
   true permissions.

4. **Invariants are enforced at load, not at use.** Tailoring's guards assume
   every `ref_id` resolves and that `demonstrated_skills ⊆ skills`. If those are
   only checked at tailor time, a guard violation is ambiguous: bad LLM output,
   or bad profile? Validating at load collapses that ambiguity — **a guard
   violation at tailor time then means exactly one thing: the LLM misbehaved.**

5. **Tier tags live on the schema.** Each field declares its context tier once,
   on the Pydantic model. `profile_summary()` is then a mechanical filter that
   reads tiers off the schema and can never drift from it.

6. **`update_profile` is the sole mutator, and its operations are typed.** A
   free-form patch dictionary cannot express whether a list write means *append*
   or *replace*. A discriminated union of named operations makes that
   unambiguous at the type level rather than by convention.

7. **Every field that can be written can be unwritten.** Add-only mutation is
   insufficient. Requirement 2's correction was a *rewrite*; an abandoned target
   track must be *removable* or it generates scrape queries forever.

8. **The layer advises but never blocks.** `target_tracks` is *intent, not
   history* — it is the one field explicitly permitted to outrun the evidence. A
   coherence gate would block precisely the career-pivot case the field exists to
   serve.

9. **A profile write must never be lost to a downstream failure.** The profile
   commits first and independently; query regeneration is a separate,
   independently-failable step. (Failure isolation, as in the scorer.)

---

## Step 3 — Input / Output Contract

### Schema

```python
IDENTITY, INDEX, BODY, RENDER = "identity", "index", "body", "render"

class Skill(BaseModel):              # ONE real competence, many spellings
    id: str                          # "rest_api" — what demonstrated_skills holds
    label: str                       # "REST API" — default surface form
    aliases: list[str]               # ["RESTful API", "REST"] — ATS variants,
                                     #   the SAME competence, not related skills

class Education(BaseModel):
    id: str
    institution: str                 # tier: identity  ("school")
    degree: str                      # tier: identity
    graduation_date: str | None      # tier: identity
    date_range: str | None           # tier: render
    notes: str | None                # tier: render

class ProfileItem(BaseModel):        # experience or project — same shape
    id: str                          # TailoredItem.ref_id resolves to this
    title: str                       # tier: index
    organization: str | None         # tier: index
    date_range: str | None           # tier: index
    bullets: list[str]               # tier: body — FROZEN FACTUAL BASE TEXT
    demonstrated_skills: list[str]   # tier: body — SKILL IDS. PERMISSION LIST.

class Profile(BaseModel):
    # identity tier — frozen, never LLM-touched
    name: str                        # tier: identity
    location: str | None             # tier: identity
    years_of_experience: float | None  # tier: identity
    candidate_status: str | None     # tier: identity
    education: list[Education]       # tier: identity

    # render-only — CV fixed slots, never enter agent context
    email: str                       # tier: render
    phone: str | None                # tier: render
    links: list[str]                 # tier: render

    # content superset — what the LLM selects and reorders from
    summary_seed: str | None         # tier: body
    experiences: list[ProfileItem]   # tier: index
    projects: list[ProfileItem]      # tier: index
    skills: list[Skill]              # tier: index

    # intent, not history
    target_tracks: list[str]         # tier: index
```

**Tier semantics** (per `agent_v2.md` §5):

| Tier | Projection into agent context |
|---|---|
| `identity` | verbatim |
| `index` | `{id, label}` per item — bodies dropped |
| `body` | **dropped** — loaded just-in-time by the tailoring service |
| `render` | **never enters agent context** — CV fixed slots only |

### Why skills carry ids, and aliases

`agent_v2.md` §7 resolves a phrase to a `skill_id`. `tailoring.md` describes
`skill_order` and `demonstrated_skills` as holding *skill ids*. **Three specs
already assume skills have ids.** Modelling `skills` as bare strings makes the
string do double duty as both identifier and display label — and that breaks the
one job the skills superset exists to do.

The superset deliberately holds ATS surface variants: `REST API`, `RESTful API`,
`REST`. These are **not three competences.** They are one competence with three
spellings. As bare strings they are unrelated, so:

- an item's `demonstrated_skills` must list all three separately (bloat);
- `remove_skill("REST API")` leaves `RESTful API` and `REST` behind;
- the tailorer should surface **one** spelling — whichever the job posting uses —
  but nothing tells it the three are alternatives, so it may emit all three on one
  CV, which reads as keyword stuffing;
- "the REST skill" resolves to three candidates, forcing a §7 clarify over a
  distinction that is not real.

The `Skill` model collapses this: one competence, one id, many spellings.
`demonstrated_skills` then genuinely holds **ids**, as all three specs already
claim. `resolve_surface(profile, "RESTful API") -> rest_api` is what lets the
tailorer choose the spelling the JD actually uses, and lets the text guard check
a surfaced term against the item's permission list.

*(Migration result on the real profile: 134 bare strings collapsed to 105 real
competences with 53 aliases folded in. The capstone item's `demonstrated_skills`
fell from 37 entries to 29.)*

### Load-time invariants

```python
def validate_invariants(p: Profile) -> None   # raises ProfileInvariantError
```

1. **Item ids are unique across experiences *and* projects.** `TailoredItem.ref_id`
   is resolved in a single flat namespace, so a collision across the two lists is
   as fatal as one within a list.
2. **Skill ids are unique.**
3. **No surface string is claimed by two skills.** If `REST` belonged to both
   `rest_api` and some other competence, `resolve_surface()` would be
   non-deterministic and the tailorer could not know which competence a JD keyword
   refers to.
4. **Every id in `demonstrated_skills` resolves to a real `Skill`.** A dangling id
   would cause the tailoring text guard to reject legitimate output.

**Orphan skills are legal and are NOT an invariant.** A skill demonstrated by no
item may still be listed on a CV; it simply can never be woven into a bullet.
`orphan_skills()` surfaces them for review. In practice an orphan usually means
*an item is missing from the profile*, not that the skill is false.

### Public surface

| Function | Signature | Consumer |
|---|---|---|
| `load_profile` | `(path) -> Profile` | everyone, at entry |
| `validate_invariants` | `(Profile) -> None`, raises | `load_profile`, internally |
| `profile_summary` | `(Profile) -> str` | agent, every turn |
| `profile_to_text` | `(Profile) -> str` | scorer (WP3) |
| `index_view` | `(Profile) -> IndexView` | query regen (WP-S6) |
| `update_profile` | `(op: ProfileOp, confirmed: bool) -> UpdateResult` | agent tool — **sole mutator** |
| `orphan_skills` | `(Profile) -> list[Skill]` | human review |
| `get_item` | `(Profile, item_id) -> ProfileItem \| None` | op construction, guard resolution |
| `get_skill` | `(Profile, skill_id) -> Skill \| None` | op construction |
| `resolve_surface` | `(Profile, surface) -> Skill \| None` | tailorer, text guard |
| `find_items` | `(Profile, phrase) -> list[ProfileItem]` | agent reference resolution (§7) |
| `find_skills` | `(Profile, phrase) -> list[Skill]` | agent reference resolution (§7) |
| `items_demonstrating` | `(Profile, skill_id) -> list[ProfileItem]` | advisory, review |

**`find_*` returns a LIST, never a single best guess.** `agent_v2.md` §7 defines
*salient* as **exactly one** candidate; zero or many means **clarify, never
assume** — no recency-pick, no position-pick. A lookup that collapsed to one best
match would silently destroy that rule. The 0/1/N decision belongs to the LLM, so
the lookup hands it every candidate intact.

### The typed operation contract

A free-form `patch` dict is ambiguous about list semantics. "Add robotics QA to
my target tracks" could be emitted by an LLM as a whole-field replacement,
silently destroying the other tracks. The `.bak` file would preserve them, but
only if someone noticed.

The mutation surface is therefore a **discriminated union** — a set of named
operations, each validated by Pydantic:

```python
class AddTargetTrack(BaseModel):
    op: Literal["add_target_track"]
    track: str

class RemoveTargetTrack(BaseModel):
    op: Literal["remove_target_track"]
    track: str

class AddSkill(BaseModel):
    op: Literal["add_skill"]
    skill: Skill                      # id + label + aliases
    demonstrated_by: list[str] = []   # item ids that genuinely exercised it

class RemoveSkill(BaseModel):
    op: Literal["remove_skill"]
    skill_id: str                     # cascades: stripped from every item too

class AddItem(BaseModel):
    op: Literal["add_item"]
    kind: Literal["experience", "project"]
    item: ProfileItem

class EditBullets(BaseModel):
    op: Literal["edit_bullets"]
    item_id: str
    bullets: list[str]                # full replacement — the diff is what is approved

class TagSkill(BaseModel):
    op: Literal["tag_skill"]
    item_id: str
    skill_ids: list[str]              # add to that item's demonstrated_skills

ProfileOp = Annotated[
    AddTargetTrack | RemoveTargetTrack | AddSkill | RemoveSkill
    | AddItem | EditBullets | TagSkill,
    Field(discriminator="op"),
]
```

`add_target_track` now means *append*. It cannot mean anything else.

`EditBullets` and the `Remove*` operations exist because **add-only mutation is
insufficient** (Requirement 7). The XR correction was a rewrite, not an addition.

**Every operation is a full-field replacement at the point of write, and the
agent constructs the complete new value.** Rationale: the diff is what the human
approves, and a diff of a full replacement is unambiguous to read. An
append-semantics diff shows `+robotics QA` while hiding what it is being
appended to.

### Advisory, never blocking

`add_target_track` and `add_item` return an **advisory** alongside the diff. The
advisory does not gate the write.

The reason for the advisory is *not* coherence policing — it is a concrete,
mechanical consequence in the scoring layer:

> The scorer compares each job against **one vector** built from the whole
> profile text. A target track with no supporting skills produces queries, which
> produce scraped jobs, which are then scored against a profile containing **no
> evidence** for that track — so they score low and never clear the gate. The
> track is silently inert, having cost queries, scrapes, and embedding calls.

So the advisory names that consequence and lets the human decide:

> *Adding "backend engineering" — 3 skills could support it (`REST API`,
> `Flask`, `Python`), all on one item. Jobs found on this track will score
> against a profile whose evidence is mostly robotics, so expect low scores that
> may not clear the gate. Add anyway?*

Blocking would be the model overruling the candidate about their own career.

### Write sequencing — profile first, queries second

Adding a target track must refresh `search_queries.json`, or the daily scrape
keeps searching the old tracks until Monday — silently, with no error.

The order is load-bearing:

```
1. Build op → compute diff + advisory → show to user   (PENDING_ACTION marker)
2. User confirms
3. Backup profile.json → write profile.json → COMMIT   ← independently failable
4. Regenerate queries                                   ← independently failable
5. Show query diff → user confirms → write search_queries.json
```

**Step 3 commits before step 4 runs.** If query regeneration fails — the LLM is
down, the call times out — the profile edit is already safe on disk. The tool
returns `{ok: true, changed: true, queries_stale: true}` and the agent tells the
user to retry `regenerate_queries`. Monday's scheduled run is the backstop.

Reversing this order would mean a Gemini outage costs the user a confirmed
profile change. That is the failure-isolation principle applied here exactly as
the scorer applies it: **commit what succeeded; do not roll it back over a
downstream failure.**

### `queries_stale` — a deterministic signal, not an LLM judgment

`update_profile` already computes a diff, so it already knows which fields the op
touched. It sets:

```python
queries_stale = op.op in {"add_target_track", "remove_target_track",
                          "add_skill", "remove_skill"}
```

Pure set logic. No LLM call, no new state, no new file, no coupling from the
profile layer to the scheduling layer. Most edits — fixing a typo in a bullet,
correcting a phone number — leave it `False` and cost nothing.

**Why `update_profile` does not simply call `regenerate_queries` itself:** doing
so would make a pure file operation depend on the LLM layer, so a profile edit
could fail because Gemini is down. The generated queries are also explicitly
*human-vetoable*; auto-firing regeneration inside a confirmation flow means
accepting queries nobody looked at.

### Query regeneration is a full pass, not an append

When a track is added, regeneration rewrites **the entire query list**, not just
the new track's queries.

This is forced by the cap. `search_queries.json` is capped (10–15 queries; see
§ Open decisions). If the list now spans five tracks, something must give — and
only a global pass can arbitrate which queries survive. An append-only
regeneration has no basis on which to drop anything, so the list grows without
bound and the daily scrape fan-out (queries × portals) grows with it.

Because a full pass changes previously-vetted queries, the regenerated list goes
through **the same two-turn diff-and-confirm** as the profile edit, reusing the
existing `PENDING_ACTION` marker.

### What query regen receives

`index_view()` — the index tier, which the schema already gives for free:

- `skills` — the full superset (~134 entries)
- `target_tracks`
- item **titles** only — bodies dropped

Titles cost almost nothing and are the strongest query seed available:
*"Robot Learning and Motion Engineer"* **is** a job-search query. Skills alone
give the model vocabulary but no sense of what roles the candidate has held.
Bullets are excluded — they are body tier and would dominate the token budget
without improving query quality.

---

## Step 4 — Work Packages

### WP-P1 — Schema and tier tags

The Pydantic models above, with `tier` declared via
`Field(json_schema_extra={"tier": ...})` on **every** field. A test asserts no
field is untagged, so a new field cannot silently escape the projection.

`model_config = ConfigDict(extra="forbid")` on every model, so a stale
`profile.json` fails loudly rather than silently dropping fields.

Location: `services/profile/schema.py`

### WP-P2 — Load and validate

`load_profile(path) -> Profile`: read, parse, `validate_invariants`, return.
Raises `ProfileInvariantError` on violation, `FileNotFoundError` if absent.

Location: `services/profile/loader.py`

### WP-P3 — Projections

Three pure functions (no network, no database):

- `profile_summary(p) -> str` — mechanical tier filter. Reads tiers off
  `Profile.model_fields`; contains **no hardcoded field list**. Applies the
  char-budget backstop to identity scalars (`agent_v2.md` §5): a tagged scalar
  exceeding budget is treated as body and excluded.
- `profile_to_text(p) -> str` — pour-everything-in, per `scoring_v2.md` WP3.
  Includes `target_tracks`. Excludes contact details and dates (noise, no
  matching signal).
- `index_view(p) -> IndexView` — skill `{id, label}`, target_tracks, item
  `{id, title}`. Aliases are dropped: query regen wants competences, not spellings.

Location: `services/profile/projections.py`

### WP-P3b — Lookups and reference resolution

`get_item`, `get_skill`, `resolve_surface`, `find_items`, `find_skills`,
`items_demonstrating`. All pure.

`resolve_surface` is the one the *tailoring* layer needs: it maps a JD keyword to
a competence id so the text guard can check it against the item's permission list.
Without it, the guard would have to string-match surface forms and would reject
`RESTful API` on an item tagged with `REST API`.

Location: `services/profile/lookup.py`

### WP-P4 — The typed mutator

`update_profile(op: ProfileOp, confirmed: bool) -> UpdateResult`.

Unconfirmed: compute the resulting profile, diff it against current, return
`{ok, changed: false, pending_confirmation: true, diff, advisory?}`.
Confirmed: backup, write, return `{ok, changed, summary, queries_stale}`.

Post-write, `validate_invariants` runs against the **new** profile. A write that
would break an invariant is rejected before the file is touched — e.g. `add_skill`
naming an `item_id` that does not exist, or `remove_skill` leaving an item
demonstrating a skill absent from the superset.

`remove_skill` **cascades**: the skill is stripped from every item's
`demonstrated_skills` as well as from the superset. Not cascading would break
invariant 2 on the next load.

Location: `services/profile/mutate.py`

### WP-P5 — Advisory

`advise(op, profile) -> str | None`. Pure function. For `add_target_track`,
counts supporting skills and names the scoring consequence. Returns `None` for
ops with no advisory. Never raises, never blocks.

Location: `services/profile/advisory.py`

### WP-P6 — Test suite

Test cases, each traced to a spec section:

| TC | Asserts |
|---|---|
| TC-P-01 | Every `Profile` field carries a tier tag (Req 5) |
| TC-P-02 | `profile_template.json` validates against the schema |
| TC-P-03 | Duplicate item id across experiences/projects raises (Inv 1) |
| TC-P-04 | `demonstrated_skills` not in superset raises (Inv 2) |
| TC-P-05 | Orphan skill does **not** raise; is returned by `orphan_skills()` |
| TC-P-06 | `extra="forbid"` — unknown field in JSON raises |
| TC-P-07 | `profile_summary` drops all body-tier and render-tier fields |
| TC-P-08 | `profile_summary` contains no hardcoded field names (add a field, it appears) |
| TC-P-09 | `profile_to_text` includes `target_tracks`; excludes email/phone |
| TC-P-10 | `add_target_track` appends; existing tracks survive (Gap 3) |
| TC-P-11 | `remove_skill` cascades to every item's `demonstrated_skills` |
| TC-P-12 | `edit_bullets` replaces in full |
| TC-P-13 | Unconfirmed op writes nothing to disk |
| TC-P-14 | Confirmed op backs up before overwrite |
| TC-P-15 | `queries_stale` true for track/skill ops, false for `edit_bullets` |
| TC-P-16 | Advisory returned for a track with no supporting skills — **and the write still succeeds** (Req 8) |
| TC-P-17 | An op that would break an invariant is rejected **before** the file is written |
| TC-P-18 | `resolve_surface("RESTful API")` and `resolve_surface("REST")` both return `rest_api` |
| TC-P-19 | Two skills claiming the same surface string raises (Inv 3) |
| TC-P-20 | `demonstrated_skills` naming an unknown skill id raises (Inv 4) |
| TC-P-21 | `find_items` returns **all** candidates above threshold, not a best guess (§7 0/1/N) |
| TC-P-22 | `add_skill` with an `id` that already exists raises |

Location: `tests/profile/`

---

## Step 5 — Build Order

1. **WP-P1** — schema. Everything else imports it.
2. **WP-P2** — load and validate. Now every consumer can be handed a known-good
   `Profile`, which unblocks the scorer and tailoring independently of the rest
   of this layer.
3. **WP-P3** — projections. `profile_to_text` first (the scorer is the nearest
   consumer), then `profile_summary`, then `index_view`.
4. **WP-P5** — advisory. Pure, no dependencies, trivially testable. Build before
   the mutator so the mutator can simply call it.
5. **WP-P4** — the mutator. Depends on schema, loader, and advisory.
6. **WP-P6** — tests, written alongside each WP rather than after (TDD).

`update_profile` is deliberately **last**. Reading the profile unblocks four
layers; writing it unblocks only the agent's edit conversation, which is the
least urgent path.

---

## Integration — how each consumer gets its profile

| Consumer | Receives | Via |
|---|---|---|
| **Scorer** | one text string | `profile_to_text()` — everything poured in, incl. `target_tracks`; contact details and dates dropped |
| **Tailoring** | the **whole `Profile` object** | but the **LLM sees only the content tier**; the renderer substitutes identity directly. This is what makes tailoring invariant 4 ("identity fields byte-identical to `profile.json`") *structurally* true rather than merely hoped-for |
| **Agent** | a compact summary | `profile_summary()` — identity verbatim, index as `{id, label}`, body dropped. The full superset is **never** carried in the loop; tailoring loads it just-in-time |
| **Query regen** | skills + tracks + titles | `index_view()` |
| **Scheduler** | nothing directly | it passes `profile` through to the scorer and tailorer |

**Cache invalidation is free.** The scorer keys its cached profile vector on a
SHA-256 fingerprint of `profile_to_text(profile)`. A profile write changes the
text, which changes the fingerprint, which forces a re-embed on the next `score`
call. No invalidation plumbing, no cross-layer notification, no stale-cache bug.
This is a direct dividend of the scorer holding a *pure function of its input*
rather than external state.

---

## Deferred seams (named, not built)

- **Per-skill proficiency.** There is currently no way to say "I exposed some
  endpoints" versus "I am a backend specialist". Depth is communicated only
  through `summary_seed` and skill ordering. A `proficiency` field on
  `demonstrated_skills` is the obvious seam, and it is a schema change to the
  tailoring contract, not a profile-layer-local one. **Not built:** the
  permission-list model is sufficient for v2, and adding a rating invites the
  LLM to reason about depth it cannot verify.
- **Alias selection policy.** The tailorer may surface any of a skill's
  `surfaces()`. *Which* one it picks for a given JD — exact-match the posting's
  spelling, else fall back to `label` — is a tailoring-layer decision, not a
  profile-layer one. The profile layer only guarantees the alternatives are
  linked. **Not built here.**
- **Per-track weighting.** `target_tracks` is flat and unweighted. Weighted
  tracks would let query regen allocate its capped slots proportionally rather
  than by LLM judgment.
- **Chunked profile embedding.** A 134-skill, 14-item superset poured into a
  single vector is **genuinely diffuse**, and `scoring_v2.md` Requirement 7
  already accepts the dilution cost. With `target_tracks` spanning four
  directions, this is the most likely place v2 first shows strain. The named
  alternative is multi-vector coverage-matrix scoring. **Watch this once real
  scores exist.**
- **Multi-profile.** One candidate, one file.

---

## Open decisions

1. **The query cap.** `search_queries.json` needs an explicit maximum. The daily
   scrape fans out across every query × three portals; an uncapped LLM handed 134
   skills and four tracks can plausibly emit thirty-plus queries, which is
   ninety-plus requests a day against sites that rate-limit. **Recommendation:
   10–15, with the regen prompt instructed to *prioritise*, not enumerate.** This
   is a one-line prompt constraint today and a scraper-ban incident later.
   *Owner: scheduling layer (WP-S6), but forced by this layer's superset size.*

2. **`scheduling_v2.md` line 18 contradicts WP-S6.** The overview says the slow
   loop runs "weekly (Monday) **or on profile change**"; WP-S6 says "weekly and
   on-demand via the agent's `regenerate_queries` tool". There is no automatic
   on-profile-change trigger anywhere in the code path. This document resolves
   the contradiction in favour of the tool-driven path plus the `queries_stale`
   signal. **`scheduling_v2.md` line 18 should be edited to match.**

3. **`tailoring.md` §8 is stale.** Its `Profile` schema predates `target_tracks`,
   `years_of_experience`, and `candidate_status`, and it never defined
   `Education`. Because there is only **one** `Profile` model, a strict
   (`extra="forbid"`) model built from that spec will **fail validation** against
   the real `profile.json`. Needs a targeted edit.