from __future__ import annotations

from pathlib import Path

import pytest

from antfarm.config import AntFarmConfig, is_inside_antfarm_worktree, resolve_repo, save_config
from antfarm.repo import build_context, build_context_plan, expand_file_globs


def test_resolve_repo_finds_initialized_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    save_config(repo, AntFarmConfig())

    monkeypatch.chdir(subdir)

    assert resolve_repo() == repo.resolve()


def test_resolve_repo_explicit_and_env_override_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
    explicit = tmp_path / "explicit"
    env_repo = tmp_path / "env"
    workdir = parent / "nested"
    workdir.mkdir(parents=True)
    explicit.mkdir()
    env_repo.mkdir()
    save_config(parent, AntFarmConfig())

    monkeypatch.chdir(workdir)
    monkeypatch.setenv("ANTFARM_REPO", str(env_repo))

    assert resolve_repo() == env_repo.resolve()
    assert resolve_repo(explicit) == explicit.resolve()


def test_save_config_hides_antfarm_state_contents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    save_config(repo, AntFarmConfig())

    assert (repo / ".antfarm" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (repo / ".antfarm" / "config.json").is_file()


def test_is_inside_antfarm_worktree(tmp_path: Path) -> None:
    assert is_inside_antfarm_worktree(tmp_path / ".antfarm" / "worktrees" / "branch" / "src")
    assert not is_inside_antfarm_worktree(tmp_path / ".antfarm-not" / "worktrees" / "branch")


def test_expand_file_globs_is_repo_relative_and_excludes_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".antfarm").mkdir()
    (repo / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / ".antfarm" / "secret.py").write_text("hidden\n", encoding="utf-8")

    matches = expand_file_globs(repo, ["**/*.py"])

    assert [path.relative_to(repo).as_posix() for path in matches] == ["src/main.py"]
    with pytest.raises(ValueError):
        expand_file_globs(repo, ["../outside.py"])
    with pytest.raises(ValueError):
        expand_file_globs(repo, [str((repo / "src" / "main.py").resolve())])


def test_build_context_obeys_total_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("a" * 20, encoding="utf-8")
    (repo / "b.txt").write_text("b" * 20, encoding="utf-8")
    cfg = AntFarmConfig(max_context_bytes_per_file=20, max_context_bytes_total=10)

    context = build_context(repo, ["*.txt"], cfg)

    assert "--- FILE: a.txt ---" in context
    assert "[context truncated: total byte limit reached]" in context


def test_context_plan_skips_binary_and_caps_file_count(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02")
    (repo / "c.txt").write_text("charlie\n", encoding="utf-8")
    cfg = AntFarmConfig(max_context_files=1)

    plan = build_context_plan(repo, ["*"], cfg)

    by_path = {entry.path: entry for entry in plan.entries}
    assert by_path["a.txt"].included
    assert by_path["b.bin"].skipped_reason == "binary or non-text file"
    assert by_path["c.txt"].skipped_reason == "max_context_files=1 reached"
    assert plan.skipped_files == 2
