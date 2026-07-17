"""The EmbeddingScorer — fulfils the Scorer contract (scoring_v2.md WP5).

Wires the profile-to-text projection, the fingerprint cache, the embedder,
and the similarity maths together per the order of operations in Step 3.
The only stateful piece in the scoring layer: it caches the profile's
fingerprint and vector on the instance so an unchanged profile is embedded
once per run, not once per job.
"""

from __future__ import annotations

import hashlib

from loguru import logger

from profile.projections import profile_to_text
from profile.schema import Profile
from scoring.embedder import Embedder
from scoring.similarity import cosine_similarity, similarity_to_score


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class EmbeddingScorer:
    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._profile_fingerprint: str | None = None
        self._profile_vector: list[float] | None = None

    async def score(self, jd_text: str, candidate: Profile) -> int:
        if not jd_text or not jd_text.strip():
            logger.warning("EmbeddingScorer: empty job description, returning 0")
            return 0

        profile_text = profile_to_text(candidate)
        fingerprint = _fingerprint(profile_text)

        if fingerprint == self._profile_fingerprint and self._profile_vector is not None:
            job_vector, = await self._embedder.embed([jd_text])
            profile_vector = self._profile_vector
        else:
            profile_vector, job_vector = await self._embedder.embed([profile_text, jd_text])
            self._profile_fingerprint = fingerprint
            self._profile_vector = profile_vector

        similarity = cosine_similarity(job_vector, profile_vector)
        return similarity_to_score(similarity)
