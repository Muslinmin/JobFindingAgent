"""Drafting layer — follow-up prompts and guarded cover letters.

The load-bearing case in this file is
`test_a_skill_from_the_posting_the_candidate_lacks_is_caught`. The whole
reason `_source_text` excludes the JD body is that including it would
silence the guard exactly where it matters: everything the posting
mentions would become "known", so a technology the candidate has never
touched could be claimed freely. If that test ever starts passing for the
wrong reason, the guard has become decorative.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from drafting.cover_letter import (
    DraftingError,
    MAX_GUARD_RETRIES,
    _source_text,
    assemble_cover_letter_prompt,
    draft_cover_letter,
)
from drafting.followup import assemble_followup_prompt, draft_followup
from profile.schema import Education, Profile, ProfileItem, Skill

JD = (
    "We are hiring a Backend Engineer. Our stack runs on Kubernetes and "
    "Terraform. You will work with a team of 40 across three offices."
)


@pytest.fixture
def profile():
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python", aliases=["Py"])],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Backend Engineer",
                organization="Acme",
                bullets=["Built a payments service in Python."],
                demonstrated_skills=["python"],
            )
        ],
        education=[Education(id="edu_1", institution="SIT", degree="BEng")],
        target_tracks=["backend engineering"],
    )


@pytest.fixture
def llm():
    return MagicMock(complete=AsyncMock(return_value="I built a payments service in Python."))


# ── follow-up ─────────────────────────────────────────────────────────────────

def test_followup_prompt_carries_role_company_and_date():
    prompt = assemble_followup_prompt("Data Engineer", "PUB", "2026-01-01T00:00:00+00:00")
    assert "Data Engineer" in prompt
    assert "PUB" in prompt
    assert "2026-01-01" in prompt


def test_followup_prompt_omits_the_note_section_when_there_is_none():
    assert "candidate asked for this" not in assemble_followup_prompt("R", "C", "D")


def test_followup_prompt_forwards_the_note_verbatim():
    prompt = assemble_followup_prompt("R", "C", "D", note="mention the robotics project")
    assert "mention the robotics project" in prompt
    assert "do not invent" in prompt


async def test_draft_followup_returns_what_the_model_said(llm):
    llm.complete.return_value = "Hi, just following up."
    assert await draft_followup("R", "C", "D", llm) == "Hi, just following up."


async def test_draft_followup_lets_the_client_error_propagate(llm):
    """Each caller decides what a failure means — the scheduler logs and
    moves on, the agent lets it abort the turn."""
    llm.complete.side_effect = RuntimeError("rate limited")
    with pytest.raises(RuntimeError):
        await draft_followup("R", "C", "D", llm)


# ── cover letter: what the guard may draw on ──────────────────────────────────

def test_source_text_includes_the_employer_and_role(profile):
    """The letter has to be able to name the job it is applying for."""
    source = _source_text(profile, "GovTech", "Backend Engineer")
    assert "GovTech" in source
    assert "Backend Engineer" in source


def test_source_text_includes_identity_the_letter_will_mention(profile):
    """The candidate's own name and school are fair game in the output even
    though the LLM never receives them in the prompt — the guard decides
    what may appear, which is a wider set than what goes in."""
    source = _source_text(profile, "GovTech", "Backend Engineer")
    assert "Jane Candidate" in source
    assert "SIT" in source


def test_source_text_excludes_the_job_description(profile):
    """The mechanism the whole guard rests on."""
    source = _source_text(profile, "GovTech", "Backend Engineer")
    assert "Kubernetes" not in source
    assert "Terraform" not in source


def test_the_llm_never_sees_render_tier_fields(profile):
    prompt = assemble_cover_letter_prompt(JD, profile, "GovTech", "Backend Engineer")
    assert "jane@example.com" not in prompt


# ── cover letter: the guard ───────────────────────────────────────────────────

async def test_a_supported_letter_passes(profile, llm):
    letter = await draft_cover_letter(
        JD, profile, "GovTech", "Backend Engineer", llm=llm
    )
    assert letter == "I built a payments service in Python."
    assert llm.complete.await_count == 1


async def test_a_skill_from_the_posting_the_candidate_lacks_is_caught(profile, llm):
    """The case the JD exclusion exists for. Kubernetes is all over the
    posting and nowhere in the profile, so claiming it must not slip
    through just because the employer mentioned it."""
    llm.complete.return_value = "I have deep experience running Kubernetes in production."

    with pytest.raises(DraftingError) as excinfo:
        await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)

    assert excinfo.value.reason == "guard_violation"
    assert any("Kubernetes" in v for v in excinfo.value.violations)


async def test_an_invented_number_is_caught(profile, llm):
    llm.complete.return_value = "I built a payments service in Python for 40 clients."

    with pytest.raises(DraftingError) as excinfo:
        await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)

    assert excinfo.value.reason == "guard_violation"


async def test_naming_the_employer_is_not_a_violation(profile, llm):
    llm.complete.return_value = "I built a payments service in Python. GovTech is where I want to be."
    assert await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)


async def test_a_flagged_draft_is_retried_with_the_violations_fed_back(profile, llm):
    llm.complete.side_effect = [
        "I ran Kubernetes clusters.",
        "I built a payments service in Python.",
    ]

    letter = await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)

    assert letter == "I built a payments service in Python."
    assert llm.complete.await_count == 2
    retry_prompt = llm.complete.await_args_list[1].args[0]
    assert "flagged by the truthfulness checker" in retry_prompt
    assert "Kubernetes" in retry_prompt


async def test_retries_are_bounded(profile, llm):
    llm.complete.return_value = "I ran Kubernetes clusters."

    with pytest.raises(DraftingError):
        await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)

    assert llm.complete.await_count == MAX_GUARD_RETRIES + 1


async def test_a_failing_client_is_not_laundered_into_a_guard_violation(profile, llm):
    """'The provider is down' and 'the letter lied' are different facts and
    the caller branches on them differently."""
    llm.complete.side_effect = RuntimeError("rate limited")

    with pytest.raises(DraftingError) as excinfo:
        await draft_cover_letter(JD, profile, "GovTech", "Backend Engineer", llm=llm)

    assert excinfo.value.reason == "llm_call_failed"
    assert llm.complete.await_count == 1  # not retried — a guard retry would not help


async def test_the_note_reaches_the_prompt(profile, llm):
    await draft_cover_letter(
        JD, profile, "GovTech", "Backend Engineer", llm=llm, note="lead with the payments work"
    )
    assert "lead with the payments work" in llm.complete.await_args.args[0]
