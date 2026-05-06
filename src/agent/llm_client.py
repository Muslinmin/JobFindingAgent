import os

from litellm import completion

from app.config import settings

_KEY_MAP = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key":    "OPENAI_API_KEY",
    "gemini_api_key":    "GEMINI_API_KEY",
}


def _export_keys() -> None:
    for attr, env_var in _KEY_MAP.items():
        value = getattr(settings, attr, "")
        if value:
            os.environ[env_var] = value


class LLMClient:
    def __init__(self, model: str | None = None):
        _export_keys()
        self.model = model or settings.model

    def chat(self, messages: list, tools: list) -> object:
        return completion(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
