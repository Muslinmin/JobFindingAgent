"""Artifact storage — naming, backup, registration (agent_v2.md §7).

The tailoring layer produces a file and stops there: `tailor()` performs no
database read or write of any kind, by design, which is what lets the whole
layer be tested with a mock LLM and no backend (tailoring_build.md, "Caller
contract"). Something has to close that gap for every caller that wants a
*durable* artifact rather than a loose PDF, and this is it.

**Two properties, deliberately kept in different places.** The artifact
table is append-only — it is the audit trail, and a row is never updated or
deleted, so the full history of what was generated for a job survives. The
"one live CV per job" property lives in the *directory* instead: a job's
artifact directory holds exactly one current file per kind, and the
previous one is renamed to a timestamped `.bak` beside it. A uniqueness
rule on the table would have collapsed those two into one and lost the
history.

**Naming.** `{company}_{role}_cv.pdf`, slugged. The name matters because it
is what the user sees when the file arrives in Telegram — `cv.pdf`, which
is what the renderer emits, tells them nothing about which of five pending
applications they are looking at. agent_v2.md §7 also called for a short
`job_id` hash as a collision suffix; that is unnecessary here because each
job gets its own directory, so two jobs with an identical company and role
cannot collide on disk.

**No FSM move happens here.** Producing and registering a file is not the
same event as the user accepting it — the scheduler advances the status in
its daily batch, and the agent only after an explicit confirmation
(architecture_v2.md, "The SCORED→PENDING_APPROVAL move is the tailor
caller's, not the tool's").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.models.enums import ArtifactKind
from app.models.job import ArtifactCreate, Job
from app.services.service import JobService

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def artifact_dir(output_dir: str | Path, job_id: int) -> Path:
    """One directory per job. Matches what `scheduler/jobs/tailor.py`
    already passes to `tailor()`, so the scheduler's output and the agent's
    land in the same place rather than in two parallel trees."""
    return Path(output_dir) / str(job_id)


def _slug(text: str) -> str:
    return _SLUG_STRIP.sub("-", text.lower()).strip("-")


def artifact_filename(company: str, role: str, kind: ArtifactKind, suffix: str) -> str:
    """`{company}_{role}_{kind}{suffix}`, slugged. Falls back to the bare
    kind if company and role slug to nothing at all (a company named only
    in a non-Latin script would), because an empty stem is not a filename.
    """
    stem = "_".join(part for part in (_slug(company), _slug(role)) if part)
    return f"{stem}_{kind.value}{suffix}" if stem else f"{kind.value}{suffix}"


def _backup_name(path: Path) -> Path:
    """`name.20260722T101500Z.bak` — the original name stays intact and
    readable, and the timestamp orders the history without needing the
    table to reproduce it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.{stamp}.bak")


@dataclass(frozen=True)
class StoredArtifact:
    """What the caller needs to narrate the result and to ship the file:
    the row id for the audit trail, the final path, and whether this
    displaced a previous version — which is the difference between "here's
    your CV" and "here's the new one, I kept the old"."""

    artifact_id: int
    kind: ArtifactKind
    path: Path
    filename: str
    replaced: bool


async def store_artifact(
    job: Job,
    kind: ArtifactKind,
    produced: str | Path,
    service: JobService,
    output_dir: str | Path,
) -> StoredArtifact:
    """Move `produced` to its canonical name in the job's artifact
    directory, backing up whatever was living under that name, then
    register the new path.

    The rename happens before the insert on purpose: a crash between the
    two leaves a correctly named file with no row, which the next run
    simply overwrites. The other order would leave a row pointing at a path
    that does not exist, and the audit trail is only worth having if every
    row in it resolves.
    """
    produced = Path(produced)
    destination = artifact_dir(output_dir, job.id) / artifact_filename(
        job.company, job.role, kind, produced.suffix
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    # `produced == destination` when the renderer happened to emit the
    # canonical name already; backing up in that case would move the very
    # file we are about to register out from under ourselves.
    replaced = destination.exists() and destination != produced
    if replaced:
        destination.rename(_backup_name(destination))
    if destination != produced:
        produced.replace(destination)

    artifact = await service.register_artifact(
        job.id, ArtifactCreate(kind=kind, path=str(destination))
    )
    return StoredArtifact(
        artifact_id=artifact.id,
        kind=kind,
        path=destination,
        filename=destination.name,
        replaced=replaced,
    )
