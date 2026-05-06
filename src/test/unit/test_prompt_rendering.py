import json
from unittest.mock import patch

from agent.agent import _render_system_prompt


def test_profile_injected_into_prompt():
    profile = {"skills": ["Python"], "target_roles": ["Data Engineer"]}
    with patch("agent.agent.read_profile", return_value=profile):
        rendered = _render_system_prompt()
    assert json.dumps(profile, indent=2) in rendered


def test_empty_profile_renders_without_error():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert isinstance(rendered, str)
    assert len(rendered) > 0


def test_placeholder_is_replaced():
    with patch("agent.agent.read_profile", return_value={"skills": []}):
        rendered = _render_system_prompt()
    assert "{profile}" not in rendered


def test_prompt_contains_role_section():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert "## Role" in rendered


def test_prompt_contains_job_management_section():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert "## Job Management Behaviour" in rendered


def test_prompt_contains_profile_behaviour_section():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert "## Profile Behaviour" in rendered


def test_prompt_contains_general_behaviour_section():
    with patch("agent.agent.read_profile", return_value={}):
        rendered = _render_system_prompt()
    assert "## General Behaviour" in rendered


def test_profile_values_appear_in_rendered_prompt():
    profile = {"target_roles": ["Backend Engineer"], "experience_years": 5}
    with patch("agent.agent.read_profile", return_value=profile):
        rendered = _render_system_prompt()
    assert "Backend Engineer" in rendered
    assert "5" in rendered
