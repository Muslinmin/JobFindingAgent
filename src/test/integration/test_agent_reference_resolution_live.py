"""RR eval rows — reference resolution against the real model
(agent_v2.md §7, work package WP-A9).

These test *judgment*, not plumbing: given 0, 1, or N candidates, does the
model act, ask, or list? That is the one behaviour the deterministic unit
rows in `src/test/unit/test_agent_reference_resolution.py` cannot cover,
because scripting the tool choice is exactly what those rows do.

Everything below the LLM is a stub, so no database, no embeddings, and no
files are touched — the only real dependency is the model. Assertions are
deliberately coarse (did it mutate? did it ask?) rather than matching
wording, because the prose is not the contract; the tool calls are.

Skipped unless MODEL_API_KEY is set. Run explicitly with:

    pytest src/test/integration/test_agent_reference_resolution_live.py -v -m live -s
"""

import json
from pathlib import Path
import pytest

from agent.context import ConversationContext
from agent.llm_client import AgentLLMClient
from agent.loop import Agent
from app.config import settings
from app.conversation.models import Role, Turn
from profile.schema import Profile, ProfileItem, Skill

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.model_api_key, reason="MODEL_API_KEY not set"),
]


def _row(id, role, company, status="applied", score=7000, changed_at="2026-07-22T00:00:00+00:00"):
    return {
        "id": id, "role": role, "company": company,
        "status": status, "score": score, "status_changed_at": changed_at,
    }


@pytest.fixture
def profile_path(tmp_path) -> Path:
    p = Profile(
        name="Jane Candidate",
        email="jane@example.com",
        location="Singapore",
        skills=[Skill(id="python", label="Python"), Skill(id="sql", label="SQL")],
        experiences=[
            ProfileItem(id="exp_1", title="Backend Engineer", organization="Acme",
                        bullets=["Built services."])
        ],
        target_tracks=["backend engineering"],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(p.model_dump(mode="json")))
    return path


class RecordingDispatcher:
    """Real dispatch shape, scripted results — records every call so a test
    can assert on what the model chose to do."""

    def __init__(self, results: dict):
        self.results = results
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, name: str, args: dict):
        self.calls.append((name, args))
        result = self.results.get(name, {"ok": True})
        return result(args) if callable(result) else result

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeContext:
    """An in-memory stand-in for ConversationContext.

    Stateful on purpose: `record` has to feed `build_context`, exactly as
    the real store does. A mock returning a canned history instead would
    drop the user's own message — the model would see nothing but the
    system prompt and, quite reasonably, do nothing.
    """

    def __init__(self, history: list[Turn] | None = None) -> None:
        self.turns: list[Turn] = list(history or [])

    async def record(self, session_id: str, role: Role, content: str) -> None:
        self.turns.append(_turn(role, content))

    async def build_context(self, session_id: str) -> list[dict]:
        return [
            {"role": "user" if t.role is Role.USER else "assistant", "content": t.content}
            for t in self.turns
        ]


def _agent(dispatcher, profile_path, history: list[Turn] | None = None):
    return Agent(
        llm=AgentLLMClient(),
        dispatcher=dispatcher,
        context=FakeContext(history),
        profile_path=profile_path,
    )


def _turn(role, content):
    return Turn(role=role, content=content, created_at="2026-07-22T00:00:00+00:00")


MUTATORS = {"update_status", "tailor_resume", "draft_followup", "draft_cover_letter", "update_profile"}


# ── RR-1 — one candidate: act ────────────────────────────────────────────────

async def test_rr1_single_match_is_resolved_then_transitioned(profile_path):
    dispatcher = RecordingDispatcher({
        "find_jobs": [_row(42, "Data Analyst", "PUB")],
        "update_status": {"ok": True, "job_id": 42, "role": "Data Analyst", "company": "PUB",
                          "old_status": "applied", "new_status": "interviewing"},
    })
    reply = (await _agent(dispatcher, profile_path).run("s1", "I got an interview with PUB")).reply

    assert "find_jobs" in dispatcher.names(), f"never looked it up: {dispatcher.calls}"
    assert "update_status" in dispatcher.names(), f"resolved but never acted: {dispatcher.calls}"
    status_args = dict(dispatcher.calls[dispatcher.names().index("update_status")][1])
    assert status_args["job_id"] == 42
    assert status_args["new_status"] == "interviewing"
    assert reply


# ── RR-2 — zero candidates: never invent ─────────────────────────────────────

async def test_rr2_no_match_never_mutates_and_never_invents_an_id(profile_path):
    dispatcher = RecordingDispatcher({"find_jobs": []})
    reply = (await _agent(dispatcher, profile_path).run("s1", "I got an interview with PUB")).reply

    mutations = [n for n in dispatcher.names() if n in MUTATORS]
    assert mutations == [], f"mutated with no candidate: {dispatcher.calls}"
    assert reply


# ── RR-3 — three candidates: list and ask, never guess ───────────────────────

async def test_rr3_three_matches_produce_a_question_not_a_mutation(profile_path):
    dispatcher = RecordingDispatcher({
        "find_jobs": [
            _row(58, "Software Engineer", "GovTech"),
            _row(60, "Data Analyst", "GovTech"),
            _row(61, "Product Manager", "GovTech"),
        ],
    })
    reply = (await _agent(dispatcher, profile_path).run("s1", "I got rejected by GovTech")).reply

    # The contract is "clarify over assume", not any particular phrasing —
    # so this asserts on the mutation and on all three candidates being
    # surfaced, never on the wording of the question.
    mutations = [n for n in dispatcher.names() if n in MUTATORS]
    assert mutations == [], f"guessed among 3 candidates: {dispatcher.calls}"
    for distinguishing_label in ("Software Engineer", "Data Analyst", "Product Manager"):
        assert distinguishing_label in reply, f"did not list {distinguishing_label}: {reply!r}"


# ── RR-4 — the follow-up turn narrows it ─────────────────────────────────────

async def test_rr4_followup_turn_resolves_from_session_context(profile_path):
    history = [
        _turn(Role.USER, "I got rejected by GovTech"),
        _turn(Role.ASSISTANT,
              "I found three GovTech roles: Software Engineer (id 58), "
              "Data Analyst (id 60), and Product Manager (id 61). Which one?"),
    ]
    dispatcher = RecordingDispatcher({
        "update_status": {"ok": True, "job_id": 60, "role": "Data Analyst", "company": "GovTech",
                          "old_status": "applied", "new_status": "rejected"},
        "find_jobs": [_row(60, "Data Analyst", "GovTech")],
    })
    await _agent(dispatcher, profile_path, history).run("s1", "the data analyst one")

    assert "update_status" in dispatcher.names(), f"never acted: {dispatcher.calls}"
    args = dict(dispatcher.calls[dispatcher.names().index("update_status")][1])
    assert args["job_id"] == 60, f"resolved to the wrong row: {args}"
    assert args["new_status"] == "rejected"


# ── RR-5 — positional reference over a deterministic order ───────────────────

async def test_rr5_third_one_resolves_positionally(profile_path):
    listed = [
        _row(11, "Backend Engineer", "Acme", changed_at="2026-07-22T05:00:00+00:00"),
        _row(12, "Platform Engineer", "Beta", changed_at="2026-07-22T04:00:00+00:00"),
        _row(13, "Data Engineer", "Gamma", changed_at="2026-07-22T03:00:00+00:00"),
        _row(14, "SRE", "Delta", changed_at="2026-07-22T02:00:00+00:00"),
        _row(15, "ML Engineer", "Epsilon", changed_at="2026-07-22T01:00:00+00:00"),
    ]
    history = [
        _turn(Role.USER, "what's in my pipeline?"),
        _turn(Role.ASSISTANT,
              "Here are your active jobs:\n"
              "1. Backend Engineer at Acme (id 11)\n"
              "2. Platform Engineer at Beta (id 12)\n"
              "3. Data Engineer at Gamma (id 13)\n"
              "4. SRE at Delta (id 14)\n"
              "5. ML Engineer at Epsilon (id 15)"),
    ]
    def _filtered(args):
        """Honour the filter, so a re-query to confirm the referent narrows
        the way the real service would. A stub that ignored `job_title`
        would hand back all five rows again and re-create the ambiguity the
        model had just resolved."""
        title = (args.get("job_title") or "").lower()
        company = (args.get("company") or "").lower()
        return [
            row for row in listed
            if title in row["role"].lower() and company in row["company"].lower()
        ]

    dispatcher = RecordingDispatcher({
        "find_jobs": _filtered,
        "tailor_resume": {"ok": True, "job_id": 13, "artifact_id": 1,
                          "kind": "cv_pdf", "replaced": False},
    })
    await _agent(dispatcher, profile_path, history).run("s1", "tailor my CV for the third one")

    assert "tailor_resume" in dispatcher.names(), f"never tailored: {dispatcher.calls}"
    args = dict(dispatcher.calls[dispatcher.names().index("tailor_resume")][1])
    assert args["job_id"] == 13, f"'third one' resolved to {args['job_id']}, expected 13"


# ── RR-6 — 'that one', with and without a salient referent ───────────────────

async def test_rr6a_single_salient_referent_is_acted_on(profile_path):
    history = [
        _turn(Role.USER, "did I hear back from PUB?"),
        _turn(Role.ASSISTANT, "Not yet — PUB Data Analyst (id 42), applied three weeks ago."),
    ]
    dispatcher = RecordingDispatcher({
        "draft_followup": {"ok": True, "job_id": 42, "artifact_id": 1, "text": "Dear ..."},
        "find_jobs": [_row(42, "Data Analyst", "PUB")],
    })
    await _agent(dispatcher, profile_path, history).run("s1", "draft a follow-up for that one")

    assert "draft_followup" in dispatcher.names(), f"never drafted: {dispatcher.calls}"
    args = dict(dispatcher.calls[dispatcher.names().index("draft_followup")][1])
    assert args["job_id"] == 42


# A question mark is too literal a proxy for "asked". A model that replies
# "Tell me the company or job title, or share which job you mean." has done
# exactly what RR-6 requires, and failed `"?" in reply` three runs out of
# three. What the row actually asserts is the pair: it did not invent a
# referent (checked separately, and that is the strong half), and it went
# back to the user for the missing one rather than picking silently.
_CLARIFY_CUES = ("?", "which", "tell me", "let me know", "specify", "share which")


def _solicits_clarification(reply: str) -> bool:
    lowered = reply.lower()
    return any(cue in lowered for cue in _CLARIFY_CUES)


async def test_rr6b_ambiguous_referent_asks_instead_of_assuming(profile_path):
    history = [
        _turn(Role.USER, "what's outstanding?"),
        _turn(Role.ASSISTANT,
              "Three applications are still open: Software Engineer at GovTech (id 58), "
              "Data Analyst at GovTech (id 60), and Product Manager at GovTech (id 61)."),
    ]
    dispatcher = RecordingDispatcher({
        "find_jobs": [
            _row(58, "Software Engineer", "GovTech"),
            _row(60, "Data Analyst", "GovTech"),
            _row(61, "Product Manager", "GovTech"),
        ],
    })
    reply = (await _agent(dispatcher, profile_path, history).run(
        "s1", "draft a follow-up for that one"
    )).reply

    assert "draft_followup" not in dispatcher.names(), f"guessed a referent: {dispatcher.calls}"
    assert _solicits_clarification(reply), f"did not ask which one: {reply!r}"


async def test_rr6c_referent_absent_from_context_asks_which(profile_path):
    dispatcher = RecordingDispatcher({"find_jobs": []})
    reply = (await _agent(dispatcher, profile_path).run("s1", "draft a follow-up for that one")).reply

    assert "draft_followup" not in dispatcher.names(), f"invented a referent: {dispatcher.calls}"
    assert _solicits_clarification(reply), f"did not ask which one: {reply!r}"


# ── RR-8 — the model relays the service's FSM verdict ────────────────────────

async def test_rr8_illegal_transition_is_relayed_not_reasoned_around(profile_path):
    dispatcher = RecordingDispatcher({
        "find_jobs": [_row(42, "Data Analyst", "PUB", status="rejected")],
        "update_status": {"ok": False, "error": "illegal_transition",
                          "from": "rejected", "to": "offer", "allowed": []},
    })
    reply = (await _agent(dispatcher, profile_path).run("s1", "I got an offer from PUB")).reply

    assert "update_status" in dispatcher.names(), f"never proposed the move: {dispatcher.calls}"
    # It must not retry the refused move — the verdict is authoritative.
    assert dispatcher.names().count("update_status") == 1, f"retried a refusal: {dispatcher.calls}"
    assert "reject" in reply.lower(), f"did not relay the reason: {reply!r}"
