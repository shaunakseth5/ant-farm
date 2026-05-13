# Ant Farm Dense Review

## Current shape

Ant Farm is a Python/Typer CLI that stores repo-local state in SQLite, asks local OpenAI-compatible llama.cpp endpoints for strict JSON worker/queen outputs, and applies model-generated patches only in isolated git worktrees.

The current codebase is compact and understandable, but it is still closer to a prototype than a durable personal CLI. The highest leverage improvements are better UX diagnostics, test coverage, safer state handling, and a clearer migration path for performance-sensitive pieces.

## Strengths

- **Good safety posture:** patch candidates are validated with `git apply --check` and applied only in `.antfarm/worktrees/`.
- **Simple architecture:** CLI, config, DB, LLM, repo context, sandbox, verifier, prompts are cleanly separated.
- **Local-first design:** no hosted control plane; model endpoint is configurable and defaults to loopback.
- **Structured outputs:** Pydantic models give worker/queen reports a stable shape.
- **Readable blackboard:** SQLite tables are easy to inspect manually when debugging.

## Immediate issues found

1. **No test suite was present.** `pytest` reported `no tests ran` and CONTRIBUTING referenced `.[dev]` even though no dev extra existed.
2. **Subdirectory UX was brittle.** Commands only used the current directory unless `ANTFARM_REPO` was set, so running from `src/` inside an initialized repo failed.
3. **No diagnostics command.** Users had to infer whether failures came from git, config, SQLite, or llama.cpp connectivity.
4. **`.antfarm/` state could be noisy.** Target repos without their own ignore rule could surface local Ant Farm internals in `git status`.
5. **Verifier commands use `shell=True`.** This is documented, but a future safer command model would help.
6. **LLM runtime knobs are minimal.** Temperature, max tokens, retries, and per-role settings are currently hard-coded or sparse.
7. **Patch repair is heuristic.** `_normalize_unified_diff` helps with common LLM mistakes, but long-term patch generation should be more tool-mediated.
8. **State schema has no migration system.** `CREATE TABLE IF NOT EXISTS` works initially, but versioned migrations will become necessary.
9. **No packaging CI baseline.** Compile/test checks should become push/PR gates.
10. **No Rust boundary defined yet.** A Rust rewrite should be staged, not a big-bang port.

## Work started

- Added a pytest suite covering config resolution, repo glob safety, JSON extraction, isolated worktree patch application, CLI commands, and Queen report compaction.
- Added `dev` extras for pytest.
- Made default repo resolution find the nearest initialized parent, so commands work from subdirectories.
- Added `.antfarm/.gitignore` creation during config save to keep state contents ignored.
- Added `antfarm doctor` to check repo, git, config, database, and optional LLM endpoint health.
- Added `antfarm context` to preview matched files, skipped files, byte budget usage, and rough token estimates before making LLM calls.
- Added config show/set commands for tuning context budgets, LLM token limits, temperature, and role model routing without hand-editing JSON.
- Added context metadata to worker prompts and binary/file-count skipping to reduce accidental token waste.
- Compacted Queen report input so large patch diffs and long findings do not balloon synthesis prompts.
- Added `antfarm memory list|accept|reject` so model-proposed memory is human-gated before reuse.

## Near-term roadmap

### 1. Make the existing Python CLI dependable

- Add CI: `python -m pytest -q`, `python -m compileall -q antfarm`.
- Add integration tests for `init`, `objective`, `status`, `task`, and `doctor` through Typer's `CliRunner`.
- Add clear CLI error handling around invalid roles, missing objectives/tasks, and failed LLM requests.
- Add versioned SQLite migrations.
- Add richer config commands: `models`, `roles`, config validation summaries, and reset-to-default helpers.

### 2. Improve agent loop quality

- Add per-role LLM settings: temperature, max tokens, top-p, retry count, JSON repair attempts.
- Store raw API metadata: model name, token counts if present, latency, HTTP status.
- Add verifier result summarization into Queen context.
- Add patch provenance: base commit SHA, file list, validation command, apply check stderr.
- Use accepted memory selectively in worker prompts with scope controls and freshness metadata.

### 3. Improve safety

- Replace free-form shell verifier with explicit modes:
  - `--cmd` remains for power users.
  - `-- pytest ...`, `-- cargo test ...`, `-- npm test ...` style safe argv commands.
- Add command allow/deny policy in config.
- Refuse patch application if repo HEAD changed since patch validation unless `--force`.
- Add worktree cleanup/list commands.

### 4. Rust migration path

Do not rewrite all at once. The first Rust target should be a small, testable core binary while Python remains the UX shell.

Good Rust candidates:

- **Context builder:** fast gitignore-aware file discovery, byte budgeting, binary detection.
- **Patch gate:** parse/validate unified diffs, map hunks to files, produce better diagnostics.
- **Sandbox manager:** robust worktree creation, branch safety, cleanup.
- **Verifier runner:** argv-safe process execution, streaming logs, cancellation.
- **SQLite state layer:** typed migrations and concurrency control.

Suggested staged architecture:

1. Add a Rust workspace with `antfarm-core` plus a tiny `antfarm-core` CLI exposing JSON-over-stdio commands.
2. Call it from Python for one operation first, likely context building or patch validation.
3. Once stable, migrate sandbox/verifier.
4. Eventually replace Typer with a Rust `clap` CLI if the Python shell stops adding value.

Candidate crates:

- `clap` for CLI.
- `serde`, `serde_json` for command protocols and report types.
- `sqlx` or `rusqlite` for SQLite.
- `ignore`, `globset`, `walkdir` for repo walking.
- `gix` or subprocess `git` for worktree operations.
- `reqwest` for OpenAI-compatible HTTP.
- `tokio` for concurrent worker waves.
- `similar` or a dedicated patch crate for diff handling.
- `miette`/`thiserror`/`anyhow` for humane errors.

## Architectural target

Long term, Ant Farm should become:

- A fast local orchestrator with explicit state transitions.
- A safe patch/verifier sandbox, not a model-output executor.
- A repo-aware CLI that feels reliable from any subdirectory.
- A model-router client with reproducible settings and traceable outputs.
- A Rust core for correctness/performance, with optional Python only for experiments.
