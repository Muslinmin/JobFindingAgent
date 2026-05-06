import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

from scraper.tavily_client import search


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_settings(api_key="tvly-test", max_results=10):
    """Return a settings-like mock with only the fields tavily_client reads."""
    m = type("S", (), {
        "tavily_api_key":    api_key,
        "scrape_max_results": max_results,
    })()
    return m


def _make_async_client(post_return=None, post_side_effect=None):
    """Build a fully-wired AsyncClient context-manager mock."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    if post_side_effect is not None:
        mock_client.post.side_effect = post_side_effect
    else:
        mock_client.post.return_value = post_return
    return mock_client


def _make_response(results: list):
    mock_resp = AsyncMock()
    mock_resp.json            = MagicMock(return_value={"results": results})
    mock_resp.raise_for_status = MagicMock(return_value=None)
    return mock_resp


# ── missing / empty API key ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_list_when_api_key_is_missing():
    with patch("scraper.tavily_client.settings", _mock_settings(api_key="")):
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_does_not_make_http_call_when_api_key_is_missing():
    with patch("scraper.tavily_client.settings", _mock_settings(api_key="")), \
         patch("scraper.tavily_client.httpx.AsyncClient") as mock_cls:
        await search("data engineer Singapore")
    mock_cls.assert_not_called()


# ── happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_results_from_tavily_response():
    fake_results = [
        {"title": "Data Engineer — GovTech", "url": "https://careers.gov.sg/1", "content": "..."},
    ]
    mock_client = _make_async_client(post_return=_make_response(fake_results))
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        result = await search("data engineer Singapore")
    assert result == fake_results


@pytest.mark.asyncio
async def test_returns_empty_list_when_tavily_returns_no_results():
    mock_client = _make_async_client(post_return=_make_response([]))
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_passes_query_in_request_payload():
    captured = {}

    async def capture_post(url, json=None, **kwargs):
        captured["payload"] = json
        return _make_response([])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = capture_post

    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        await search("backend engineer SG")

    assert captured["payload"]["query"] == "backend engineer SG"


@pytest.mark.asyncio
async def test_passes_api_key_in_request_payload():
    captured = {}

    async def capture_post(url, json=None, **kwargs):
        captured["payload"] = json
        return _make_response([])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = capture_post

    with patch("scraper.tavily_client.settings", _mock_settings(api_key="tvly-abc123")), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        await search("any query")

    assert captured["payload"]["api_key"] == "tvly-abc123"


@pytest.mark.asyncio
async def test_uses_settings_max_results_by_default():
    captured = {}

    async def capture_post(url, json=None, **kwargs):
        captured["payload"] = json
        return _make_response([])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = capture_post

    with patch("scraper.tavily_client.settings", _mock_settings(max_results=5)), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        await search("any query")

    assert captured["payload"]["max_results"] == 5


@pytest.mark.asyncio
async def test_caller_can_override_max_results():
    captured = {}

    async def capture_post(url, json=None, **kwargs):
        captured["payload"] = json
        return _make_response([])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = capture_post

    with patch("scraper.tavily_client.settings", _mock_settings(max_results=10)), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        await search("any query", max_results=3)

    assert captured["payload"]["max_results"] == 3


# ── error handling ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_list_on_request_error():
    mock_client = _make_async_client(
        post_side_effect=httpx.RequestError("connection refused")
    )
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_list_on_http_status_error():
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)
    ))
    mock_client = _make_async_client(post_return=mock_resp)
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_list_on_unexpected_exception():
    mock_client = _make_async_client(post_side_effect=Exception("something broke"))
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        result = await search("data engineer Singapore")
    assert result == []


@pytest.mark.asyncio
async def test_never_raises_on_any_failure():
    mock_client = _make_async_client(post_side_effect=RuntimeError("boom"))
    with patch("scraper.tavily_client.settings", _mock_settings()), \
         patch("scraper.tavily_client.httpx.AsyncClient", return_value=mock_client):
        try:
            await search("data engineer Singapore")
        except Exception:
            pytest.fail("search() raised an exception — it must never raise")
