"""Tailoring layer — LLM call + prompt assembly (tailoring_build.md WP3).

The LLM client is injected as an `LLMTailor` — the whole contract is
"text in, text out" (`async complete(prompt) -> str`). This module owns
turning that raw string into a validated `TailoredSelection`; the client
itself never sees a Pydantic model. Whatever the client raises (a rate
limit, a network error) propagates unchanged from `call_llm_tailor` — WP4's
`tailor()` is the only place that translates failures into a
`TailoringError`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from profile.schema import BODY, INDEX, Profile, field_tier
from tailoring.schema import TailoredSelection

_DOMAIN_KNOWLEDGE_PATH = Path(__file__).parent / "prompts" / "tailoring.md"


class LLMTailor(Protocol):
    async def complete(self, prompt: str) -> str: ...


def _content_for_prompt(profile: Profile) -> dict:
    """BODY + INDEX tier fields only. IDENTITY and RENDER tiers (name,
    email, phone, links, education, ...) are structurally excluded here —
    the LLM never sees them (tailoring.md §2)."""
    cls = type(profile)
    return {
        name: getattr(profile, name)
        for name in cls.model_fields
        if field_tier(cls, name) in (BODY, INDEX)
    }


def assemble_prompt(jd: str, profile: Profile) -> str:
    content = _content_for_prompt(profile)
    profile_json = json.dumps(
        {
            "summary_seed": content.get("summary_seed"),
            "target_tracks": content.get("target_tracks", []),
            "experiences": [item.model_dump() for item in content.get("experiences", [])],
            "projects": [item.model_dump() for item in content.get("projects", [])],
            "skills": [skill.model_dump() for skill in content.get("skills", [])],
        },
        indent=2,
    )
    domain_knowledge = _DOMAIN_KNOWLEDGE_PATH.read_text()

    return (
        f"{domain_knowledge}\n\n"
        f"# Job description\n{jd}\n\n"
        f"# Candidate profile (superset — select and reframe, never invent)\n{profile_json}\n"
    )


async def call_llm_tailor(jd: str, profile: Profile, llm: LLMTailor) -> TailoredSelection:
    prompt = assemble_prompt(jd, profile)
    raw = await llm.complete(prompt)
    data = json.loads(raw)  # malformed JSON propagates as JSONDecodeError
    return TailoredSelection.model_validate(data, context={"profile": profile})
