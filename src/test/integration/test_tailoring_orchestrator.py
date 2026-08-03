import json
import shutil
from pathlib import Path

import pytest

from profile.schema import Profile, ProfileItem, Skill
from tailoring.tailor import ArtifactResult, TailoringError, tailor

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"

skip_if_no_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed"
)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str) -> str:
        return self.response


class SequenceFakeLLM:
    """Returns a different response on each successive call — used to drive
    the guard-retry loop through a scripted sequence of attempts, e.g. a
    violating first attempt followed by a corrected second one."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _profile() -> Profile:
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=["Built a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
    )


def _profile_with_bullet(bullet: str) -> Profile:
    """A one-experience profile whose source bullet is the caller's choice
    — used by the numeral-guard scenarios below, where what matters is the
    exact text the LLM's rewrite gets diffed against."""
    return Profile(
        name="Jane Candidate",
        email="jane@example.com",
        skills=[Skill(id="python", label="Python")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                bullets=[bullet],
                demonstrated_skills=["python"],
            )
        ],
        projects=[],
    )


def _selection_json(bullets: list[str]) -> str:
    return json.dumps(
        {
            "summary": "A concise summary.",
            "experience_order": ["exp_1"],
            "experiences": [{"ref_id": "exp_1", "bullets": bullets}],
            "project_order": [],
            "projects": [],
            "skill_order": ["python"],
        }
    )


# TC-ORCH-08 — end-to-end with a mock LLM and the real renderer; no DB touch
# anywhere in the layer (nothing here imports app.* at all — the boundary
# is enforced at the module level, not just by not calling anything).
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_tailor_end_to_end_with_real_renderer(tmp_path):
    profile = _profile()
    llm = FakeLLM(
        json.dumps(
            {
                "summary": "A concise summary.",
                "experience_order": ["exp_1"],
                "experiences": [{"ref_id": "exp_1", "bullets": ["Built a thing with Python."]}],
                "project_order": [],
                "projects": [],
                "skill_order": ["python"],
            }
        )
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )

    assert isinstance(result, ArtifactResult)
    assert result.path.exists()
    assert result.path.stat().st_size > 0


# TC-ORCH-09 — end-to-end, real renderer: a comma-thousands numeral
# ("$20,000") sits directly before a clause-break comma. The rewrite swaps
# only the verb in the clause AFTER the comma ("ensuring" -> "maintaining");
# the word the number is actually anchored to BEFORE the comma ("total")
# is unchanged. This is exactly the "S$20,000, ensuring/maintaining" case
# the backward-anchor guard (tailoring_guard_retry_delta.md R1) exists to
# let through — a real paraphrase, not a fabricated claim.
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_tailor_passes_with_correctly_paraphrased_comma_numeral(tmp_path):
    profile = _profile_with_bullet(
        "Raised a total of $20,000, ensuring full transparency with donors."
    )
    llm = FakeLLM(
        _selection_json(
            ["Raised a total of $20,000, maintaining full transparency with donors."]
        )
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )

    assert isinstance(result, ArtifactResult)
    assert result.path.exists()


# TC-ORCH-10 — end-to-end: same numeral, same digits ("$20,000"), but the
# word it's anchored to BEFORE the comma silently changes ("a total of" ->
# "a grant of"). The digits alone still appear in the source, so a
# context-blind check would wave this through — but the amount has been
# re-attributed to a different (fabricated) fact. tailor() must fail
# cleanly with no render, never a partial artifact.
@pytest.mark.asyncio
async def test_tailor_raises_guard_violation_when_comma_numeral_context_changes(tmp_path):
    profile = _profile_with_bullet(
        "Raised a total of $20,000, ensuring full transparency with donors."
    )
    llm = FakeLLM(
        _selection_json(
            ["Raised a grant of $20,000, maintaining full transparency with donors."]
        )
    )

    with pytest.raises(TailoringError) as exc_info:
        await tailor("jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path)

    assert exc_info.value.reason == "guard_violation"
    assert any("20,000" in v for v in exc_info.value.violations)


# TC-ORCH-11 — end-to-end, real renderer: a decimal numeral ("20.00%") is a
# documented soft edge (tailoring.md §4), not something WP-A touches. The
# guard's number regex has no notion of "." — it splits "20.00" into two
# independent digit runs ("20", "00"), and neither is glued to a
# clause-break comma, so each falls back to the loose bare-presence check
# rather than a strict neighbour pairing. A full rewrite of the words
# around the number therefore still passes. This test pins that behavior
# end-to-end so a future regex change doesn't silently alter it.
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_tailor_passes_with_paraphrased_decimal_numeral(tmp_path):
    profile = _profile_with_bullet(
        "Reduced deployment time by 20.00%, improving reliability across the board."
    )
    llm = FakeLLM(
        _selection_json(
            ["Cut deployment time by 20.00%, boosting reliability across the board."]
        )
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )

    assert isinstance(result, ArtifactResult)
    assert result.path.exists()


# TC-ORCH-12 — end-to-end: the FIRST LLM attempt reuses "$20,000" under a
# different backward anchor ("a grant of") than the source ("a total of")
# — a real guard violation, same as TC-ORCH-10, so it's genuinely retried
# rather than a false positive slipping through. The SECOND attempt
# restores the true anchor. This is the one test in the suite that proves
# the retry loop actually fires end-to-end (not just that it CAN, per
# TC-RETRY-01 in the unit suite) and that recovery produces a real PDF
# from the corrected text, not a stale/partial one from the failed attempt.
@skip_if_no_tectonic
@pytest.mark.asyncio
async def test_tailor_recovers_via_retry_after_comma_numeral_violation(tmp_path):
    profile = _profile_with_bullet(
        "Raised a total of $20,000, ensuring full transparency with donors."
    )
    llm = SequenceFakeLLM(
        [
            _selection_json(
                ["Raised a grant of $20,000, maintaining full transparency with donors."]
            ),
            _selection_json(
                ["Raised a total of $20,000, maintaining full transparency with donors."]
            ),
        ]
    )

    result = await tailor(
        "jd", profile, llm=llm, template_path=TEMPLATE_PATH, output_dir=tmp_path
    )

    assert isinstance(result, ArtifactResult)
    assert result.path.exists()
    # The retry actually happened, via the real loop in tailor.py — not a
    # first-attempt pass that happened to also satisfy the assertions above.
    assert llm.calls == 2
    assert "20,000" in llm.prompts[1]
