from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from antfarm import db
from antfarm.cli import app
from antfarm.config import load_config


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)


def test_init_and_doctor_work_from_subdirectory(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    runner = CliRunner()

    init_result = runner.invoke(app, ["init", "--repo", str(repo)])
    assert init_result.exit_code == 0, init_result.output

    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    doctor_result = runner.invoke(app, ["doctor", "--no-llm"])
    assert doctor_result.exit_code == 0, doctor_result.output
    assert str(repo) in doctor_result.output
    assert "database schema" in doctor_result.output


def test_context_command_previews_budget(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "main.py").write_text("print('ants')\n", encoding="utf-8")
    runner = CliRunner()
    init_result = runner.invoke(app, ["init", "--repo", str(repo)])
    assert init_result.exit_code == 0, init_result.output
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["context", "--files", "*.py"])
    global_result = runner.invoke(app, ["-C", str(repo), "context", "--files", "*.py"])

    assert result.exit_code == 0, result.output
    assert global_result.exit_code == 0, global_result.output
    assert "Context budget preview" in result.output
    assert "main.py" in result.output
    assert "~" in result.output and "tokens" in result.output


def test_run_refuses_unscoped_goal_before_llm_calls(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    runner = CliRunner()
    init_result = runner.invoke(app, ["init", "--repo", str(repo)])
    assert init_result.exit_code == 0, init_result.output
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["run", "Fix the CLI"])

    assert result.exit_code == 1
    assert "No --files globs provided" in result.output
    with db.connect(repo) as conn:
        assert conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] == 0


def test_config_set_validates_and_persists_values(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    runner = CliRunner()
    init_result = runner.invoke(app, ["init", "--repo", str(repo)])
    assert init_result.exit_code == 0, init_result.output
    monkeypatch.chdir(repo)

    set_result = runner.invoke(app, ["config", "set", "max_context_bytes_total", "60000"])
    role_result = runner.invoke(app, ["config", "set", "model_by_role.debug", "tiny-debug-model"])
    show_result = runner.invoke(app, ["config", "show"])

    assert set_result.exit_code == 0, set_result.output
    assert role_result.exit_code == 0, role_result.output
    assert show_result.exit_code == 0, show_result.output
    cfg = load_config(repo)
    assert cfg.max_context_bytes_total == 60000
    assert cfg.model_by_role["debug"] == "tiny-debug-model"


def test_memory_commands_list_and_accept_candidates(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    runner = CliRunner()
    init_result = runner.invoke(app, ["init", "--repo", str(repo)])
    assert init_result.exit_code == 0, init_result.output
    monkeypatch.chdir(repo)
    with db.connect(repo) as conn:
        objective_id = db.create_objective(conn, "remember constraints")
        task_id = db.create_task(conn, objective_id, "debug", "find facts", [])
        db.save_report(
            conn,
            task_id,
            objective_id,
            "debug",
            {
                "role": "debug",
                "summary": "found fact",
                "findings": [],
                "commands_suggested": [],
                "patch_diff": None,
                "memory_candidates": ["The project prefers local-only inference."],
                "risks": [],
                "confidence": 0.9,
            },
            raw_response="{}",
        )
        memory_id = db.list_memory_candidates(conn)[0]["id"]

    list_result = runner.invoke(app, ["memory", "list"])
    accept_result = runner.invoke(app, ["memory", "accept", str(memory_id)])

    assert list_result.exit_code == 0, list_result.output
    assert "local-only inference" in list_result.output
    assert accept_result.exit_code == 0, accept_result.output
    with db.connect(repo) as conn:
        assert db.list_memory_candidates(conn, status="accepted")[0]["id"] == memory_id
