from enum import Enum


class ApplicationStatus(str, Enum):
    FOUND = "found"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


VALID_TRANSITIONS: dict[ApplicationStatus, list[ApplicationStatus]] = {
    ApplicationStatus.FOUND: [ApplicationStatus.APPLIED, ApplicationStatus.REJECTED],
    ApplicationStatus.APPLIED: [ApplicationStatus.SCREENING, ApplicationStatus.REJECTED],
    ApplicationStatus.SCREENING: [ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED],
    ApplicationStatus.INTERVIEW: [ApplicationStatus.OFFER, ApplicationStatus.REJECTED],
    ApplicationStatus.OFFER: [],
    ApplicationStatus.REJECTED: [],
}


class InvalidTransitionError(Exception):
    pass


def transition(current: ApplicationStatus, next: ApplicationStatus) -> ApplicationStatus:
    allowed = VALID_TRANSITIONS.get(current, [])
    if next not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from '{current.value}' to '{next.value}'"
        )
    return next
