import pytest
from pydantic import ValidationError
from app.models.job import JobCreate, JobUpdate


def test_job_create_requires_company_role_url():
    job = JobCreate(company="Acme", role="Engineer", url="https://acme.com/jobs/1")
    assert job.company == "Acme"
    assert job.role == "Engineer"


def test_job_create_rejects_invalid_url():
    with pytest.raises(ValidationError):
        JobCreate(company="Acme", role="Engineer", url="not-a-url")


def test_job_create_source_defaults_to_manual():
    job = JobCreate(company="Acme", role="Engineer", url="https://acme.com/jobs/1")
    assert job.source == "manual"


def test_job_create_notes_is_optional():
    job = JobCreate(company="Acme", role="Engineer", url="https://acme.com/jobs/1", notes=None)
    assert job.notes is None


def test_job_update_rejects_invalid_status():
    with pytest.raises(ValidationError):
        JobUpdate(status="banana")
