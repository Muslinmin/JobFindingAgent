"""
Unit tests for jobs/digest_job.py — digest_job() plain function.

Jobs are tested by calling digest_job() directly — no scheduler is started.
Mocks: httpx.AsyncClient (GET /jobs), telegram.Bot (send_message).
Tested: staleness filtering (14-day boundary), message content, empty list path, failure handling.

Run with:
    pytest src/test/unit/test_digest_job.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from jobs.digest_job import digest_job


# ── helpers ───────────────────────────────────────────────────────────────────

def _job(company: str, role: str, days_old: int, job_id: int = 1) -> dict:
    date = datetime.now(timezone.utc) - timedelta(days=days_old)
    return {
        "id": job_id,
        "company": company,
        "role": role,
        "url": f"https://example.com/job/{job_id}",
        "status": "found",
        "source": "tavily",
        "notes": None,
        "description": "A job listing.",
        "score": 0.5,
        "date_logged": date.isoformat(),
    }


def _mock_jobs_api(jobs: list):
    mock_response = MagicMock()
    mock_response.json.return_value = jobs
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_client


def _mock_telegram_bot():
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()

    mock_bot_cls = MagicMock()
    mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=mock_bot)
    mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_bot_cls, mock_bot


# ── staleness filtering ───────────────────────────────────────────────────────

async def test_digest_job_identifies_stale_applications():
    """Jobs at exactly 14 days and beyond are stale; under 14 days are excluded."""
    jobs = [
        _job("GovTech",  "Data Engineer",    days_old=20, job_id=1),
        _job("Grab",     "Backend Engineer",  days_old=5,  job_id=2),
        _job("DBS",      "SWE",               days_old=14, job_id=3),
    ]
    mock_cls, _ = _mock_jobs_api(jobs)
    mock_bot_cls, mock_bot = _mock_telegram_bot()

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 12345
        await digest_job()

    mock_bot.send_message.assert_called_once()
    message_text = mock_bot.send_message.call_args[1]["text"]
    assert "GovTech" in message_text
    assert "DBS" in message_text
    assert "Grab" not in message_text


async def test_digest_job_excludes_all_fresh_applications():
    jobs = [
        _job("Shopee", "Frontend Engineer", days_old=3, job_id=1),
        _job("Sea",    "Data Analyst",      days_old=7, job_id=2),
    ]
    mock_cls, _ = _mock_jobs_api(jobs)
    mock_bot_cls, mock_bot = _mock_telegram_bot()

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 12345
        await digest_job()

    mock_bot.send_message.assert_not_called()


async def test_digest_job_no_applications_sends_no_message():
    mock_cls, _ = _mock_jobs_api([])
    mock_bot_cls, mock_bot = _mock_telegram_bot()

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 12345
        await digest_job()

    mock_bot.send_message.assert_not_called()


# ── message content ───────────────────────────────────────────────────────────

async def test_digest_job_message_contains_company_and_role():
    jobs = [_job("Stripe", "Platform Engineer", days_old=21, job_id=1)]
    mock_cls, _ = _mock_jobs_api(jobs)
    mock_bot_cls, mock_bot = _mock_telegram_bot()

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 12345
        await digest_job()

    message_text = mock_bot.send_message.call_args[1]["text"]
    assert "Stripe" in message_text
    assert "Platform Engineer" in message_text


async def test_digest_job_message_sent_to_configured_chat_id():
    jobs = [_job("Grab", "ML Engineer", days_old=30, job_id=1)]
    mock_cls, _ = _mock_jobs_api(jobs)
    mock_bot_cls, mock_bot = _mock_telegram_bot()

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 99999
        await digest_job()

    assert mock_bot.send_message.call_args[1]["chat_id"] == 99999


# ── Telegram failure ──────────────────────────────────────────────────────────

async def test_digest_job_telegram_failure_exits_cleanly(caplog):
    jobs = [_job("Grab", "Data Engineer", days_old=20, job_id=1)]
    mock_cls, _ = _mock_jobs_api(jobs)

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API down"))
    mock_bot_cls = MagicMock()
    mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=mock_bot)
    mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("jobs.digest_job.httpx.AsyncClient", mock_cls), \
         patch("jobs.digest_job.Bot", mock_bot_cls), \
         patch("jobs.digest_job.settings") as mock_settings:
        mock_settings.api_base_url = "http://localhost:8000"
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.telegram_chat_id = 12345
        await digest_job()
