"""WP-S6 — query regen job tests.

`load_profile` is patched at the module level rather than constructing a
real `Profile`, since `_assemble_query_regen_prompt` only reads `.skills`
and `.target_tracks`. All file I/O goes through `tmp_path`.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from scheduler.jobs.query_regen import _assemble_query_regen_prompt, _write_with_backup, run_query_regen


def _fake_profile():
    profile = MagicMock()
    profile.skills = [MagicMock(label="Python"), MagicMock(label="FastAPI")]
    profile.target_tracks = ["backend", "robotics"]
    return profile


def _llm(queries: list[str]) -> AsyncMock:
    llm = AsyncMock()
    llm.complete.return_value = json.dumps(queries)
    return llm


class _Settings:
    pass


# ── _assemble_query_regen_prompt ──────────────────────────────────────────────

def test_assemble_prompt_includes_skills_and_tracks():
    with patch("scheduler.jobs.query_regen.load_profile", return_value=_fake_profile()):
        prompt = _assemble_query_regen_prompt("profile.json")

    assert "Python" in prompt
    assert "FastAPI" in prompt
    assert "backend" in prompt
    assert "robotics" in prompt


def test_assemble_prompt_propagates_missing_profile():
    with patch("scheduler.jobs.query_regen.load_profile", side_effect=FileNotFoundError()):
        try:
            _assemble_query_regen_prompt("nope.json")
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass


# ── _write_with_backup ────────────────────────────────────────────────────────

def test_write_with_backup_writes_new_file(tmp_path):
    path = tmp_path / "search_queries.json"
    assert _write_with_backup(path, '["a", "b"]') is True
    assert path.read_text() == '["a", "b"]'


def test_write_with_backup_backs_up_and_overwrites_on_change(tmp_path):
    path = tmp_path / "search_queries.json"
    path.write_text('["old"]')

    written = _write_with_backup(path, '["new"]')

    assert written is True
    assert path.read_text() == '["new"]'
    backup = tmp_path / "search_queries.json.bak"
    assert backup.read_text() == '["old"]'


def test_write_with_backup_no_write_when_unchanged(tmp_path):
    path = tmp_path / "search_queries.json"
    path.write_text('["same"]')

    written = _write_with_backup(path, '["same"]')

    assert written is False
    assert not (tmp_path / "search_queries.json.bak").exists()


# ── run_query_regen ───────────────────────────────────────────────────────────

async def test_run_query_regen_writes_llm_output(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    llm = _llm(["backend engineer", "robotics intern"])

    with patch("scheduler.jobs.query_regen.load_profile", return_value=_fake_profile()):
        await run_query_regen(llm, _Settings(), "profile.json", queries_path)

    written = json.loads(queries_path.read_text())
    assert written == ["backend engineer", "robotics intern"]


async def test_run_query_regen_backs_up_on_change(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    queries_path.write_text(json.dumps(["old query"]))
    llm = _llm(["new query"])

    with patch("scheduler.jobs.query_regen.load_profile", return_value=_fake_profile()):
        await run_query_regen(llm, _Settings(), "profile.json", queries_path)

    assert json.loads(queries_path.read_text()) == ["new query"]
    assert json.loads((tmp_path / "search_queries.json.bak").read_text()) == ["old query"]


async def test_run_query_regen_idempotent_no_write_when_identical(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    queries_path.write_text(json.dumps(["same query"], indent=2))
    llm = _llm(["same query"])

    with patch("scheduler.jobs.query_regen.load_profile", return_value=_fake_profile()):
        await run_query_regen(llm, _Settings(), "profile.json", queries_path)

    assert not (tmp_path / "search_queries.json.bak").exists()


async def test_run_query_regen_missing_profile_logs_warning_and_returns(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    llm = _llm(["should never be written"])

    with patch("scheduler.jobs.query_regen.load_profile", side_effect=FileNotFoundError()), \
         patch("scheduler.jobs.query_regen.logger") as mock_logger:
        await run_query_regen(llm, _Settings(), tmp_path / "missing_profile.json", queries_path)

    llm.complete.assert_not_called()
    assert not queries_path.exists()
    mock_logger.warning.assert_called_once()
    assert "not found" in mock_logger.warning.call_args[0][0]


async def test_run_query_regen_malformed_llm_output_does_not_raise(tmp_path):
    queries_path = tmp_path / "search_queries.json"
    llm = AsyncMock()
    llm.complete.return_value = "not valid json"

    with patch("scheduler.jobs.query_regen.load_profile", return_value=_fake_profile()):
        await run_query_regen(llm, _Settings(), "profile.json", queries_path)  # must not raise

    assert not queries_path.exists()
