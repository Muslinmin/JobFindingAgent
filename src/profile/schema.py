"""Profile layer — schema and the typed mutation surface.

The Profile is the SINGLE source of truth for everything true about the
candidate. It is a deliberate SUPERSET: every experience, project and skill.
Downstream layers only ever SELECT from it. Nothing downstream may add a fact
that is not already here.

Tier tags (agent_v2.md §5) are declared on the model itself, so `profile_summary()`
is a mechanical projection that can never drift from the schema:

    identity — scalar, shown verbatim in the agent's context
    index    — collection, projected to {id, label} (bodies dropped)
    body     — dropped from agent context; loaded just-in-time by tailoring
    render   — never enters agent context at all; CV fixed slots only
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

IDENTITY, INDEX, BODY, RENDER = "identity", "index", "body", "render"


def tier(t: str) -> dict:
    """Declare which context tier a field belongs to."""
    return {"tier": t}


def field_tier(model_cls: type[BaseModel], field_name: str) -> str | None:
    """Read back the tier a field was declared with. The one place that
    knows the storage shape (`json_schema_extra`), so every tier-walking
    projection (profile/projections.py, tailoring/prompt.py,
    tailoring/render.py) reads it the same way."""
    extra = model_cls.model_fields[field_name].json_schema_extra
    return extra.get("tier") if isinstance(extra, dict) else None


# ==========================================================================
# Schema
# ==========================================================================

class Skill(BaseModel):
    """ONE real competence, with its ATS surface spellings.

    `aliases` are alternate spellings of the SAME competence — "RESTful API"
    and "REST" for `rest_api` — not related-but-different skills. This is what
    lets the tailorer surface whichever spelling the job posting actually uses,
    instead of keyword-stuffing all three onto one CV.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable id. This is what demonstrated_skills holds.")
    label: str = Field(..., description="Default surface form, used when the JD gives no steer.")
    aliases: list[str] = Field(
        default_factory=list,
        description="ATS variant spellings of the SAME competence.",
    )
    category: str | None = Field(
        None,
        description=(
            "Display grouping for the CV's Skills section (e.g. 'Programming "
            "Languages', 'Tools & Frameworks'). Rendering concern only — the "
            "tailorer still selects by skill id; category never affects "
            "which skills may be selected, only how selected ones are grouped."
        ),
    )

    def surfaces(self) -> list[str]:
        """Every spelling this skill may legitimately appear as."""
        return [self.label, *self.aliases]


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    institution: str = Field(..., json_schema_extra=tier(IDENTITY))
    degree: str = Field(..., json_schema_extra=tier(IDENTITY))
    graduation_date: str | None = Field(None, json_schema_extra=tier(IDENTITY))
    date_range: str | None = Field(None, json_schema_extra=tier(RENDER))
    notes: str | None = Field(None, json_schema_extra=tier(RENDER))


class ProfileItem(BaseModel):
    """An experience or a project. Same shape for both — the distinction is
    which list it sits in, not its structure."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        description=(
            "Stable id. Every TailoredItem.ref_id must resolve to one of these. "
            "Renaming an id breaks previously-rendered artifacts."
        ),
    )
    title: str = Field(..., json_schema_extra=tier(INDEX))
    organization: str | None = Field(None, json_schema_extra=tier(INDEX))
    date_range: str | None = Field(None, json_schema_extra=tier(INDEX))

    bullets: list[str] = Field(
        ...,
        min_length=1,
        json_schema_extra=tier(BODY),
        description=(
            "FROZEN FACTUAL BASE TEXT. Every tailored rewrite is a constrained "
            "rephrasing of these lines, so an overclaim here propagates into "
            "every CV the system will ever generate. Must be literally true."
        ),
    )
    demonstrated_skills: list[str] = Field(
        default_factory=list,
        json_schema_extra=tier(BODY),
        description=(
            "SKILL IDS (not surface strings). A PERMISSION LIST, not an "
            "endorsement of depth: a skill may be surfaced on THIS item only if "
            "its id appears here. Blocks false recombination ('I have leadership' "
            "+ 'I did X' -> 'I led team X'). Human-authored, or parse-proposed "
            "then human-reviewed — NEVER inferred at tailoring time. "
            "Removing a skill to signal modest depth is a BUG: if a bullet says "
            "'exposing Flask REST APIs' but the item is not tagged `rest_api`, "
            "the text guard rejects the candidate's own true sentence."
        ),
    )


class Profile(BaseModel):
    """The superset. One object, read by every downstream layer."""

    model_config = ConfigDict(extra="forbid")

    # --- identity: frozen, never LLM-touched
    name: str = Field(..., json_schema_extra=tier(IDENTITY))
    location: str | None = Field(None, json_schema_extra=tier(IDENTITY))
    years_of_experience: float | None = Field(None, json_schema_extra=tier(IDENTITY))
    candidate_status: str | None = Field(None, json_schema_extra=tier(IDENTITY))
    education: list[Education] = Field(
        default_factory=list, json_schema_extra=tier(IDENTITY)
    )

    # --- render-only: CV fixed slots, never enter agent context
    email: str = Field(..., json_schema_extra=tier(RENDER))
    phone: str | None = Field(None, json_schema_extra=tier(RENDER))
    links: list[str] = Field(default_factory=list, json_schema_extra=tier(RENDER))
    headline: str | None = Field(
        None,
        json_schema_extra=tier(RENDER),
        description=(
            "One-line CV subtitle under the name, e.g. 'Robotics Systems, "
            "Singapore Institute of Technology'. Human-authored personal "
            "branding, not derived from education/target_tracks — those "
            "phrase differently for different purposes."
        ),
    )

    # --- content superset: what the LLM selects and reorders from
    summary_seed: str | None = Field(None, json_schema_extra=tier(BODY))
    experiences: list[ProfileItem] = Field(
        default_factory=list, json_schema_extra=tier(INDEX)
    )
    projects: list[ProfileItem] = Field(
        default_factory=list, json_schema_extra=tier(INDEX)
    )
    skills: list[Skill] = Field(default_factory=list, json_schema_extra=tier(INDEX))

    # --- intent, not history
    target_tracks: list[str] = Field(
        default_factory=list,
        json_schema_extra=tier(INDEX),
        description=(
            "Career directions as plain intent — NOT query strings. The one field "
            "explicitly permitted to outrun the evidence. Seeds search_queries.json "
            "via query_regen; poured into the scorer's profile text. Flat and "
            "unweighted in v2."
        ),
    )

    @property
    def items(self) -> list[ProfileItem]:
        """Experiences and projects in ONE FLAT NAMESPACE — which is how
        TailoredItem.ref_id resolves them, and why ids must be unique across
        both lists, not merely within each."""
        return [*self.experiences, *self.projects]


# ==========================================================================
# Mutation — the typed operation surface (input to the SOLE mutator)
# ==========================================================================
# A free-form patch dict cannot express whether a list write means APPEND or
# REPLACE. "Add robotics QA to my target tracks" could be emitted by an LLM as a
# whole-field replacement, silently destroying the other tracks. A discriminated
# union makes the intent unambiguous AT THE TYPE LEVEL, not by convention.

class AddTargetTrack(BaseModel):
    op: Literal["add_target_track"]
    track: str


class RemoveTargetTrack(BaseModel):
    op: Literal["remove_target_track"]
    track: str


class AddSkill(BaseModel):
    op: Literal["add_skill"]
    skill: Skill
    demonstrated_by: list[str] = Field(
        default_factory=list, description="Item ids that genuinely exercised it."
    )


class RemoveSkill(BaseModel):
    op: Literal["remove_skill"]
    skill_id: str  # CASCADES: also stripped from every item's demonstrated_skills


class AddItem(BaseModel):
    op: Literal["add_item"]
    kind: Literal["experience", "project"]
    item: ProfileItem


class EditBullets(BaseModel):
    """Exists because ADD-ONLY MUTATION IS INSUFFICIENT. A bullet claiming 'XR
    interactive components on an extended reality platform' described work that
    was actually a UI inside a VR app. That needed a REWRITE, not an addition."""

    op: Literal["edit_bullets"]
    item_id: str
    bullets: list[str]  # FULL replacement — the diff is what the human approves


class TagSkill(BaseModel):
    op: Literal["tag_skill"]
    item_id: str
    skill_ids: list[str]


ProfileOp = Annotated[
    AddTargetTrack | RemoveTargetTrack | AddSkill | RemoveSkill
    | AddItem | EditBullets | TagSkill,
    Field(discriminator="op"),
]

# Ops that invalidate search_queries.json. Pure set logic — no LLM call, no
# coupling from this layer to scheduling. Everything else leaves queries alone.
QUERY_AFFECTING_OPS = {
    "add_target_track",
    "remove_target_track",
    "add_skill",
    "remove_skill",
}
