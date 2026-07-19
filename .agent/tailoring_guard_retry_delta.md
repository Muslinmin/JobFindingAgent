# Tailoring Layer — Delta: Numeral Context Anchor + Bounded Guard-Retry Loop

> Delta to `tailoring.md` and `tailoring_build.md`. Touches `src/tailoring/guards.py`,
> `src/tailoring/prompt.py`, `src/tailoring/tailor.py`, and their test files.
> **Supersedes** `tailoring.md` §5's "No retry, no partial render" line.

---

## 0. One-line summary

Two independent, additive changes: (1) `guards.py`'s clause-break numeral check gets
a backward content-word anchor instead of dropping context entirely; (2) `tailor.py`
gains a bounded retry loop (hard-coded max 2 retries) that re-prompts the *same*
tailoring LLM call with the prior guard violations whenever any guard fires, before
failing. The deterministic guards themselves are unchanged in role: they remain the
sole, unmodified arbiter of pass/fail on every attempt. No LLM ever overturns a guard
verdict — it only gets a chance to produce different text that the same check re-runs
against.

---

## 1. Scope boundary

**In scope:**
- `guards.py` — `_numeral_contexts` only.
- `prompt.py` — `assemble_prompt`, `call_llm_tailor` (new optional parameter).
- `tailor.py` — new retry-wrapping helper.
- `tailoring.md` §5 and `tailoring_build.md`'s reason-list note (doc updates).
- `test_tailoring_guards.py`, `test_tailoring_orchestrator.py` (new/updated tests).

**Out of scope:** `check_skill_subset`, `render.py`, `schema.py`, the
`TailoringError` reason taxonomy (unchanged), the Caller contract (unchanged —
retries happen inside `tailor()`, invisible to both callers named in
`tailoring_build.md`).

---

## 2. Requirements

- **R1.** A numeral immediately followed by a clause-break character (`,` `;` `:`) is
  anchored to the nearest preceding content word instead of `None`, so its context is
  still checked against the source rather than being ignored.
- **R2.** When `_collect_guard_violations` returns any violation — from either guard,
  not just the numeral case — `tailor()` retries the tailoring LLM call rather than
  failing immediately.
- **R3.** Each retry's prompt includes the specific violations from the previous
  attempt, plus a note that the checker can produce false positives, so the model
  knows to use judgment rather than blindly stripping anything mentioned.
- **R4.** Retries are capped at a hard-coded constant of 2 — three tailoring attempts
  total (one original + two retries) — with no dynamic backoff or config-driven
  tuning for now.
- **R5.** If violations persist after the retry budget is exhausted, `tailor()`
  raises `TailoringError("guard_violation", violations=...)` exactly as it does
  today. No change to the exception shape or the caller-facing contract.
- **R6.** The guard functions (`check_skill_subset`, `check_no_new_specifics`) do not
  change behavior across retries. Same input always produces the same verdict; only
  the tailored text being checked changes between attempts.

---

## 3. Data model / API contract

### 3.1 `guards.py` — backward anchor for clause-break numerals

```python
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "by", "with", "for", "to", "and",
})

def _backward_anchor(text: str, match_start: int) -> str | None:
    """Walk backward word-by-word from `match_start`, skipping `_STOPWORDS`,
    and return the first content word (lowercased). Returns None if the
    numeral opens the text, or only stopwords precede it — same fallback
    as today's clause-break handling in that edge case."""
```

`_numeral_contexts` change: when a clause-break character follows the numeral,
store `(numeral, f"<-{anchor}")` if `_backward_anchor` finds one, else
`(numeral, None)` as before. The `<-` prefix keeps backward-anchored pairs from
colliding with forward `(numeral, next_word)` pairs that happen to share the same
word string.

`_new_numerals`'s comparison logic is unchanged — it already treats `None` loosely
and any non-`None` pairing strictly. The new `<-word` pairing is just another
instance of the existing strict branch.

### 3.2 `prompt.py` — retry-aware prompt assembly

```python
def assemble_prompt(
    jd: str, profile: Profile, *, previous_violations: list[str] | None = None
) -> str:
    """Unchanged when previous_violations is None (default) — first-attempt
    prompts stay byte-for-byte identical to today. When given, appends a
    block naming each violation and stating that the automated checker can
    produce false positives, so the model should only change text that is
    actually inaccurate."""

async def call_llm_tailor(
    jd: str, profile: Profile, llm: LLMTailor, *, previous_violations: list[str] | None = None
) -> TailoredSelection:
    """Passes previous_violations through to assemble_prompt. No other
    behavior change."""
```

### 3.3 `tailor.py` — bounded retry loop

```python
MAX_GUARD_RETRIES: Final[int] = 2  # hard-coded; not config-driven yet

async def _tailor_with_guard_retries(
    job_description: str, profile: Profile, llm: LLMTailor
) -> TailoredSelection:
    """Wraps call_llm_tailor + _collect_guard_violations in a loop bounded
    by MAX_GUARD_RETRIES. Logs WARNING on each retryable attempt, CRITICAL
    only on the terminal failure (matches today's CRITICAL-on-fail
    behavior exactly). Raises TailoringError("guard_violation", ...) on
    exhaustion — identical shape to today's raise."""
```

`tailor()` change: replaces its current single `call_llm_tailor` +
`_collect_guard_violations` block with a call to `_tailor_with_guard_retries`.
Everything after — render step, debug artifacts — is untouched.

Unchanged: `TailoringError`, `ArtifactResult`, `_collect_guard_violations`,
`save_debug_artifact`.

---

## 4. Work packages

| WP | Covers | Depends on |
|---|---|---|
| WP-A | `guards.py` backward anchor (R1) | none — ships alone |
| WP-B | `prompt.py` retry-feedback param (R3) | none |
| WP-C | `tailor.py` retry loop (R2, R4, R5, R6) | WP-B (needs the new param) |
| WP-D | Doc updates — rewrite `tailoring.md` §5, add a note to `tailoring_build.md`'s reason list confirming `guard_violation` is now only reached after retries are exhausted | WP-C |

---

## 5. Test catalog additions

- **TC-GUARD-09** — numeral before a comma; backward anchor differs between source
  and tailored text → flagged (the "5 clients" hiding behind a comma case).
- **TC-GUARD-10** — numeral before a comma; backward anchor matches source → passes
  (the "$20,000, ensuring/maintaining" paraphrase case).
- **TC-GUARD-11** — numeral opens the sentence with a clause-break immediately after
  (no content word precedes it) → falls back to bare-presence check, unchanged from
  today.
- **TC-RETRY-01** — first attempt has violations, second attempt is clean →
  `tailor()` succeeds; `call_llm_tailor` invoked exactly twice; second call's prompt
  contains the first attempt's violations.
- **TC-RETRY-02** — all three attempts have violations → `tailor()` raises
  `TailoringError("guard_violation", ...)`; `call_llm_tailor` invoked exactly three
  times (1 + `MAX_GUARD_RETRIES`).
- **TC-RETRY-03** — first attempt is clean → `tailor()` succeeds; `call_llm_tailor`
  invoked exactly once (no regression to the common, no-violation case).

---

## 6. Build order

1. **WP-A** (`guards.py`) — self-contained, testable in isolation with
   TC-GUARD-09/10/11.
2. **WP-B** (`prompt.py`) — additive parameter, testable in isolation.
3. **WP-C** (`tailor.py`) — depends on WP-B; testable with TC-RETRY-01/02/03 using a
   fake `LLMTailor` that returns different violations per call, matching the
   existing test pattern already used elsewhere in this layer.
4. **WP-D** (docs) — last, once behavior is locked in.
