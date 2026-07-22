"""The two LLM clients, split by retry policy rather than by async-ness.

`TaskLLMClient` is single-shot and fails fast: a scheduled job that hits a
rate limit should surface as a failed job, not silently absorb minutes of
backoff inside a nightly batch.

`AgentLLMClient` is the interactive one. A user is waiting on the other
end of a Telegram message, so a transient 429 is worth retrying rather
than turning into "something went wrong" — but every wait is bounded, and
the whole turn sits under a deadline the caller applies
(`concurrencyFor_agentV2.md` §3: llm call < turn deadline < client
timeout). Both are native-async: `await acompletion`, `await
asyncio.sleep` — never `time.sleep`, which would freeze the single event
loop this app runs the API, both bots, and the scheduler on.
"""

import asyncio

from litellm import acompletion
from loguru import logger

from app.config import settings


class LLMCallFailed(Exception):
    """Every attempt failed. Carries the last underlying error as `__cause__`
    so the loop can log a real diagnosis, not just "LLM unavailable"."""


class TaskLLMClient:
    """Single-message-in, text-out LLM access — no tool-calling, no retry.

    Built for the tailoring layer (tailoring_build.md WP3) and shared by the
    scheduler's other single-pass jobs (follow-up drafting, query regen):
    a rate limit or transient failure here is meant to surface immediately
    as a failed job (tailor() wraps it into a TailoringError) rather than be
    silently absorbed by a backoff loop the way the interactive agent's
    client retries (see concurrencyFor_agentV2.md).
    """

    def __init__(self, model: str | None = None):
        self.model = model or settings.model

    async def complete(self, prompt: str) -> str:
        response = await acompletion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            api_key=settings.model_api_key or None,
        )
        return response.choices[0].message.content


class AgentLLMClient:
    """Tool-calling, retrying LLM access for the interactive ReAct loop.

    Returns the provider's raw `message` object rather than a parsed
    result: the loop needs both the text and any `tool_calls`, and it has
    to append the assistant message back onto the conversation verbatim for
    the next iteration to make sense to the provider.

    Retries use a *fixed* wait, not exponential backoff. The turn deadline
    is the real budget, and a fixed wait makes the worst case something you
    can compute (`llm_max_retries × llm_retry_wait_s + call time`) and check
    against that deadline, which config asserts at import.
    """

    def __init__(
        self,
        model: str | None = None,
        max_retries: int | None = None,
        retry_wait_s: int | None = None,
        timeout_s: int | None = None,
    ):
        self.model = model or settings.model
        self.max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self.retry_wait_s = retry_wait_s if retry_wait_s is not None else settings.llm_retry_wait_s
        self.timeout_s = timeout_s if timeout_s is not None else settings.llm_call_timeout_s

    async def chat(self, messages: list[dict], tools: list[dict] | None = None):
        """One completion, retried on failure. Raises `LLMCallFailed` when
        every attempt is exhausted.

        `asyncio.CancelledError` is deliberately not caught: when the
        caller's `wait_for` deadline fires it cancels this coroutine, and
        swallowing that to sleep and retry would keep burning time on a turn
        that has already given up.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    acompletion(
                        model=self.model,
                        messages=messages,
                        tools=tools or None,
                        api_key=settings.model_api_key or None,
                        # Reasoning models reject function tools on
                        # /v1/chat/completions unless reasoning_effort is
                        # 'none' (live-verified against gpt-5.6-luna, which
                        # 400s otherwise). Extended per-call reasoning is
                        # also not what this loop wants: it reasons ACROSS
                        # iterations, one tool call at a time. `drop_params`
                        # makes the flag a no-op on providers that have no
                        # such knob — Gemini raises UnsupportedParamsError
                        # if it is passed through to them.
                        reasoning_effort="none",
                        drop_params=True,
                    ),
                    timeout=self.timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    logger.warning(
                        f"AgentLLMClient: attempt {attempt}/{self.max_retries} failed "
                        f"({type(e).__name__}: {e}); retrying in {self.retry_wait_s}s"
                    )
                    await asyncio.sleep(self.retry_wait_s)

        raise LLMCallFailed(f"all {self.max_retries} attempts failed") from last_error
