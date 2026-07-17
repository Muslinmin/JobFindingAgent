import pytest

from profile.projections import profile_to_text
from profile.schema import Profile, ProfileItem, Skill
from scoring.embedding_scorer import EmbeddingScorer


class FakeEmbedder:
    """Canned-vector embedder. Records every call so tests can assert how
    many times (and with what texts) the scorer reached for the network."""

    def __init__(self, vectors_by_text: dict[str, list[float]] | None = None,
                 default: list[float] | None = None, raises: Exception | None = None):
        self.calls: list[list[str]] = []
        self._vectors_by_text = vectors_by_text or {}
        self._default = default if default is not None else [1.0, 0.0]
        self._raises = raises

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._raises:
            raise self._raises
        return [self._vectors_by_text.get(t, self._default) for t in texts]


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Backend Engineer",
                bullets=["Built things."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
        target_tracks=["backend engineering"],
    )
    base.update(overrides)
    return Profile(**base)


# ── TC-SCORE-01: normal input produces a whole number within 0..10000 ──────────

async def test_score_returns_int_within_range():
    scorer = EmbeddingScorer(FakeEmbedder())
    result = await scorer.score("We need a backend engineer.", _profile())
    assert isinstance(result, int)
    assert 0 <= result <= 10000


# ── TC-SCORE-02: determinism ─────────────────────────────────────────────────

async def test_score_is_deterministic_for_same_inputs():
    embedder = FakeEmbedder(vectors_by_text={"the job text": [1.0, 0.0]}, default=[0.5, 0.5])
    profile = _profile()
    first = await EmbeddingScorer(embedder).score("the job text", profile)
    second = await EmbeddingScorer(embedder).score("the job text", profile)
    assert first == second


# ── TC-SCORE-03: empty JD returns 0 without calling the embedder ───────────────

async def test_score_empty_jd_returns_zero_without_calling_embedder():
    embedder = FakeEmbedder()
    result = await EmbeddingScorer(embedder).score("", _profile())
    assert result == 0
    assert embedder.calls == []


async def test_score_whitespace_only_jd_returns_zero_without_calling_embedder():
    embedder = FakeEmbedder()
    result = await EmbeddingScorer(embedder).score("   \n  ", _profile())
    assert result == 0
    assert embedder.calls == []


# ── TC-SCORE-04: all-zero vector returns 0 rather than NaN/error ───────────────

async def test_score_zero_vectors_return_zero():
    embedder = FakeEmbedder(default=[0.0, 0.0])
    result = await EmbeddingScorer(embedder).score("a job description", _profile())
    assert result == 0


# ── TC-SCORE-06: embedder failure raises rather than returning a number ────────

async def test_score_propagates_embedder_failure():
    embedder = FakeEmbedder(raises=ConnectionError("network down"))
    with pytest.raises(ConnectionError):
        await EmbeddingScorer(embedder).score("a job description", _profile())


# ── TC-SCORE-09: scoring the same profile twice embeds it only once ────────────

async def test_score_reuses_cached_profile_vector_on_second_call():
    embedder = FakeEmbedder()
    scorer = EmbeddingScorer(embedder)
    profile = _profile()

    await scorer.score("job one", profile)
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == [profile_to_text(profile), "job one"]

    await scorer.score("job two", profile)
    assert len(embedder.calls) == 2
    # second call embeds only the job text — profile text is not re-sent
    assert embedder.calls[1] == ["job two"]


# ── TC-SCORE-10: a changed profile forces a re-embed on the next call ──────────

async def test_score_changed_profile_forces_reembed():
    embedder = FakeEmbedder()
    scorer = EmbeddingScorer(embedder)

    await scorer.score("job one", _profile())
    assert len(embedder.calls) == 1  # [profile_text, "job one"]

    changed_profile = _profile(target_tracks=["robotics", "backend engineering"])
    await scorer.score("job two", changed_profile)
    assert len(embedder.calls) == 2
    # cache miss re-sends the (new) profile text alongside the job text
    assert len(embedder.calls[1]) == 2
