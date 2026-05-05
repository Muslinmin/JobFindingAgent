import pytest


BASE_URL = "/jobs"

JOB_PAYLOAD = {
    "company": "Acme",
    "role": "Engineer",
    "url": "https://acme.com/jobs/1",
}


def _job(company: str, role: str, n: int) -> dict:
    return {"company": company, "role": role, "url": f"https://example.com/jobs/{n}"}


# ── POST /jobs ────────────────────────────────────────────────────────────────


async def test_create_job_returns_201(async_client):
    response = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    assert response.status_code == 201
    assert response.json()["created"] is True


async def test_create_job_default_status_is_found(async_client):
    response = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    assert response.json()["status"] == "found"


# ── GET /jobs ─────────────────────────────────────────────────────────────────


async def test_list_jobs_returns_all(async_client):
    for i in range(3):
        await async_client.post(BASE_URL, json=_job("Acme", "Engineer", i))
    response = await async_client.get(BASE_URL)
    assert response.status_code == 200
    assert len(response.json()) == 3


async def test_status_filter_returns_correct_subset(async_client):
    r1 = await async_client.post(BASE_URL, json=_job("Acme", "Engineer", 1))
    await async_client.post(BASE_URL, json=_job("Globex", "Developer", 2))
    await async_client.post(BASE_URL, json=_job("Initech", "Analyst", 3))

    job_id = r1.json()["id"]
    await async_client.patch(f"{BASE_URL}/{job_id}/status", json={"status": "applied"})

    found = await async_client.get(BASE_URL, params={"status": "found"})
    assert len(found.json()) == 2

    applied = await async_client.get(BASE_URL, params={"status": "applied"})
    assert len(applied.json()) == 1


# ── GET /jobs/{id} ────────────────────────────────────────────────────────────


async def test_get_job_by_id_returns_correct_record(async_client):
    created = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    job_id = created.json()["id"]

    response = await async_client.get(f"{BASE_URL}/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["company"] == "Acme"
    assert data["role"] == "Engineer"


async def test_get_nonexistent_job_returns_404(async_client):
    response = await async_client.get(f"{BASE_URL}/9999")
    assert response.status_code == 404


# ── PATCH /jobs/{id}/status ───────────────────────────────────────────────────


async def test_valid_transition_returns_200(async_client):
    created = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    job_id = created.json()["id"]

    response = await async_client.patch(
        f"{BASE_URL}/{job_id}/status", json={"status": "applied"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"


async def test_invalid_transition_returns_422(async_client):
    created = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    job_id = created.json()["id"]

    response = await async_client.patch(
        f"{BASE_URL}/{job_id}/status", json={"status": "offer"}
    )
    assert response.status_code == 422


async def test_patch_nonexistent_job_returns_404(async_client):
    response = await async_client.patch(
        f"{BASE_URL}/9999/status", json={"status": "applied"}
    )
    assert response.status_code == 404


# ── DELETE /jobs/{id} ─────────────────────────────────────────────────────────


async def test_delete_job_removes_record(async_client):
    created = await async_client.post(BASE_URL, json=JOB_PAYLOAD)
    job_id = created.json()["id"]

    delete_response = await async_client.delete(f"{BASE_URL}/{job_id}")
    assert delete_response.status_code == 204

    get_response = await async_client.get(f"{BASE_URL}/{job_id}")
    assert get_response.status_code == 404


async def test_delete_nonexistent_job_returns_404(async_client):
    response = await async_client.delete(f"{BASE_URL}/9999")
    assert response.status_code == 404
