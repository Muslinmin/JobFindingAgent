# Profile Layer

Status: implemented. Package lives at `src/profile/`.

The profile layer answers one question: **what is true about the candidate?**
It owns `profile.json` — the single source of truth — and hands out
purpose-built projections of it to every other layer. It does not score,
tailor, scrape, or render. Its job is to guarantee that every layer
downstream receives a candidate profile that is already known-good, so no
downstream failure is ever ambiguous about whether the profile was at fault.

The profile is a deliberate **superset**: it holds every experience, every
project, every skill — including ATS-variant surface forms of the same real
competence (`REST` / `RESTful API`, `ROS` / `Robot Operating System`).
Downstream layers only ever *select* from it. Nothing downstream may
introduce a fact that is not already here.

---

## What the layer does

1. **Schema** (`profile/schema.py`) — `Profile`, `ProfileItem`, `Education`,
   `Skill`, and the typed `ProfileOp` mutation union. Every field carries a
   **tier tag** (`identity` / `index` / `body` / `render`) declared on the
   Pydantic model itself, via `Field(json_schema_extra={"tier": ...})`. All
   models set `extra="forbid"`, so an unknown key in `profile.json` raises
   rather than being silently dropped.
2. **Loading and validating** (`profile/loader.py`, `profile/invariants.py`) —
   `load_profile()` reads the file, parses it into a `Profile`, and runs
   `validate_invariants()`. Raises `FileNotFoundError`, `pydantic.ValidationError`,
   or `ProfileInvariantError` rather than returning a broken object.
3. **Projections** (`profile/projections.py`) — `profile_summary()` for the
   agent's context, `profile_to_text()` for the scorer's embedding call,
   `index_view()` for query regeneration. The full `Profile` object goes to
   tailoring, which sees only the content tier at prompt time and lets the
   renderer substitute identity fields directly.
4. **Lookups** (`profile/lookup.py`) — exact (`get_item`, `get_skill`) and
   fuzzy (`find_items`, `find_skills`) resolution, plus `resolve_surface()`
   (ATS spelling → competence id) and `items_demonstrating()`.
5. **Mutation** (`profile/mutate.py`) — `update_profile()` is the **sole
   writer** of `profile.json`. No other code path writes that file.
6. **Advisory** (`profile/advisory.py`) — `advise()` surfaces the scoring
   consequence of a track/item with thin skill coverage. Never blocks.

---

## Schema

```python
IDENTITY, INDEX, BODY, RENDER = "identity", "index", "body", "render"

class Skill(BaseModel):              # ONE real competence, many spellings
    id: str                          # what demonstrated_skills holds
    label: str                       # default surface form
    aliases: list[str]               # ATS variants — the SAME competence
    category: str | None             # CV Skills-section grouping only

    def surfaces(self) -> list[str]: ...   # [label, *aliases]

class Education(BaseModel):
    id: str
    institution: str                 # tier: identity
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
    demonstrated_skills: list[str]   # tier: body — SKILL IDS, a permission list

class Profile(BaseModel):
    name: str                        # tier: identity
    location: str | None             # tier: identity
    years_of_experience: float | None  # tier: identity
    candidate_status: str | None     # tier: identity
    education: list[Education]       # tier: identity

    email: str                       # tier: render
    phone: str | None                # tier: render
    links: list[str]                 # tier: render
    headline: str | None             # tier: render — human-authored CV subtitle

    summary_seed: str | None         # tier: body
    experiences: list[ProfileItem]   # tier: index
    projects: list[ProfileItem]      # tier: index
    skills: list[Skill]              # tier: index

    target_tracks: list[str]         # tier: index — intent, not history

    @property
    def items(self) -> list[ProfileItem]:  # experiences + projects, ONE flat namespace
        ...
```

**Tier semantics** — what each tier means for the agent's context:

| Tier | Projection into agent context |
|---|---|
| `identity` | verbatim |
| `index` | `{id, label}` per item — bodies dropped |
| `body` | **dropped** — loaded just-in-time by the tailoring service |
| `render` | **never enters agent context** — CV fixed slots only |

### Why skills carry ids, and aliases

`demonstrated_skills` holds skill **ids**, not bare strings. The superset
deliberately holds ATS surface variants (`REST API`, `RESTful API`, `REST`) —
one competence, three spellings, not three competences. Modelling skills as
bare strings would make an item list all three separately, let
`remove_skill` leave stragglers behind, and give the tailorer no signal that
the three are alternatives (so it could emit all of them on one CV, reading
as keyword stuffing). `Skill` collapses this to one id, one label, many
aliases; `resolve_surface()` maps any spelling back to the one competence.

---

## Load-time invariants

`validate_invariants(p: Profile) -> None` (raises `ProfileInvariantError`):

1. **Item ids are unique across experiences *and* projects** — `ref_id` is
   resolved in one flat namespace, so a cross-list collision is as fatal as
   an in-list one.
2. **Skill ids are unique.**
3. **No surface string is claimed by two skills** — otherwise
   `resolve_surface()` is non-deterministic.
4. **Every id in `demonstrated_skills` resolves to a real `Skill`.**

**Orphan skills are legal and NOT an invariant.** A skill demonstrated by no
item may still be listed on a CV; it can just never be woven into a bullet.
`orphan_skills()` surfaces them for human review — in practice an orphan
usually means an item is missing from the profile, not that the skill is
false.

Invariants are enforced at **load** (and again, on the resulting profile,
inside `update_profile` before any write) rather than at use, so that a
guard violation at tailor time means exactly one thing: the LLM misbehaved.

---

## Public surface

| Function | Signature | Consumer |
|---|---|---|
| `load_profile` | `(path) -> Profile` | everyone, at entry |
| `validate_invariants` | `(Profile) -> None`, raises | `load_profile`, `update_profile` |
| `profile_summary` | `(Profile) -> str` | agent, every turn |
| `profile_to_text` | `(Profile) -> str` | scorer (`scoring/embedding_scorer.py`) |
| `index_view` | `(Profile) -> IndexView` | query regeneration |
| `update_profile` | `(op, confirmed, path) -> UpdateResult` | agent tool — **sole mutator** |
| `orphan_skills` | `(Profile) -> list[Skill]` | human review |
| `get_item` / `get_skill` | `(Profile, id) -> ... \| None` | op construction |
| `resolve_surface` | `(Profile, surface) -> Skill \| None` | tailorer, text guard |
| `find_items` / `find_skills` | `(Profile, phrase) -> list[...]` | agent reference resolution |
| `items_demonstrating` | `(Profile, skill_id) -> list[ProfileItem]` | advisory, review |

`find_items` / `find_skills` return a **list**, never a single best guess:
zero or many matches means the caller (the agent) must clarify with the
user rather than assume.

```python
IDENTITY_CHAR_BUDGET: int = 200   # profile_summary treats an identity scalar
                                   # longer than this as body and drops it

class IndexItem(BaseModel):
    id: str
    label: str

class IndexView(BaseModel):
    skills: list[IndexItem]        # aliases dropped — competences, not spellings
    target_tracks: list[str]
    experiences: list[IndexItem]
    projects: list[IndexItem]
```

`profile_summary` and `profile_to_text` never hardcode a field name — they
walk `Profile.model_fields` and read each field's declared tier, so a new
schema field cannot silently escape (or silently leak into) a projection.

`profile_to_text` pours everything in for the embedding call: name,
location, candidate status, summary seed, education, every item's title/org/
bullets, every skill's surfaces, and `target_tracks`. It excludes contact
details and dates (no matching signal). The scorer keys its embedding cache
on a SHA-256 fingerprint of this string, so a profile write invalidates the
cache for free — no explicit invalidation plumbing.

---

## The typed mutation contract

```python
class AddTargetTrack(BaseModel):
    op: Literal["add_target_track"]
    track: str

class RemoveTargetTrack(BaseModel):
    op: Literal["remove_target_track"]
    track: str

class AddSkill(BaseModel):
    op: Literal["add_skill"]
    skill: Skill
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
    bullets: list[str]                # full replacement

class TagSkill(BaseModel):
    op: Literal["tag_skill"]
    item_id: str
    skill_ids: list[str]

ProfileOp = Annotated[
    AddTargetTrack | RemoveTargetTrack | AddSkill | RemoveSkill
    | AddItem | EditBullets | TagSkill,
    Field(discriminator="op"),
]

QUERY_AFFECTING_OPS = {"add_target_track", "remove_target_track",
                        "add_skill", "remove_skill"}
```

A discriminated union rather than a free-form patch dict: list semantics
(append vs. replace) are unambiguous at the type level. `add_target_track`
can only mean *append*. `EditBullets` and the `Remove*` ops exist because
add-only mutation is insufficient — a wrong claim in the base bullet text
needs a rewrite, not an addition.

**Every operation is a full-field replacement at the point of write**, with
the agent constructing the complete new value — the diff shown to the human
for approval is then unambiguous to read.

### `update_profile` — the two-call contract

```python
def update_profile(op: ProfileOp, confirmed: bool, path: str | Path) -> UpdateResult

class UpdateResult(BaseModel):
    ok: bool
    changed: bool
    pending_confirmation: bool = False
    diff: str | None = None
    advisory: str | None = None
    summary: str | None = None
    queries_stale: bool = False
```

1. `confirmed=False` → nothing written. Returns
   `{ok: True, changed: False, pending_confirmation: True, diff, advisory}`.
2. Same `op`, `confirmed=True` → backs up the current file (to
   `profiles/backups/`, timestamped) and writes the new one. Returns
   `{ok: True, changed: True, summary, queries_stale}`.
3. If `op` would break an invariant, **both** calls return
   `{ok: False, changed: False, summary: "<reason>"}` and nothing is ever
   written — not even a backup.

`queries_stale = op.op in QUERY_AFFECTING_OPS` — pure set logic, no LLM call.
`update_profile` never regenerates `search_queries.json` itself: coupling a
pure file write to the LLM layer would mean a profile edit could fail
because an LLM call is down, and the regenerated queries are meant to be
human-vetoable rather than auto-fired inside a confirmation flow. The caller
(the agent) is responsible for prompting the user to run query
regeneration when `queries_stale` is `True`.

### Advisory, never blocking

`advise(op, profile) -> str | None` fires for `AddTargetTrack` (always,
reporting how many of the profile's skills have supporting evidence) and
`AddItem` (only when the new item has no `demonstrated_skills`). It is
mechanical — it counts evidence, it does not judge semantic fit — and it
never gates the write. Blocking would be the model overruling the candidate
about their own career; `target_tracks` is explicitly allowed to outrun the
evidence.

---

## How each consumer gets its profile

| Consumer | Receives | Via |
|---|---|---|
| **Scorer** (`scoring/embedding_scorer.py`) | one text string | `profile_to_text()` |
| **Tailoring** (`tailoring/`) | the whole `Profile` object | full object at runtime, but the LLM prompt sees only the content tier (`tailoring/prompt.py`); the renderer substitutes identity/render fields directly (`tailoring/render.py`) |
| **Agent** | a compact summary | `profile_summary()` — identity verbatim, index as `{id, label}`, body dropped |
| **Query regeneration** | skills + tracks + item titles | `index_view()` |

---

## Deferred (named, not built)

- **Per-skill proficiency.** No depth field — `demonstrated_skills` is a
  permission list, not a rating. Depth is communicated only through
  `summary_seed` and skill ordering.
- **Alias selection policy.** Which of a skill's `surfaces()` a tailored CV
  uses for a given JD is a tailoring-layer decision; the profile layer only
  guarantees the alternatives are linked.
- **Per-track weighting.** `target_tracks` is flat and unweighted.
- **Chunked profile embedding.** A large superset poured into one vector for
  `profile_to_text()` is genuinely diffuse once `target_tracks` spans several
  directions. Worth watching once real scores exist; the named alternative
  is multi-vector coverage-matrix scoring.
- **Multi-profile support.** One candidate, one `profile.json`.
