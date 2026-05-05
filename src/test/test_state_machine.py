import pytest
from app.models.enums import ApplicationStatus, InvalidTransitionError, transition


def test_valid_forward_transition():
    result = transition(ApplicationStatus.APPLIED, ApplicationStatus.SCREENING)
    assert result == ApplicationStatus.SCREENING


def test_cannot_skip_stages():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.FOUND, ApplicationStatus.OFFER)


def test_offer_is_terminal():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.OFFER, ApplicationStatus.SCREENING)


def test_rejected_is_terminal():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW)


def test_can_reject_from_any_active_stage():
    for status in [ApplicationStatus.APPLIED, ApplicationStatus.SCREENING, ApplicationStatus.INTERVIEW]:
        result = transition(status, ApplicationStatus.REJECTED)
        assert result == ApplicationStatus.REJECTED


def test_cannot_transition_to_same_status():
    with pytest.raises(InvalidTransitionError):
        transition(ApplicationStatus.FOUND, ApplicationStatus.FOUND)
