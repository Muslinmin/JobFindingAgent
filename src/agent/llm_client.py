import time

from litellm import acompletion, completion
from litellm.exceptions import RateLimitError

from app.config import settings


class LLMClient:
    def __init__(self, model: str | None = None):
        self.model = model or settings.model

    def chat(self, messages: list, tools: list) -> object:
        for attempt in range(4):
            try:
                return completion(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    api_key=settings.model_api_key or None,
                )
            except RateLimitError:
                if attempt == 3:
                    raise
                wait = 10 * (2 ** attempt)  # 10s, 20s, 40s
                time.sleep(wait)


class AsyncLLMClient:
    """Single-message-in, text-out LLM access — no tool-calling, no retry.

    Built for the tailoring layer (tailoring_build.md WP3), which is a
    single-pass call: a rate limit or transient failure here is meant to
    surface immediately as a failed job (tailor() wraps it into a
    TailoringError), not be silently absorbed by a backoff loop the way
    `LLMClient.chat` retries for the interactive agent.
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
