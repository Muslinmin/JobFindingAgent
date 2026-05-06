import time

from litellm import completion
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
