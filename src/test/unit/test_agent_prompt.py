from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.prompt import _SYSTEM_PROMPT_PATH, compose, system_message
from profile.schema import Profile, ProfileItem, Skill


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        location="Singapore",
        summary_seed="A seed paragraph that must never reach the agent.",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Backend Engineer",
                organization="Acme",
                bullets=["A frozen factual bullet."],
            )
        ],
        target_tracks=["backend engineering"],
    )
    base.update(overrides)
    return Profile(**base)


@pytest.fixture
def context():
    c = MagicMock()
    c.build_context = AsyncMock(return_value=[])
    return c


# ── system_message ────────────────────────────────────────────────────────────

def test_system_message_is_a_system_role_message():
    message = system_message(_profile())
    assert message["role"] == "system"


def test_system_message_contains_the_spine_from_disk():
    spine = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    content = system_message(_profile())["content"]
    assert spine.rstrip() in content


def test_system_message_appends_the_profile_summary():
    content = system_message(_profile())["content"]
    assert "Jane Candidate" in content
    assert "python: Python" in content


def test_system_message_carries_summary_not_the_full_profile():
    """Reference tier only — bodies and contact details are loaded
    just-in-time by the tailoring service, never carried in the loop."""
    content = system_message(_profile())["content"]
    assert "A frozen factual bullet." not in content
    assert "seed paragraph" not in content
    assert "jane@example.com" not in content


def test_system_message_orders_spine_before_profile():
    content = system_message(_profile())["content"]
    assert content.index("## Role") < content.index("## Current profile")


def test_system_message_handles_an_empty_profile():
    content = system_message(Profile(name="", email=""))["content"]
    assert "profile is empty" in content


def test_spine_uses_the_real_status_vocabulary():
    """The spine must not teach a status vocabulary the FSM doesn't have —
    the v1 spine listed 'screening'/'interview'/'found', none of which are
    ApplicationStatus members."""
    spine = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").lower()
    for invented in ("screening", "status enum values exactly"):
        assert invented not in spine


def test_spine_states_the_resolve_then_act_discipline():
    spine = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert "find_jobs" in spine
    assert "PENDING_ACTION" in spine


# ── compose ───────────────────────────────────────────────────────────────────

async def test_compose_puts_the_system_message_first(context):
    messages = await compose("s1", _profile(), context)
    assert messages[0]["role"] == "system"


async def test_compose_appends_turns_after_the_system_message(context):
    context.build_context.return_value = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    messages = await compose("s1", _profile(), context)
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert messages[1]["content"] == "hi"


async def test_compose_reads_the_session_it_was_given(context):
    await compose("session-42", _profile(), context)
    context.build_context.assert_called_once_with("session-42")


async def test_compose_on_an_empty_session_is_system_message_only(context):
    context.build_context.return_value = []
    messages = await compose("s1", _profile(), context)
    assert len(messages) == 1
