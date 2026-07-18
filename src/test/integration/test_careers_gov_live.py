"""
Live integration test — makes a real call to OGP's published Careers@Gov
open-data mirror (raw.githubusercontent.com). No credentials needed since
the file is public and MIT-licensed; run explicitly with:

    pytest src/test/integration/test_careers_gov_live.py -v -m live
"""

import pytest

from app.models.job import JobCreate
from scraper.careers_gov_adapter import CareersGovSource

pytestmark = pytest.mark.live


async def test_careers_gov_live_smoke():
    source = CareersGovSource()
    result = await source.fetch("robotics engineering")

    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(job, JobCreate) for job in result)
    assert all(job.description for job in result)
    assert all("posted_at" not in (job.metadata or {}) for job in result)


async def test_careers_gov_live_blank_query_returns_full_corpus():
    source = CareersGovSource()
    result = await source.fetch("")

    assert len(result) > 1000
