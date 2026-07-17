# Tailoring Layer — Status & Usage

> Companion to `tailoring.md` (design decisions) and `architecture_v2.md`
> §5/§7. Implementation is complete (all of WP0–WP5). This file used to be
> the build plan (work packages + a `TC-*` test-case catalog); that plan has
> been executed, so this is now a usage reference instead. The original
> catalog is still there in git history if it's ever needed as a spec again.

---

## What's implemented

`src/tailoring/` is the layer's entire code surface:

| File | Responsibility |
|---|---|
| `schema.py` | `TailoredItem`, `TailoredSelection` — the LLM's output shape. Ref-id / skill-order / order-consistency are validated against a `Profile` via `TailoredSelection.model_validate(data, context={"profile": profile})`. |
| `guards.py` | `check_skill_subset`, `check_no_new_specifics` — the two deterministic truthfulness guards (tailoring.md §3–§4). |
| `prompt.py` | `assemble_prompt`, `call_llm_tailor`, the `LLMTailor` protocol (`async def complete(prompt: str) -> str`). |
| `render.py` | `latex_escape`, `build_render_model`, `render` — Jinja2 templating → `tectonic` → PDF. |
| `tailor.py` | `tailor()`, the layer's single public entry point; `TailoringError`; `ArtifactResult`; `save_debug_artifact`. |
| `prompts/tailoring.md` | Domain-knowledge content (`resume-tailor` / `resume-ats-optimizer` / `resume-section-builder`) injected into the LLM prompt. |
| `templates/cv.tex.jinja` | The LaTeX template. Owns all LaTeX syntax — the LLM never emits any. |

The async LLM client (`AsyncLLMClient`) lives in `src/agent/llm_client.py`,
not in this package, since talking to the LLM provider is a generic concern
shared with the rest of `agent/`.

Tests: `src/test/{unit,integration}/test_tailoring_*.py` and the
`AsyncLLMClient` cases in `test_llm_client.py`. A handful of integration
tests require a local `tectonic` install and skip cleanly without one
(`pytest.mark.skipif(shutil.which("tectonic") is None, ...)`).

---

## How to use

The layer's only public entry point:

```python
from tailoring.tailor import tailor, TailoringError

result = await tailor(
    job_description,        # str
    profile,                 # Profile — profile.loader.load_profile(path)
    llm=llm,                  # anything with `async def complete(prompt: str) -> str`
    template_path=...,        # src/tailoring/templates/cv.tex.jinja
    output_dir=...,           # caller's choice — tailor() will mkdir -p it
    save_debug_artifacts=False,
)
```

- `llm` — in production, `agent.llm_client.AsyncLLMClient()`. Tests pass a
  fake implementing the same one-method `LLMTailor` protocol.
- Returns `ArtifactResult(kind="cv_pdf", path=Path(...))` on success.
- Raises `TailoringError` on any failure. `.reason` is one of
  `"llm_call_failed"`, `"schema_invalid"`, `"guard_violation"`,
  `"render_failed"`; `.violations` is populated only for
  `"guard_violation"`. Single pass, no retry (tailoring.md §5) — deciding
  what happens next (log, notify, leave for the next batch run) is the
  caller's call.
- Does **no database I/O** of any kind. Registering the artifact and
  advancing the job's `ApplicationStatus` are the caller's responsibility.
- The `render()` step shells out to `tectonic` via a blocking subprocess;
  `tailor()` runs it through `asyncio.to_thread` so a multi-second PDF
  compile never stalls the event loop (FastAPI requests, the scheduler, the
  Telegram bot's polling all keep running concurrently).

**Full worked example** — construction, error handling, artifact
registration, and the `SCORED → TAILORED → PENDING_APPROVAL` FSM
transitions — lives in `src/app/tailoring_usage_example.py`. It is a
reference file only (not imported by `main.py`, not run by the app).

---

## Caller contract (owned by the scheduler and agent layers — not yet built)

Two callers are expected to exist eventually, both calling the same
`tailor()`: the daily budgeted batch job (top `tailor_batch_size` `SCORED`
jobs, via the already-built `JobService.top_scored_for_tailoring`) and the
on-demand `tailor_resume` agent tool. `tailoring_usage_example.py` sketches
both call shapes (`run_daily_batch`, `tailor_on_demand`).

The single most important property this boundary buys: the tailoring layer
was built and tested end-to-end with only a mock LLM and no backend at all,
because it has no database dependency to stub.
