# Tailoring Layer — Build Plan

> Companion to `tailoring.md` (decision record) and `architecture_v2.md` §5/§7.
> This file is the executable specification: test cases are the contract; work
> packages are "make this cluster go green." All decisions and rationale live in
> `tailoring.md` — this file references section numbers there rather than
> repeating them.
>
> Mock policy: LLM, embedder, and HTTP are mocked in all tests except those
> explicitly marked `[live]`. Live tests run in Docker only and are marked
> `-m live`.

---

## Build order

```
WP0 → {WP2, WP3, WP5} → WP4
```

WP2, WP3, and WP5 are a parallel front: all three unblock from WP0 and can be
built at the same time. They converge at WP4, which is the layer's single public
entry point. There is no WP6 or WP7 in this layer any more; the responsibilities
that used to live there — registering the artifact, advancing the job's state,
and selecting which jobs to tailor — now belong to the caller, and they are
recorded in the "Caller contract" section at the end of this file.

---

## WP0 — Schemas & validators

**Depends on:** nothing
**Deliverables:** `Profile`, `ProfileItem`, `TailoredSelection`, `TailoredItem`
Pydantic models with all validators; `profile_template.json` filled with
representative dummy data covering every field.
**Done when:** TC-SCHEMA-01 through TC-SCHEMA-10 pass.

```
TC-SCHEMA-01  [unit]  uut: Profile(**data)
  given   all required fields present (name, email, education, experiences,
          projects, skills)
  when    Profile is constructed
  then    validates without error
  traces  tailoring.md §8

TC-SCHEMA-02  [unit]  uut: ProfileItem(**data)
  given   a ProfileItem with demonstrated_skills as a list of skill IDs
  when    ProfileItem is constructed
  then    demonstrated_skills is accessible as list[str]
  traces  tailoring.md §3; §8

TC-SCHEMA-03  [unit]  uut: ProfileItem(**data)
  given   a ProfileItem missing the 'id' field
  when    ProfileItem is constructed
  then    raises ValidationError
  traces  tailoring.md §8

TC-SCHEMA-04  [unit]  uut: ProfileItem(**data)
  given   a ProfileItem missing 'bullets'
  when    ProfileItem is constructed
  then    raises ValidationError
  traces  tailoring.md §8

TC-SCHEMA-05  [unit]  uut: TailoredSelection(**data)
  given   valid ref_ids, orders, and summary; no identity fields present
  when    TailoredSelection is constructed
  then    validates without error; model fields contain no identity attributes
  traces  tailoring.md §2; §8

TC-SCHEMA-06  [unit]  uut: TailoredSelection validator (ref_id integrity)
  given   a ref_id not matching any Profile item id
  when    TailoredSelection is validated against the Profile
  then    raises ValidationError
  traces  tailoring.md §2; invariant 1

TC-SCHEMA-07  [unit]  uut: TailoredSelection validator (ref_id integrity)
  given   a ref_id matching an existing Profile item id
  when    TailoredSelection is validated against the Profile
  then    passes
  traces  tailoring.md §2; invariant 1

TC-SCHEMA-08  [unit]  uut: TailoredSelection validator (skill_order)
  given   a skill_order entry not present in Profile.skills
  when    TailoredSelection is validated
  then    raises ValidationError
  traces  tailoring.md §2

TC-SCHEMA-09  [unit]  uut: TailoredSelection validator (order/items consistency)
  given   experience_order contains an id with no corresponding TailoredItem
          in the experiences list
  when    TailoredSelection is validated
  then    raises ValidationError
  traces  tailoring.md §2

TC-SCHEMA-10  [unit]  uut: Profile.model_validate(json.load("profile_template.json"))
  given   the provided profile_template.json
  when    validated against the Profile schema
  then    validates without error
  traces  tailoring.md §8 — template is a living fixture; schema changes break
          this test, forcing the template to stay current
```

---

## WP2 — Truthfulness guards

**Depends on:** WP0
**Deliverables:** `check_skill_subset(tailored_item, source_item)`,
`check_no_new_specifics(tailored_text, source_text)` — both deterministic, no I/O.
The skill detector (the `resume-ats-optimizer` keyword model) is **internal** to
this module, not a caller argument. There is one construction point for it
(`_get_skill_detector()`), which tests patch to substitute a fake detector.
**Done when:** TC-GUARD-01 through TC-GUARD-08 pass.

```
TC-GUARD-01  [unit]  uut: check_skill_subset(tailored_item, source_item)
  given   source item with demonstrated_skills=['python', 'fastapi']; the
          internal detector is patched to report {'python'} for the text
  when    check_skill_subset is called
  then    passes — the detected skill is authorised for this item
  traces  tailoring.md §3; §4; invariant 2

TC-GUARD-02  [unit]  uut: check_skill_subset(tailored_item, source_item)
  given   source item with demonstrated_skills=['python', 'fastapi']; the
          internal detector is patched to report {'leadership'} for the text
  when    check_skill_subset is called
  then    flagged — 'leadership' is an unauthorised skill attribution
  traces  tailoring.md §3; §4; invariant 2

TC-GUARD-03  [unit]  uut: check_skill_subset(tailored_item, source_item)
  given   the internal detector is patched to report no skills for the text
  when    check_skill_subset is called
  then    passes — nothing to check
  traces  tailoring.md §4

TC-GUARD-04  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source = "Built backend services with Postgres"
  when    tailored = "Developed backend services using PostgreSQL"
  then    passes — synonym and tense change only; no new numeral or named entity
  traces  tailoring.md §4; invariant 3

TC-GUARD-05  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source = "Built backend services"
  when    tailored = "Led a team of 5 to build backend services"
  then    flagged — "5" is a new numeral absent from source. And "team" is not mentioned in backend services. - fabrication of fact
  traces  tailoring.md §4; invariant 3

TC-GUARD-06  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source contains "5 engineers"
  when    tailored preserves "5"
  then    fails — engineers are removed from the phrasing, completely alters the meaning
  traces  tailoring.md §4; invariant 3

TC-GUARD-07  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source = "worked on backend services"
  when    tailored introduces "AWS" (absent from source)
  then    flagged — new named entity absent from source
  traces  tailoring.md §4; invariant 3

TC-GUARD-08  [int]   uut: check_skill_subset with the real internal detector (not patched)
  given   the default detector from `_get_skill_detector()` — the real
          resume-ats-optimizer keyword model — and a ProfileItem with known
          demonstrated_skills
  when    check_skill_subset runs against a text sample
  then    the real detector surfaces the skill IDs present in the text, and the
          guard flags any that are absent from demonstrated_skills
  traces  tailoring.md §7 (resume-ats-optimizer keyword model integration)
```

---

## WP3 — LLM tailoring call + prompt assembly

**Depends on:** WP0
**Deliverables:** `assemble_prompt(jd, profile) -> str`,
`call_llm_tailor(jd, profile, llm) -> TailoredSelection`. LLM injected and
mocked in all tests.
**Done when:** TC-PROMPT-01 through TC-PROMPT-08 pass.

```
TC-PROMPT-01  [unit]  uut: assemble_prompt(jd, profile)
  given   a JD text string and a Profile
  when    prompt is assembled
  then    JD text is present in the prompt
  traces  tailoring.md §6

TC-PROMPT-02  [unit]  uut: assemble_prompt(jd, profile)
  given   a Profile with experiences, projects, skills, demonstrated_skills
  when    prompt is assembled
  then    all content fields are present in the prompt
  traces  tailoring.md §6

TC-PROMPT-03  [unit]  uut: assemble_prompt(jd, profile)
  given   a Profile containing identity fields (name, email, phone, links)
  when    prompt is assembled
  then    identity fields are absent from the prompt
  traces  tailoring.md §2 — identity tier is never LLM-visible

TC-PROMPT-04  [unit]  uut: assemble_prompt(jd, profile)
  given   the tailoring domain knowledge files are loaded
          (resume-tailor, resume-ats-optimizer, resume-section-builder)
  when    prompt is assembled
  then    domain knowledge content is present in the prompt
  traces  tailoring.md §9

TC-PROMPT-05  [unit]  uut: call_llm_tailor(jd, profile, llm=mock_valid)
  given   mock LLM returns syntactically valid TailoredSelection JSON
  when    call_llm_tailor is invoked
  then    returns a TailoredSelection instance
  traces  tailoring.md §8; invariant 5

TC-PROMPT-06  [unit]  uut: call_llm_tailor(jd, profile, llm=mock_malformed)
  given   mock LLM returns malformed JSON
  when    call_llm_tailor is invoked
  then    raises a validation error — does not silently produce None or
          partial data
  traces  invariant 5

TC-PROMPT-07  [unit]  uut: call_llm_tailor(jd, profile, llm=mock_bad_ref)
  given   mock LLM returns JSON with a ref_id not present in the Profile
  when    call_llm_tailor is invoked
  then    raises ValidationError (caught by WP0 schema validator)
  traces  tailoring.md §2; invariant 1

TC-PROMPT-08  [int]   uut: call_llm_tailor with a full Profile + JD fixture
  given   realistic Profile and JD fixture; mock LLM returns a
          representative TailoredSelection
  when    call_llm_tailor is invoked
  then    output passes WP0 schema validation end-to-end without error
  traces  tailoring.md §8; invariants 1, 5
```

---

## WP4 — Public entry point: `tailor()`

This is the layer's single public function and its only supported entry point.
It takes a job description and a profile, runs the full single pass (LLM call →
schema validation → truthfulness guards → render to a PDF on disk), and returns
an `ArtifactResult` that names the file. On any failure it raises
`TailoringError`; it never returns a partial result and it never retries. It
performs no database read or write of any kind — registering the artifact and
advancing the job's state are the caller's job (see "Caller contract" below).

**Depends on:** WP2, WP3, WP5
**Deliverables:** `tailor(job_description, profile, *, llm, template_path,
output_dir, save_debug_artifacts=False) -> ArtifactResult`;
the `ArtifactResult` result type (the PDF path and its kind); the
`TailoringError` exception (carrying a reason and, for guard failures, the list
of violations); and the `save_debug_artifact(...)` helper. The skill detector is
internal to the guard module (WP2) and is not a parameter here. The layer writes
the PDF to disk (option 1 boundary) and returns its path; it does not hold the
bytes in memory for the caller.
**Done when:** TC-ORCH-01 through TC-ORCH-08 pass.

```
TC-ORCH-01  [unit]  uut: tailor(jd, profile, llm=mock_valid)
  given   mock LLM returns a valid selection whose text trips no guard
  when    tailor is invoked
  then    returns an ArtifactResult naming  a PDF file that exists on disk (a directory)
  traces  tailoring.md §5-boundary; §6

TC-ORCH-02  [unit]  uut: tailor(jd, profile, llm=mock_malformed)
  given   mock LLM returns malformed JSON
  when    tailor is invoked
  then    raises TailoringError with reason 'schema_invalid'; the render step
          is never reached; no file is written
  traces  tailoring.md §6; invariant 7

TC-ORCH-03  [unit]  uut: tailor(jd, profile, llm=mock_unauthorized_skill)
  given   mock LLM returns a valid schema, but a tailored bullet surfaces a
          skill absent from that item's demonstrated_skills (trips the real
          skill-subset guard)
  when    tailor is invoked
  then    raises TailoringError with reason 'guard_violation'; the failure is
          logged at CRITICAL; no render happens and no file is written
  traces  tailoring.md §4; §6; invariant 7

TC-ORCH-04  [unit]  uut: tailor(jd, profile, llm=mock_new_specific)
  given   mock LLM returns a valid schema, but a tailored bullet adds a numeral
          absent from the source (trips the real no-new-specifics guard)
  when    tailor is invoked
  then    raises TailoringError with reason 'guard_violation'; the failure is
          logged at CRITICAL; no render happens and no file is written
  traces  tailoring.md §4; §6; invariant 7

TC-ORCH-05  [unit]  uut: tailor(...) with a renderer that fails to compile
  given   the LLM output is valid and guards pass, but the LaTeX compile fails
  when    tailor is invoked
  then    raises TailoringError; no partial ArtifactResult is returned
  traces  tailoring.md §6; invariant 7

TC-ORCH-06  [unit]  uut: save_debug_artifact(tex_source, output_dir, enabled=True)
  given   save_debug_artifacts is True
  when    tailor runs
  then    a debug/ folder is created beside the output and the intermediate
          .tex is written into it
  traces  tailoring.md §6

TC-ORCH-07  [unit]  uut: save_debug_artifact(tex_source, output_dir, enabled=False)
  given   save_debug_artifacts is False (the default)
  when    tailor runs
  then    no debug/ folder is created
  traces  tailoring.md §6

TC-ORCH-08  [int]   uut: tailor(...) end-to-end with a mock LLM and the real renderer
  given   a realistic Profile and JD; mock LLM returns a passing TailoredSelection
  when    tailor is invoked
  then    returns an ArtifactResult; no backend/registration call is made
          anywhere in the layer (the boundary holds — the layer touches no DB)
  traces  tailoring.md §5-boundary; §9
```

---

## WP5 — Renderer

**Depends on:** WP0
**Deliverables:** `latex_escape(text) -> str`, `build_render_model(selection, profile) -> dict`,
`render(selection, profile, *, template_path, output_dir) -> Path`. The renderer
writes the compiled PDF to disk and returns its path; WP4 calls it as the final
step of `tailor()`. The template owns all LaTeX syntax; the LLM never produces
layout. 
**Done when:** TC-RENDER-01 through TC-RENDER-09 pass.

```
TC-RENDER-01  [unit]  uut: build_render_model(selection, profile)
  given   a TailoredSelection and a Profile
  when    render model is built
  then    identity fields in the model are byte-identical to those in the
          source Profile
  traces  tailoring.md §2; invariant 4

TC-RENDER-02  [unit]  uut: latex_escape(text)
  given   a string containing all LaTeX special chars: & % $ # _ { } ~ ^ \
  when    latex_escape is applied
  then    all special chars are correctly escaped
  traces  tailoring.md §7

TC-RENDER-03  [unit]  uut: latex_escape applied inside render pipeline
  given   a bullet containing an unescaped '&'
  when    the bullet is passed through latex_escape before template substitution
  then    '&' is replaced with '\&' in the .tex output
  traces  tailoring.md §7

TC-RENDER-04  [unit]  uut: render(selection, profile)
  given   a fixture TailoredSelection + Profile
  when    render is called
  then    .tex output matches the stored snapshot byte-for-byte
  traces  invariant 8

TC-RENDER-05  [unit]  uut: render(selection, profile)
  given   selection with experience_order=[A, B] and project_order=[C]
  when    rendered
  then    experiences appear in order A then B; project C is present;
  traces  tailoring.md §2; §7

TC-RENDER-06  [unit]  uut: render(selection, profile)
  given   the entry-level template structure
  when    rendered
  then    Skills and Projects sections precede Experience; Education section
          is present; 
  traces  tailoring.md §7

TC-RENDER-07  [unit]  uut: render(selection, profile)
  given   a TailoredItem bullet containing a raw LaTeX string (e.g. '\textbf{foo}')
  when    render is called
  then    the .tex output contains the escaped version — the LLM cannot inject
          layout commands through content strings
  traces  tailoring.md §7

TC-RENDER-08  [int]   uut: tectonic compile step
  given   a valid .tex file produced from a fixture
  when    tectonic is invoked
  then    exits with code 0 and surfaces/raises a clear error on bad input — does not hang
  traces  tailoring.md §7

TC-RENDER-09  [live]  uut: render(selection, profile) end-to-end — Docker only
  given   a fixture TailoredSelection + Profile
  when    render is called including the tectonic compile step
  then    produces a non-empty PDF; tectonic exits 0
  traces  invariant 8
```

---W

## Caller contract (owned by the scheduler and agent layers)

The tailoring layer's only public entry is `tailor(job_description, profile) ->
ArtifactResult`, which raises `TailoringError` on failure. Everything that reads
from or writes to the database sits with the caller, which is the scheduler for
the daily batch and the agent for the on-demand tool. 

The single most important property this boundary buys is that the tailoring layer
can be tested end-to-end with only a mock LLM and no backend at all, because it
has no database dependency to stub. TC-ORCH-08 asserts exactly this.