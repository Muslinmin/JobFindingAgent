# Tailoring Layer — Design Decisions

> Scope: build-order step 5 (`Tailoring service`)
> Complements `architecture_v2.md` §5 (Tailoring Service) and §7 (LaTeX output
> path, incorporated skills). 

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

## 5. The tailoring call (single pass)

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

## 6. Renderer (deterministic)

- Single Jinja2 `.tex` template owns **all** LaTeX syntax; the LLM never emits
  LaTeX. (LaTeX equivalent of invariant 2.) It emits a lightweight `**term**`
  markdown-style marker for emphasis instead; the renderer is the only thing
  that ever turns that into `\textbf{}`.
- Mandatory LaTeX-escape pass over every content string (`& % $ # _ { } ~ ^ \`)
  before substitution.
- Compiled with `tectonic`, run off the event loop via `asyncio.to_thread`
  (it's a blocking subprocess call).
- **One entry-level template** (Skills grouped by category, Projects and
  Experience, Education). No multi-template selection — that would reopen
  the "LLM affects layout" question.
- Cover letter is a **plain-text artifact** (`kind='cover_letter'`), not a
  second LaTeX render — delivered as text via Telegram.

---

## 7. Implementation, integration, and guarantees

Implemented in full — see `tailoring_build.md` for the module map and the
calling convention (`tailor()`'s signature, error handling,
`src/app/tailoring_usage_example.py` for a worked example). Summarized here
is what the implementation *guarantees*, independent of how it's called:

1. Every `ref_id` in a `TailoredSelection` resolves to a real `Profile` item.
2. Skill terms detected in any tailored item's text ⊆ that item's `demonstrated_skills`.
3. No numeral or named entity appears in tailored text that is absent from the source item (including entities introduced only via the `**bold**` marker — it carries no truthfulness exemption).
4. Identity fields in the render model are byte-identical to `profile.json`.
5. LLM output validates against the `TailoredSelection` schema, checked against the `Profile` in the same pass (a dangling `ref_id` is a schema failure, not a guard failure).
6. A guard violation logs at CRITICAL and fails cleanly — no partial render, no retry.

The tailoring layer performs no database I/O; two callers (a daily batch job
and an on-demand agent tool) are expected to call the same `tailor()`, but
neither is built yet — see `tailoring_build.md`'s Caller contract.
