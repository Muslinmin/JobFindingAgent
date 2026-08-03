"""The Scorer contract (scoring_v2.md WP1 / Step 1).

The one thing the rest of the system is allowed to depend on. Says nothing
about how the score is computed — that's deliberate, so a different scoring
method can be swapped in later without any caller noticing.
"""

from __future__ import annotations

from typing import Protocol

from profile.schema import Profile


class Scorer(Protocol):
    async def score(self, jd_text: str, candidate: Profile) -> int: ...
