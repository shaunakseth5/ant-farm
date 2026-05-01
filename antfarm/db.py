from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import db_path, state_dir


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(repo: Path) -> sqlite3.Connection:
    state_dir(repo).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(repo))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS objectives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            target TEXT NOT NULL,
            files_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'queued',
            report_json TEXT,
            raw_response TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(objective_id) REFERENCES objectives(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            json_text TEXT NOT NULL,
            raw_response TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY(objective_id) REFERENCES objectives(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id INTEGER,
            task_id INTEGER,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verifier_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id INTEGER,
            command TEXT NOT NULL,
            cwd TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            stdout TEXT NOT NULL,
            stderr TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            timed_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS patch_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            diff_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            worktree_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY(objective_id) REFERENCES objectives(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY(objective_id) REFERENCES objectives(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()


def add_event(conn: sqlite3.Connection, kind: str, message: str, objective_id: int | None = None, task_id: int | None = None, data: Dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO events(objective_id, task_id, kind, message, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (objective_id, task_id, kind, message, json.dumps(data or {}), utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def create_objective(conn: sqlite3.Connection, title: str) -> int:
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO objectives(title, status, created_at, updated_at) VALUES (?, 'open', ?, ?)",
        (title, now, now),
    )
    oid = int(cur.lastrowid)
    add_event(conn, "objective.created", title, objective_id=oid)
    return oid


def get_objective(conn: sqlite3.Connection, objective_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
    if row is None:
        raise KeyError(f"Objective {objective_id} not found")
    return row


def create_task(conn: sqlite3.Connection, objective_id: int, role: str, target: str, files: Iterable[str]) -> int:
    get_objective(conn, objective_id)
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO tasks(objective_id, role, target, files_json, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
        (objective_id, role, target, json.dumps(list(files)), now, now),
    )
    task_id = int(cur.lastrowid)
    add_event(conn, "task.created", f"{role}: {target}", objective_id=objective_id, task_id=task_id)
    return task_id


def update_task_status(conn: sqlite3.Connection, task_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, utc_now(), task_id),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"Task {task_id} not found")
    return row


def save_report(conn: sqlite3.Connection, task_id: int, objective_id: int, role: str, report: Dict[str, Any], raw_response: str) -> int:
    now = utc_now()
    json_text = json.dumps(report, indent=2, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO reports(task_id, objective_id, role, json_text, raw_response, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, objective_id, role, json_text, raw_response, now),
    )
    conn.execute(
        "UPDATE tasks SET status = 'done', report_json = ?, raw_response = ?, error = NULL, updated_at = ? WHERE id = ?",
        (json_text, raw_response, now, task_id),
    )
    patch = report.get("patch_diff") if report.get("role") == "patch" else None
    if isinstance(patch, str) and patch.strip():
        conn.execute(
            "INSERT INTO patch_candidates(task_id, objective_id, diff_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, objective_id, patch, now, now),
        )
    for mem in report.get("memory_candidates") or []:
        if isinstance(mem, str) and mem.strip():
            conn.execute(
                "INSERT INTO memory_candidates(task_id, objective_id, text, created_at) VALUES (?, ?, ?, ?)",
                (task_id, objective_id, mem.strip(), now),
            )
    add_event(conn, "task.reported", f"{role} completed", objective_id=objective_id, task_id=task_id)
    conn.commit()
    return int(cur.lastrowid)


def list_objectives(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM objectives ORDER BY id DESC LIMIT 20"))


def list_tasks(conn: sqlite3.Connection, objective_id: int | None = None) -> List[sqlite3.Row]:
    if objective_id is None:
        return list(conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 50"))
    return list(conn.execute("SELECT * FROM tasks WHERE objective_id = ? ORDER BY id DESC", (objective_id,)))


def reports_for_objective(conn: sqlite3.Connection, objective_id: int) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM reports WHERE objective_id = ? ORDER BY id ASC", (objective_id,)))


def insert_verifier_result(
    conn: sqlite3.Connection,
    objective_id: int | None,
    command: str,
    cwd: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_seconds: float,
    timed_out: bool,
) -> int:
    cur = conn.execute(
        """INSERT INTO verifier_results(objective_id, command, cwd, exit_code, stdout, stderr, duration_seconds, timed_out, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (objective_id, command, cwd, exit_code, stdout, stderr, duration_seconds, int(timed_out), utc_now()),
    )
    add_event(conn, "verifier.finished", command, objective_id=objective_id, data={"exit_code": exit_code, "timed_out": timed_out})
    conn.commit()
    return int(cur.lastrowid)


def latest_verifier_results(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM verifier_results ORDER BY id DESC LIMIT ?", (limit,)))


def patch_for_task(conn: sqlite3.Connection, task_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM patch_candidates WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def mark_patch_applied(conn: sqlite3.Connection, patch_id: int, worktree_path: str) -> None:
    conn.execute(
        "UPDATE patch_candidates SET status = 'applied_to_worktree', worktree_path = ?, updated_at = ? WHERE id = ?",
        (worktree_path, utc_now(), patch_id),
    )
    conn.commit()
