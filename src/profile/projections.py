"""Projections — purpose-built views of a Profile for each consumer.

Pure functions, no I/O. `profile_summary` and `profile_to_text` never
hardcode a field name: they walk `model_fields` and read the `tier` each
field declared on the schema, so a new field cannot silently escape (or
silently leak into) a projection. See schema.py's tier docstring and
.agent/profile.md Requirement 5.
"""

from __future__ import annotations

from pydantic import BaseModel

from profile.schema import BODY, IDENTITY, INDEX, Profile

# agent_v2.md §5: "a tagged scalar exceeding the budget is treated as a body
# and excluded" — stops an oversized identity field (e.g. an accidental
# objective paragraph) from leaking body-sized text into the agent's context.
# No specific number is fixed in the spec; this is a deliberately generous
# ceiling for a single scalar (name, location, a graduation date, ...).
IDENTITY_CHAR_BUDGET = 200


class IndexItem(BaseModel):
    id: str
    label: str


class IndexView(BaseModel):
    skills: list[IndexItem]
    target_tracks: list[str]
    experiences: list[IndexItem]
    projects: list[IndexItem]


def _field_tier(model_cls: type[BaseModel], field_name: str) -> str | None:
    extra = model_cls.model_fields[field_name].json_schema_extra
    return extra.get("tier") if isinstance(extra, dict) else None


def _index_label(item: object) -> str:
    if isinstance(item, str):
        return item
    for attr in ("label", "title"):
        if hasattr(item, attr):
            return getattr(item, attr)
    return str(item)


def _project_identity_model(instance: BaseModel) -> dict[str, object]:
    """Project a nested model (e.g. one Education entry) down to its OWN
    identity-tier fields, verbatim. Mirrors the top-level Profile filter one
    level down, so a nested model's tier tags are honoured too."""
    cls = type(instance)
    return {
        name: getattr(instance, name)
        for name in cls.model_fields
        if _field_tier(cls, name) == IDENTITY and getattr(instance, name) not in (None, "")
    }


def profile_summary(p: Profile) -> str:
    """Reference-tier context for the agent: identity verbatim, index as
    {id, label}, body and render dropped entirely (agent_v2.md §5).
    """
    sections: list[str] = []

    cls = type(p)
    for name in cls.model_fields:
        t = _field_tier(cls, name)
        if t not in (IDENTITY, INDEX):
            continue

        value = getattr(p, name)
        if value in (None, "", []):
            continue

        if t == IDENTITY:
            if isinstance(value, list):
                # A collection of nested identity models (education): project
                # each item down to its own identity-tier fields.
                rendered = [_project_identity_model(item) for item in value]
                text = "; ".join(
                    ", ".join(f"{k}: {v}" for k, v in item.items()) for item in rendered
                )
            else:
                text = str(value)
                if len(text) > IDENTITY_CHAR_BUDGET:
                    continue
            sections.append(f"{name}: {text}")

        elif t == INDEX:
            labels = ", ".join(_index_label(item) for item in value)
            sections.append(f"{name}: {labels}")

    return "\n".join(sections)


def profile_to_text(p: Profile) -> str:
    """Pour-everything-in text for the scorer's embedding call
    (scoring_v2.md WP3). Includes target_tracks. Excludes contact details
    (email, phone, links) and dates — noise with no matching signal.
    """
    parts: list[str] = [p.name]

    if p.location:
        parts.append(p.location)
    if p.candidate_status:
        parts.append(p.candidate_status)
    if p.summary_seed:
        parts.append(p.summary_seed)

    for edu in p.education:
        parts.append(f"{edu.degree}, {edu.institution}")

    for item in p.items:
        parts.append(item.title)
        if item.organization:
            parts.append(item.organization)
        parts.extend(item.bullets)

    for skill in p.skills:
        parts.extend(skill.surfaces())

    parts.extend(p.target_tracks)

    return "\n".join(parts)


def index_view(p: Profile) -> IndexView:
    """What query regeneration receives: skills {id, label} (aliases
    dropped — query regen wants competences, not spellings), target_tracks,
    and item {id, title} (bodies dropped).
    """
    return IndexView(
        skills=[IndexItem(id=s.id, label=s.label) for s in p.skills],
        target_tracks=list(p.target_tracks),
        experiences=[IndexItem(id=i.id, label=i.title) for i in p.experiences],
        projects=[IndexItem(id=i.id, label=i.title) for i in p.projects],
    )
