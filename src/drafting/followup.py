"""Follow-up email drafting (agent_v2.md §7).

The drafting half of `scheduler/jobs/follow_up.py`, split from its Telegram
push so the agent's `draft_followup` tool can reach it. What stayed behind
in the scheduler job is what is genuinely the *nudge*: selecting the
qualifying buckets, pushing to Telegram, and stamping
`mark_follow_up_nudged`.

**Drafting is not sending, and it is not nudging.** This module writes no
database row of any kind. In particular the agent path must never call
`mark_follow_up_nudged` — that stamps "we reminded you", which belongs to
the scheduler's push, not to a user who asked to see a draft. `status` and
`follow_up_count` are untouched by both callers; `follow_up_count` only
moves on the user's own `[Sent it]` tap.

No guards here, unlike `cover_letter.py`. The prompt carries no profile and
no JD — only role, company, and the date applied — so there is almost no
surface to fabricate against, and the output is a hundred words the user
reads in full before sending. The guard would cost a retry loop to check
text the human is about to proofread anyway.
"""

from __future__ import annotations

from typing import Protocol


class DraftLLM(Protocol):
    """The narrow contract this layer needs from an LLM client — text in,
    text out. `TaskLLMClient.complete` satisfies it structurally, the same
    way `tailoring.prompt.LLMTailor` does."""

    async def complete(self, prompt: str) -> str: ...


def assemble_followup_prompt(
    role: str, company: str, applied_date: str, note: str | None = None
) -> str:
    """Builds the one-shot drafting prompt. Deliberately minimal context for
    a short nudge email — no profile, no JD.

    `note` is the user's own words, forwarded verbatim from the chat turn
    ("mention I've since shipped the robotics project"). It is only ever
    reached from the agent path; the scheduled nudge has nobody to ask.
    """
    prompt = (
        "Draft a short, polite follow-up email to send after applying for a job "
        "and not hearing back yet. Keep it under 100 words, professional, and "
        "copy-paste ready — no placeholders.\n\n"
        f"Role: {role}\n"
        f"Company: {company}\n"
        f"Applied on: {applied_date}\n"
    )
    if note:
        prompt += (
            "\nThe candidate asked for this specifically — follow it, but do not "
            "invent any fact it does not state:\n"
            f"{note}\n"
        )
    return prompt


async def draft_followup(role: str, company: str, applied_date: str, llm: DraftLLM,
                         note: str | None = None) -> str:
    """Assemble, call, return the text. Whatever the client raises (rate
    limit, network) propagates unchanged — each caller decides what a
    failure means: the scheduler logs it and moves to the next record, the
    agent lets it abort the turn.

    Takes the three fields rather than a `Job` so this layer keeps no
    dependency on `app.models`, matching `tailoring/`'s rule that a content
    layer never imports the backend's types.
    """
    return await llm.complete(assemble_followup_prompt(role, company, applied_date, note))
