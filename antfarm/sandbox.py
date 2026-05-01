from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict

from . import db
from .config import state_dir


BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _run(args: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, input=input_text, text=True, capture_output=True, timeout=120)


def _safe_branch(branch: str) -> None:
    if not branch or branch.startswith("-") or ".." in branch or not BRANCH_RE.match(branch):
        raise ValueError("Branch name may only contain letters, numbers, '.', '_', '-', and '/', and must not contain '..'")


def apply_task_patch(repo: Path, task_id: int, branch: str) -> Dict[str, Any]:
    """Apply a task patch candidate only inside a repo-local git worktree."""
    _safe_branch(branch)
    with db.connect(repo) as conn:
        task = db.get_task(conn, task_id)
        patch = db.patch_for_task(conn, task_id)
        if patch is None:
            raise RuntimeError(f"Task {task_id} has no patch candidate")
        diff_text = patch["diff_text"]

    git_check = _run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"])
    if git_check.returncode != 0:
        raise RuntimeError(f"Repository is not a git worktree: {git_check.stderr.strip()}")

    worktrees = state_dir(repo) / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    safe_name = branch.replace("/", "__")
    worktree = (worktrees / safe_name).resolve()
    if worktree.exists():
        raise RuntimeError(f"Worktree already exists: {worktree}")
    if state_dir(repo).resolve() not in worktree.parents:
        raise RuntimeError("Refusing to create worktree outside .antfarm/worktrees")

    add = _run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), "HEAD"])
    if add.returncode != 0:
        raise RuntimeError(f"git worktree add failed:\n{add.stderr}")

    check = _run(["git", "-C", str(worktree), "apply", "--check"], input_text=diff_text)
    if check.returncode != 0:
        raise RuntimeError(f"Patch did not apply cleanly in isolated worktree {worktree}:\n{check.stderr}")

    applied = _run(["git", "-C", str(worktree), "apply"], input_text=diff_text)
    if applied.returncode != 0:
        raise RuntimeError(f"git apply failed in isolated worktree {worktree}:\n{applied.stderr}")

    with db.connect(repo) as conn:
        db.mark_patch_applied(conn, patch["id"], str(worktree))
        db.add_event(
            conn,
            "patch.applied_to_worktree",
            f"Task {task_id} patch applied to {worktree}",
            objective_id=task["objective_id"],
            task_id=task_id,
            data={"branch": branch, "worktree": str(worktree), "patch_id": patch["id"]},
        )

    return {"task_id": task_id, "patch_id": patch["id"], "branch": branch, "worktree": str(worktree)}
