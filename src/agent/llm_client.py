from litellm import completion

from app.config import settings


class LLMClient:
    def __init__(self, model: str | None = None):
        self.model = model or settings.model

    def chat(self, messages: list, tools: list) -> object:
        return completion(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
