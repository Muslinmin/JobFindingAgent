from unittest.mock import patch

BASE_URL = "/jobs"

JOB = {
    "company": "Acme",
    "role": "Data Engineer",
    "url": "https://acme.com/jobs/1",
    "description": "We use Python, FastAPI, and SQL daily.",
}

NO_DESCRIPTION_JOB = {
    "company": "Acme",
    "role": "Data Engineer",
    "url": "https://acme.com/jobs/2",
}


# ── Scoring ───────────────────────────────────────────────────────────────────


async def test_score_is_zero_when_profile_has_no_skills(async_client):
    with patch("app.routes.jobs.read_profile", return_value={}):
        response = await async_client.post(BASE_URL, json=JOB)
    assert response.status_code == 201
    assert response.json()["score"] == 0.0


async def test_score_is_zero_when_description_is_absent(async_client):
    with patch("app.routes.jobs.read_profile", return_value={"skills": ["python", "sql"]}):
        response = await async_client.post(BASE_URL, json=NO_DESCRIPTION_JOB)
    assert response.status_code == 201
    assert response.json()["score"] == 0.0


async def test_score_reflects_matching_profile_skills(async_client):
    with patch("app.routes.jobs.read_profile", return_value={"skills": ["python", "sql", "docker"]}):
        response = await async_client.post(BASE_URL, json=JOB)
    # description contains "python" and "sql" but not "docker" → 2/3
    assert response.status_code == 201
    assert abs(response.json()["score"] - 2 / 3) < 1e-9


async def test_score_is_one_when_all_skills_match(async_client):
    with patch("app.routes.jobs.read_profile", return_value={"skills": ["python", "fastapi", "sql"]}):
        response = await async_client.post(BASE_URL, json=JOB)
    assert response.status_code == 201
    assert response.json()["score"] == 1.0


# ── Deduplication ─────────────────────────────────────────────────────────────


async def test_duplicate_insert_returns_created_false(async_client):
    with patch("app.routes.jobs.read_profile", return_value={}):
        first  = await async_client.post(BASE_URL, json=JOB)
        second = await async_client.post(BASE_URL, json=JOB)
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["id"] == second.json()["id"]


async def test_case_and_whitespace_do_not_create_duplicate(async_client):
    job_a = {"company": "Google", "role": "Engineer", "url": "https://google.com/1"}
    job_b = {"company": "  google  ", "role": "  engineer  ", "url": "  https://google.com/1  "}
    with patch("app.routes.jobs.read_profile", return_value={}):
        first  = await async_client.post(BASE_URL, json=job_a)
        second = await async_client.post(BASE_URL, json=job_b)
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["id"] == second.json()["id"]
