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
from .config import assert_not_inside_antfarm_worktree
from .orchestrator import WORKER_ROLES, run_ant, run_queen, run_wave
from .sandbox import apply_task_patch
from .verifier import run_verifier, run_verifier_in_dir

app = typer.Typer(help="Ant Farm: local repo-scoped multi-agent coding system")
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

    console.print(Panel(
        f"[bold yellow]🐜 Ant Farm Mission[/bold yellow]\n"
        f"Objective: [bold]{objective_id}[/bold]\n"
        f"Target: {target}\n"
        f"Ants: {', '.join(roles)}\n"
        f"Files: {', '.join(files) if files else '(none)'}",
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
