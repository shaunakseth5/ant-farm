# Ant Farm

**Ant Farm** is a local, repo-scoped multi-agent coding CLI. It spawns specialized "ant" processes that collaborate to understand, fix, and verify code — all running entirely on your machine against a local llama.cpp inference server.

## Why narrow worker ants?

Instead of one large model trying to do everything, Ant Farm uses **narrow specialists**: each ant role (debug, trace, risk, review, test, patch) runs with a dedicated model tuned for its job. This gives you:

- **Focused context** — each ant receives only the files relevant to its role.
- **Role-appropriate models** — small fast models handle analysis; larger models handle synthesis and patching.
- **Isolation** — agents can fail independently without corrupting one another's output.
- **Parallelism** — waves of worker ants run concurrently, speeding up exploration.

Think of it as a code colony: debug ants scout the problem space, trace ants follow the error trail, risk ants flag dangerous changes, review ants sanity-check reasoning, test ants validate behavior, and a queen ant orchestrates everything into a coherent plan.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    User (Terminal)                       │
│                   antfarm CLI                             │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP /v1/chat/completions
                         ▼
┌──────────────────────────────────────────────────────────┐
│                llama.cpp Router                          │
│              (http://127.0.0.1:8080/v1)                  │
│  Routes model aliases → local GGUF models                 │
└────────────────────────┬─────────────────────────────────┘
                         │
    ┌────────────────────┼────────────────────┐
    ▼                    ▼                    ▼
┌────────┐         ┌──────────┐        ┌──────────┐
│ Debug  │         │ Patch    │        │ Queen    │
│ Trace  │   ...   │ Test     │        │ Review   │
│ Risk   │         │ Risk     │        │ (synthesis)│
│ Review │         │ Debug    │        │          │
└────────┘         └──────────┘        └──────────┘

State layer: SQLite blackboard (.antfarm/blackboard.sqlite3)
Patch layer: git worktrees under .antfarm/worktrees/
```

- **SQLite Blackboard** — objectives, tasks, reports, events, verifier results, patch candidates, and memory candidates are persisted in `.antfarm/blackboard.sqlite3`.
- **Worker Ants** — `debug`, `trace`, `test`, `patch`, `review`, `risk`. Each receives scoped file context only and must return strict JSON. Workers cannot write durable memory; they may only propose memory candidates.
- **QueenAnt** — synthesizes worker reports, decides next steps, and delegates work.
- **Verifier** — runs deterministic shell commands (tests, linters) in the target repo or in an isolated worktree with timeouts.
- **Sandbox / Worktrees** — patches are never applied directly to the main checkout. All patch application happens inside git worktrees under `.antfarm/worktrees/`.

## Model Routing Table

| Role      | Model                       | Purpose                                |
|-----------|-----------------------------|----------------------------------------|
| `debug`   | worker-qwen3.5-9b           | Debug and error localization            |
| `trace`   | worker-qwen3.5-9b           | Trace execution paths and call chains    |
| `risk`    | worker-qwen3.5-9b           | Risk assessment of proposed changes      |
| `review`  | worker-qwen3.5-9b           | Review reasoning and logic              |
| `test`    | fast-coder-qwen2.5-7b       | Write and run tests                     |
| `patch`   | patch-qwen2.5-coder-14b     | Generate unified-diff patches           |
| `queen`   | queen-qwen3.6-35b-a3b       | Synthesis, planning, and coordination    |

The queen alias is also used as the fallback model for unknown roles. You can customize role-to-model mapping in `.antfarm/config.json`.

## llama.cpp Router Setup

Ant Farm expects an OpenAI-compatible endpoint at `http://127.0.0.1:8080/v1`. The recommended setup uses **llama.cpp's router mode** to serve multiple models from a single process and route by model alias.

### 1. Start the llama.cpp router

Create an INI config (see `configs/llama-router.antfarm.ini.example`):

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

Then launch the router:

```bash
llama-server \
  --config configs/llama-router.antfarm.ini.example \
  --host 127.0.0.1 \
  --port 8080
```

### 2. Simple (single-model) setup

If you only have one model, use the example script:

```bash
export MODEL_PATH=/path/to/your-model.gguf
bash scripts/run_llama_server_example.sh
```

### 3. Verify connectivity

```bash
curl -s http://127.0.0.1:8080/v1/models | python -m json.tool
```

## Install

**Prerequisites:** Python 3.10+, git, a llama.cpp build with router support.

```bash
# Clone the repo
git clone <repo-url>
cd antfarm_pi_build

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# Install in editable mode
pip install -e .
```

> **Model weights are not included.** You must provide your own GGUF files. See the routing table above and the llama.cpp router example for model paths.

## Quick Start: Toy Smoke Test

Use the included smoke repo to verify everything works end-to-end.

```bash
cd _smoke_repo

# 1. Initialize Ant Farm
antfarm init --repo .

# 2. Create an objective
antfarm objective "Fix the add() bug in pkg/math_bug.py"

# 3. Run a worker wave to investigate
antfarm wave 1 \
  --ants debug,trace,risk \
  --target "Find why add(2, 3) returns -1 instead of 5" \
  --files "pkg/math_bug.py"

# 4. Ask the Queen to synthesize and propose a fix
antfarm queen 1

# 5. Run the PatchAnt to generate a diff
antfarm ant 1 patch \
  --target "Propose a minimal fix for add()" \
  --files "pkg/math_bug.py"

# 6. Inspect the patch candidate
antfarm task <TASK_ID>

# 7. Apply the patch in an isolated worktree
antfarm apply <TASK_ID> --branch smoke-fix

# 8. Verify inside the worktree
antfarm verify-worktree <TASK_ID> --cmd "python -m pytest tests/check_math.py"

# 9. Check overall status
antfarm status
```

### One-shot mission

Ant Farm also supports an animated all-in-one mission:

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

## Basic Real-Repo Usage

```bash
# In any git repository:

# Initialize
antfarm init --repo .

# Define what you want to accomplish
antfarm objective "Refactor config loading to support YAML and JSON backends"

# Investigate
antfarm wave 1 \
  --ants debug,trace,risk \
  --target "Analyze current config.py structure and test coverage" \
  --files "antfarm/**/*.py" "tests/**/*.py"

# Synthesize plan
antfarm queen 1

# Generate a patch
antfarm ant 1 patch \
  --target "Implement YAML backend alongside existing JSON config loader" \
  --files "antfarm/config.py" "antfarm/__init__.py"

# Apply and verify in isolation
antfarm apply <task_id> --branch refactor-config \
&& antfarm verify-worktree <task_id> --cmd "python -m compileall -q antfarm"

# Monitor progress
antfarm status
```

## Command Reference

### `antfarm init --repo PATH`

Initialize repo-local `.antfarm/` state (config, database). Creates `.antfarm/config.json` and `.antfarm/blackboard.sqlite3`.

```bash
antfarm init --repo /path/to/my/repo
```

### `antfarm objective "TITLE"`

Create a new objective with a human-readable title. Returns the objective ID.

```bash
antfarm objective "Fix flaky test in auth module"
```

### `antfarm wave <OBJ_ID> --ants ROLES --target TGT [--files GLOB ...]`

Run a concurrent wave of worker ants. All ants share the same target description and file context but work independently.

```bash
antfarm wave 1 \
  --ants debug,trace,risk \
  --target "Investigate failing auth tests" \
  --files "tests/test_auth.py" "src/auth/**/*.py" \
  --max-workers 3
```

### `antfarm ant <OBJ_ID> <ROLE> --target TGT [--files GLOB ...]`

Run a single worker ant. Roles: `debug`, `trace`, `test`, `patch`, `review`, `risk`.

```bash
antfarm ant 1 patch \
  --target "Add input validation to login function" \
  --files "src/auth/login.py"
```

### `antfarm queen <OBJ_ID>`

Ask QueenAnt to synthesize all worker reports for the objective and decide the next steps. Returns a structured decision JSON.

```bash
antfarm queen 1
```

### `antfarm verify --objective-id <OBJ_ID> --cmd "SHELL_COMMAND"`

Run a deterministic shell command in the target repo and store the result (exit code, stdout, stderr, duration).

```bash
antfarm verify --objective-id 1 --cmd "python -m pytest tests/ -q"
```

### `antfarm verify-worktree <TASK_ID> --cmd "SHELL_COMMAND"`

Run a verifier command inside the isolated git worktree where a patch was applied. Used to validate patches without touching the main checkout.

```bash
antfarm verify-worktree 42 --cmd "python -m pytest tests/test_auth.py"
```

### `antfarm mission <OBJ_ID> --target TGT [--files GLOB ...]`

Run an animated end-to-end mission: worker wave → optional verifier → QueenAnt → optional PatchAnt → optional worktree verification.

| Flag               | Description                                          |
|--------------------|------------------------------------------------------|
| `--obj-id`         | Objective to run against (positional arg)            |
| `--target`, `-t`   | Shared target description for the worker wave        |
| `--files`, `-f`    | File glob(s); each must match at least one file      |
| `--ants`           | Comma-separated roles for the worker wave            |
| `--max-workers`    | Max concurrent ants (default: 3)                     |
| `--verify`         | Optional verifier command after worker wave          |
| `--patch-target`   | If set, runs PatchAnt after Queen                    |
| `--patch-files`    | File globs for the patch ant (defaults to --files)   |
| `--apply-branch`   | Branch name for the isolated worktree                |
| `--verify-worktree`| Verifier command run inside the applied worktree     |

```bash
antfarm mission 1 \
  --target "Refactor error handling" \
  --files "src/**/*.py" \
  --ants debug,trace,risk,review \
  --verify "python -m compileall -q src" \
  --patch-target "Centralize all error paths through AppError" \
  --apply-branch refactor-errors \
  --verify-worktree "python -m pytest tests/ -x -q"
```

### `antfarm task <TASK_ID>`

Show details of a single task: report JSON, status, target, and any associated patch candidate with its unified diff.

```bash
antfarm task 42
```

### `antfarm apply <TASK_ID> --branch BRANCH_NAME`

Apply a patch candidate inside an isolated git worktree under `.antfarm/worktrees/`. The main checkout is never modified. Patches are validated with `git apply --check` before application.

```bash
antfarm apply 42 --branch my-fix-branch
```

### `antfarm status`

Display a comprehensive dashboard: repo info, LLM endpoint, model routing table, objectives, recent tasks, and verifier results.

```bash
antfarm status
```

## Safety Model

Ant Farm is designed with security-by-default in mind:

1. **No direct patch application** — `antfarm apply` creates a git worktree under `.antfarm/worktrees/`. Patches are never applied to the main repository checkout. You must explicitly choose the branch name and inspect diffs before applying.

2. **Worktree isolation** — Ant Farm refuses to run any command from inside `.antfarm/worktrees/`. All commands must be run from the original repository root.

3. **Patch validation** — Every patch candidate is validated with `git apply --check` inside the worktree before being applied. If the diff does not apply cleanly, the operation fails silently and the task is marked failed.

4. **Unified-diff normalizer** — Patch diffs are normalized to a consistent unified-diff format before storage and application, ensuring compatibility across platforms.

5. **File glob enforcement** — Every mission `--files` glob must match at least one existing file in the repository. Blind wildcards that expand to nothing are rejected.

6. **No patch, no apply** — If PatchAnt produces no validated diff for a task, the mission stops before attempting any worktree creation or application.

7. **Verifiers run independently** — Deterministic verifier commands are executed with configurable timeouts and do not have network access beyond what the host shell provides.

8. **Branch name sanitization** — Branch names are validated to contain only safe characters (letters, numbers, `.`, `_`, `-`, `/`) and must not contain `..`.

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| `Endpoint connection failed` | llama.cpp server not running | Verify `http://127.0.0.1:8080/v1/models` returns JSON |
| `Model alias not found in router` | Model alias missing from router INI | Check `configs/llama-router.antfarm.ini.example` for correct names |
| `Worktree already exists` | Branch name collision | Use a different `--branch` value or clean old worktrees manually |
| `Refusing to run from inside .antfarm/worktrees` | CWD is inside a worktree | `cd` back to the repo root |
| `One or more --files globs matched no files` | Typo in glob path | Verify the glob matches files relative to the repo root |
| `Patch did not apply cleanly` | Diff context mismatch or stale repo | Check the diff with `antfarm task <id>`, then re-apply from a clean HEAD |
| `Verifier timed out` | Slow command or test suite | Increase `verifier_timeout_seconds` in `config.json` |

## Limitations

- **Requires git** — Ant Farm uses git worktrees for isolated patch application. A bare repo without git support is not supported.
- **Local inference only** — Ant Farm connects to a local llama.cpp endpoint. No cloud LLM providers are supported out of the box.
- **Model weights not included** — You must download and manage your own GGUF model files.
- **Single-repo scope** — Each `antfarm` invocation operates within one repository. Cross-repo operations are not supported.
- **No interactive editing** — Patch ants generate diffs; there is no inline editor or interactive patch selection built in.
- **Linux/macOS primary support** — While Python itself is cross-platform, git worktree behavior and shell verifier commands may vary on Windows.

## License

Ant Farm source code is licensed under the [MIT License](LICENSE). Model weights referenced by the router are provided separately by their respective owners and carry their own licenses.
