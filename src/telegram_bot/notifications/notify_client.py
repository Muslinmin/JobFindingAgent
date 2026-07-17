import httpx

from telegram_bot.shared.errors import BackendError, extract_detail


class NotifyBackendClient:
    """Thin httpx wrapper for the notification channel's button endpoints."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url
        self._http = http

    async def post_action(self, job_id: int, action: str) -> dict:
        """POST {base_url}/jobs/{job_id}/action with {"action": action}.

        Returns the updated Job dict on 200. Raises BackendError on 422
        (bad action), 404 (no job), 409 (illegal transition).
        """
        resp = await self._http.post(
            f"{self._base_url}/jobs/{job_id}/action", json={"action": action}
        )
        if resp.status_code != 200:
            raise BackendError(resp.status_code, extract_detail(resp))
        return resp.json()

    async def post_followup(self, job_id: int) -> dict:
        """POST {base_url}/jobs/{job_id}/follow-up with {} (note omitted).

        Returns the updated Job dict on 200. Raises BackendError on 422,
        404 (no job), 409 (job not APPLIED).
        """
        resp = await self._http.post(f"{self._base_url}/jobs/{job_id}/follow-up", json={})
        if resp.status_code != 200:
            raise BackendError(resp.status_code, extract_detail(resp))
        return resp.json()
