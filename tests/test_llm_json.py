from __future__ import annotations

import pytest

from antfarm.llm import LLMError, extract_json


def test_extract_json_from_markdown_fence() -> None:
    assert extract_json("Here you go:\n```json\n{\"ok\": true}\n```\n") == {"ok": True}


def test_extract_json_from_prose_with_balanced_object() -> None:
    assert extract_json("thinking... result = {\"items\": [1, 2, {\"x\": \"}\"}]} done") == {
        "items": [1, 2, {"x": "}"}],
    }


def test_extract_json_accepts_top_level_array() -> None:
    assert extract_json("prefix [1, {\"a\": 2}] suffix") == [1, {"a": 2}]


def test_extract_json_raises_on_missing_json() -> None:
    with pytest.raises(LLMError):
        extract_json("no structured payload here")
