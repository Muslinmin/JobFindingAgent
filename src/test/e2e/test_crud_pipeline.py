JOB_PAYLOAD = {
    "company": "Acme",
    "role": "Engineer",
    "url": "https://acme.com/jobs/1",
}


async def test_full_crud_lifecycle(async_client):
    # create
    r = await async_client.post("/jobs", json=JOB_PAYLOAD)
    assert r.status_code == 201
    assert r.json()["status"] == "found"
    job_id = r.json()["id"]

    # read
    r = await async_client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "found"

    # patch found → applied
    r = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "applied"})
    assert r.status_code == 200
    assert r.json()["status"] == "applied"

    # patch applied → screening
    r = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "screening"})
    assert r.status_code == 200
    assert r.json()["status"] == "screening"

    # delete
    r = await async_client.delete(f"/jobs/{job_id}")
    assert r.status_code == 204

    # confirm gone
    r = await async_client.get(f"/jobs/{job_id}")
    assert r.status_code == 404


async def test_rejection_from_any_stage(async_client):
    r = await async_client.post("/jobs", json=JOB_PAYLOAD)
    job_id = r.json()["id"]

    await async_client.patch(f"/jobs/{job_id}/status", json={"status": "applied"})
    await async_client.patch(f"/jobs/{job_id}/status", json={"status": "screening"})

    # reject from screening
    r = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "rejected"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # rejected is terminal — any further patch must fail
    r = await async_client.patch(f"/jobs/{job_id}/status", json={"status": "interview"})
    assert r.status_code == 422
