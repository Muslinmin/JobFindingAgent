import json
import pytest
from agent.profile import read_profile, write_profile


def test_read_returns_empty_dict_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert read_profile() == {}


def test_write_creates_profile_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    assert (tmp_path / "profile.json").exists()


def test_write_stamps_updated_at(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    profile = read_profile()
    assert "updated_at" in profile


def test_write_creates_backup_on_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 1


def test_write_skips_backup_when_no_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backups = list(backup_dir.iterdir()) if backup_dir.exists() else []
    assert len(backups) == 0


def test_multiple_changes_produce_multiple_backups(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    write_profile({"skills": ["Python", "SQL", "FastAPI"]})
    backups = list((tmp_path / "profiles" / "backups").iterdir())
    assert len(backups) == 2


def test_first_write_produces_no_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backups = list(backup_dir.iterdir()) if backup_dir.exists() else []
    assert len(backups) == 0


def test_read_returns_written_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"target_roles": ["Data Engineer"], "skills": ["Python"]})
    profile = read_profile()
    assert profile["target_roles"] == ["Data Engineer"]
    assert profile["skills"] == ["Python"]


def test_write_merges_fields_correctly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    current = read_profile()
    current["experience_years"] = 3
    write_profile(current)
    profile = read_profile()
    assert profile["skills"] == ["Python"]
    assert profile["experience_years"] == 3


def test_updated_at_does_not_cause_false_positive_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backups = list(backup_dir.iterdir()) if backup_dir.exists() else []
    assert len(backups) == 0


def test_backup_file_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backup_file = list(backup_dir.iterdir())[0]
    data = json.loads(backup_file.read_text())
    assert isinstance(data, dict)


def test_backup_contains_previous_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_profile({"skills": ["Python"]})
    write_profile({"skills": ["Python", "SQL"]})
    backup_dir = tmp_path / "profiles" / "backups"
    backup_file = list(backup_dir.iterdir())[0]
    data = json.loads(backup_file.read_text())
    assert data["skills"] == ["Python"]
