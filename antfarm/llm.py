from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Tuple

import httpx

from .config import AntFarmConfig


class LLMError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _balanced_json_candidates(text: str) -> Iterable[str]:
    starts = [i for i, ch in enumerate(text) if ch in "[{"]
    for start in starts:
        stack: list[str] = []
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or ch != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    yield text[start : i + 1]
                    break


def extract_json(text: str) -> Any:
    """Parse JSON from model output that may contain prose, markdown, or thinking text."""
    cleaned = _strip_code_fence(text)
    for candidate in [cleaned, *list(_balanced_json_candidates(cleaned)), *list(_balanced_json_candidates(text))]:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMError("Could not extract valid JSON from model response")


class LLMClient:
    def __init__(self, config: AntFarmConfig):
        self.config = config
        self.base_url = config.llm_base_url.rstrip("/")

    def chat(self, messages: List[Dict[str, str]], model: str | None = None) -> Tuple[str, Dict[str, Any]]:
        routed_messages = [dict(m) for m in messages]
        for message in routed_messages:
            if message.get("role") == "user":
                content = message.get("content", "")
                if not content.lstrip().startswith("/no_think"):
                    message["content"] = "/no_think\n" + content
                break

        payload = {
            "model": model or self.config.model,
            "messages": routed_messages,
            "temperature": self.config.llm_temperature,
            "max_tokens": self.config.llm_max_tokens,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.config.llm_timeout_seconds) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data!r}") from exc
        return content, data

    def chat_json(self, messages: List[Dict[str, str]], model: str | None = None) -> Tuple[Any, str, Dict[str, Any]]:
        content, raw = self.chat(messages, model=model)
        return extract_json(content), content, raw
