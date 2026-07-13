import hashlib

from app.models.job import JobCreate


def fingerprint(job: JobCreate) -> str:
    raw = "|".join([job.company.strip().lower(), job.role.strip().lower()])
    return hashlib.sha256(raw.encode()).hexdigest()
