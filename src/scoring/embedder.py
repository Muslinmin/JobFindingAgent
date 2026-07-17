"""The Embedder seam — talks to the cloud embedding service (scoring_v2.md WP2).

A separate injected piece so tests never hit the real network: hand the
scorer a fake embedder that returns canned vectors and it can't tell the
difference. Routed through LiteLLM (same library and same one-key-any-
provider pattern as agent/llm_client.py) so swapping the embedding provider
later is a settings change, not a rewrite.
"""

from __future__ import annotations

from typing import Protocol

from litellm import aembedding

from app.config import settings


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LiteLLMEmbedder:
    def __init__(self, model: str | None = None):
        self.model = model or settings.embedding_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await aembedding(
            model=self.model,
            input=texts,
            api_key=settings.embedding_api_key or None,
        )
        return [item["embedding"] for item in response.data]
