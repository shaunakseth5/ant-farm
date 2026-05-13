from __future__ import annotations

import json

from antfarm.orchestrator import _bounded_reports_json, _compact_report_for_queen


class Row(dict):
    def __getitem__(self, key: str):  # type: ignore[override]
        return super().__getitem__(key)


def test_compact_report_for_queen_replaces_large_patch_with_summary() -> None:
    patch = "diff --git a/a b/a\n" + "+x\n" * 2000
    row = Row(
        task_id=7,
        role="patch",
        json_text=json.dumps(
            {
                "role": "patch",
                "summary": "s" * 2000,
                "findings": [{"severity": "high", "file": "a", "line": 1, "message": "m" * 900}],
                "commands_suggested": ["pytest"],
                "patch_diff": patch,
                "memory_candidates": ["remember this"],
                "risks": ["risk"],
                "confidence": 0.8,
            }
        ),
    )

    compact = _compact_report_for_queen(row)

    assert compact["task_id"] == 7
    assert compact["patch_diff"]["present"] is True
    assert compact["patch_diff"]["bytes"] == len(patch.encode("utf-8"))
    assert len(compact["patch_diff"]["preview"]) < len(patch)
    assert "truncated" in compact["summary"]
    assert "truncated" in compact["findings"][0]["message"]


def test_bounded_reports_json_caps_payload() -> None:
    reports = [{"task_id": i, "summary": "x" * 1000} for i in range(10)]

    text = _bounded_reports_json(reports, max_bytes=1500)
    payload = json.loads(text)

    assert len(text.encode("utf-8")) < 2000
    assert payload[-1]["omitted_reports"] > 0
