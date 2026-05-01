from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable, List, Tuple

from .config import AntFarmConfig


EXCLUDED_DIRS = {".git", ".antfarm", "__pycache__", ".venv", "venv", "node_modules"}


def ensure_inside_repo(repo: Path, path: Path) -> Path:
    resolved_repo = repo.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_repo and resolved_repo not in resolved_path.parents:
        raise ValueError(f"Path escapes repo: {path}")
    return resolved_path


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def expand_file_globs(repo: Path, patterns: Iterable[str]) -> List[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        if not pattern:
            continue
        p = Path(pattern)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Only repo-relative file globs are allowed: {pattern}")
        matches = list(repo.glob(pattern))
        if not matches and (repo / pattern).is_file():
            matches = [repo / pattern]
        for match in sorted(matches):
            if not match.is_file() or _is_excluded(match.relative_to(repo)):
                continue
            safe = ensure_inside_repo(repo, match)
            if safe not in seen:
                seen.add(safe)
                files.append(safe)
    return files


def read_limited(path: Path, limit: int) -> Tuple[str, bool]:
    data = path.read_bytes()
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def build_context(repo: Path, patterns: Iterable[str], config: AntFarmConfig) -> str:
    files = expand_file_globs(repo, patterns)
    if not files:
        return "No files were included."

    chunks: list[str] = []
    used = 0
    for file_path in files:
        remaining = config.max_context_bytes_total - used
        if remaining <= 0:
            chunks.append("\n[context truncated: total byte limit reached]")
            break
        per_file = min(config.max_context_bytes_per_file, remaining)
        rel = file_path.relative_to(repo).as_posix()
        text, truncated = read_limited(file_path, per_file)
        used += len(text.encode("utf-8", errors="replace"))
        suffix = "\n[truncated]" if truncated else ""
        chunks.append(f"\n--- FILE: {rel} ---\n{text}{suffix}\n--- END FILE: {rel} ---")
    return "\n".join(chunks)


def simple_tree(repo: Path, max_entries: int = 200) -> str:
    entries: list[str] = []
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if _is_excluded(rel):
            continue
        entries.append(rel.as_posix() + ("/" if path.is_dir() else ""))
        if len(entries) >= max_entries:
            entries.append("[tree truncated]")
            break
    return "\n".join(entries)
