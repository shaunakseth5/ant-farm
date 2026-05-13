from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

from .config import AntFarmConfig


EXCLUDED_DIRS = {".git", ".antfarm", "__pycache__", ".venv", "venv", "node_modules"}
_BINARY_SAMPLE_BYTES = 4096


@dataclass(frozen=True)
class ContextEntry:
    """One file considered for an LLM context bundle."""

    path: str
    size_bytes: int
    included_bytes: int = 0
    truncated: bool = False
    skipped_reason: str | None = None
    text: str = ""

    @property
    def included(self) -> bool:
        return self.skipped_reason is None

    @property
    def estimated_tokens(self) -> int:
        # Cheap conservative proxy for BPE-ish tokenization. The exact tokenizer is
        # model-specific, but bytes/4 is good enough for budget warnings.
        return max(1, self.included_bytes // 4) if self.included_bytes else 0


@dataclass(frozen=True)
class ContextPlan:
    """A planned/built context plus metadata for auditing token spend."""

    entries: List[ContextEntry]
    total_included_bytes: int
    total_size_bytes: int
    truncated_by_total_limit: bool = False
    truncated_by_file_limit: bool = False

    @property
    def estimated_tokens(self) -> int:
        return sum(entry.estimated_tokens for entry in self.entries)

    @property
    def included_files(self) -> int:
        return sum(1 for entry in self.entries if entry.included)

    @property
    def skipped_files(self) -> int:
        return sum(1 for entry in self.entries if entry.skipped_reason)


def ensure_inside_repo(repo: Path, path: Path) -> Path:
    resolved_repo = repo.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_repo and resolved_repo not in resolved_path.parents:
        raise ValueError(f"Path escapes repo: {path}")
    return resolved_path


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def _is_probably_binary(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:_BINARY_SAMPLE_BYTES]
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return control / len(sample) > 0.30


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
    with path.open("rb") as file:
        data = file.read(limit + 1)
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def build_context_plan(repo: Path, patterns: Iterable[str], config: AntFarmConfig) -> ContextPlan:
    """Build a bounded context bundle and retain file/token-spend metadata."""
    files = expand_file_globs(repo, patterns)
    entries: list[ContextEntry] = []
    used = 0
    total_size = 0
    truncated_by_total = False
    truncated_by_file = False
    max_files = max(1, config.max_context_files)
    included_file_count = 0

    for file_path in files:
        rel = file_path.relative_to(repo).as_posix()
        try:
            size = file_path.stat().st_size
        except OSError:
            entries.append(ContextEntry(path=rel, size_bytes=0, skipped_reason="stat failed"))
            continue
        total_size += size

        if _is_probably_binary(file_path):
            entries.append(ContextEntry(path=rel, size_bytes=size, skipped_reason="binary or non-text file"))
            continue

        if included_file_count >= max_files:
            truncated_by_file = True
            entries.append(ContextEntry(path=rel, size_bytes=size, skipped_reason=f"max_context_files={max_files} reached"))
            continue

        remaining = config.max_context_bytes_total - used
        if remaining <= 0:
            truncated_by_total = True
            entries.append(ContextEntry(path=rel, size_bytes=size, skipped_reason="total byte limit reached"))
            continue

        per_file_limit = min(config.max_context_bytes_per_file, remaining)
        text, truncated = read_limited(file_path, per_file_limit)
        included_bytes = len(text.encode("utf-8", errors="replace"))
        used += included_bytes
        included_file_count += 1
        truncated_by_total = truncated_by_total or used >= config.max_context_bytes_total and size > included_bytes
        entries.append(
            ContextEntry(
                path=rel,
                size_bytes=size,
                included_bytes=included_bytes,
                truncated=truncated,
                text=text,
            )
        )

    return ContextPlan(
        entries=entries,
        total_included_bytes=used,
        total_size_bytes=total_size,
        truncated_by_total_limit=truncated_by_total,
        truncated_by_file_limit=truncated_by_file,
    )


def render_context(plan: ContextPlan) -> str:
    if not plan.entries or plan.included_files == 0:
        return "No files were included."

    chunks = [
        "Context budget:",
        f"- included_files: {plan.included_files}",
        f"- skipped_files: {plan.skipped_files}",
        f"- included_bytes: {plan.total_included_bytes}",
        f"- estimated_tokens: {plan.estimated_tokens}",
    ]
    for entry in plan.entries:
        if entry.skipped_reason:
            continue
        suffix = "\n[truncated]" if entry.truncated else ""
        chunks.append(f"\n--- FILE: {entry.path} ---\n{entry.text}{suffix}\n--- END FILE: {entry.path} ---")
    if plan.truncated_by_total_limit:
        chunks.append("\n[context truncated: total byte limit reached]")
    if plan.truncated_by_file_limit:
        chunks.append("\n[context truncated: file count limit reached]")
    return "\n".join(chunks)


def build_context(repo: Path, patterns: Iterable[str], config: AntFarmConfig) -> str:
    return render_context(build_context_plan(repo, patterns, config))


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
