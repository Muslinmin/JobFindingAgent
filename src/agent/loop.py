"""The ReAct driver (agent_v2.md §4).

Stateless per call: record the user turn, compose the prompt, iterate
think→act until a stop condition fires, record the assistant turn, return
the reply. It owns no timeout of its own — the caller (`POST /chat`)
applies the turn deadline, because the deadline belongs to the request, not
to the reasoning.

**Four stop conditions, in priority order.** Only the last is a failure:

1. *Final answer* — the model replied with text and no tool call.
2. *Max iterations* — the cap is a proxy for a token budget at single-user
   scale. Exits with a best-effort reply, not an error: the user should see
   how far it got, not a blank apology.
3. *No progress* — the same tool called with the same arguments twice in a
   row means the model is stuck in a groove that another iteration will not
   break. This is the drift guard.
4. *Consecutive exceptions* — two unexpected failures in a row and the turn
   is abandoned.

The distinction that makes the loop work: an *expected* `{ok:false}` result
is fed straight back as a tool message and the loop continues, because the
model is supposed to read it and change course (disambiguate, relay an
illegal transition, offer an alternative). An unexpected exception is not
data and never becomes a tool result.
"""

from __future__ import annotations

import json

from loguru import logger

from agent.context import ConversationContext
from agent.handlers import ToolDispatcher
from agent.prompt import compose
from agent.schemas import TOOL_SCHEMAS
from app.conversation.models import Role
from profile.loader import load_profile

MAX_ITERATIONS = 5
MAX_CONSECUTIVE_EXCEPTIONS = 2

_CAP_REPLY = (
    "I couldn't fully finish that one — here's where I got to. "
    "Ask me again and I'll pick it up from here."
)
_NO_PROGRESS_REPLY = (
    "I got stuck repeating the same lookup without making progress. "
    "Could you give me a bit more detail about which job you mean?"
)
_ERROR_REPLY = "Something went wrong on my end and I couldn't finish that. Please try again."


def _tool_calls(message) -> list:
    """Provider messages carry `tool_calls` as None when absent; normalise
    so callers can just check truthiness."""
    return getattr(message, "tool_calls", None) or []


def _text(message) -> str:
    return getattr(message, "content", None) or ""


def _parse_args(raw: str) -> dict:
    """Tool arguments arrive as a JSON *string*. Malformed JSON is the
    model's mistake, not a crash — an empty dict lets the handler reject it
    on its own terms and the model see a real error."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Agent:
    """One agent per process, holding its collaborators; every turn passes
    through `run`. Session identity is resolved per call, not held here —
    `/chat` is stateless and two turns of the same conversation may be
    served by different requests.
    """

    def __init__(
        self,
        llm,
        dispatcher: ToolDispatcher,
        context: ConversationContext,
        profile_path,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._context = context
        self._profile_path = profile_path

    async def run(self, session_id: str, user_text: str) -> str:
        await self._context.record(session_id, Role.USER, user_text)

        profile = load_profile(self._profile_path)
        messages = await compose(session_id, profile, self._context)

        reply = await self._iterate(messages)

        await self._context.record(session_id, Role.ASSISTANT, reply)
        return reply

    async def _iterate(self, messages: list[dict]) -> str:
        last_call: tuple[str, str] | None = None
        consecutive_exceptions = 0

        for iteration in range(MAX_ITERATIONS):
            try:
                # Deliberately does NOT reset consecutive_exceptions. Every
                # tool failure is followed by a successful LLM call — that
                # is how the loop asks what to do next — so resetting here
                # would mean two consecutive tool failures could never
                # accumulate and stop condition 4 would never fire. Only a
                # successful *tool* call clears the counter.
                response = await self._llm.chat(messages, TOOL_SCHEMAS)
            except Exception:
                consecutive_exceptions += 1
                logger.exception(
                    f"agent loop: LLM call failed "
                    f"({consecutive_exceptions}/{MAX_CONSECUTIVE_EXCEPTIONS})"
                )
                if consecutive_exceptions >= MAX_CONSECUTIVE_EXCEPTIONS:
                    return _ERROR_REPLY
                continue

            message = response.choices[0].message
            calls = _tool_calls(message)

            # Stop condition 1 — final answer.
            if not calls:
                return _text(message) or _CAP_REPLY

            messages.append(_assistant_message(message, calls))

            for call in calls:
                name = call.function.name
                raw_args = call.function.arguments

                # Stop condition 3 — no progress. Compared on the RAW
                # argument string so it can be checked before parsing, and
                # so two structurally identical calls match regardless of
                # key order.
                if last_call == (name, raw_args):
                    logger.warning(f"agent loop: no progress, {name} repeated with identical args")
                    return _NO_PROGRESS_REPLY
                last_call = (name, raw_args)

                try:
                    result = await self._dispatcher.dispatch(name, _parse_args(raw_args))
                    consecutive_exceptions = 0
                except Exception:
                    consecutive_exceptions += 1
                    logger.exception(
                        f"agent loop: tool '{name}' raised "
                        f"({consecutive_exceptions}/{MAX_CONSECUTIVE_EXCEPTIONS})"
                    )
                    # Stop condition 4 — unexpected failures. Note this
                    # never becomes a tool message: an exception is not a
                    # result the model gets to narrate.
                    if consecutive_exceptions >= MAX_CONSECUTIVE_EXCEPTIONS:
                        return _ERROR_REPLY
                    break

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": name,
                        "content": json.dumps(result, default=str),
                    }
                )

        # Stop condition 2 — iteration cap. Best-effort, not an error.
        logger.warning(f"agent loop: hit the {MAX_ITERATIONS}-iteration cap")
        return _CAP_REPLY


def _assistant_message(message, calls: list) -> dict:
    """Re-serialise the provider's assistant message for the next request.

    It has to go back on the conversation verbatim — most providers reject a
    `tool` result whose matching `tool_calls` entry isn't in the history.
    """
    return {
        "role": "assistant",
        "content": _text(message),
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in calls
        ],
    }
