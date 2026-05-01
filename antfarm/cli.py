from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from . import __version__, db
from .config import AntFarmConfig, ensure_repo_initialized, load_config, resolve_repo, save_config
from .orchestrator import WORKER_ROLES, run_ant, run_queen, run_wave
from .sandbox import apply_task_patch
from .verifier import run_verifier, run_verifier_in_dir

app = typer.Typer(help="Ant Farm: local repo-scoped multi-agent coding system")
console = Console()


def _repo() -> Path:
    repo = resolve_repo()
    ensure_repo_initialized(repo)
    return repo


def _print_json(data: object) -> None:
    console.print(Syntax(json.dumps(data, indent=2, sort_keys=True), "json", word_wrap=True))


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


@app.command("status")
def status_cmd() -> None:
    """Show repo-local Ant Farm status."""
    repo = _repo()
    cfg = load_config(repo)
    console.print(f"Repo: {repo}")
    console.print(f"LLM: {cfg.model} at {cfg.llm_base_url}")
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
    result = apply_task_patch(repo, task_id, branch)
    console.print(f"[green]Patch applied in isolated worktree[/green]: {result['worktree']}")
    console.print("Main checkout was not modified.")


def main() -> None:
    app()
