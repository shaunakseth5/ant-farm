from __future__ import annotations

import subprocess
from pathlib import Path

from antfarm import db
from antfarm.config import AntFarmConfig, save_config
from antfarm.sandbox import apply_task_patch


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)


def test_patch_candidate_applies_only_to_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Ant Farm Test")
    _git(repo, "config", "user.email", "antfarm@example.invalid")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-m", "initial")

    save_config(repo, AntFarmConfig())
    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+hello ants
"""
    with db.connect(repo) as conn:
        db.init_db(conn)
        objective_id = db.create_objective(conn, "greet ants")
        task_id = db.create_task(conn, objective_id, "patch", "change greeting", ["hello.txt"])
        db.save_report(
            conn,
            task_id,
            objective_id,
            "patch",
            {
                "role": "patch",
                "summary": "change greeting",
                "findings": [],
                "commands_suggested": [],
                "patch_diff": patch,
                "memory_candidates": [],
                "risks": [],
                "confidence": 1.0,
            },
            raw_response="{}",
        )

    result = apply_task_patch(repo, task_id, "antfarm-test-branch")
    worktree = Path(result["worktree"])

    assert (repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert (worktree / "hello.txt").read_text(encoding="utf-8") == "hello ants\n"
    with db.connect(repo) as conn:
        stored_patch = db.patch_for_task(conn, task_id)
    assert stored_patch is not None
    assert stored_patch["status"] == "applied_to_worktree"
    assert stored_patch["worktree_path"] == str(worktree)
