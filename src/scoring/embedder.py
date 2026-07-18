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
    def __init__(self, model: str | None = None, timeout: float = 30.0):
        self.model = model or settings.embedding_model
        # Without an explicit timeout, a stalled connection to the
        # embeddings provider hangs indefinitely — litellm/httpx apply no
        # default. Bounding it means a bad request fails fast (raises) and
        # surfaces as a normal scoring failure (scoring_v2.md: "the scorer
        # raises rather than fabricating a 0"), instead of blocking the
        # entire scrape run.
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await aembedding(
            model=self.model,
            input=texts,
            api_key=settings.embedding_api_key or None,
            timeout=self.timeout,
        )
        return [item["embedding"] for item in response.data]
