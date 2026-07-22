# Concurrency & Turn-Budget Delta — Agent Layer

> Delta document against `agent_v2.md`, required before WP-A6. WP-C1, C1a,
> C2, C3, and C4 (below) are executed. The full findings, decisions, test
> catalog, and reasoning that justified them are no longer here — they did
> their job informing the changes below and are preserved in git history if
> ever needed again. This file is now a short changelog plus a pointer to
> where the remaining concurrency work is tracked.

**One-line problem:** this app runs one event loop. `src/app/main.py`'s
`lifespan` starts the FastAPI server, both Telegram bot polling loops, and
APScheduler's six jobs on that one loop — anything that blocks it freezes
all three at once. The agent layer's LLM calls and per-session state are
where that risk concentrates.

---

## What shipped (WP-C1, C1a, C2, C3, C4)

- **`LLMClient` deleted.** The synchronous, blocking-retry v1 leftover
  (`time.sleep` backoff, no production callers) is gone from
  `src/agent/llm_client.py` and `src/test/unit/test_llm_client.py`.
- **`AsyncLLMClient` → `TaskLLMClient`.** Same single-shot, fail-fast
  behaviour, renamed across every caller (`main.py`, `scheduler/bootstrap.py`,
  `scheduler/jobs/tailor.py`, both `scripts/*.py`,
  `test/integration/test_pipeline_live.py`, plus the structural-typing
  docstring mentions in `scheduler/jobs/follow_up.py` and `query_regen.py`)
  so the name now encodes retry policy, not async-ness.
- **`agent_v2.md` corrected.** Every agent-layer signature that touches
  `context.py`, a service, or the LLM is now `async def` (was incorrectly
  specified as sync — see its §2/§2a); `loop.run`'s `llm` parameter is
  retyped from the deleted `LLMClient` to the still-to-be-built
  `AgentLLMClient`.
- **Timeout ladder settings added** to `app/config.py`: `llm_call_timeout_s`
  (60), `agent_turn_deadline_s` (180), `backend_read_timeout_s` (240),
  `llm_retry_wait_s` (5), `llm_max_retries` (3). Ordering (`call < turn <
  client`) is asserted in `src/test/unit/test_config.py`, not just assumed.
- **Explicit httpx timeouts** on both Telegram bot backend clients
  (`telegram_bot/shared/bootstrap.py::build_applications`): the chat client
  takes `chat_read_timeout_s` (wired from `main.py` as
  `settings.backend_read_timeout_s`), the notifications client keeps a much
  shorter default since it only ever calls deterministic, non-LLM routes
  (`/action`, `/follow-up`). Neither relies on httpx's 5s implicit default
  any more — tested in `src/test/telegram_bot/shared/test_bootstrap.py`.

---

## What's left

`WP-C1b` (`AgentLLMClient`) and `WP-C5` (per-session `asyncio.Lock` +
agent construction in `lifespan`) are **done**, delivered as part of
`agent_v2.md`'s WP-A7 and WP-A8 — see those entries for what landed.

Two findings from that build worth carrying forward:

- **A per-session lock is not sufficient on its own.** Session
  *resolution* has to be serialised too, by a separate global lock, or two
  simultaneous first messages each start their own session and the
  per-session locks are keyed differently — so the turns never serialise
  at all. The resolution lock is held for a read and at most one insert;
  the slow part of the turn stays under the per-session lock, so
  conversations still run concurrently.
- **`AgentLLMClient` must re-raise `asyncio.CancelledError`.** Catching it
  as a retryable failure would let a turn the deadline has already
  abandoned keep sleeping and retrying.

`WP-C6` (a Telegram-side typing indicator so a turn taking up to the full
deadline doesn't look like a dead bot) remains open and isn't claimed by
any work package yet. It matters more now that the loop is real: a
five-iteration turn against a reasoning model is visibly slow.
