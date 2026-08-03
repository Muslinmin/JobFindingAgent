"""System-prompt composition — static spine plus per-turn injection.

Three tiers, assembled in a fixed order (agent_v2.md §5):

1. the **static spine** (`prompts/system.md`) — role, truthfulness, tool
   discipline; identical on every turn, so it is read from disk once at
   import and reused,
2. the **profile summary** — reference tier, a compact projection of the
   profile owned by `profile.projections`; the *full* profile is never
   carried in the loop, it is loaded just-in-time by the tailoring service,
3. the **assembled turns** — ephemeral, from `ConversationContext`.

Tiers 1 and 2 become one system message; the turns follow as themselves.
The spine stays a separate file rather than a Python string so it can be
edited as prose without touching code.
"""

from __future__ import annotations

from pathlib import Path

from agent.context import ConversationContext, Message
from profile.projections import profile_summary
from profile.schema import Profile

# Resolved from this file's own location, not the working directory — the
# spine is a bundled source asset, and pytest/uvicorn/systemd each run from
# a different cwd (same reasoning as config.py's template path default).
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"

_SPINE = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

_PROFILE_HEADER = "## Current profile"

_NO_PROFILE = (
    "The profile is empty. Ask the user about themselves before relying on "
    "anything profile-derived, and do not invent details."
)


def system_message(profile: Profile) -> Message:
    """Spine + profile summary as one system message.

    Kept separate from `compose` so a test — and the loop, if it ever needs
    to re-issue the system message mid-iteration — can build it without a
    session or a store.
    """
    summary = profile_summary(profile).strip()
    content = f"{_SPINE.rstrip()}\n\n{_PROFILE_HEADER}\n{summary or _NO_PROFILE}\n"
    return {"role": "system", "content": content}


async def compose(
    session_id: str, profile: Profile, context: ConversationContext
) -> list[Message]:
    """The full message list for one turn: system message, then the session's
    turns oldest-first. Async because it awaits `build_context`.
    """
    return [system_message(profile), *await context.build_context(session_id)]
