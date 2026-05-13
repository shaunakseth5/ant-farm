from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, List, Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from . import __version__, db
from .config import AntFarmConfig, config_path, db_path, ensure_repo_initialized, load_config, resolve_repo, save_config
from .config import assert_not_inside_antfarm_worktree, is_inside_antfarm_worktree
from .orchestrator import WORKER_ROLES, run_ant, run_queen, run_wave
from .repo import build_context_plan, render_context
from .sandbox import apply_task_patch
from .verifier import run_verifier, run_verifier_in_dir

app = typer.Typer(help="Ant Farm: local repo-scoped multi-agent coding system")
config_app = typer.Typer(help="Inspect and edit repo-local Ant Farm configuration")
memory_app = typer.Typer(help="Review and promote worker memory candidates")
app.add_typer(config_app, name="config")
app.add_typer(memory_app, name="memory")
console = Console()


def _repo() -> Path:
    repo = resolve_repo()
    try:
        assert_not_inside_antfarm_worktree(repo)
        ensure_repo_initialized(repo)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    return repo


def _matched_repo_files(repo: Path, patterns: List[str]) -> List[str]:
    matched: list[str] = []
    for pattern in patterns:
        for path in repo.glob(pattern):
            if path.is_file() and ".antfarm" not in path.parts and ".git" not in path.parts:
                matched.append(str(path.relative_to(repo)))
    return sorted(set(matched))


def _print_json(data: object) -> None:
    console.print(Syntax(json.dumps(data, indent=2, sort_keys=True), "json", word_wrap=True))


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _config_to_dict(cfg: AntFarmConfig) -> dict[str, Any]:
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump()
    return json.loads(cfg.json())


def _parse_config_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


@app.callback()
def callback(version: bool = typer.Option(False, "--version", help="Show version and exit.")) -> None:
    if version:
        console.print(f"antfarm {__version__}")
        raise typer.Exit()


@app.command("init")
def init_cmd(repo: Path = typer.Option(..., "--repo", help="Target repository path.")) -> None:
    """Initialize repo-local .antfarm state."""
    target = resolve_repo(repo)
    target.mkdir(parents=True, exist_ok=True)
    cfg = AntFarmConfig()
    save_config(target, cfg)
    with db.connect(target) as conn:
        db.init_db(conn)
        db.add_event(conn, "system.initialized", "Ant Farm initialized")
    console.print(f"[green]Initialized Ant Farm[/green] in {target / '.antfarm'}")


@app.command("objective")
def objective_cmd(title: str = typer.Argument(..., help="Objective title.")) -> None:
    """Create an objective."""
    repo = _repo()
    with db.connect(repo) as conn:
        db.init_db(conn)
        objective_id = db.create_objective(conn, title)
    console.print(f"[green]Objective {objective_id}[/green]: {title}")


@config_app.command("show")
def config_show_cmd() -> None:
    """Print the repo-local configuration as JSON."""
    repo = _repo()
    _print_json(_config_to_dict(load_config(repo)))


@config_app.command("set")
def config_set_cmd(
    key: str = typer.Argument(..., help="Top-level key, model_by_role.<role>, or extra.<key>."),
    value: str = typer.Argument(..., help="JSON value or raw string."),
) -> None:
    """Set one repo-local configuration value with validation."""
    repo = _repo()
    cfg = load_config(repo)
    data = _config_to_dict(cfg)
    parsed = _parse_config_value(value)

    if key.startswith("model_by_role."):
        role = key.split(".", 1)[1]
        if not role:
            console.print("[red]Role name is required after model_by_role.[/red]")
            raise typer.Exit(1)
        data.setdefault("model_by_role", {})[role] = str(parsed)
    elif key.startswith("extra."):
        extra_key = key.split(".", 1)[1]
        if not extra_key:
            console.print("[red]Key name is required after extra.[/red]")
            raise typer.Exit(1)
        data.setdefault("extra", {})[extra_key] = parsed
    elif key in data and not isinstance(data.get(key), dict):
        data[key] = parsed
    else:
        console.print(f"[red]Unknown or unsupported config key: {key}[/red]")
        raise typer.Exit(1)

    try:
        updated = AntFarmConfig(**data)
    except Exception as exc:
        console.print(f"[red]Invalid config value for {key}: {exc}[/red]")
        raise typer.Exit(1)
    save_config(repo, updated)
    console.print(f"[green]Updated[/green] {key} = {json.dumps(parsed)}")


@memory_app.command("list")
def memory_list_cmd(
    objective_id: Optional[int] = typer.Option(None, "--objective-id", help="Filter by objective id."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status: proposed, accepted, rejected."),
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Maximum rows to show."),
) -> None:
    """List worker-proposed memory candidates without automatically trusting them."""
    repo = _repo()
    with db.connect(repo) as conn:
        rows = db.list_memory_candidates(conn, objective_id=objective_id, status=status, limit=limit)
    table = Table(title="Memory candidates")
    table.add_column("ID")
    table.add_column("Obj")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Text")
    for row in rows:
        table.add_row(str(row["id"]), str(row["objective_id"]), str(row["task_id"]), row["status"], row["text"])
    console.print(table)


@memory_app.command("accept")
def memory_accept_cmd(memory_id: int = typer.Argument(..., help="Memory candidate id.")) -> None:
    """Mark a memory candidate as accepted for future reuse."""
    _set_memory_status(memory_id, "accepted")


@memory_app.command("reject")
def memory_reject_cmd(memory_id: int = typer.Argument(..., help="Memory candidate id.")) -> None:
    """Reject a memory candidate so it is not reused."""
    _set_memory_status(memory_id, "rejected")


def _set_memory_status(memory_id: int, status: str) -> None:
    repo = _repo()
    try:
        with db.connect(repo) as conn:
            db.update_memory_status(conn, memory_id, status)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Memory {memory_id} marked {status}[/green]")


@app.command("doctor")
def doctor_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository to inspect. Defaults to nearest initialized parent."),
    check_llm: bool = typer.Option(True, "--llm/--no-llm", help="Check the configured OpenAI-compatible LLM endpoint."),
) -> None:
    """Check local Ant Farm setup, git state, database, and LLM connectivity."""
    target = resolve_repo(repo)
    table = Table(title=f"Ant Farm doctor: {target}")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Details")
    failed = False

    def add_check(name: str, ok: bool, details: str) -> None:
        nonlocal failed
        if not ok:
            failed = True
        table.add_row(name, "[green]ok[/green]" if ok else "[red]fail[/red]", details)

    add_check("repo directory", target.is_dir(), str(target))
    add_check("not managed worktree", not is_inside_antfarm_worktree(target), "path is outside .antfarm/worktrees")

    git = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    add_check("git repository", git.returncode == 0, (git.stdout or git.stderr).strip())

    cfg = None
    cfg_file = config_path(target)
    add_check("config", cfg_file.is_file(), str(cfg_file))
    if cfg_file.is_file():
        try:
            cfg = load_config(target)
            add_check("config parse", True, f"endpoint={cfg.llm_base_url} fallback_model={cfg.model}")
        except Exception as exc:
            add_check("config parse", False, str(exc))

    database = db_path(target)
    add_check("blackboard database", database.is_file(), str(database))
    if database.is_file():
        try:
            with db.connect(target) as conn:
                db.init_db(conn)
                objective_count = conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
                task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            add_check("database schema", True, f"objectives={objective_count} tasks={task_count}")
        except Exception as exc:
            add_check("database schema", False, str(exc))

    if check_llm and cfg is not None:
        url = f"{cfg.llm_base_url.rstrip('/')}/models"
        try:
            response = httpx.get(url, timeout=3.0)
            add_check("LLM endpoint", response.status_code < 400, f"GET {url} -> HTTP {response.status_code}")
        except Exception as exc:
            add_check("LLM endpoint", False, f"GET {url} failed: {exc}")

    console.print(table)
    if failed:
        raise typer.Exit(1)


@app.command("context")
def context_cmd(
    files: List[str] = typer.Option([], "--files", "-f", help="Repo-relative file glob. Repeat for multiple globs."),
    show: bool = typer.Option(False, "--show", help="Print the exact context that would be sent to an ant."),
) -> None:
    """Preview scoped file context and token-budget impact before spending LLM calls."""
    repo = _repo()
    cfg = load_config(repo)
    try:
        plan = build_context_plan(repo, files, cfg)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    table = Table(title="Context budget preview")
    table.add_column("File")
    table.add_column("Size", justify="right")
    table.add_column("Included", justify="right")
    table.add_column("Est. tokens", justify="right")
    table.add_column("Status")
    for entry in plan.entries:
        status = entry.skipped_reason or ("truncated" if entry.truncated else "included")
        style = "red" if entry.skipped_reason else ("yellow" if entry.truncated else "green")
        table.add_row(
            entry.path,
            _format_bytes(entry.size_bytes),
            _format_bytes(entry.included_bytes),
            str(entry.estimated_tokens),
            f"[{style}]{status}[/{style}]",
        )
    console.print(table)
    console.print(
        f"Included {plan.included_files} file(s), skipped {plan.skipped_files}; "
        f"{_format_bytes(plan.total_included_bytes)} / {_format_bytes(cfg.max_context_bytes_total)} budget; "
        f"~{plan.estimated_tokens} tokens."
    )
    if not files:
        console.print("[yellow]No --files globs were provided; ants would receive no file contents.[/yellow]")
    if show:
        console.print(Panel(render_context(plan), title="Rendered context"))


@app.command("ant")
def ant_cmd(
    objective_id: int = typer.Argument(..., help="Objective id."),
    role: str = typer.Argument(..., help=f"Worker role: {', '.join(sorted(WORKER_ROLES))}."),
    target: str = typer.Option(..., "--target", "-t", help="Narrow target for this ant."),
    files: Optional[List[str]] = typer.Option(None, "--files", "-f", help="Repo-relative file glob. Repeat for multiple globs."),
) -> None:
    """Run one scoped worker ant."""
    repo = _repo()
    result = run_ant(repo, objective_id, role, target, files or [])
    console.print(f"[green]Task {result['task_id']}[/green] status={result['status']}")
    _print_json(result["report"])


@app.command("wave")
def wave_cmd(
    objective_id: int = typer.Argument(..., help="Objective id."),
    ants: str = typer.Option("debug,trace,risk", "--ants", help="Comma-separated worker roles."),
    target: str = typer.Option(..., "--target", "-t", help="Shared target for this wave."),
    files: Optional[List[str]] = typer.Option(None, "--files", "-f", help="Repo-relative file glob. Repeat for multiple globs."),
    max_workers: int = typer.Option(3, "--max-workers", min=1, help="Maximum concurrent ants."),
) -> None:
    """Run a concurrent wave of scoped worker ants."""
    repo = _repo()
    roles = [item.strip() for item in ants.split(",")]
    results = run_wave(repo, objective_id, roles, target, files or [], max_workers)
    table = Table(title="Wave results")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Role")
    table.add_column("Summary")
    for item in results:
        table.add_row(str(item["task_id"]), item["status"], item["report"].get("role", ""), item["report"].get("summary", ""))
    console.print(table)


@app.command("queen")
def queen_cmd(objective_id: int = typer.Argument(..., help="Objective id.")) -> None:
    """Ask QueenAnt to synthesize worker reports and decide next steps."""
    repo = _repo()
    result = run_queen(repo, objective_id)
    console.print(f"[green]Queen task {result['task_id']}[/green] status={result['status']}")
    _print_json(result["decision"])


@app.command("verify")
def verify_cmd(
    objective_id: int = typer.Option(..., "--objective-id", help="Objective id."),
    cmd: str = typer.Option(..., "--cmd", help="Deterministic shell command to run in the repo."),
) -> None:
    """Run a deterministic verifier command and store the result."""
    repo = _repo()
    result = run_verifier(repo, objective_id, cmd)
    color = "green" if result["exit_code"] == 0 else "red"
    console.print(f"[{color}]Verifier {result['id']} exit={result['exit_code']} timed_out={result['timed_out']} duration={result['duration_seconds']:.2f}s[/{color}]")
    if result["stdout"]:
        console.print(Panel(result["stdout"], title="stdout"))
    if result["stderr"]:
        console.print(Panel(result["stderr"], title="stderr"))


@app.command("verify-worktree")
def verify_worktree_cmd(
    task_id: int = typer.Argument(..., help="Task id containing an applied patch candidate."),
    cmd: str = typer.Option(..., "--cmd", help="Deterministic shell command to run in the isolated worktree."),
) -> None:
    """Run a verifier command inside the isolated worktree for a patch task."""
    repo = _repo()
    with db.connect(repo) as conn:
        task = db.get_task(conn, task_id)
        patch = db.patch_for_task(conn, task_id)

    if patch is None:
        console.print(f"[red]Task {task_id} has no patch candidate.[/red]")
        raise typer.Exit(1)

    if not patch["worktree_path"]:
        console.print(f"[red]Task {task_id} patch has not been applied to a worktree.[/red]")
        raise typer.Exit(1)

    worktree = Path(patch["worktree_path"]).resolve()
    if not worktree.is_dir():
        console.print(f"[red]Recorded worktree does not exist: {worktree}[/red]")
        raise typer.Exit(1)

    result = run_verifier_in_dir(repo, task["objective_id"], cmd, worktree)
    color = "green" if result["exit_code"] == 0 else "red"
    console.print(f"[{color}]Worktree verifier {result['id']} exit={result['exit_code']} timed_out={result['timed_out']} duration={result['duration_seconds']:.2f}s[/{color}]")
    console.print(f"Worktree: {worktree}")
    if result["stdout"]:
        console.print(Panel(result["stdout"], title="stdout"))
    if result["stderr"]:
        console.print(Panel(result["stderr"], title="stderr"))


@app.command("mission")
def mission_cmd(
    objective_id: int = typer.Argument(..., help="Objective id to run the animated Ant Farm mission against."),
    target: str = typer.Option(..., "--target", "-t", help="Narrow target for the worker wave."),
    files: List[str] = typer.Option([], "--files", "-f", help="Repo-relative file glob. Repeat for multiple globs."),
    ants: str = typer.Option("debug,trace,risk", "--ants", help="Comma-separated worker roles for the first wave."),
    max_workers: int = typer.Option(3, "--max-workers", help="Maximum parallel ants for the worker wave."),
    verify: Optional[str] = typer.Option(None, "--verify", help="Optional verifier command to run after the worker wave."),
    patch_target: Optional[str] = typer.Option(None, "--patch-target", help="Optional PatchAnt target. If set, runs PatchAnt after Queen."),
    patch_files: List[str] = typer.Option([], "--patch-files", help="Optional patch file globs. Defaults to --files."),
    apply_branch: Optional[str] = typer.Option(None, "--apply-branch", help="Optional branch name for isolated worktree patch application."),
    verify_worktree: Optional[str] = typer.Option(None, "--verify-worktree", help="Optional verifier command to run inside the applied worktree."),
) -> None:
    """Run an animated Ant Farm mission: wave -> verify -> Queen -> optional patch -> optional worktree verify."""
    repo = _repo()
    roles = [role.strip() for role in ants.split(",") if role.strip()]
    if not roles:
        console.print("[red]No ant roles provided.[/red]")
        raise typer.Exit(1)

    matched_files = _matched_repo_files(repo, files)
    missing_globs = [pattern for pattern in files if not _matched_repo_files(repo, [pattern])]
    if files and (not matched_files or missing_globs):
        console.print(Panel(
            "[bold red]One or more --files globs matched no files.[/bold red]\n"
            f"Repo: {repo}\n"
            f"Globs: {', '.join(files)}\n"
            f"Missing globs: {', '.join(missing_globs) if missing_globs else '(all)'}\n\n"
            "Fix the globs or cd to the correct repository root before running mission.",
            title="Ant Farm preflight failed",
        ))
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold yellow]🐜 Ant Farm Mission[/bold yellow]\n"
        f"Objective: [bold]{objective_id}[/bold]\n"
        f"Target: {target}\n"
        f"Ants: {', '.join(roles)}\n"
        f"Files: {', '.join(files) if files else '(none)'}\n"
        f"Matched files: {len(matched_files)}",
        title="colony launch",
    ))

    with console.status("[bold yellow]🐜🐜🐜 worker ants crawling through the codebase...[/bold yellow]", spinner="dots"):
        wave = run_wave(repo, objective_id, roles, target, files, max_workers)

    table = Table(title="🐜 Worker wave results")
    table.add_column("Task")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Summary")
    for result in wave:
        report = result.get("report") or {}
        table.add_row(str(result["task_id"]), report.get("role", ""), result["status"], report.get("summary", ""))
    console.print(table)

    if verify:
        with console.status("[bold cyan]🧪 verifier ants running deterministic checks...[/bold cyan]", spinner="bouncingBar"):
            verify_result = run_verifier(repo, objective_id, verify)
        color = "green" if verify_result["exit_code"] == 0 else "red"
        console.print(f"[{color}]Verifier {verify_result['id']} exit={verify_result['exit_code']} timed_out={verify_result['timed_out']} duration={verify_result['duration_seconds']:.2f}s[/{color}]")
        if verify_result["stdout"]:
            console.print(Panel(verify_result["stdout"], title="verifier stdout"))
        if verify_result["stderr"]:
            console.print(Panel(verify_result["stderr"], title="verifier stderr"))

    with console.status("[bold magenta]👑 QueenAnt reviewing the colony reports...[/bold magenta]", spinner="moon"):
        queen_result = run_queen(repo, objective_id)
    console.print(f"[green]Queen task {queen_result['task_id']}[/green] status={queen_result['status']}")
    _print_json(queen_result["decision"])
    if queen_result["status"] != "done":
        console.print(Panel("[bold red]Queen failed; stopping mission before patch phase.[/bold red]", title="Ant Farm"))
        raise typer.Exit(1)

    patch_task_id = None
    if patch_target:
        pf = patch_files or files
        with console.status("[bold yellow]🛠️ PatchAnt forging a gated diff...[/bold yellow]", spinner="line"):
            patch_result = run_ant(repo, objective_id, "patch", patch_target, pf)
        patch_task_id = patch_result["task_id"]
        console.print(f"[green]PatchAnt task {patch_task_id}[/green] status={patch_result['status']}")
        _print_json(patch_result["report"])
        if patch_result["status"] != "done":
            console.print(Panel("[bold red]PatchAnt failed; stopping mission before apply.[/bold red]", title="Ant Farm"))
            raise typer.Exit(1)

    applied = None
    if apply_branch:
        if patch_task_id is None:
            console.print("[red]--apply-branch requires --patch-target in this mission command.[/red]")
            raise typer.Exit(1)
        with db.connect(repo) as conn:
            patch_candidate = db.patch_for_task(conn, patch_task_id)
        if patch_candidate is None:
            console.print(Panel(
                "[bold red]PatchAnt produced no validated patch candidate; stopping before apply.[/bold red]\n"
                f"Patch task: {patch_task_id}\n"
                f"Check the PatchAnt report with: antfarm task {patch_task_id}",
                title="Ant Farm",
            ))
            raise typer.Exit(1)

        with console.status("[bold green]🐜📦 moving patch into isolated worktree...[/bold green]", spinner="arc"):
            applied = apply_task_patch(repo, patch_task_id, apply_branch)
        console.print(f"[green]Patch applied in isolated worktree[/green]: {applied['worktree']}")
        console.print("Main checkout was not modified.")

    if verify_worktree:
        if patch_task_id is None or applied is None:
            console.print("[red]--verify-worktree requires --patch-target and --apply-branch.[/red]")
            raise typer.Exit(1)
        worktree = Path(applied["worktree"]).resolve()
        with console.status("[bold cyan]🐜✅ verifier ants testing the worktree...[/bold cyan]", spinner="aesthetic"):
            wt_result = run_verifier_in_dir(repo, objective_id, verify_worktree, worktree)
        color = "green" if wt_result["exit_code"] == 0 else "red"
        console.print(f"[{color}]Worktree verifier {wt_result['id']} exit={wt_result['exit_code']} timed_out={wt_result['timed_out']} duration={wt_result['duration_seconds']:.2f}s[/{color}]")
        console.print(f"Worktree: {worktree}")
        if wt_result["stdout"]:
            console.print(Panel(wt_result["stdout"], title="worktree stdout"))
        if wt_result["stderr"]:
            console.print(Panel(wt_result["stderr"], title="worktree stderr"))
        if wt_result["exit_code"] != 0 or wt_result["timed_out"]:
            console.print(Panel("[bold red]🐜 Worktree verifier failed; colony mission failed.[/bold red]", title="Ant Farm"))
            raise typer.Exit(1)

    console.print(Panel("[bold green]🐜 colony mission complete[/bold green]", title="Ant Farm"))


@app.command("status")
def status_cmd() -> None:
    """Show repo-local Ant Farm status."""
    repo = _repo()
    cfg = load_config(repo)
    console.print(f"Repo: {repo}")
    console.print(f"LLM endpoint: {cfg.llm_base_url}")
    console.print(f"Fallback model: {cfg.model}")
    console.print(f"LLM generation: max_tokens={cfg.llm_max_tokens} temperature={cfg.llm_temperature}")
    console.print(
        "Context budget: "
        f"per_file={_format_bytes(cfg.max_context_bytes_per_file)} "
        f"total={_format_bytes(cfg.max_context_bytes_total)} "
        f"max_files={cfg.max_context_files} "
        f"queen_reports={_format_bytes(cfg.queen_max_report_bytes)}"
    )

    role_table = Table(title="Role model routing")
    role_table.add_column("Role")
    role_table.add_column("Model")
    for role, model in sorted(cfg.model_by_role.items()):
        role_table.add_row(role, model)
    console.print(role_table)

    with db.connect(repo) as conn:
        objectives = db.list_objectives(conn)
        tasks = db.list_tasks(conn)
        verifiers = db.latest_verifier_results(conn)
    obj_table = Table(title="Objectives")
    obj_table.add_column("ID")
    obj_table.add_column("Status")
    obj_table.add_column("Title")
    for row in objectives:
        obj_table.add_row(str(row["id"]), row["status"], row["title"])
    console.print(obj_table)

    task_table = Table(title="Recent tasks")
    task_table.add_column("ID")
    task_table.add_column("Obj")
    task_table.add_column("Role")
    task_table.add_column("Status")
    task_table.add_column("Target")
    for row in tasks:
        task_table.add_row(str(row["id"]), str(row["objective_id"]), row["role"], row["status"], row["target"])
    console.print(task_table)

    if verifiers:
        verify_table = Table(title="Recent verifier results")
        verify_table.add_column("ID")
        verify_table.add_column("Exit")
        verify_table.add_column("Command")
        for row in verifiers:
            verify_table.add_row(str(row["id"]), str(row["exit_code"]), row["command"])
        console.print(verify_table)


@app.command("task")
def task_cmd(task_id: int = typer.Argument(..., help="Task id.")) -> None:
    """Show one task, report JSON, and patch-candidate status."""
    repo = _repo()
    with db.connect(repo) as conn:
        row = db.get_task(conn, task_id)
        patch = db.patch_for_task(conn, task_id)
    console.print(Panel(f"Objective: {row['objective_id']}\nRole: {row['role']}\nStatus: {row['status']}\nTarget: {row['target']}\nFiles: {row['files_json']}\nError: {row['error'] or ''}", title=f"Task {task_id}"))
    if row["report_json"]:
        console.print(Syntax(row["report_json"], "json", word_wrap=True))
    if patch:
        console.print(Panel(f"Patch candidate {patch['id']} status={patch['status']} worktree={patch['worktree_path'] or ''}", title="Patch"))
        console.print(Syntax(patch["diff_text"], "diff", word_wrap=True))


@app.command("apply")
def apply_cmd(
    task_id: int = typer.Argument(..., help="Task id containing a patch candidate."),
    branch: str = typer.Option(..., "--branch", help="New branch name for the isolated git worktree."),
) -> None:
    """Apply a patch candidate in .antfarm/worktrees only."""
    repo = _repo()
    try:
        result = apply_task_patch(repo, task_id, branch)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Patch applied in isolated worktree[/green]: {result['worktree']}")
    console.print("Main checkout was not modified.")


def main() -> None:
    app()
