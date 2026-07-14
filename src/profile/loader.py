"""Loading and validating — every consumer's entry point.

Raises rather than returns a broken Profile: no downstream layer should ever
have to check whether the object it was handed is trustworthy.
"""

from __future__ import annotations

import json
from pathlib import Path

from profile.invariants import validate_invariants
from profile.schema import Profile


def load_profile(path: str | Path) -> Profile:
    """Read `path`, parse it into a `Profile`, enforce invariants, return it.

    Raises `FileNotFoundError` if `path` doesn't exist, a pydantic
    `ValidationError` if the JSON doesn't match the schema, and
    `ProfileInvariantError` if it matches the schema but breaks a structural
    guarantee (Requirement 4 — invariants are enforced at load, not at use).
    """
    data = json.loads(Path(path).read_text())
    profile = Profile.model_validate(data)
    validate_invariants(profile)
    return profile
