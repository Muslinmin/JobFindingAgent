import json

from agent.schemas import TOOL_SCHEMAS
from app.models.enums import ApplicationStatus

EXPECTED_NAMES = {
    "find_jobs",
    "search_jobs",
    "score_job",
    "score_ingest",
    "update_status",
    "tailor_resume",
    "draft_followup",
    "draft_cover_letter",
    "regenerate_queries",
    "update_profile",
}

MUTATING_TOOLS_WITH_JOB_ID = {
    "update_status",
    "tailor_resume",
    "draft_followup",
    "draft_cover_letter",
}


def _by_name() -> dict[str, dict]:
    return {s["function"]["name"]: s for s in TOOL_SCHEMAS}


def test_exactly_ten_schemas_with_expected_names():
    assert len(TOOL_SCHEMAS) == 10
    assert {s["function"]["name"] for s in TOOL_SCHEMAS} == EXPECTED_NAMES


def test_every_schema_matches_the_function_calling_shape():
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)
        assert set(params["required"]).issubset(params["properties"].keys())


def test_every_schema_is_json_serialisable():
    json.dumps(TOOL_SCHEMAS)


def test_mutating_tools_expose_required_job_id():
    schemas = _by_name()
    for name in MUTATING_TOOLS_WITH_JOB_ID:
        params = schemas[name]["function"]["parameters"]
        assert "job_id" in params["properties"]
        assert params["properties"]["job_id"]["type"] == "integer"
        assert "job_id" in params["required"]


def test_update_status_new_status_is_the_full_fsm_enum():
    params = _by_name()["update_status"]["function"]["parameters"]
    enum_values = set(params["properties"]["new_status"]["enum"])
    assert enum_values == {s.value for s in ApplicationStatus}
    assert "new_status" in params["required"]
    assert "job_id" in params["required"]


def test_find_jobs_status_set_uses_the_full_fsm_enum_and_has_no_required_fields():
    params = _by_name()["find_jobs"]["function"]["parameters"]
    enum_values = set(params["properties"]["status_set"]["items"]["enum"])
    assert enum_values == {s.value for s in ApplicationStatus}
    assert params["required"] == []


def test_search_jobs_requires_only_query():
    params = _by_name()["search_jobs"]["function"]["parameters"]
    assert params["required"] == ["query"]


def test_score_job_requires_description():
    params = _by_name()["score_job"]["function"]["parameters"]
    assert params["required"] == ["description"]


def test_score_ingest_requires_company_title_description():
    params = _by_name()["score_ingest"]["function"]["parameters"]
    assert set(params["required"]) == {"company", "title", "description"}


def test_regenerate_queries_takes_no_arguments():
    params = _by_name()["regenerate_queries"]["function"]["parameters"]
    assert params["properties"] == {}
    assert params["required"] == []


def test_update_profile_requires_a_typed_op_and_has_confirmed_flag():
    """Deliberate deviation from agent_v2.md §3's free-form `patch`: the
    mutator takes a discriminated ProfileOp, and a patch dict cannot say
    whether a list write appends or replaces (profile/schema.py §Mutation)."""
    params = _by_name()["update_profile"]["function"]["parameters"]
    assert params["required"] == ["op"]
    assert params["properties"]["confirmed"]["type"] == "boolean"
    assert "patch" not in params["properties"]


def test_update_profile_op_enum_is_derived_from_the_mutator_union():
    from typing import get_args

    from profile.schema import ProfileOp

    union_ops = {
        member.model_fields["op"].annotation.__args__[0]
        for member in get_args(get_args(ProfileOp)[0])
    }
    schema_ops = set(_by_name()["update_profile"]["function"]["parameters"]["properties"]["op"]["enum"])
    assert schema_ops == union_ops


def test_update_profile_warns_that_list_ops_replace():
    """edit_bullets/tag_skill are full replacements — if the model thinks
    they append, it silently drops the bullets it didn't resend."""
    description = _by_name()["update_profile"]["function"]["description"]
    assert "REPLACE" in description


def test_job_id_taking_tools_instruct_resolution_via_find_jobs_first():
    schemas = _by_name()
    for name in MUTATING_TOOLS_WITH_JOB_ID:
        assert "find_jobs" in schemas[name]["function"]["description"]
