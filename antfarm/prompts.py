from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    severity: str = Field(description="info, low, medium, high, critical")
    file: Optional[str] = None
    line: Optional[int] = None
    message: str


class WorkerReport(BaseModel):
    role: str
    summary: str
    findings: List[Finding] = Field(default_factory=list)
    commands_suggested: List[str] = Field(default_factory=list)
    patch_diff: Optional[str] = Field(default=None, description="Unified diff, or null")
    memory_candidates: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class NextTask(BaseModel):
    role: str
    target: str
    files: List[str] = Field(default_factory=list)
    reason: str


class QueenDecision(BaseModel):
    summary: str
    decision: str
    next_tasks: List[NextTask] = Field(default_factory=list)
    verifier_commands: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    ready_for_patch_review: bool = False
    confidence: float = Field(default=0.5, ge=0, le=1)


ROLE_GUIDANCE: Dict[str, str] = {
    "debug": "Find likely defects, failure modes, broken assumptions, and minimal reproduction ideas.",
    "trace": "Trace control/data flow and explain how selected code paths behave. Avoid broad speculation.",
    "test": "Identify missing tests and propose deterministic test commands or test cases.",
    "patch": "Propose the smallest safe code change as a unified diff when enough context exists. Do not claim it was applied.",
    "review": "Review proposed changes or code for correctness, maintainability, and regressions.",
    "risk": "Identify security, data-loss, concurrency, portability, and operational risks.",
}


def _schema(model: Any) -> str:
    if hasattr(model, "model_json_schema"):
        data = model.model_json_schema()
    else:
        data = model.schema()
    return json.dumps(data, indent=2)


def worker_messages(role: str, objective_title: str, target: str, context: str) -> list[dict[str, str]]:
    guidance = ROLE_GUIDANCE.get(role, "Perform a narrow, scoped code-investigation task.")
    system = f"""You are an Ant Farm worker ant with role: {role}.
{guidance}

Rules:
- You receive scoped context only; do not ask for the full repository.
- Return strict JSON only, matching the schema below.
- Do not write durable memory. You may only propose memory_candidates.
- If role=patch, patch_diff must be a valid unified diff accepted by `git apply --check`. Use complete git-style diff format: `diff --git a/path b/path`, `--- a/path`, `+++ b/path`, and a correct hunk header such as `@@ -1,2 +1,2 @@`. Do not invent malformed line breaks, fake function signatures, or incomplete hunks. For every other role, patch_diff must be null.
- Do not say patches were applied. Patches are opt-in and applied only in isolated git worktrees.
- Be concise, factual, and cite files/lines when possible.

JSON schema:
{_schema(WorkerReport)}"""
    user = f"""Objective: {objective_title}
Target: {target}

Scoped repository context:
{context}

Return JSON now."""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def queen_messages(objective_title: str, reports_json: str) -> list[dict[str, str]]:
    system = f"""You are QueenAnt, the coordinator for Ant Farm.
Synthesize worker reports, decide next steps, and propose verifier commands.

Rules:
- Return strict JSON only, matching the schema below.
- Do not invent files or results not present in the reports.
- Do not apply patches. Patch application is opt-in and isolated.
- Prefer small, deterministic next tasks.

JSON schema:
{_schema(QueenDecision)}"""
    user = f"""Objective: {objective_title}

Worker reports as JSON:
{reports_json}

Return JSON now."""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
