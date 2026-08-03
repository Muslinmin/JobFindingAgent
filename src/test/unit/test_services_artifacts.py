"""Artifact storage — naming, backup, registration (agent_v2.md §7).

What is under test is the gap `tailor()` deliberately leaves: it produces a
file and writes nothing, so something has to give that file a durable name,
preserve the one it displaces, and record it. Two properties in different
places are the point — the table is append-only history, the *directory*
holds one live file per kind.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.enums import ArtifactKind
from app.models.job import Artifact, Job
from app.services.artifacts import artifact_dir, artifact_filename, store_artifact

T = "2026-07-22T00:00:00+00:00"


def _job(id=1, company="GovTech", role="Backend Engineer"):
    return Job(
        id=id, fingerprint=f"fp{id}", company=company, role=role, description="a jd",
        url="https://example.com", posted_at=None, metadata=None, status="scored", score=7000,
        status_changed_at=T, follow_up_count=0, last_follow_up_at=None, follow_up_nudge_at=None,
        seen_count=1, last_seen_at=T, created_at=T, updated_at=T,
    )


@pytest.fixture
def service():
    s = MagicMock()
    s.register_artifact = AsyncMock(
        return_value=Artifact(id=7, job_id=1, kind=ArtifactKind.CV_PDF, path="x", created_at=T)
    )
    return s


@pytest.fixture
def produced(tmp_path):
    """What the renderer leaves behind: `cv.pdf`, in the job's own dir."""
    path = artifact_dir(tmp_path / "artifacts", 1) / "cv.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"the new one")
    return path


# ── naming ────────────────────────────────────────────────────────────────────

def test_filename_is_readable_because_the_user_sees_it_in_telegram():
    """`cv.pdf` tells someone with five pending applications nothing."""
    assert (
        artifact_filename("GovTech", "Backend Engineer", ArtifactKind.CV_PDF, ".pdf")
        == "govtech_backend-engineer_cv_pdf.pdf"
    )


def test_filename_slugs_punctuation_that_would_break_a_path():
    assert (
        artifact_filename("A*STAR / I²R", "Sr. Engineer (AI)", ArtifactKind.CV_PDF, ".pdf")
        == "a-star-i-r_sr-engineer-ai_cv_pdf.pdf"
    )


def test_filename_falls_back_to_the_kind_when_nothing_slugs():
    """An empty stem is not a filename — a company named only in a
    non-Latin script would otherwise produce one."""
    assert artifact_filename("株式会社", "技術者", ArtifactKind.CV_PDF, ".pdf") == "cv_pdf.pdf"


def test_each_job_gets_its_own_directory():
    assert artifact_dir("artifacts", 42) == Path("artifacts/42")


# ── storage ───────────────────────────────────────────────────────────────────

async def test_stores_under_the_canonical_name_and_registers_that_path(tmp_path, service, produced):
    stored = await store_artifact(
        _job(), ArtifactKind.CV_PDF, produced, service, tmp_path / "artifacts"
    )

    assert stored.filename == "govtech_backend-engineer_cv_pdf.pdf"
    assert stored.path.read_bytes() == b"the new one"
    assert stored.artifact_id == 7
    assert not produced.exists()  # moved, not copied

    registered = service.register_artifact.await_args.args[1]
    assert registered.path == str(stored.path)
    assert Path(registered.path).exists()


async def test_first_artifact_for_a_job_replaced_nothing(tmp_path, service, produced):
    stored = await store_artifact(
        _job(), ArtifactKind.CV_PDF, produced, service, tmp_path / "artifacts"
    )
    assert stored.replaced is False


async def test_the_previous_version_is_backed_up_not_overwritten(tmp_path, service, produced):
    live = produced.parent / "govtech_backend-engineer_cv_pdf.pdf"
    live.write_bytes(b"the old one")

    stored = await store_artifact(
        _job(), ArtifactKind.CV_PDF, produced, service, tmp_path / "artifacts"
    )

    assert stored.replaced is True
    assert stored.path.read_bytes() == b"the new one"

    backups = list(produced.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"the old one"
    assert backups[0].name.startswith("govtech_backend-engineer_cv_pdf.pdf.")


async def test_registration_is_append_only_so_history_survives(tmp_path, service, produced):
    """The single-live-file property lives in the directory; the table keeps
    every version that was ever generated."""
    await store_artifact(_job(), ArtifactKind.CV_PDF, produced, service, tmp_path / "artifacts")

    produced.write_bytes(b"a third one")
    await store_artifact(_job(), ArtifactKind.CV_PDF, produced, service, tmp_path / "artifacts")

    assert service.register_artifact.await_count == 2


async def test_two_jobs_at_the_same_company_and_role_do_not_collide(tmp_path, service):
    """Which is why no job_id hash suffix is needed: the directories differ
    even when the slugged names are identical."""
    paths = []
    for job_id in (1, 2):
        rendered = artifact_dir(tmp_path / "artifacts", job_id) / "cv.pdf"
        rendered.parent.mkdir(parents=True)
        rendered.write_bytes(f"cv for {job_id}".encode())
        stored = await store_artifact(
            _job(id=job_id), ArtifactKind.CV_PDF, rendered, service, tmp_path / "artifacts"
        )
        paths.append(stored.path)

    assert paths[0] != paths[1]
    assert paths[0].name == paths[1].name
    assert paths[0].read_bytes() == b"cv for 1"
    assert paths[1].read_bytes() == b"cv for 2"


async def test_a_renderer_that_already_used_the_canonical_name_is_not_lost(tmp_path, service):
    """Backing up in that case would move the very file being registered
    out from under us."""
    directory = artifact_dir(tmp_path / "artifacts", 1)
    directory.mkdir(parents=True)
    canonical = directory / "govtech_backend-engineer_cv_pdf.pdf"
    canonical.write_bytes(b"already canonical")

    stored = await store_artifact(
        _job(), ArtifactKind.CV_PDF, canonical, service, tmp_path / "artifacts"
    )

    assert stored.replaced is False
    assert stored.path.read_bytes() == b"already canonical"
