# Ant Farm

Ant Farm is a **local, repo-scoped, multi-agent coding CLI** for running narrow coding agents against your own repositories. It is designed for local llama.cpp/OpenAI-compatible inference, explicit context budgeting, human-gated memory, verifier-backed patch review, and isolated git worktree application.

The design goal is not "let an agent roam the repo." The goal is a deterministic command-line harness where every expensive or risky step is visible:

- what files are sent to the model,
- approximately how many tokens will be spent,
- which model alias each role uses,
- what JSON the agent returned,
- whether a patch is syntactically applicable,
- where that patch was applied,
- which verifier commands passed or failed,
- which memory candidates are trusted by a human.

Ant Farm is currently Python-first, with a planned staged Rust core for the pieces that benefit most from correctness, speed, and process control: context building, patch validation, sandbox/worktree management, verification, and SQLite migrations.

---

## Table of Contents

- [Core Concepts](#core-concepts)
- [Architecture](#architecture)
- [Install](#install)
- [llama.cpp / OpenAI-Compatible Setup](#llamacpp--openai-compatible-setup)
- [Quick Start](#quick-start)
- [Real Repository Workflow](#real-repository-workflow)
- [Command Reference](#command-reference)
- [Configuration](#configuration)
- [Context and Token Budgeting](#context-and-token-budgeting)
- [Agentic Memory Model](#agentic-memory-model)
- [Patch and Worktree Safety](#patch-and-worktree-safety)
- [SQLite Blackboard](#sqlite-blackboard)
- [Development](#development)
- [Roadmap: Rust Core](#roadmap-rust-core)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [License](#license)

---

## Core Concepts

### Repo-local state

Ant Farm stores all operational state inside the target repository:

```text
.antfarm/
├── .gitignore
├── blackboard.sqlite3
├── config.json
└── worktrees/
```

The `.antfarm/.gitignore` file ignores all local state so blackboards, logs, and worktrees do not pollute `git status`.

After initialization, Ant Farm resolves the nearest initialized parent repository, so commands can be run from subdirectories without setting `ANTFARM_REPO`. You can also target any repo explicitly from anywhere:

```bash
antfarm -C /path/to/repo status
antfarm -C /path/to/repo run "Investigate flaky tests" --files "tests/**/*.py"
```

### Narrow worker ants

Ant Farm uses specialized worker roles instead of one broad agent:

| Role | Purpose |
|------|---------|
| `debug` | Locate defects, broken assumptions, and likely failure modes. |
| `trace` | Explain control/data flow through scoped files. |
| `risk` | Identify security, data-loss, concurrency, portability, and operational risks. |
| `review` | Review code or proposed changes for correctness and maintainability. |
| `test` | Propose deterministic tests and verifier commands. |
| `patch` | Produce the smallest safe unified-diff patch when enough context exists. |
| `queen` | Synthesize reports, propose next tasks, and recommend verifier commands. |

Each worker receives only explicitly selected context and must return strict JSON. Non-patch workers are forced to return `patch_diff: null`.

### Human-gated memory

Workers cannot write trusted memory directly. They may only propose `memory_candidates`. You explicitly accept or reject candidates with:

```bash
antfarm memory list
antfarm memory accept <id>
antfarm memory reject <id>
```

Only accepted memory is provided to Queen synthesis.

### Patch isolation

Patch candidates are never applied to the main checkout. A patch must:

1. be returned by a `patch` worker,
2. survive normalization,
3. pass `git apply --check` against the target repo,
4. be explicitly applied with `antfarm apply`,
5. land inside `.antfarm/worktrees/<branch>`.

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│                              User                                │
│                         antfarm CLI                              │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                │ Typer commands
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                       Orchestration Layer                         │
│  objective/task creation, waves, Queen synthesis, patch gating    │
└─────────────┬──────────────────┬────────────────────┬────────────┘
              │                  │                    │
              │                  │                    │
              ▼                  ▼                    ▼
┌─────────────────────┐ ┌──────────────────┐ ┌─────────────────────┐
│ Context Builder     │ │ LLM Client        │ │ Verifier/Sandbox     │
│ glob expansion      │ │ /chat/completions │ │ shell checks         │
│ binary skip         │ │ JSON extraction   │ │ git worktrees        │
│ byte/token budgets  │ │ role model route  │ │ patch application    │
└─────────┬───────────┘ └─────────┬────────┘ └──────────┬──────────┘
          │                       │                     │
          ▼                       ▼                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                    OpenAI-compatible endpoint                     │
│              default: http://127.0.0.1:8080/v1                   │
│             commonly llama.cpp router with GGUF models            │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                       SQLite Blackboard                           │
│ .antfarm/blackboard.sqlite3                                      │
│ objectives, tasks, reports, events, verifiers, patches, memory    │
└──────────────────────────────────────────────────────────────────┘
```

### Python modules

```text
antfarm/
├── cli.py           # Typer CLI and rich terminal output
├── config.py        # repo resolution, .antfarm paths, Pydantic config
├── db.py            # SQLite blackboard operations
├── llm.py           # OpenAI-compatible HTTP client and JSON extraction
├── orchestrator.py  # run_ant, run_wave, run_queen, patch gating
├── prompts.py       # role prompts and Pydantic report schemas
├── repo.py          # glob expansion, binary detection, context budgets
├── sandbox.py       # isolated git worktree patch application
└── verifier.py      # deterministic verifier command execution
```

---

## Install

Prerequisites:

- Python 3.10+
- git
- local or reachable OpenAI-compatible chat-completions endpoint
- optionally llama.cpp with router support

```bash
git clone https://github.com/shaunakseth5/ant-farm.git
cd ant-farm

python -m venv .venv
source .venv/bin/activate

pip install -e .
```

For development and tests:

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q antfarm
```

---

## llama.cpp / OpenAI-Compatible Setup

By default Ant Farm expects:

```text
http://127.0.0.1:8080/v1
```

It calls:

```text
POST /chat/completions
GET  /models       # used by antfarm doctor
```

### Recommended llama.cpp router config

Create a router config like `configs/llama-router.antfarm.ini.example`:

```ini
version = 1

[*]
jinja = true
reasoning-format = deepseek
flash-attn = on
n-gpu-layers = 99

[worker-qwen3.5-9b]
model = /path/to/qwen3.5-9b-Q5_K_XL.gguf
ctx-size = 32768
parallel = 4
temp = 0.2
top-p = 0.9

[fast-coder-qwen2.5-7b]
model = /path/to/qwen2.5-coder-7b-Q5_K_M.gguf
ctx-size = 32768
parallel = 4
temp = 0.15
top-p = 0.9

[patch-qwen2.5-coder-14b]
model = /path/to/qwen2.5-coder-14b-Q5_K_M.gguf
ctx-size = 32768
parallel = 2
temp = 0.15
top-p = 0.9

[queen-qwen3.6-35b-a3b]
model = /path/to/qwen3.6-35b-a3b-Q4_K_M.gguf
ctx-size = 65536
parallel = 1
temp = 0.7
top-p = 0.95
presence-penalty = 1.5
```

Launch:

```bash
llama-server \
  --config configs/llama-router.antfarm.ini.example \
  --host 127.0.0.1 \
  --port 8080
```

Verify:

```bash
curl -s http://127.0.0.1:8080/v1/models | python -m json.tool
antfarm doctor
```

### Single-model mode

For a simpler one-model setup:

```bash
export MODEL_PATH=/path/to/model.gguf
bash scripts/run_llama_server_example.sh
```

Then point every role at that model if needed:

```bash
antfarm config set model your-model-alias
antfarm config set model_by_role.debug your-model-alias
antfarm config set model_by_role.trace your-model-alias
antfarm config set model_by_role.risk your-model-alias
antfarm config set model_by_role.review your-model-alias
antfarm config set model_by_role.test your-model-alias
antfarm config set model_by_role.patch your-model-alias
antfarm config set model_by_role.queen your-model-alias
```

---

## Quick Start

### Usable one-shot path

```bash
# inside any git repository
antfarm init --repo .
antfarm doctor

# One command creates an objective, previews context, runs workers,
# asks QueenAnt to synthesize, then optionally runs PatchAnt.
antfarm run "Fix failing parser test" \
  --files "src/parser/**/*.py" \
  --files "tests/test_parser.py" \
  --verify "python -m pytest tests/test_parser.py -q" \
  --patch
```

If PatchAnt produces a validated patch, Ant Farm prints the next `antfarm task` and `antfarm apply` commands. To apply and verify in one flow:

```bash
antfarm run "Fix failing parser test" \
  --files "src/parser/**/*.py" \
  --files "tests/test_parser.py" \
  --patch \
  --apply-branch antfarm-parser-fix \
  --verify-worktree "python -m pytest tests/test_parser.py -q"
```

### Step-by-step path

```bash
antfarm new "Fix failing parser test"

# Check what will be sent before spending tokens
antfarm context --files "src/**/*.py" --files "tests/**/*.py"

# Run focused investigation ants
antfarm wave 1 \
  --ants debug,trace,risk \
  --target "Find why the parser rejects valid nested expressions" \
  --files "src/parser/**/*.py" \
  --files "tests/test_parser.py"

# Ask QueenAnt to synthesize findings and propose next steps
antfarm queen 1

# Generate a patch candidate
antfarm ant 1 patch \
  --target "Make the smallest safe parser fix and preserve existing behavior" \
  --files "src/parser/**/*.py" \
  --files "tests/test_parser.py"

# Inspect, apply, and verify isolated patch
antfarm task <PATCH_TASK_ID>
antfarm apply <PATCH_TASK_ID> --branch antfarm-parser-fix
antfarm verify-worktree <PATCH_TASK_ID> --cmd "python -m pytest tests/test_parser.py -q"
```

---

## Real Repository Workflow

### 1. Initialize and diagnose

```bash
antfarm init --repo .
antfarm doctor
antfarm status
```

`doctor` checks:

- target repo exists,
- command is not running inside `.antfarm/worktrees`,
- git recognizes the repo,
- `.antfarm/config.json` exists and parses,
- `.antfarm/blackboard.sqlite3` exists and has the expected schema,
- optional LLM `/models` connectivity.

### 2. Tune context budgets

```bash
antfarm config set max_context_bytes_total 60000
antfarm config set max_context_bytes_per_file 12000
antfarm config set max_context_files 30
antfarm config set queen_max_report_bytes 20000
antfarm config set llm_max_tokens 2048
```

Preview:

```bash
antfarm context --files "src/**/*.py" --files "tests/**/*.py"
antfarm context --files "src/suspicious.py" --show
```

### 3. Create an objective

```bash
antfarm objective "Refactor configuration loading without changing CLI behavior"
```

### 4. Run narrow waves

```bash
antfarm wave 1 \
  --ants debug,trace,risk,review \
  --target "Analyze the current configuration-loading path, risks, and tests" \
  --files "antfarm/config.py" \
  --files "antfarm/cli.py" \
  --files "tests/**/*.py" \
  --max-workers 4
```

### 5. Synthesize and verify

```bash
antfarm queen 1
antfarm verify --objective-id 1 --cmd "python -m pytest -q"
```

### 6. Generate/apply/verify patch

```bash
antfarm ant 1 patch \
  --target "Implement the smallest safe config-loading improvement" \
  --files "antfarm/config.py" \
  --files "tests/**/*.py"

antfarm task <PATCH_TASK_ID>
antfarm apply <PATCH_TASK_ID> --branch config-loading-improvement
antfarm verify-worktree <PATCH_TASK_ID> --cmd "python -m pytest -q"
```

---

## Command Reference

### `antfarm init --repo PATH`

Initialize repo-local state.

```bash
antfarm init --repo .
```

Creates:

- `.antfarm/config.json`
- `.antfarm/blackboard.sqlite3`
- `.antfarm/.gitignore`

### `antfarm doctor [--repo PATH] [--llm/--no-llm]`

Run setup diagnostics.

```bash
antfarm doctor
antfarm doctor --no-llm
antfarm doctor --repo /path/to/repo
```

### `antfarm objective TITLE` / `antfarm new TITLE`

Create an objective row in the blackboard. `new` is a friendly alias.

```bash
antfarm objective "Fix flaky auth test"
antfarm new "Fix flaky auth test"
```

### `antfarm run GOAL --files GLOB...`

Run the ergonomic default workflow: create an objective, preview context, run a worker wave, run Queen synthesis, and optionally patch/apply/verify.

```bash
antfarm run "Fix auth timeout bug" \
  --files "src/auth/**/*.py" \
  --files "tests/test_auth.py" \
  --ants debug,trace,risk \
  --verify "python -m pytest tests/test_auth.py -q"

antfarm run "Fix auth timeout bug" \
  --files "src/auth/**/*.py" \
  --files "tests/test_auth.py" \
  --patch \
  --apply-branch auth-timeout-fix \
  --verify-worktree "python -m pytest tests/test_auth.py -q"
```

`run` refuses to execute with no `--files` unless `--no-files-ok` is passed. This is intentional: unscoped agent runs are usually token-wasteful and low quality.

### `antfarm context --files GLOB [--show]`

Preview context selection and rough token cost.

```bash
antfarm context --files "src/**/*.py" --files "tests/**/*.py"
antfarm context --files "src/main.py" --show
```

Output includes:

- file path,
- source size,
- included bytes,
- estimated tokens,
- skipped/truncated reason.

### `antfarm config show`

Print resolved repo-local config as JSON.

```bash
antfarm config show
```

### `antfarm config set KEY VALUE`

Set a top-level config key, a role model alias, or an `extra.*` key.

```bash
antfarm config set llm_base_url "http://127.0.0.1:8080/v1"
antfarm config set llm_max_tokens 4096
antfarm config set llm_temperature 0.2
antfarm config set max_context_bytes_total 120000
antfarm config set model_by_role.patch patch-qwen2.5-coder-14b
antfarm config set extra.notes '"local workstation profile"'
```

Values are parsed as JSON when possible; otherwise they are kept as strings.

### `antfarm ant OBJ_ID ROLE --target TEXT --files GLOB...`

Run one scoped worker.

```bash
antfarm ant 1 debug \
  --target "Find likely null-handling bugs in request parsing" \
  --files "src/request.py" \
  --files "tests/test_request.py"
```

Valid roles:

```text
debug trace test patch review risk
```

### `antfarm wave OBJ_ID --ants ROLES --target TEXT --files GLOB...`

Run multiple workers concurrently.

```bash
antfarm wave 1 \
  --ants debug,trace,risk \
  --target "Investigate failing checkout tests" \
  --files "src/checkout/**/*.py" \
  --files "tests/test_checkout.py" \
  --max-workers 3
```

### `antfarm queen OBJ_ID`

Synthesize worker reports. Queen input is compacted to control token usage:

- large patch diffs become summaries plus previews,
- long findings are truncated,
- command/risk/memory lists are capped,
- total report payload is capped by `queen_max_report_bytes`,
- accepted memory candidates are included.

```bash
antfarm queen 1
```

### `antfarm verify --objective-id OBJ_ID --cmd COMMAND`

Run a deterministic verifier command in the target repo and persist stdout/stderr/exit code/duration.

```bash
antfarm verify --objective-id 1 --cmd "python -m pytest -q"
```

> Security note: verifier commands currently run through the host shell with `shell=True`. Treat `--cmd` as a trusted local power-user interface.

### `antfarm task TASK_ID`

Show task metadata, JSON report, and patch candidate if present.

```bash
antfarm task 42
```

### `antfarm apply TASK_ID --branch BRANCH`

Apply a validated patch candidate into `.antfarm/worktrees/<branch>`.

```bash
antfarm apply 42 --branch antfarm-fix-auth
```

Main checkout remains unchanged.

### `antfarm verify-worktree TASK_ID --cmd COMMAND`

Run a verifier command inside the worktree where a task's patch was applied.

```bash
antfarm verify-worktree 42 --cmd "python -m pytest tests/test_auth.py -q"
```

### `antfarm mission OBJ_ID ...`

Run an end-to-end animated flow:

```text
worker wave -> optional repo verifier -> Queen -> optional PatchAnt -> optional apply -> optional worktree verifier
```

Example:

```bash
antfarm mission 1 \
  --target "Fix the add() function" \
  --files "pkg/math_bug.py" \
  --ants debug,trace,risk \
  --verify "python -m pytest tests/check_math.py" \
  --patch-target "Fix add() to return a + b" \
  --patch-files "pkg/math_bug.py" \
  --apply-branch mission-fix \
  --verify-worktree "python -m pytest tests/check_math.py"
```

### `antfarm memory list|accept|reject`

Review and gate worker-proposed memory.

```bash
antfarm memory list
antfarm memory list --objective-id 1 --status proposed
antfarm memory accept 12
antfarm memory reject 13
```

### `antfarm status`

Show repo, endpoint, model routing, LLM/context budgets, objectives, recent tasks, and verifier results.

```bash
antfarm status
```

---

## Configuration

Default config:

```json
{
  "llm_base_url": "http://127.0.0.1:8080/v1",
  "model": "queen-qwen3.6-35b-a3b",
  "model_by_role": {
    "debug": "worker-qwen3.5-9b",
    "trace": "worker-qwen3.5-9b",
    "risk": "worker-qwen3.5-9b",
    "review": "worker-qwen3.5-9b",
    "test": "fast-coder-qwen2.5-7b",
    "patch": "patch-qwen2.5-coder-14b",
    "queen": "queen-qwen3.6-35b-a3b"
  },
  "llm_timeout_seconds": 120.0,
  "llm_temperature": 0.2,
  "llm_max_tokens": 4096,
  "verifier_timeout_seconds": 300.0,
  "max_context_bytes_per_file": 20000,
  "max_context_bytes_total": 120000,
  "max_context_files": 60,
  "queen_max_report_bytes": 24000,
  "created_by": "antfarm",
  "extra": {}
}
```

### Important knobs

| Key | Purpose |
|-----|---------|
| `llm_base_url` | OpenAI-compatible API base URL. |
| `model` | fallback model alias. |
| `model_by_role.*` | per-role model routing. |
| `llm_timeout_seconds` | HTTP timeout for model calls. |
| `llm_temperature` | chat-completions temperature sent by Ant Farm. |
| `llm_max_tokens` | output token cap sent by Ant Farm. |
| `verifier_timeout_seconds` | timeout for verifier subprocesses. |
| `max_context_bytes_per_file` | per-file context byte cap. |
| `max_context_bytes_total` | total context byte cap per worker call. |
| `max_context_files` | maximum included text files per worker call. |
| `queen_max_report_bytes` | hard cap for compacted Queen report payload. |

---

## Context and Token Budgeting

Ant Farm deliberately makes context explicit. Workers only receive files selected by `--files` globs.

The context builder:

1. expands repo-relative globs,
2. rejects absolute paths and `..` escapes,
3. excludes `.git`, `.antfarm`, virtualenvs, `node_modules`, and caches,
4. skips probable binary/non-text files,
5. caps included text files with `max_context_files`,
6. caps each file with `max_context_bytes_per_file`,
7. caps total bytes with `max_context_bytes_total`,
8. reports a rough token estimate using bytes/4.

Example:

```bash
antfarm context --files "src/**/*.py" --files "tests/**/*.py"
```

Use `--show` only when you need to audit the exact rendered prompt context.

```bash
antfarm context --files "src/app.py" --show
```

This is the main defense against token waste and accidental whole-repo prompts.

---

## Agentic Memory Model

Ant Farm treats model memory as untrusted until reviewed.

Flow:

1. Worker returns `memory_candidates` in its JSON report.
2. Candidates are stored with status `proposed`.
3. You inspect them:

   ```bash
   antfarm memory list --status proposed
   ```

4. You accept durable, accurate facts:

   ```bash
   antfarm memory accept 12
   ```

5. You reject noisy/stale/wrong facts:

   ```bash
   antfarm memory reject 13
   ```

6. QueenAnt receives accepted memory for the same objective during synthesis.

This prevents common agentic-memory failure modes:

- stale assumptions becoming permanent,
- hallucinated project facts being reused,
- verbose memories bloating every prompt,
- unreviewed implementation preferences biasing future decisions.

---

## Patch and Worktree Safety

Patch lifecycle:

```text
PatchAnt JSON -> patch_diff -> normalize -> git apply --check -> patch_candidates row
       -> antfarm task -> antfarm apply --branch X -> git worktree add -> git apply --check -> git apply
       -> antfarm verify-worktree
```

Safety controls:

- non-patch roles cannot store patch diffs,
- patch diffs must apply cleanly before storage,
- application happens only in `.antfarm/worktrees`,
- branch names are sanitized,
- existing worktree paths are refused,
- Ant Farm refuses to run from inside managed worktrees,
- main checkout is never modified by `antfarm apply`.

---

## SQLite Blackboard

The blackboard stores durable state for auditability and recovery.

Current tables:

| Table | Purpose |
|-------|---------|
| `objectives` | top-level user goals. |
| `tasks` | individual ant/queen work items and status. |
| `reports` | structured JSON reports and raw model responses. |
| `events` | append-only operational event log. |
| `verifier_results` | verifier command results, stdout, stderr, timing. |
| `patch_candidates` | validated diffs and worktree application status. |
| `memory_candidates` | proposed/accepted/rejected memory facts. |

Inspect manually if needed:

```bash
sqlite3 .antfarm/blackboard.sqlite3 '.tables'
sqlite3 .antfarm/blackboard.sqlite3 'select id, role, status, target from tasks order by id desc limit 10;'
```

---

## Development

Install:

```bash
pip install -e ".[dev]"
```

Run checks:

```bash
python -m pytest -q
python -m compileall -q antfarm
python -m antfarm --help
```

Current test coverage includes:

- repo resolution from subdirectories,
- config persistence and validation,
- context budget planning,
- binary skip behavior,
- JSON extraction from messy model output,
- isolated git worktree patch application,
- CLI `init`, `doctor`, `context`, `config`, and `memory`,
- Queen report compaction.

See [`docs/REVIEW.md`](docs/REVIEW.md) for the dense technical review and upgrade plan.

---

## Roadmap: Rust Core

Ant Farm should not be rewritten in one pass. The intended path is incremental:

1. keep the Python Typer CLI while introducing a small Rust workspace,
2. expose Rust functionality through a JSON-over-stdio `antfarm-core` binary,
3. migrate high-value internals one at a time,
4. later decide whether the full CLI should move to Rust `clap`.

Best Rust candidates:

| Component | Why Rust helps |
|-----------|----------------|
| Context builder | faster gitignore-aware walking, binary detection, exact budget accounting. |
| Patch gate | robust unified-diff parsing, diagnostics, hunk/file validation. |
| Sandbox manager | safer process/worktree lifecycle, cleanup, branch validation. |
| Verifier runner | argv-safe commands, streaming logs, cancellation. |
| SQLite state | typed migrations, stronger concurrency guarantees. |

Likely crates:

- `clap`
- `serde`, `serde_json`
- `rusqlite` or `sqlx`
- `ignore`, `globset`, `walkdir`
- `gix` or controlled `git` subprocesses
- `reqwest`
- `tokio`
- `thiserror`, `anyhow`, `miette`

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|--------------|-----|
| `Ant Farm is not initialized` | No `.antfarm/config.json` in current/parent repo. | Run `antfarm init --repo .`. |
| `LLM endpoint failed` in doctor | llama.cpp/OpenAI-compatible endpoint is down or wrong URL. | Start server or update `llm_base_url`. |
| `Model alias not found` | Router config lacks the alias in `model_by_role`. | Update router INI or `antfarm config set model_by_role.<role> ...`. |
| `No files were included` | Missing/incorrect `--files` globs. | Run `antfarm context --files ...` first. |
| `One or more --files globs matched no files` | Mission preflight rejects unmatched globs. | Fix repo-relative glob paths. |
| `Patch rejected before storage` | Model produced malformed/stale diff. | Narrow files, rerun PatchAnt, inspect risks. |
| `Worktree already exists` | Branch/worktree path collision. | Use a new branch name or clean old worktree manually. |
| `Refusing to run from inside .antfarm/worktrees` | CWD is inside a managed worktree. | `cd` back to the original repo. |
| `Verifier timed out` | Command exceeded configured timeout. | Tune `verifier_timeout_seconds` or narrow the command. |

---

## Limitations

- Verifier commands currently use `shell=True`; only run trusted commands.
- SQLite schema creation is currently simple `CREATE TABLE IF NOT EXISTS`; versioned migrations are planned.
- Context token counts are estimates, not tokenizer-exact counts.
- Patch normalization is heuristic; robust diff parsing is a prime Rust-core target.
- Cross-repository tasks are not supported.
- Model quality depends heavily on your local GGUFs and router settings.
- Windows support is not a primary target yet; git worktree and shell behavior may differ.

---

## License

Ant Farm source code is licensed under the [MIT License](LICENSE). Model weights are not included and are governed by their respective licenses.
