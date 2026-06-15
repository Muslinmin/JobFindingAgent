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
WP0 → {WP2, WP3, WP5}
{WP2, WP3} → WP4
WP5 → WP6
{WP4, WP6} → WP7
```

WP2, WP3, and WP5 are a parallel front — all unblock from WP0 and can be
built simultaneously. They converge at WP4 and WP6, which converge at WP7.

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
**Deliverables:** `check_skill_subset(tailored_item, source_item, skill_detector)`,
`check_no_new_specifics(tailored_text, source_text)` — both pure functions,
no I/O.
**Done when:** TC-GUARD-01 through TC-GUARD-08 pass.

```
TC-GUARD-01  [unit]  uut: check_skill_subset(tailored_item, source_item, skill_detector)
  given   source item with demonstrated_skills=['python', 'fastapi']
  when    tailored bullet surfaces 'python'
  then    passes — skill is authorised for this item
  traces  tailoring.md §3; §4; invariant 2

TC-GUARD-02  [unit]  uut: check_skill_subset(tailored_item, source_item, skill_detector)
  given   source item with demonstrated_skills=['python', 'fastapi']
  when    tailored bullet surfaces 'leadership' (absent from demonstrated_skills)
  then    flagged — unauthorised skill attribution
  traces  tailoring.md §3; §4; invariant 2

TC-GUARD-03  [unit]  uut: check_skill_subset(tailored_item, source_item, skill_detector)
  given   tailored rewrite contains no detectable skill terms
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
  then    flagged — "5" is a new numeral absent from source
  traces  tailoring.md §4; invariant 3

TC-GUARD-06  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source contains "5 engineers"
  when    tailored preserves "5"
  then    passes — numeral present in source
  traces  tailoring.md §4; invariant 3

TC-GUARD-07  [unit]  uut: check_no_new_specifics(tailored_text, source_text)
  given   source = "worked on backend services"
  when    tailored introduces "AWS" (absent from source)
  then    flagged — new named entity absent from source
  traces  tailoring.md §4; invariant 3

TC-GUARD-08  [int]   uut: check_skill_subset wired with resume-ats-optimizer keyword detector
  given   a text sample and a ProfileItem with known demonstrated_skills
  when    the keyword detector identifies skill IDs present in the text
  then    correctly surfaces present skill IDs and flags those absent from
          demonstrated_skills
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

## WP4 — Tailoring orchestration

**Depends on:** WP2, WP3
**Deliverables:** `tailor_once(jd, profile, llm, guard_checker) -> TailoredSelection | TailoringFailure`.
Single-pass: call LLM → validate schema → run guards → return. Guard
violation logs at CRITICAL and returns a failure; no retry, no partial output.
**Done when:** TC-ORCH-01 through TC-ORCH-05 pass.

```
TC-ORCH-01  [unit]  uut: tailor_once(jd, profile, llm=mock_valid, guards=mock_pass)
  given   mock LLM returns valid selection; both guards pass
  when    tailor_once is invoked
  then    returns a TailoredSelection
  traces  tailoring.md §6

TC-ORCH-02  [unit]  uut: tailor_once(jd, profile, llm=mock_malformed, guards=mock_pass)
  given   mock LLM returns malformed JSON
  when    tailor_once is invoked
  then    logs error, returns TailoringFailure; guard check is never attempted
  traces  tailoring.md §6; invariant 7

TC-ORCH-03  [unit]  uut: tailor_once(jd, profile, llm=mock_valid, guards=mock_skill_fail)
  given   mock LLM returns valid schema; skill-subset guard fails
  when    tailor_once is invoked
  then    logs error at CRITICAL, returns TailoringFailure cleanly
  traces  tailoring.md §4; §6; invariant 7

TC-ORCH-04  [unit]  uut: tailor_once(jd, profile, llm=mock_valid, guards=mock_specifics_fail)
  given   mock LLM returns valid schema; no-new-specifics guard fails
  when    tailor_once is invoked
  then    logs error at CRITICAL, returns TailoringFailure cleanly
  traces  tailoring.md §4; §6; invariant 7

TC-ORCH-05  [int]   uut: tailor_once with a full Profile + JD fixture (mock LLM)
  given   realistic Profile and JD; mock LLM returns a passing TailoredSelection
  when    tailor_once is invoked end-to-end
  then    returns a validated TailoredSelection with no errors
  traces  tailoring.md §6
```

---

## WP5 — Renderer

**Depends on:** WP0
**Deliverables:** `latex_escape(text) -> str`, `build_render_model(selection, profile) -> dict`,
`render(selection, profile) -> Path`. Template owns all LaTeX syntax; the LLM
never produces layout. One `[live]` test (Docker only).
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
          unselected items are absent from the .tex output
  traces  tailoring.md §2; §7

TC-RENDER-06  [unit]  uut: render(selection, profile)
  given   the entry-level template structure
  when    rendered
  then    Skills and Projects sections precede Experience; Education section
          is present; each item contains 3–5 bullets
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
  then    exits with code 0 and surfaces a clear error on bad input — does not hang
  traces  tailoring.md §7

TC-RENDER-09  [live]  uut: render(selection, profile) end-to-end — Docker only
  given   a fixture TailoredSelection + Profile
  when    render is called including the tectonic compile step
  then    produces a non-empty PDF; tectonic exits 0
  traces  invariant 8
```

---

## WP6 — Artifact registration

**Depends on:** WP5 + backend (`POST /jobs/{id}/artifacts`, FSM)
**Deliverables:** `register_artifact(job_id, pdf_path, kind) -> ArtifactRecord`,
FSM advance to `PENDING_APPROVAL`, `save_debug_artifacts` flag + `debug/` folder.
**Done when:** TC-ARTIFACT-01 through TC-ARTIFACT-05 pass.

```
TC-ARTIFACT-01  [int]  uut: register_artifact(job_id, pdf_path, kind='cv_pdf')
  given   a valid job_id and a PDF written to the expected path
  when    register_artifact is called
  then    PDF file exists at the expected path on disk
  traces  tailoring.md §9

TC-ARTIFACT-02  [int]  uut: POST /jobs/{id}/artifacts
  given   a valid job_id
  when    register_artifact calls POST /jobs/{id}/artifacts
  then    artifact record created with correct job_id, path, and kind='cv_pdf'
  traces  tailoring.md §9

TC-ARTIFACT-03  [int]  uut: FSM advance after artifact registration
  given   a successful artifact registration for a TAILORED job
  when    FSM advance is triggered
  then    job transitions to PENDING_APPROVAL
  traces  tailoring.md §9

TC-ARTIFACT-04  [unit]  uut: save_debug_artifacts(tex_path, output_dir, enabled=True)
  given   save_debug_artifacts=True
  when    tailoring completes
  then    debug/ folder is created beside the output; .tex file is written there
  traces  tailoring.md §6

TC-ARTIFACT-05  [unit]  uut: save_debug_artifacts(tex_path, output_dir, enabled=False)
  given   save_debug_artifacts=False (default)
  when    tailoring completes
  then    no debug/ folder is created
  traces  tailoring.md §6
```

---

## WP7 — Entry points

**Depends on:** WP4, WP6
**Deliverables:** `select_batch(session, tailor_batch_size) -> list[Job]`,
`run_tailor_batch(session, tailor_batch_size, tailor_service)`,
`tailor_resume` agent tool. Batch tested as a plain function — not via the
scheduler. Tool and batch share one service; no divergent code path.
**Done when:** TC-ENTRY-01 through TC-ENTRY-07 pass.

```
TC-ENTRY-01  [unit]  uut: select_batch(session, tailor_batch_size=3)
  given   multiple SCORED jobs with different scores
  when    select_batch is called
  then    returns the top 3 by score DESC
  traces  architecture_v2.md flow §4

TC-ENTRY-02  [unit]  uut: select_batch(session, tailor_batch_size)
  given   jobs in various FSM states (SCORED, TAILORED, PENDING_APPROVAL,
          DISCOVERED)
  when    select_batch is called
  then    returns only SCORED jobs
  traces  architecture_v2.md flow §4

TC-ENTRY-03  [unit]  uut: run_tailor_batch(session, tailor_batch_size=2, tailor_service)
  given   5 SCORED jobs available
  when    run_tailor_batch is called with tailor_batch_size=2
  then    exactly 2 jobs are processed; the remaining 3 are untouched
  traces  architecture_v2.md flow §4

TC-ENTRY-04  [unit]  uut: run_tailor_batch — guard violation path
  given   a job whose tailoring produces a guard violation
  when    run_tailor_batch processes it
  then    job remains in SCORED; error logged at CRITICAL level;
          no artifact registered
  traces  tailoring.md §6; invariant 7

TC-ENTRY-05  [int]   uut: run_tailor_batch end-to-end (mock LLM)
  given   one SCORED job; mock LLM returning a valid fixture TailoredSelection
  when    run_tailor_batch is called
  then    job transitions SCORED → PENDING_APPROVAL; artifact registered
  traces  architecture_v2.md flow §4; tailoring.md §9

TC-ENTRY-06  [int]   uut: tailor_resume tool (mock LLM)
  given   a specific job_id; mock LLM returning a valid fixture
  when    tailor_resume tool is invoked
  then    same service as batch is called; job transitions to PENDING_APPROVAL
  traces  tailoring.md §9

TC-ENTRY-07  [int]   uut: tailor_resume tool vs run_tailor_batch — shared service
  given   the same job fixture passed to each entry point
  when    each is called with the same input
  then    both invoke the same underlying tailor_once + render pipeline;
          no divergent code path
  traces  tailoring.md §9
```
