"""LLM-facing function-calling schemas — one per tool (agent_v2.md §3).

No execution lives here. Each schema is the OpenAI/LiteLLM tool-calling
shape (`{"type": "function", "function": {...}}`), the format
`AgentLLMClient.chat(messages, tools)` passes straight to `acompletion`.
`TOOL_SCHEMAS` is the fixed, ordered list `loop.py` hands to every call.

The `description` fields are where behavioural guidance the model needs
lives — in particular "resolve via `find_jobs` first" for every tool that
takes a `job_id`: resolution is centralised in the loop (agent_v2.md §3
Cross-cutting rules), so no mutating tool accepts a fuzzy hint, only an
already-resolved id.
"""

from typing import get_args

from app.models.enums import ApplicationStatus
from profile.schema import ProfileOp

_STATUS_VALUES = [s.value for s in ApplicationStatus]

# `update_profile` exposes the mutator's own discriminated union, not a
# free-form patch dict (a deliberate deviation from agent_v2.md §3's
# `patch` wording). profile/schema.py's own comment says why: a patch dict
# cannot express whether a list write means APPEND or REPLACE, so "add
# robotics QA to my target tracks" can silently destroy the other tracks.
# Derived from ProfileOp rather than hardcoded so a new op cannot be added
# to the union without appearing here.
_PROFILE_OPS = [
    member.model_fields["op"].annotation.__args__[0]
    for member in get_args(get_args(ProfileOp)[0])
]

_RESOLVE_FIRST = (
    "`job_id` must already be resolved — call `find_jobs` first and use the "
    "id of the single matching row. Never invent or guess a job_id."
)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _tool(
        "find_jobs",
        "The single read tool for resolving what job a user is referring to, "
        "or listing what's in the pipeline. With `job_title` given, it's a "
        "named lookup (partial, case-insensitive match on title, optionally "
        "narrowed by `company`), searching all statuses including terminal "
        "ones by default — the user may be recalling a rejected job. Without "
        "`job_title`, it's a status listing of the active pipeline only. "
        "Matches on company/title only, never on the job description. "
        "Results are ordered most-recently-active first, which is also what "
        "makes 'the third one' a deterministic reference. If more than one "
        "row comes back for a named lookup, ask the user which one rather "
        "than guessing.",
        {
            "job_title": {
                "type": "string",
                "description": "Partial, case-insensitive match against the job title.",
            },
            "company": {
                "type": "string",
                "description": "Partial, case-insensitive match against the company name, AND-combined with job_title.",
            },
            "status_set": {
                "type": "array",
                "items": {"type": "string", "enum": _STATUS_VALUES},
                "description": (
                    "Restrict to these statuses. If omitted: named lookups search "
                    "all statuses; a bare listing (no title, no company) searches "
                    "only the active pipeline."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Safety cap on rows returned. Default 50.",
            },
        },
        [],
    ),
    _tool(
        "search_jobs",
        "Search external job sources for a free-text query and ingest what's "
        "found into the pipeline (dedup → DISCOVERED → scored). This is not a "
        "preview — every result enters the database; over-broad recall is "
        "absorbed by the scorer, not filtered here.",
        {
            "query": {"type": "string", "description": "Free-text search query, e.g. 'backend engineer python Singapore'."},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict the search to these source names. Omit to search all configured sources.",
            },
            "limit": {"type": "integer", "description": "Max results to fetch. Default 20."},
        },
        ["query"],
    ),
    _tool(
        "score_job",
        "Score a pasted job description against the candidate's profile "
        "without recording anything — assess-only, for 'how well does this "
        "job match me'. Writes nothing to the database. If the user wants "
        "the job recorded as well as scored, use `score_ingest` instead.",
        {
            "description": {"type": "string", "description": "The job description text to score."},
        },
        ["description"],
    ),
    _tool(
        "score_ingest",
        "Record a job and score it — the 'score this and put it in the "
        "database' path. Upserts on (company, title); re-ingesting an "
        "existing job only bumps its liveness unless it has no score yet. "
        "There is no record-only tool: every job entering the database gets "
        "scored, so if the user only wants it recorded, decline and offer "
        "this instead.",
        {
            "company": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string", "description": "The full job description text."},
            "url": {"type": "string", "description": "Canonical listing URL, if known."},
            "posted_at": {"type": "string", "description": "ISO-8601 date the job was posted, if known."},
        },
        ["company", "title", "description"],
    ),
    _tool(
        "update_status",
        "Move a job to a new status. " + _RESOLVE_FIRST + " The backend "
        "enforces the state machine unconditionally — an illegal move is "
        "rejected before any write and returned as `illegal_transition` "
        "naming the allowed targets; relay that to the user rather than "
        "retrying.",
        {
            "job_id": {"type": "integer", "description": "The resolved job id."},
            "new_status": {"type": "string", "enum": _STATUS_VALUES, "description": "The target status."},
            "note": {"type": "string", "description": "Optional free-text note about the transition."},
        },
        ["job_id", "new_status"],
    ),
    _tool(
        "tailor_resume",
        "Generate a tailored CV for a job and register it as an artifact. "
        + _RESOLVE_FIRST + " This only produces the file — it does not move "
        "the job's status. The status only advances to PENDING_APPROVAL "
        "once the user explicitly accepts the tailored resume, as the "
        "second turn of a two-turn confirmation.",
        {
            "job_id": {"type": "integer", "description": "The resolved job id."},
        },
        ["job_id"],
    ),
    _tool(
        "draft_followup",
        "Draft a follow-up email for a job that's already been applied to. "
        + _RESOLVE_FIRST + " This only produces and registers the draft — it "
        "never changes status or follow_up_count. Drafting is not sending.",
        {
            "job_id": {"type": "integer", "description": "The resolved job id."},
        },
        ["job_id"],
    ),
    _tool(
        "draft_cover_letter",
        "Draft a cover letter for a job. " + _RESOLVE_FIRST + " This only "
        "produces and registers the draft — it never changes status.",
        {
            "job_id": {"type": "integer", "description": "The resolved job id."},
        },
        ["job_id"],
    ),
    _tool(
        "regenerate_queries",
        "Regenerate the saved search queries used for job search, from the "
        "candidate's current profile (skills and target tracks). Takes no "
        "arguments — it always reads the live profile.",
        {},
        [],
    ),
    _tool(
        "update_profile",
        "Propose a change to the candidate's profile — the only source-of-"
        "truth mutator, and the only two-phase tool. The first call with "
        "`confirmed` omitted or false returns a diff and writes nothing; "
        "only call again with `confirmed=true` and the exact same arguments "
        "after the user has explicitly agreed to the diff you showed them. "
        "`op` selects the kind of change and decides which other arguments "
        "are required; supply only those. Note that `edit_bullets` and "
        "`tag_skill` REPLACE the item's bullets/skill tags wholesale, so "
        "pass the complete intended list, not just the additions.",
        {
            "op": {
                "type": "string",
                "enum": _PROFILE_OPS,
                "description": (
                    "add_target_track/remove_target_track need `track`; "
                    "add_skill needs `skill` (and optionally `demonstrated_by`); "
                    "remove_skill needs `skill_id`; "
                    "add_item needs `kind` and `item`; "
                    "edit_bullets needs `item_id` and `bullets`; "
                    "tag_skill needs `item_id` and `skill_ids`."
                ),
            },
            "track": {"type": "string", "description": "A target track, e.g. 'backend engineering'."},
            "skill": {
                "type": "object",
                "description": "A skill object: {id, label, aliases?}. `id` is the stable handle items reference.",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
            "demonstrated_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids of items that genuinely exercised the skill. Never claim one that didn't.",
            },
            "skill_id": {"type": "string", "description": "Id of the skill to remove."},
            "kind": {
                "type": "string",
                "enum": ["experience", "project"],
                "description": "Which collection an added item joins.",
            },
            "item": {
                "type": "object",
                "description": "A profile item: {id, title, organization?, date_range?, bullets, demonstrated_skills?}.",
            },
            "item_id": {"type": "string", "description": "Id of the item being edited or tagged."},
            "bullets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The item's COMPLETE new bullet list — this replaces the existing one.",
            },
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The item's COMPLETE new skill-id list — this replaces the existing one.",
            },
            "confirmed": {
                "type": "boolean",
                "description": "False (default) to preview the diff; true to commit it.",
            },
        },
        ["op"],
    ),
]
