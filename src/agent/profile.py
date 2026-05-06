import json
from datetime import datetime
from pathlib import Path

PROFILE_PATH = Path("profile.json")
BACKUP_DIR   = Path("profiles/backups")

_backup_counter = 0


def read_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text())


def write_profile(updated: dict) -> None:
    current = read_profile()
    _strip_metadata(current)
    _strip_metadata(updated)

    if current == updated:
        return

    _backup(current)
    updated["updated_at"] = datetime.utcnow().isoformat()
    PROFILE_PATH.write_text(json.dumps(updated, indent=2))


def _backup(profile: dict) -> None:
    global _backup_counter
    if not profile:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S-%f")
    seq = _backup_counter % 1000
    _backup_counter += 1
    dest = BACKUP_DIR / f"profile_{timestamp}_{seq}.json"
    dest.write_text(json.dumps(profile, indent=2))


def _strip_metadata(profile: dict) -> None:
    profile.pop("updated_at", None)
