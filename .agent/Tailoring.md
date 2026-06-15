# Tailoring Layer — Design Decisions

> Scope: build-order step 5 (`Tailoring service`), target **v0.6.x**.
> Complements `architecture_v2.md` §5 (Tailoring Service) and §7 (LaTeX output
> path, incorporated skills). This file records *decisions and their rationale*,
> not implementation steps. Each decision is stable unless superseded here.

---

## 0. One-line summary

The profile is a **superset of everything true**. Tailoring is a **selection +
constrained-rewrite** problem over that superset, never a gap-closing problem.
The LLM has full freedom over *how it phrases* and zero freedom over *what it
claims*. A deterministic renderer turns the selection into a LaTeX PDF.

---

## 1. Core model: tailoring is elimination, not gap-closing

| | |
|---|---|
| **Decision** | `profile.json` holds everything truthful — every project (large or small), every skill, including ATS-variant surface forms of the *same* real competence (`PostgreSQL`/`Postgres`, `REST`/`RESTful`, `CI/CD`). Tailoring selects a subset; it never adds content. |
| **Rationale** | The only ethical source of any keyword is the candidate's own history. A "missing keyword" can therefore only mean *true content not yet selected*, never *content to invent*. Reframing the loop around selection makes invariant 4 (no invented experience) **structurally impossible to violate** rather than prompt-enforced. |
| **Consequence** | The profile superset + the three-tier guards are the complete correctness guarantee. No scoring pass after tailoring is needed — the structural controls are sufficient. |

---

## 2. Three-tier output model

Every field of a tailored CV falls into exactly one tier, with a decreasing
rigidity and its own guard. This replaces the earlier by-reference-vs-by-value
binary.

| Tier | Fields | LLM freedom | Guard (deterministic) |
|---|---|---|---|
| **Identity** | name, email, phone, location, school, degree, grad date, links | none — LLM never sees these | substituted directly from `profile.json` |
| **Selection** | which experiences/projects/skills, and their order | choose + order only | every emitted `ref_id` resolves to a real profile entry |
| **Text** | professional summary, bullet phrasing | reframe / re-emphasise / mirror JD language | (a) surfaced skill terms ⊆ that item's `demonstrated_skills`; (b) no new specifics (numerals / named entities) absent from the source text |

The render model is `identity(profile) + resolved(selection)` merged into one
object, fed to a single Jinja `.tex` template. Profile and LLM output are **not**
the same schema — the profile is the superset; the LLM emits a narrower
*projection* with no identity fields.

---

## 3. `demonstrated_skills`: associations are authored, not inferred

| | |
|---|---|
| **Decision** | Each experience/project carries a `demonstrated_skills` list — the skills that *specific item* genuinely exercised. Authored by the human (or proposed by the CV→profile parse, build-order step 4, then human-reviewed). |
| **Rationale** | Vocabulary-level constraint is insufficient. Two true atoms can form a false molecule: "I have leadership" + "I did project X" does **not** entail "I led a team on X." Pre-authorising the *association* lets the LLM emphasise a true skill on the right item ("JD wants leadership → X is tagged with leadership → lead X's summary with it") while making the dangerous recombination impossible. |
| **Test** | For every tailored item, the skill terms detected in its rewritten text (via the `resume-ats-optimizer` keyword detector) must be a subset of that item's `demonstrated_skills`. |

---

## 4. What the LLM may and may not do (text tier)

| Allowed (phrasing) | Blocked (claims) |
|---|---|
| Grammar, tense, articles, connectives | New skills not in the item's `demonstrated_skills` |
| Synonyms / verb swaps (`built` → `developed`) | New numerals (`team of 5`, `30% faster`) absent from source |
| Joining clipped CV fragments into clean prose | New named entities (tools, companies, people) absent from source |
| Mirroring the JD's wording for things already true | Any specific claim present in neither the item text nor its tags |

Rule of thumb: **full freedom over *how it says things*, zero freedom over *what
it claims*.** Polishing is unrestricted; inventing is blocked. The two guards in
§2 ignore phrasing entirely — a verb swap or reflow passes; a new skill or a new
number is flagged.

> Soft edge (known, not airtight): the skill-subset check catches fabricated
> *attributions*; it does not catch every fabricated *specific*. "Team of 5" is
> caught only by the "no new specifics" numeral/entity diff, which is
> prompt-enforced plus a light source-diff — not a formal proof. Treat this tier
> as strong, not perfect.

---

## 5. Scoring: discovery only

| | |
|---|---|
| **Decision** | The scorer runs **once, at discovery** — `score(jd_text, full_profile) -> int (0–10000)`. Tailoring does **not** re-score. |
| **Rationale** | The three-tier model (§2) + `demonstrated_skills` (§3) is the correctness guarantee. A post-tailor re-score would be redundant validation on top of structural enforcement, and adds complexity (serializer, loop, target, settings) with no truthfulness benefit. |
| **Discovery threshold** | Lenient and configurable (`score_threshold`). Discovery is a coarse filter — "plausibly relevant, let it through." The daily budget (`tailor_batch_size`) is the real throttle, not the threshold. |
| **Deferred (0.7.x)** | Once embeddings land (v2.1), mean-pooling the bloated superset into one vector can depress good-fit jobs in the top-N ranking. Fix: rank on profile *chunks* (top-k / max-pool similarity), not one pooled blob. |

---

## 6. The tailoring call (single pass)

```
tailored = llm_tailor(jd, profile)     # one LLM call; no iteration
validate_schema(tailored)              # Pydantic — TailoredSelection
validate_guards(tailored, profile)     # ref_id integrity + skill-subset + no-new-specifics
render(tailored, profile)              # deterministic: identity + resolved selection → PDF
```

**Guard violation → log and fail cleanly.** If any guard fires, the job is
logged (loguru) and the tailoring attempt is marked failed. No retry, no
partial render. The failure surfaces through the existing pipeline notification
path. A guard breach means the LLM fabricated a claim; re-prompting the same
input is unlikely to fix a structural hallucination and would burn an extra call
for nothing.

**Debug artifacts.** When `save_debug_artifacts=true` (configurable, default
off), the intermediate `.tex` file and any failed LLM outputs are written to a
`debug/` folder beside the final PDF. Intended for local iteration, not
production.

---

## 7. Renderer (deterministic)

- Single Jinja2 `.tex` template owns **all** LaTeX syntax; the LLM never emits
  LaTeX. (LaTeX equivalent of invariant 2.)
- Mandatory LaTeX-escape pass over every content string (`& % $ # _ { } ~ ^ \`)
  before substitution.
- Compile with `tectonic` (alt: `latexmk`) → PDF → register via
  `POST /jobs/{id}/artifacts` (`kind='cv_pdf'`).
- **One entry-level template** (Skills + Projects prioritised, Education
  weighted, 3–5 achievement bullets). No multi-template selection — that would
  reopen the "LLM affects layout" question.
- Cover letter is a **plain-text artifact** (`kind='cover_letter'`), not a second
  LaTeX render — delivered as text via Telegram.

---

## 8. Indicative schema (encodes §2–§3; not yet locked)

```python
# --- Profile: the superset, source of truth ---
class ProfileItem(BaseModel):          # experience or project
    id: str
    title: str
    organization: str | None = None
    date_range: str | None = None
    bullets: list[str]                 # frozen factual base text
    demonstrated_skills: list[str]     # skill ids genuinely exercised by THIS item

class Profile(BaseModel):
    # identity tier — frozen, never LLM-touched
    name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: list[str] = []
    education: list[Education]
    # content superset
    summary_seed: str | None = None
    experiences: list[ProfileItem]
    projects: list[ProfileItem]
    skills: list[str]                  # full superset incl. ATS surface variants

# --- TailoredSelection: LLM output, projection only (NO identity) ---
class TailoredItem(BaseModel):
    ref_id: str                        # MUST resolve to a Profile item id
    bullets: list[str]                 # constrained rewrite of that item's bullets

class TailoredSelection(BaseModel):
    summary: str                       # constrained-rewrite summary
    experience_order: list[str]        # ref_ids — inclusion + order
    experiences: list[TailoredItem]
    project_order: list[str]
    projects: list[TailoredItem]
    skill_order: list[str]             # skill ids to surface, ordered
```

Validators encode the guards: `ref_id` referential integrity, surfaced-skill
containment, no-new-specifics diff against `Profile`.

---

## 9. Integration points (unchanged plumbing)

- **Two entry points, one service:** the daily budgeted `tailor` job (top
  `tailor_batch_size` SCORED by score) and the on-demand `tailor_resume` agent
  tool both call the same tailoring service.
- **Skills as prompt-file domain knowledge:** `prompts/tailoring.md` carries
  `resume-tailor` + `resume-ats-optimizer` + `resume-section-builder`; loaded
  only by the tailoring stage, never on every chat call. They constrain content
  selection only — they never touch the renderer.
- **Artifacts** written to disk, registered via `POST /jobs/{id}/artifacts`;
  reaching `TAILORED` advances the FSM toward `PENDING_APPROVAL`.

---

## 10. Testable invariants (TDD targets)

1. Every `ref_id` in a `TailoredSelection` resolves to a real `Profile` item.
2. Skill terms detected in any tailored item's text ⊆ that item's `demonstrated_skills`.
3. No numeral or named entity appears in tailored text that is absent from the source item.
4. Identity fields in the render model are byte-identical to `profile.json`.
5. LLM output validates against the `TailoredSelection` schema (mock the LLM).
6. Scorer returns `0`, never `NaN`, on empty keywords (v2.0) / zero vector (v2.1) — applies at discovery.
7. A guard violation logs and fails cleanly; no partial render is produced and no retry is attempted.
8. `.tex` template output is snapshot-stable; one `live`-marked test compiles a fixture to PDF (catches escaping/template regressions).

---

## 11. Deferred decisions (flagged, not solved here)

| Item | When | Note |
|---|---|---|
| Discovery **ranking** dilution | 0.7.x | A lenient gate fixes the gate, not the top-N ranking. Once embeddings land (v2.1), rank on profile **chunks** (top-k / max-pool similarity), not one pooled blob. |
| `demonstrated_skills` authoring | step 4 | Manual vs parse-proposed-then-reviewed. Tie to CV→profile parse. |
