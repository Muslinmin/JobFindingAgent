"""
Live integration test — makes a real call to the Algolia index behind
jobs.careers.gov.sg.

Skipped automatically when CAREERS_GOV_APP_ID / CAREERS_GOV_API_KEY are not
set. Run explicitly with:

    pytest src/test/integration/test_careers_gov_live.py -v -m live
"""

import pytest

from app.config import settings
from app.models.job import JobCreate
from scraper.careers_gov_adapter import CareersGovSource

pytestmark = pytest.mark.live

skip_if_no_key = pytest.mark.skipif(
    not settings.careers_gov_app_id or not settings.careers_gov_api_key,
    reason="CAREERS_GOV_APP_ID/CAREERS_GOV_API_KEY not set in .env — skipping live test",
)


@skip_if_no_key
async def test_careers_gov_live_smoke():
    source = CareersGovSource()
    result = await source.fetch("software engineer")

    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(job, JobCreate) for job in result)
    assert all("posted_at" not in (job.metadata or {}) for job in result)
