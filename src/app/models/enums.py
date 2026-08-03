from enum import Enum


class ArtifactKind(str, Enum):
    CV_PDF = "cv_pdf"
    COVER_LETTER = "cover_letter"
    FOLLOW_UP_EMAIL = "follow_up_email"


class ApplicationStatus(str, Enum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    TAILORED = "tailored"
    PENDING_APPROVAL = "pending_approval"
    APPLYING = "applying"
    APPLY_FAILED = "apply_failed"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    USER_SKIPPED = "user_skipped"
    EXPIRED = "expired"
    DECLINED = "declined"
    GHOSTED = "ghosted"


VALID_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    ApplicationStatus.DISCOVERED: {ApplicationStatus.SCORED, ApplicationStatus.REJECTED},
    ApplicationStatus.SCORED: {ApplicationStatus.TAILORED, ApplicationStatus.REJECTED},
    # APPLIED is reachable directly because the conversational path has no
    # use for PENDING_APPROVAL. That state means "the system produced a CV
    # and is waiting for a human" — it has duration only in the scheduler's
    # push flow, where the human is asleep. A user who asked for the resume,
    # read it, and says "I'm applying" would otherwise have to walk three
    # status moves to express one intent.
    ApplicationStatus.TAILORED: {
        ApplicationStatus.PENDING_APPROVAL,
        ApplicationStatus.APPLYING,
        ApplicationStatus.APPLIED,
    },
    ApplicationStatus.PENDING_APPROVAL: {
        ApplicationStatus.APPLIED,
        ApplicationStatus.USER_SKIPPED,
        ApplicationStatus.EXPIRED,
        ApplicationStatus.APPLYING,
    },
    ApplicationStatus.APPLYING: {ApplicationStatus.APPLIED, ApplicationStatus.APPLY_FAILED},
    ApplicationStatus.APPLY_FAILED: {ApplicationStatus.APPLIED, ApplicationStatus.USER_SKIPPED},
    ApplicationStatus.APPLIED: {
        ApplicationStatus.INTERVIEWING,
        ApplicationStatus.REJECTED,
        ApplicationStatus.GHOSTED,
        ApplicationStatus.DECLINED,
    },
    ApplicationStatus.INTERVIEWING: {
        ApplicationStatus.INTERVIEWING,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
        ApplicationStatus.GHOSTED,
        ApplicationStatus.DECLINED,
    },
    ApplicationStatus.OFFER: {
        ApplicationStatus.ACCEPTED,
        ApplicationStatus.DECLINED,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.GHOSTED: {ApplicationStatus.INTERVIEWING},
    ApplicationStatus.ACCEPTED: set(),
    ApplicationStatus.REJECTED: set(),
    ApplicationStatus.USER_SKIPPED: set(),
    ApplicationStatus.EXPIRED: set(),
    ApplicationStatus.DECLINED: set(),
}


class UserAction(str, Enum):
    APPLIED = "applied"
    USER_SKIPPED = "user_skipped"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    REJECTED = "rejected"


class InvalidTransitionError(Exception):
    pass


def can_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    return target in VALID_TRANSITIONS[current]


def legal_targets(current: ApplicationStatus) -> set[ApplicationStatus]:
    return set(VALID_TRANSITIONS[current])


def transition(current: ApplicationStatus, target: ApplicationStatus) -> ApplicationStatus:
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"Cannot transition from '{current.value}' to '{target.value}'"
        )
    return target
