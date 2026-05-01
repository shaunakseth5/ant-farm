# Contributing to Ant Farm

Thank you for your interest in Ant Farm! This document covers how to get started, the development workflow, and the expectations for contributions.

## Getting Started

1. **Fork** this repository.
2. **Clone** your fork:
   ```bash
   git clone https://github.com/<your-username>/antfarm_pi_build.git
   cd antfarm_pi_build
   ```
3. **Install** development dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
4. **Set up a local llama.cpp server** following the instructions in [README.md](README.md#llamacpp-router-setup) so you can test agent interactions.

## Project Structure

```
antfarm/
├── cli.py         # CLI entry point (Typer app with all commands)
├── config.py      # Configuration models and repo resolution helpers
├── db.py          # SQLite blackboard operations
├── llm.py         # OpenAI-compatible HTTP client to llama.cpp
├── orchestrator.py # Wave, ant, queen execution logic
├── prompts.py     # System prompts for worker ants and QueenAnt
├── repo.py        # Git tree and context builders
├── sandbox.py     # Isolated patch application via git worktrees
└── verifier.py    # Deterministic shell command runners

configs/           # Example config files
scripts/           # Utility scripts (llama server example)
_smoke_repo/       # Minimal repo for smoke testing
```

## Development Workflow

1. Create a feature branch: `git checkout -b feature/my-improvement`
2. Make changes and **ensure all Python modules compile**:
   ```bash
   python -m compileall -q antfarm
   ```
3. Run the toy smoke test in `_smoke_repo/` to verify end-to-end behavior with a running llama.cpp server.
4. Write or update tests in `tests/` (if you add new modules that aren't CLI-entry only).
5. Update documentation if you change behavior, commands, or configurations.

## Code Style

- **Python 3.10+** type hints throughout.
- **Docstrings**: Google-style for public functions and methods.
- **No external linting tools required**, but run `python -m py_compile antfarm/*.py` to catch syntax issues.
- Keep diffs small and focused. A PR should do one thing well.

## Pull Request Guidelines

1. **Title** a clear, imperative summary (e.g., "Add verify-worktree command").
2. **Describe** what changed and why in the body. Include a before/after example if relevant.
3. **Reference** any related issues.
4. **Include** a smoke test scenario when you add new CLI commands or behaviors.
5. **Do not** include model weights, GGUF files, or local paths to your machine's models.

## Adding a New Worker Role

1. Add the role name to `WORKER_ROLES` in `orchestrator.py`.
2. Define a `model_alias` entry in `config.py`'s default `model_by_role` dict.
3. Add role-specific prompt templates to `prompts.py`.
4. Update the README model routing table and documentation.

## Adding a New CLI Command

1. Use Typer commands in `cli.py` (see existing patterns like `init_cmd`, `ant_cmd`, `queen_cmd`).
2. Call into existing module functions rather than duplicating logic.
3. Add rich-table or Panel output for human-readable results where appropriate.
4. Ensure the command is accessible via `antfarm --help`.

## Testing with a Smoke Repo

The `_smoke_repo/` directory contains a minimal Python package with a deliberate bug. Use it to verify:

```bash
cd _smoke_repo
antfarm init --repo .
antfarm objective "Fix add() in pkg/math_bug.py"
antfarm wave 1 --ants debug,trace,risk --target "Find the bug" --files "pkg/math_bug.py"
antfarm queen 1
# ... continue with patch, apply, verify-worktree
```

This lets you validate agent behavior without needing a large codebase.

## Reporting Issues

- Describe your setup: OS, Python version, llama.cpp version.
- Include the exact `antfarm` command and output.
- Share `.antfarm/blackboard.sqlite3` if the issue involves database state (redact any sensitive content).
- For model routing issues, include your router INI config (with model paths redacted).

## Code of Conduct

Be respectful, inclusive, and constructive. Ant Farm is a tool built by developers, for developers — treat every contributor as a fellow ant in the colony.
