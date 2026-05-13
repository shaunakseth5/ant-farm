from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class AntFarmConfig(BaseModel):
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "queen-qwen3.6-35b-a3b"
    model_by_role: Dict[str, str] = Field(default_factory=lambda: {
        "debug": "worker-qwen3.5-9b",
        "trace": "worker-qwen3.5-9b",
        "risk": "worker-qwen3.5-9b",
        "review": "worker-qwen3.5-9b",
        "test": "fast-coder-qwen2.5-7b",
        "patch": "patch-qwen2.5-coder-14b",
        "queen": "queen-qwen3.6-35b-a3b",
    })
    llm_timeout_seconds: float = 120.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    verifier_timeout_seconds: float = 300.0
    max_context_bytes_per_file: int = 20_000
    max_context_bytes_total: int = 120_000
    max_context_files: int = 60
    queen_max_report_bytes: int = 24_000
    created_by: str = "antfarm"
    extra: Dict[str, Any] = Field(default_factory=dict)

    def model_for_role(self, role: str) -> str:
        return self.model_by_role.get(role, self.model)


STATE_DIR_NAME = ".antfarm"
CONFIG_FILE = "config.json"
DB_FILE = "blackboard.sqlite3"


def state_dir(repo: Path) -> Path:
    return repo / STATE_DIR_NAME


def config_path(repo: Path) -> Path:
    return state_dir(repo) / CONFIG_FILE


def db_path(repo: Path) -> Path:
    return state_dir(repo) / DB_FILE


def _nearest_initialized_repo(start: Path) -> Path | None:
    """Return the closest parent containing Ant Farm state, if any."""
    current = start.expanduser().resolve()
    candidates = [current, *current.parents]
    for candidate in candidates:
        if config_path(candidate).is_file():
            return candidate
    return None


def resolve_repo(repo: Optional[str | Path] = None) -> Path:
    """Resolve the repository Ant Farm should operate on.

    Explicit ``repo`` and ``ANTFARM_REPO`` are honored exactly. Otherwise, Ant Farm
    walks upward from the current directory and selects the nearest parent that has
    been initialized with ``.antfarm/config.json``. This keeps state repo-local while
    allowing normal CLI use from subdirectories. If no initialized parent exists,
    the current working directory is returned so callers can produce the standard
    "run antfarm init" error for that location.
    """
    if repo is not None:
        return Path(repo).expanduser().resolve()
    env_repo = os.environ.get("ANTFARM_REPO")
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    return _nearest_initialized_repo(Path.cwd()) or Path.cwd().resolve()


def is_inside_antfarm_worktree(path: Path) -> bool:
    """Return True if path is inside an Ant Farm managed git worktree."""
    parts = path.expanduser().resolve().parts
    for i in range(len(parts) - 1):
        if parts[i] == STATE_DIR_NAME and parts[i + 1] == "worktrees":
            return True
    return False


def assert_not_inside_antfarm_worktree(path: Path) -> None:
    if is_inside_antfarm_worktree(path):
        raise RuntimeError(
            "Refusing to run Ant Farm from inside .antfarm/worktrees. "
            "cd to the original repository root, then rerun the command."
        )


def ensure_repo_initialized(repo: Path) -> None:
    if not state_dir(repo).is_dir() or not config_path(repo).is_file():
        raise RuntimeError(f"Ant Farm is not initialized in {repo}. Run: antfarm init --repo {repo}")


def load_config(repo: Path) -> AntFarmConfig:
    ensure_repo_initialized(repo)
    path = config_path(repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    return AntFarmConfig(**data)


def save_config(repo: Path, config: AntFarmConfig) -> None:
    sd = state_dir(repo)
    sd.mkdir(parents=True, exist_ok=True)
    # Keep blackboard databases, worktrees, and transient logs out of the target repo.
    # The ignore file intentionally ignores itself too, avoiding a noisy untracked
    # .antfarm/ directory after initialization.
    ignore_path = sd / ".gitignore"
    if not ignore_path.exists():
        ignore_path.write_text("*\n", encoding="utf-8")
    path = config_path(repo)
    if hasattr(config, "model_dump"):
        text = json.dumps(config.model_dump(), indent=2)
    else:
        text = config.json(indent=2)
    path.write_text(text + "\n", encoding="utf-8")
