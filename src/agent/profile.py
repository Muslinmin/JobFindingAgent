import json
from datetime import datetime
from pathlib import Path

from app.config import settings

_backup_counter = 0


def _profile_path() -> Path:
    return Path(settings.profile_path)


def _backup_dir() -> Path:
    return _profile_path().parent / "profiles" / "backups"


def read_profile() -> dict:
    path = _profile_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_profile(updated: dict) -> None:
    current = read_profile()
    _strip_metadata(current)
    _strip_metadata(updated)

    if current == updated:
        return

    _backup(current)
    updated["updated_at"] = datetime.utcnow().isoformat()
    _profile_path().write_text(json.dumps(updated, indent=2))


def _backup(profile: dict) -> None:
    global _backup_counter
    if not profile:
        return
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S-%f")
    seq = _backup_counter % 1000
    _backup_counter += 1
    dest = backup_dir / f"profile_{timestamp}_{seq}.json"
    dest.write_text(json.dumps(profile, indent=2))


def _strip_metadata(profile: dict) -> None:
    profile.pop("updated_at", None)
