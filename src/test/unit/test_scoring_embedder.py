from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from scoring.embedder import LiteLLMEmbedder


def _mock_response(vectors: list[list[float]]):
    return MagicMock(data=[{"embedding": v} for v in vectors])


# ── instantiation ─────────────────────────────────────────────────────────────

def test_embedder_uses_settings_model_by_default():
    embedder = LiteLLMEmbedder()
    assert embedder.model == settings.embedding_model


def test_embedder_accepts_model_override():
    embedder = LiteLLMEmbedder(model="test-embedding-model")
    assert embedder.model == "test-embedding-model"


# ── embed call ────────────────────────────────────────────────────────────────

async def test_embedder_calls_litellm_aembedding():
    mock_aembedding = AsyncMock(return_value=_mock_response([[0.1, 0.2]]))
    with patch("scoring.embedder.aembedding", mock_aembedding):
        await LiteLLMEmbedder(model="test-model").embed(["hello"])
    mock_aembedding.assert_called_once()


async def test_embedder_passes_texts_as_input():
    mock_aembedding = AsyncMock(return_value=_mock_response([[0.1], [0.2]]))
    with patch("scoring.embedder.aembedding", mock_aembedding):
        await LiteLLMEmbedder(model="test-model").embed(["a", "b"])
    assert mock_aembedding.call_args.kwargs["input"] == ["a", "b"]


async def test_embedder_passes_correct_model():
    mock_aembedding = AsyncMock(return_value=_mock_response([[0.1]]))
    with patch("scoring.embedder.aembedding", mock_aembedding):
        await LiteLLMEmbedder(model="test-model-override").embed(["hi"])
    assert mock_aembedding.call_args.kwargs["model"] == "test-model-override"


async def test_embedder_parses_vectors_from_response():
    mock_aembedding = AsyncMock(return_value=_mock_response([[1.0, 2.0], [3.0, 4.0]]))
    with patch("scoring.embedder.aembedding", mock_aembedding):
        vectors = await LiteLLMEmbedder(model="test-model").embed(["a", "b"])
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]


async def test_embedder_preserves_input_order_in_output():
    mock_aembedding = AsyncMock(return_value=_mock_response([[9.0], [8.0], [7.0]]))
    with patch("scoring.embedder.aembedding", mock_aembedding):
        vectors = await LiteLLMEmbedder(model="test-model").embed(["x", "y", "z"])
    assert vectors == [[9.0], [8.0], [7.0]]
