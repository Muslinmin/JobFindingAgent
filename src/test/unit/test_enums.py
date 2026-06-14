import pytest

from app.models.enums import (
    ApplicationStatus,
    VALID_TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    legal_targets,
    transition,
)

TERMINAL_STATES = {
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.USER_SKIPPED,
    ApplicationStatus.EXPIRED,
    ApplicationStatus.DECLINED,
}


# --- legal edges -----------------------------------------------------------
@pytest.mark.parametrize(
    "current,target",
    [
        (ApplicationStatus.DISCOVERED, ApplicationStatus.SCORED),
        (ApplicationStatus.DISCOVERED, ApplicationStatus.REJECTED),
        (ApplicationStatus.SCORED, ApplicationStatus.TAILORED),
        (ApplicationStatus.SCORED, ApplicationStatus.REJECTED),          # staleness
        (ApplicationStatus.TAILORED, ApplicationStatus.PENDING_APPROVAL),
        (ApplicationStatus.TAILORED, ApplicationStatus.APPLYING),        # auto_apply branch
        (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.APPLIED),
        (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.EXPIRED),
        (ApplicationStatus.APPLYING, ApplicationStatus.APPLY_FAILED),
        (ApplicationStatus.APPLY_FAILED, ApplicationStatus.APPLIED),
        (ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEWING),
        (ApplicationStatus.APPLIED, ApplicationStatus.GHOSTED),
        (ApplicationStatus.APPLIED, ApplicationStatus.DECLINED),         # candidate withdraws
        (ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER),
        (ApplicationStatus.INTERVIEWING, ApplicationStatus.DECLINED),    # candidate withdraws
        (ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED),
        (ApplicationStatus.OFFER, ApplicationStatus.DECLINED),           # candidate declines
        (ApplicationStatus.OFFER, ApplicationStatus.REJECTED),           # offer rescinded
        (ApplicationStatus.GHOSTED, ApplicationStatus.INTERVIEWING),     # resurrection
    ],
)
def test_legal_edges_pass(current, target):
    assert can_transition(current, target)
    assert transition(current, target) == target


# --- illegal edges ---------------------------------------------------------
@pytest.mark.parametrize(
    "current,target",
    [
        (ApplicationStatus.DISCOVERED, ApplicationStatus.APPLIED),       # must be scored/tailored first
        (ApplicationStatus.APPLIED, ApplicationStatus.OFFER),            # must interview first
        (ApplicationStatus.SCORED, ApplicationStatus.INTERVIEWING),
        (ApplicationStatus.REJECTED, ApplicationStatus.OFFER),           # terminal
        (ApplicationStatus.ACCEPTED, ApplicationStatus.INTERVIEWING),    # terminal
    ],
)
def test_illegal_edges_rejected(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError):
        transition(current, target)


# --- self-loop (multiple interview rounds) ---------------------------------
def test_interviewing_self_loop_allowed():
    assert can_transition(ApplicationStatus.INTERVIEWING, ApplicationStatus.INTERVIEWING)
    assert (
        transition(ApplicationStatus.INTERVIEWING, ApplicationStatus.INTERVIEWING)
        == ApplicationStatus.INTERVIEWING
    )


# --- terminal states have no exits -----------------------------------------
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_exits(state):
    assert legal_targets(state) == set()


# --- structural completeness -----------------------------------------------
def test_every_status_is_defined_in_the_map():
    # Guards against forgetting to define transitions for a new state.
    assert set(VALID_TRANSITIONS.keys()) == set(ApplicationStatus)


def test_transition_returns_target_on_success():
    assert (
        transition(ApplicationStatus.DISCOVERED, ApplicationStatus.SCORED)
        == ApplicationStatus.SCORED
    )


def test_error_message_names_both_states():
    with pytest.raises(InvalidTransitionError) as exc:
        transition(ApplicationStatus.REJECTED, ApplicationStatus.OFFER)
    message = str(exc.value)
    assert "rejected" in message and "offer" in message
