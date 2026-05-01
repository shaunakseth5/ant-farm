# Ant Farm

Ant Farm is an independent local multi-agent coding system. It stores repo-local state in `TARGET_REPO/.antfarm/`, talks to a llama.cpp OpenAI-compatible endpoint, and never applies generated patches unless you explicitly request an isolated git worktree.

Defaults:

- API: `http://127.0.0.1:8080/v1`
- Model alias: `qwen3.6-35b-a3b`
- State: `.antfarm/blackboard.sqlite3`

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick start

```bash
antfarm init --repo .
antfarm objective "Fix failing tests"
antfarm wave 1 --ants debug,trace,risk --target "Investigate current failure" --files "antfarm/**/*.py" --max-workers 3
antfarm queen 1
antfarm verify --objective-id 1 --cmd "python -m compileall -q antfarm"
antfarm status
```

Run a single ant:

```bash
antfarm ant 1 patch --target "Propose a minimal fix" --files "antfarm/cli.py"
```

Inspect and apply a patch candidate only in an isolated git worktree:

```bash
antfarm task 2
antfarm apply 2 --branch antfarm/task-2-candidate
```

Worktrees are created under `.antfarm/worktrees/`. The main checkout is not modified by `antfarm apply`.

## Architecture

- SQLite blackboard: objectives, tasks, reports, events, verifier results, patch candidates, and memory candidates.
- Narrow worker ants: `debug`, `trace`, `test`, `patch`, `review`, `risk`.
- QueenAnt synthesizes worker reports and proposes next steps.
- Workers receive scoped file context only and must return strict JSON.
- Workers cannot write durable memory; they may only propose memory candidates.
- Patch workers may propose unified diffs; Ant Farm stores them as patch candidates.
- Verifier commands are deterministic shell commands run in the target repo with timeouts.

## llama.cpp

Start a llama.cpp OpenAI-compatible server with the alias `qwen3.6-35b-a3b`. See `scripts/run_llama_server_example.sh` for an example.

## Configuration

`antfarm init --repo PATH` writes `PATH/.antfarm/config.json`. An example is available in `configs/antfarm.example.json`.
