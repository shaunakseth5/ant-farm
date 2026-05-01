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
    verifier_timeout_seconds: float = 300.0
    max_context_bytes_per_file: int = 20_000
    max_context_bytes_total: int = 120_000
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


def resolve_repo(repo: Optional[str | Path] = None) -> Path:
    """Resolve the repo root without walking parent directories.

    Ant Farm is intentionally repo-local. Commands run against ANTFARM_REPO if set,
    otherwise the current working directory, unless an explicit repo is provided.
    """
    raw = repo or os.environ.get("ANTFARM_REPO") or Path.cwd()
    return Path(raw).expanduser().resolve()


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
    path = config_path(repo)
    if hasattr(config, "model_dump"):
        text = json.dumps(config.model_dump(), indent=2)
    else:
        text = config.json(indent=2)
    path.write_text(text + "\n", encoding="utf-8")
