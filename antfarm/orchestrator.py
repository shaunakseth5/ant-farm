from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import db
from .config import load_config
from .llm import LLMClient, LLMError
from .prompts import QueenDecision, WorkerReport, queen_messages, worker_messages
from .repo import build_context, simple_tree

WORKER_ROLES = {"debug", "trace", "test", "patch", "review", "risk"}


def _validate_model(model_cls: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    if hasattr(model_cls, "model_validate"):
        obj = model_cls.model_validate(data)
        return obj.model_dump()
    obj = model_cls.parse_obj(data)
    return obj.dict()




_HUNK_RE = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

def _normalize_unified_diff(diff: str) -> str:
    """Repair common LLM unified-diff formatting mistakes before validation."""
    lines = diff.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    out = []
    old_remaining = 0
    new_remaining = 0

    for line in lines:
        m = _HUNK_RE.match(line)
        if m:
            old_remaining = int(m.group(1) or "1")
            new_remaining = int(m.group(2) or "1")
            out.append(line)
            continue

        if old_remaining > 0 or new_remaining > 0:
            fixed = line
            if not fixed:
                fixed = " "
            elif fixed[0] not in " +-\\":  # context lines in hunks must begin with a space
                fixed = " " + fixed

            out.append(fixed)

            if fixed.startswith(" "):
                old_remaining -= 1
                new_remaining -= 1
            elif fixed.startswith("-"):
                old_remaining -= 1
            elif fixed.startswith("+"):
                new_remaining -= 1
            continue

        out.append(line)

    return "\n".join(out).rstrip() + "\n"


def _gate_patch_diff(repo: Path, report: Dict[str, Any]) -> None:
    """Only keep patch_diff when Git can apply it cleanly to the current repo."""
    if report.get("role") != "patch":
        report["patch_diff"] = None
        return

    patch = report.get("patch_diff")
    if not isinstance(patch, str) or not patch.strip():
        report["patch_diff"] = None
        return

    patch = _normalize_unified_diff(patch)
    report["patch_diff"] = patch

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "apply", "--check", "-"],
            input=patch,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except Exception as exc:
        report["patch_diff"] = None
        report.setdefault("risks", []).append(f"Patch rejected before storage: git apply --check failed to run: {exc}")
        return

    if proc.returncode != 0:
        report["patch_diff"] = None
        err = (proc.stderr or proc.stdout or "").strip()
        report.setdefault("risks", []).append(f"Patch rejected before storage: git apply --check failed: {err}")


def run_ant(repo: Path, objective_id: int, role: str, target: str, files: Iterable[str]) -> Dict[str, Any]:
    if role not in WORKER_ROLES:
        raise ValueError(f"Unknown worker role {role!r}. Expected one of: {', '.join(sorted(WORKER_ROLES))}")
    cfg = load_config(repo)
    file_list = list(files)
    with db.connect(repo) as conn:
        objective = db.get_objective(conn, objective_id)
        task_id = db.create_task(conn, objective_id, role, target, file_list)
        db.update_task_status(conn, task_id, "running")

    try:
        context = build_context(repo, file_list, cfg)
        messages = worker_messages(role, objective["title"], target, context)
        parsed, raw_text, _raw_api = LLMClient(cfg).chat_json(messages, model=cfg.model_for_role(role))
        report = _validate_model(WorkerReport, parsed)
        report["role"] = role
        _gate_patch_diff(repo, report)
        status = "done"
        error = None
    except Exception as exc:
        raw_text = ""
        report = {
            "role": role,
            "summary": "Worker failed before producing a valid report.",
            "findings": [{"severity": "high", "file": None, "line": None, "message": str(exc)}],
            "commands_suggested": [],
            "patch_diff": None,
            "memory_candidates": [],
            "risks": [str(exc)],
            "confidence": 0.0,
        }
        status = "error"
        error = str(exc)

    with db.connect(repo) as conn:
        if status == "done":
            db.save_report(conn, task_id, objective_id, role, report, raw_text)
        else:
            db.update_task_status(conn, task_id, "error", error)
            db.save_report(conn, task_id, objective_id, role, report, raw_text)
            db.update_task_status(conn, task_id, "error", error)
        task = db.get_task(conn, task_id)
    return {"task_id": task_id, "status": task["status"], "report": report}


def run_wave(repo: Path, objective_id: int, roles: Iterable[str], target: str, files: Iterable[str], max_workers: int) -> List[Dict[str, Any]]:
    roles_list = [role.strip() for role in roles if role.strip()]
    if not roles_list:
        raise ValueError("At least one ant role is required")
    max_workers = max(1, min(max_workers, len(roles_list)))
    results: list[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_ant, repo, objective_id, role, target, list(files)) for role in roles_list]
        for fut in as_completed(futures):
            results.append(fut.result())
    return sorted(results, key=lambda item: item["task_id"])


def run_queen(repo: Path, objective_id: int) -> Dict[str, Any]:
    cfg = load_config(repo)
    with db.connect(repo) as conn:
        objective = db.get_objective(conn, objective_id)
        reports = db.reports_for_objective(conn, objective_id)
        reports_payload = [
            {"task_id": row["task_id"], "role": row["role"], "report": json.loads(row["json_text"])} for row in reports
        ]
        task_id = db.create_task(conn, objective_id, "queen", "synthesize worker reports", [])
        db.update_task_status(conn, task_id, "running")

    if reports_payload:
        reports_json = json.dumps(reports_payload, indent=2)
    else:
        reports_json = json.dumps([{"note": "No worker reports yet.", "repo_tree": simple_tree(repo)}], indent=2)

    try:
        messages = queen_messages(objective["title"], reports_json)
        parsed, raw_text, _raw_api = LLMClient(cfg).chat_json(messages, model=cfg.model_for_role("queen"))
        decision = _validate_model(QueenDecision, parsed)
        error = None
    except Exception as exc:
        raw_text = ""
        decision = {
            "summary": "Queen failed before producing a valid decision.",
            "decision": "Run scoped worker ants or check the local LLM server.",
            "next_tasks": [],
            "verifier_commands": [],
            "risks": [str(exc)],
            "ready_for_patch_review": False,
            "confidence": 0.0,
        }
        error = str(exc)

    with db.connect(repo) as conn:
        db.save_report(conn, task_id, objective_id, "queen", decision, raw_text)
        if error:
            db.update_task_status(conn, task_id, "error", error)
        task = db.get_task(conn, task_id)
    return {"task_id": task_id, "status": task["status"], "decision": decision}
