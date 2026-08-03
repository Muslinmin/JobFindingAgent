from typing import Protocol

import httpx
from loguru import logger


class BackendError(Exception):
    """Raised by AgentBackendClient / NotifyBackendClient on any non-200
    response from the backend. Carries the status code and the `detail`
    string FastAPI's exception handlers already put in the JSON body
    (app/exception_handlers.py), so callers never have to re-parse it.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class SendClient(Protocol):
    async def send_message(self, text: str) -> None: ...


def extract_detail(resp: httpx.Response) -> str:
    """Pull the `detail` field out of a FastAPI error response body,
    falling back to raw text if the body isn't JSON or has no `detail`.
    Shared by AgentBackendClient and NotifyBackendClient so both raise
    BackendError with the same, real backend-provided message.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    return str(body.get("detail", resp.text))


_STATUS_MESSAGES: dict[int, str] = {
    422: "That request wasn't valid: {detail}",
    404: "I can't find that job anymore — it may have been removed.",
    409: "That action doesn't apply to this job right now: {detail}",
    500: "Something went wrong on my end handling that. Please try again.",
}


async def handle_backend_error(send_client: SendClient, exc: Exception, context: str) -> None:
    """Log via loguru and send the user a plain-text error message through
    whichever send-client the calling bot uses (ChatTelegramClient or
    NotificationTelegramClient — both expose send_message(text)).

    BackendError carries a real status code, so 422/404/409/500 each get a
    distinct, accurate message (see _STATUS_MESSAGES) rather than a single
    generic failure string — a 404 ("job not found") and a 409 ("wrong
    state for that action") must never look the same to the user, since
    they mean different things about the job's actual state in the DB.
    Any other exception (transport failure: connection refused, timeout,
    DNS) is treated as "backend unreachable", distinct from either.
    """
    if isinstance(exc, BackendError):
        logger.error(f"{context}: backend returned {exc.status_code} — {exc.detail}")
        text = _STATUS_MESSAGES.get(
            exc.status_code, "The request failed: {detail}"
        ).format(detail=exc.detail)
    elif isinstance(exc, httpx.HTTPError):
        logger.error(f"{context}: transport failure — {type(exc).__name__}: {exc}")
        text = "I couldn't reach the backend right now. Please try again shortly."
    else:
        logger.error(f"{context}: unexpected error — {type(exc).__name__}: {exc}")
        text = "Something unexpected went wrong handling that."

    await send_client.send_message(text)
