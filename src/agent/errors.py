"""Frozen `{ok:false}` result shapes the LLM narrates (agent_v2.md §2/§3).

No logic lives here — these are data, constructed by `handlers.py` when a
service call fails in an *expected* way (the tool table's "Expected
failures" column), and re-enter the ReAct loop for the model to relay or
act on. An unexpected failure (a raised exception a handler didn't catch)
is a different path entirely — it aborts the loop, it does not become one
of these.

Each variant is its own `TypedDict` rather than one shape with optional
fields, so a caller who narrows on `error` gets the right extra fields for
free (`illegal_transition["from"]`, `guard_violation["guard"]`, ...).
`illegal_transition` uses the functional `TypedDict` form because `from` is
a reserved word and cannot be a class-body field name.
"""

from typing import Literal, TypedDict


class ToolError(TypedDict):
    ok: Literal[False]
    error: str


class NotFoundError(ToolError):
    error: Literal["not_found"]


class AmbiguousError(ToolError):
    error: Literal["ambiguous"]


class ProfileEmptyError(ToolError):
    error: Literal["profile_empty"]


class EmbeddingUnavailableError(ToolError):
    error: Literal["embedding_unavailable"]


IllegalTransitionError = TypedDict(
    "IllegalTransitionError",
    {
        "ok": Literal[False],
        "error": Literal["illegal_transition"],
        "from": str,
        "to": str,
        "allowed": list[str],
    },
)


class GuardViolationError(ToolError):
    error: Literal["guard_violation"]
    guard: str


class InvalidPatchError(ToolError):
    error: Literal["invalid_patch"]
    detail: str


def not_found() -> NotFoundError:
    return {"ok": False, "error": "not_found"}


def ambiguous() -> AmbiguousError:
    return {"ok": False, "error": "ambiguous"}


def profile_empty() -> ProfileEmptyError:
    return {"ok": False, "error": "profile_empty"}


def embedding_unavailable() -> EmbeddingUnavailableError:
    return {"ok": False, "error": "embedding_unavailable"}


def illegal_transition(from_status: str, to: str, allowed: list[str]) -> IllegalTransitionError:
    return {"ok": False, "error": "illegal_transition", "from": from_status, "to": to, "allowed": allowed}


def guard_violation(guard: str) -> GuardViolationError:
    return {"ok": False, "error": "guard_violation", "guard": guard}


def invalid_patch(detail: str) -> InvalidPatchError:
    return {"ok": False, "error": "invalid_patch", "detail": detail}
