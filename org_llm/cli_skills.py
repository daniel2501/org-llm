# [[file:../../../org/20260425230731-org_llm.org::*`skill` CLI commands (additions to cli.py)][`skill` CLI commands (additions to cli.py):1]]
"""Skill commands — imported and attached to app in cli.py."""
from __future__ import annotations

from typing import Annotated

import typer

from .db import get_session, make_engine, DB_PATH
from .ui import console, hail, make_it_so, on_screen, red_alert, warp, TREK_MSGS


def _engine():
    import os
    from pathlib import Path
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key):
    from .db import Config
    r = session.get(Config, key)
    return r.value if r else ""


def register(app: typer.Typer) -> None:
    """Attach skill sub-commands to a Typer app."""

    @app.command("skills")
    def skills_list():
        """List all discovered skills."""
        from .skills import Skill
        from rich.table import Table
        engine = _engine()
        with get_session(engine) as session:
            rows = session.query(Skill).order_by(Skill.name).all()
        if not rows:
            on_screen("No skills found. Tag a heading :skill: in any org file.")
            return
        table = Table(box=None, pad_edge=False)
        table.add_column("Name",      style="lcars2")
        table.add_column("Model key", style="dim")
        table.add_column("Lang",      style="lcars3")
        table.add_column("File",      style="dim")
        for s in rows:
            from pathlib import Path
            table.add_row(s.name, s.model_key, s.lang, Path(s.file_path).name)
        console.print(table)

    @app.command("skill")
    def skill_run(
        name:  Annotated[str, typer.Argument(help="Skill name to run")],
        input: Annotated[str, typer.Argument(help="Input text")] = "",
        node:  Annotated[str, typer.Option("--node", "-n",
               help="Use body of this node title as input")] = "",
    ):
        """Run a skill by name, optionally pulling input from an indexed node."""
        from .skills import Skill, run_skill
        engine = _engine()
        with get_session(engine) as session:
            sk = session.query(Skill).filter_by(name=name).first()
            if not sk:
                red_alert(f"Skill {name!r} not found. Run `org-llm skills` to list.")
                raise typer.Exit(1)

            cfg_dict = {
                r.key: r.value
                for r in session.query(__import__("org_llm.db", fromlist=["Config"]).Config).all()
            }
            url = cfg_dict.get("ollama_url", "http://localhost:11434")

            if node and not input:
                from .db import Node
                n = session.query(Node).filter(Node.title.ilike(f"%{node}%")).first()
                input = (n.body or n.title) if n else ""

        with warp(f"Running skill: {name}"):
            output = run_skill(sk, input_text=input, cfg=cfg_dict, base_url=url)

        console.print()
        console.rule(f"[lcars2]{name}[/lcars2]")
        console.print(output)
        console.rule()

    @app.command("skill-index")
    def skill_index():
        """Scan org files and register skills into the database."""
        from pathlib import Path
        from .skills import index_skills
        engine = _engine()
        with get_session(engine) as session:
            org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
            with warp(TREK_MSGS["index"] + " for skills"):
                count = index_skills(session, org_dir)
        hail(f"Registered {count} skills.")
        make_it_so()

    @app.command("skill-new")
    def skill_new(
        name:      Annotated[str, typer.Argument(help="Skill name (snake_case)")],
        lang:      Annotated[str, typer.Option("--lang", "-l",
                   help="Language: python | sh")] = "python",
        model_key: Annotated[str, typer.Option("--model-key", "-m",
                   help="Model config key to use")] = "text_model",
        file:      Annotated[str, typer.Option("--file", "-f",
                   help="Org file to append to (relative to org_dir)")] = "skills.org",
    ):
        """Scaffold a new skill block into an org file and open instructions."""
        from pathlib import Path
        from rich.panel import Panel

        engine = _engine()
        with get_session(engine) as session:
            org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()

        heading = name.replace("_", " ").title()
        if lang == "python":
            body = (
                "# {{input}} is replaced with your input at runtime.\n"
                "# cfg dict has all config keys (chat_model, org_dir, etc.)\n"
                "# llm.chat(prompt) calls the configured model.\n\n"
                "prompt = f\"Process this input:\\n\\n{{input}}\"\n"
                "result = llm.chat(prompt)\n"
                "print(result)\n"
            )
        else:
            body = (
                "# {{input}} is replaced with your input at runtime.\n"
                "echo \"Processing: {{input}}\"\n"
            )

        template = (
            f"\n* {heading}                                    :skill:\n"
            f":PROPERTIES:\n"
            f":SKILL_NAME: {name}\n"
            f":SKILL_LANG: {lang}\n"
            f":SKILL_MODEL: {model_key}\n"
            f":END:\n\n"
            f"Describe what this skill does.\n\n"
            f"#+begin_src {lang}\n"
            f"{body}"
            f"#+end_src\n"
        )

        target = org_dir / file
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as fh:
            fh.write(template)

        hail(f"Skill template added to {target}")
        console.print(Panel(
            f"[lcars2]Next steps:[/lcars2]\n\n"
            f"1. Open [bold]{target}[/bold] in Emacs\n"
            f"2. Edit the =#+begin_src {lang}= block — your skill logic goes here\n"
            f"3. Variables available:\n"
            f"   [lcars1]{{{{input}}}}[/lcars1]   — text passed via CLI\n"
            f"   [lcars1]llm.chat(p)[/lcars1] — call the LLM (python only)\n"
            f"   [lcars1]cfg[/lcars1]          — dict of all config values\n"
            f"4. Run [bold]org-llm skill-index[/bold] to register it\n"
            f"5. Run [bold]org-llm skill {name} \"your input\"[/bold] to test\n\n"
            f"[dim]See: org-llm tutor skills  for the full guide[/dim]",
            title="[lcars1]Skill scaffolded[/lcars1]",
            border_style="lcars2",
        ))
        make_it_so()
# `skill` CLI commands (additions to cli.py):1 ends here
