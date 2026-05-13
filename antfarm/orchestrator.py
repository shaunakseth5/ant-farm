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



def _normalize_queen_payload(parsed: Any) -> Dict[str, Any]:
    """Accept imperfect Queen JSON and coerce it into QueenDecision shape."""
    if isinstance(parsed, dict):
        return parsed

    if isinstance(parsed, list):
        return {
            "summary": "Queen returned a top-level task list instead of a decision object; Ant Farm normalized it.",
            "decision": "Review the normalized next_tasks and continue with verifier-backed patch flow.",
            "next_tasks": parsed,
            "verifier_commands": [],
            "risks": ["Queen output was a JSON list, not a QueenDecision object."],
            "ready_for_patch_review": False,
            "confidence": 0.5,
        }

    return {
        "summary": "Queen returned an unsupported JSON shape; Ant Farm normalized it.",
        "decision": "Run another scoped worker wave or inspect the raw Queen response.",
        "next_tasks": [],
        "verifier_commands": [],
        "risks": [f"Unsupported Queen JSON type: {type(parsed).__name__}"],
        "ready_for_patch_review": False,
        "confidence": 0.0,
    }


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


def _truncate_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n[truncated {len(value) - limit} chars]"


def _compact_findings(findings: Any, limit: int = 8) -> List[Dict[str, Any]]:
    if not isinstance(findings, list):
        return []
    compact: list[Dict[str, Any]] = []
    for item in findings[:limit]:
        if not isinstance(item, dict):
            continue
        compact.append({
            "severity": item.get("severity"),
            "file": item.get("file"),
            "line": item.get("line"),
            "message": _truncate_text(item.get("message", ""), 500),
        })
    if len(findings) > limit:
        compact.append({"severity": "info", "file": None, "line": None, "message": f"{len(findings) - limit} additional findings omitted"})
    return compact


def _compact_report_for_queen(row: Any) -> Dict[str, Any]:
    """Strip token-heavy fields before asking QueenAnt to synthesize reports."""
    report = json.loads(row["json_text"])
    patch = report.get("patch_diff")
    patch_summary = None
    if isinstance(patch, str) and patch.strip():
        patch_summary = {
            "present": True,
            "bytes": len(patch.encode("utf-8", errors="replace")),
            "lines": len(patch.splitlines()),
            "preview": _truncate_text(patch, 1200),
        }

    compact = {
        "task_id": row["task_id"],
        "role": row["role"],
        "summary": _truncate_text(report.get("summary", ""), 1200),
        "findings": _compact_findings(report.get("findings")),
        "commands_suggested": [_truncate_text(cmd, 300) for cmd in (report.get("commands_suggested") or [])[:8] if isinstance(cmd, str)],
        "patch_diff": patch_summary,
        "memory_candidates": [_truncate_text(mem, 500) for mem in (report.get("memory_candidates") or [])[:5] if isinstance(mem, str)],
        "risks": [_truncate_text(risk, 500) for risk in (report.get("risks") or [])[:8] if isinstance(risk, str)],
        "confidence": report.get("confidence"),
    }
    return compact


def _bounded_reports_json(reports_payload: List[Dict[str, Any]], max_bytes: int) -> str:
    """Serialize reports with a hard cap so Queen prompts do not balloon."""
    if not reports_payload:
        return "[]"
    bounded: list[Dict[str, Any]] = []
    for report in reports_payload:
        candidate = [*bounded, report]
        text = json.dumps(candidate, indent=2)
        if len(text.encode("utf-8", errors="replace")) > max_bytes and bounded:
            bounded.append({"omitted_reports": len(reports_payload) - len(bounded), "reason": f"queen_max_report_bytes={max_bytes} reached"})
            break
        if len(text.encode("utf-8", errors="replace")) > max_bytes:
            bounded.append({"omitted_report": report.get("task_id"), "reason": f"single report exceeded queen_max_report_bytes={max_bytes}"})
            break
        bounded.append(report)
    return json.dumps(bounded, indent=2)


def run_queen(repo: Path, objective_id: int) -> Dict[str, Any]:
    cfg = load_config(repo)
    with db.connect(repo) as conn:
        objective = db.get_objective(conn, objective_id)
        reports = db.reports_for_objective(conn, objective_id)
        accepted_memories = db.list_memory_candidates(conn, objective_id=objective_id, status="accepted", limit=20)
        reports_payload = [_compact_report_for_queen(row) for row in reports]
        if accepted_memories:
            reports_payload.insert(0, {
                "accepted_memory": [
                    {"id": row["id"], "text": _truncate_text(row["text"], 500)} for row in accepted_memories
                ]
            })
        task_id = db.create_task(conn, objective_id, "queen", "synthesize worker reports", [])
        db.update_task_status(conn, task_id, "running")

    if reports_payload:
        reports_json = _bounded_reports_json(reports_payload, cfg.queen_max_report_bytes)
    else:
        reports_json = json.dumps([{"note": "No worker reports yet.", "repo_tree": simple_tree(repo)}], indent=2)

    try:
        messages = queen_messages(objective["title"], reports_json)
        parsed, raw_text, _raw_api = LLMClient(cfg).chat_json(messages, model=cfg.model_for_role("queen"))
        decision = _validate_model(QueenDecision, _normalize_queen_payload(parsed))
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
