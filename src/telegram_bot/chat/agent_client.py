import httpx

from telegram_bot.shared.errors import BackendError, extract_detail


class AgentBackendClient:
    """Thin httpx wrapper for the agent's /chat endpoint."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url
        self._http = http

    async def post_chat(self, message: str) -> dict:
        """POST {base_url}/chat with {"message": message} — nothing else.

        Returns the parsed body {reply, attachments} on 200, where each
        attachment is {kind, filename, mime_type, content_b64} matching
        app/routes/chat.py's Attachment model verbatim. Raises BackendError
        on non-200 (422 empty message / 500 agent raised), reading `detail`
        out of the FastAPI error envelope.
        """
        resp = await self._http.post(f"{self._base_url}/chat", json={"message": message})
        if resp.status_code != 200:
            raise BackendError(resp.status_code, extract_detail(resp))
        return resp.json()
