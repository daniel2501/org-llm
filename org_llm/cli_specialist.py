"""CLI verbs for org_llm.specialist — direct-FOSS-API specialist runtime.

Adds an `org-llm specialist` sub-typer with a `run` command that wraps
`org_llm.specialist.run_specialist`. Replaces the opencode-based
agentic_tool runtime for FOSS specialists; bypasses the substrate-level
silent-failure bug found in round-12.

Registered via `register(app)` at the bottom of cli.py (mirrors cli_skills).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .specialist import (
    DEFAULT_TOOLS,
    SpecialistResult,
    SpecialistTask,
    run_specialist,
)


_specialist_app = typer.Typer(
    name="specialist",
    help="Run an org-llm specialist via direct FOSS API (bypasses opencode).",
    rich_markup_mode="rich",
)

console = Console()


@_specialist_app.command("run")
def specialist_run(
    task_spec: Annotated[Path, typer.Argument(
        help="JSON task spec file with handle/persona/instruction/workdir/...")],
    model: Annotated[Optional[str], typer.Option(
        "--model", "-m",
        help="Override model (default from task spec or qwen3-coder-30b).")] = None,
    workdir: Annotated[Optional[Path], typer.Option(
        "--workdir",
        help="Override workdir (default from task spec).")] = None,
    max_iterations: Annotated[Optional[int], typer.Option(
        "--max-iter",
        help="Override max iteration count.")] = None,
    max_budget_usd: Annotated[Optional[float], typer.Option(
        "--max-budget",
        help="Override per-task budget cap (USD).")] = None,
    json_output: Annotated[bool, typer.Option(
        "--json",
        help="Print result as JSON instead of human-readable.")] = False,
):
    """Run a specialist task end-to-end via direct FOSS API.

    The task spec is a JSON file shaped like:
    {
      "handle": "@atoz",
      "persona": "You are @atoz...",
      "instruction": "Wrap the mention on line 9...",
      "workdir": "/abs/path/to/workdir",
      "target_files": ["/abs/path/file.org"],
      "model": "qwen/qwen3-coder-30b-a3b-instruct",
      "max_iterations": 8,
      "max_budget_usd": 1.0
    }
    """
    if not task_spec.exists():
        rprint(f"[red]task spec not found: {task_spec}[/red]")
        raise typer.Exit(1)
    spec = json.loads(task_spec.read_text())

    # Build SpecialistTask from spec + overrides
    wd = Path(workdir or spec.get("workdir", "."))
    task = SpecialistTask(
        handle=spec["handle"],
        persona=spec["persona"],
        instruction=spec["instruction"],
        workdir=wd,
        model=model or spec.get("model", "qwen/qwen3-coder-30b-a3b-instruct"),
        target_files=[Path(p) for p in spec.get("target_files") or []],
        max_iterations=max_iterations or spec.get("max_iterations", 8),
        max_budget_usd=max_budget_usd or spec.get("max_budget_usd", 1.0),
    )
    if not json_output:
        rprint(f"[dim]running {task.handle} (model={task.model}, "
               f"workdir={task.workdir})[/dim]")
    result = run_specialist(task)

    if json_output:
        print(json.dumps({
            "handle": result.handle,
            "success": result.success,
            "iterations": result.iterations,
            "edits_applied": result.edits_applied,
            "text_output": result.text_output,
            "cost_usd": result.cost_usd,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
        }, indent=2))
        raise typer.Exit(0 if result.success else 1)

    # Human-readable
    table = Table(title=f"Specialist {result.handle}",
                   show_header=False, box=None)
    table.add_row("success",   str(result.success))
    table.add_row("iterations", str(result.iterations))
    table.add_row("edits",     str(len(result.edits_applied)))
    table.add_row("cost_usd",  f"${result.cost_usd:.6f}")
    table.add_row("duration",  f"{result.duration_seconds}s")
    if result.error:
        table.add_row("error",  result.error)
    console.print(table)
    if result.edits_applied:
        rprint("[bold]Edits applied:[/bold]")
        for e in result.edits_applied:
            rprint(f"  iter={e['iteration']} {e['tool']}: {e['result']}")
    if result.text_output:
        rprint(f"\n[bold]Final summary:[/bold]\n{result.text_output}")
    raise typer.Exit(0 if result.success else 1)


@_specialist_app.command("schema")
def specialist_schema():
    """Print the task-spec JSON schema (for editor / harness reference)."""
    schema = {
        "type": "object",
        "properties": {
            "handle":     {"type": "string"},
            "persona":    {"type": "string"},
            "instruction": {"type": "string"},
            "workdir":    {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "model":      {"type": "string"},
            "max_iterations": {"type": "integer"},
            "max_budget_usd": {"type": "number"},
        },
        "required": ["handle", "persona", "instruction", "workdir"],
    }
    print(json.dumps(schema, indent=2))


def register(app: typer.Typer) -> None:
    app.add_typer(_specialist_app, name="specialist",
                   rich_help_panel="Multi-agent")
