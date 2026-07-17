"""Tailoring layer — schema (tailoring_build.md WP0).

`TailoredSelection` is the LLM's output: a PROJECTION over the `Profile`
superset, never a copy of it (tailoring.md §2). It carries no identity
fields, and every reference it makes back into the `Profile` (ref_ids,
skill ids) is checked here — so a guard violation downstream can only ever
mean the LLM misbehaved, never that the schema let something slip through.

Cross-referential integrity is intentionally NOT enforced at bare
construction (`TailoredSelection(**data)`): the raw LLM JSON has no `Profile`
in scope until the caller supplies one. Pass it via pydantic's validation
context — `TailoredSelection.model_validate(data, context={"profile": p})`
— and the checks below run as part of that same call, raising the same
`pydantic.ValidationError` as a plain shape mismatch (tailoring.md §8,
invariant 1; tailoring_build.md TC-SCHEMA-05..09).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

from profile.schema import Profile


class TailoredItem(BaseModel):
    """A constrained rewrite of one `Profile` item's bullets. TC-SCHEMA-05."""

    model_config = ConfigDict(extra="forbid")

    ref_id: str
    bullets: list[str]


class TailoredSelection(BaseModel):
    """The LLM's projection: which items/skills, their order, and reframed
    text. No identity fields (tailoring.md §2). TC-SCHEMA-05..09."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    experience_order: list[str]
    experiences: list[TailoredItem]
    project_order: list[str]
    projects: list[TailoredItem]
    skill_order: list[str]

    @model_validator(mode="after")
    def _check_against_profile(self, info: ValidationInfo) -> TailoredSelection:
        profile: Profile | None = (info.context or {}).get("profile")
        if profile is None:
            return self

        item_ids = {item.id for item in profile.items}
        skill_ids = {skill.id for skill in profile.skills}

        # TC-SCHEMA-06/07 — every ref_id must resolve to a real Profile item.
        for tailored_item in (*self.experiences, *self.projects):
            if tailored_item.ref_id not in item_ids:
                raise ValueError(
                    f"ref_id '{tailored_item.ref_id}' does not resolve to any Profile item"
                )

        # TC-SCHEMA-08 — skill_order must be a subset of Profile.skills.
        if unknown := [s for s in self.skill_order if s not in skill_ids]:
            raise ValueError(f"skill_order names unknown skill ids: {unknown}")

        # TC-SCHEMA-09 — order lists may only reference ids present among items.
        _check_order_resolves("experience_order", self.experience_order, self.experiences)
        _check_order_resolves("project_order", self.project_order, self.projects)

        return self


def _check_order_resolves(label: str, order: list[str], items: list[TailoredItem]) -> None:
    ref_ids = {item.ref_id for item in items}
    if missing := [ref_id for ref_id in order if ref_id not in ref_ids]:
        raise ValueError(f"{label} references ref_ids with no matching item: {missing}")
