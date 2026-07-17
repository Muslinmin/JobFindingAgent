"""Similarity maths — pure, no I/O (scoring_v2.md WP4).

Two steps, kept separate so each is trivially testable: turn two vectors into
a 0..1 similarity, then turn that into the one basis-points integer the rest
of the system stores and compares.
"""

from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. Returns 0.0 for the
    degenerate all-zero case instead of raising a ZeroDivisionError/NaN
    (scoring_v2.md Requirement 3)."""
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (norm_a * norm_b)


def similarity_to_score(similarity: float) -> int:
    """The one conversion point from decimal similarity to the stored
    integer (scoring_v2.md Requirement 2): round(similarity * 10000),
    clamped to [0, 10000] so an out-of-range float can never leak through."""
    basis_points = round(similarity * 10000)
    return max(0, min(10000, basis_points))
