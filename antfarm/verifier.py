from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict

from . import db
from .config import load_config


def run_verifier(repo: Path, objective_id: int | None, command: str) -> Dict[str, Any]:
    cfg = load_config(repo)
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=repo,
            shell=True,
            text=True,
            capture_output=True,
            timeout=cfg.verifier_timeout_seconds,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        stderr += f"\n[antfarm] verifier timed out after {cfg.verifier_timeout_seconds} seconds"
    duration = time.monotonic() - started
    with db.connect(repo) as conn:
        result_id = db.insert_verifier_result(conn, objective_id, command, str(repo), exit_code, stdout, stderr, duration, timed_out)
    return {
        "id": result_id,
        "objective_id": objective_id,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": duration,
        "timed_out": timed_out,
    }


def run_verifier_in_dir(repo: Path, objective_id: int | None, command: str, cwd: Path) -> Dict[str, Any]:
    cfg = load_config(repo)
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=cfg.verifier_timeout_seconds,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        stderr += f"\n[antfarm] verifier timed out after {cfg.verifier_timeout_seconds} seconds"
    duration = time.monotonic() - started
    with db.connect(repo) as conn:
        result_id = db.insert_verifier_result(conn, objective_id, command, str(cwd), exit_code, stdout, stderr, duration, timed_out)
    return {
        "id": result_id,
        "objective_id": objective_id,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": duration,
        "timed_out": timed_out,
        "cwd": str(cwd),
    }
