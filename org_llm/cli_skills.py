# [[file:../../../org/20260425230731-org_llm.org::*`skill` CLI commands (additions to cli.py)][`skill` CLI commands (additions to cli.py):1]]
"""Skill commands — imported and attached to app in cli.py."""
from __future__ import annotations

from typing import Annotated

import typer

from .db import get_session, make_engine, DB_PATH
from .ui import (console, hail, make_it_so, on_screen, red_alert, warp,
                  TREK_MSGS, thinking)


def _ollama_url(session) -> str:
    """Match cli.py's _ollama_url helper — env > config > default."""
    import os
    return (os.environ.get("ORG_LLM_OLLAMA_URL")
            or _cfg(session, "ollama_url")
            or "http://localhost:11434")


def _engine():
    import os
    from pathlib import Path
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key):
    from .db import Config
    r = session.get(Config, key)
    return r.value if r else ""


# (Note: `_ollama_url` is defined above the imports section just below
# `_cfg` is referenced — but in Python module-load order this still
# resolves fine since both are top-level names defined before any
# call into `skill_new` happens.)


def _org_dir(session):
    """Mirror cli._org_dir — env var first, then config, then default.

    Defined here too so cli_skills doesn't depend on cli (avoids circular
    import at module load time).
    """
    import os
    from pathlib import Path
    return Path(os.environ.get("ORG_LLM_ORG_DIR")
                or _cfg(session, "org_dir") or "~/org").expanduser()


def register(app: typer.Typer) -> None:
    """Attach skill sub-commands to a Typer app."""

    @app.command("skills", rich_help_panel="Skills")
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

    @app.command("skill", rich_help_panel="Skills")
    def skill_run(
        name:  Annotated[str, typer.Argument(help="Skill name to run")],
        input: Annotated[str, typer.Argument(help="Input text")] = "",
        node:  Annotated[str, typer.Option("--node", "-n",
               help="Use body of this node title as input")] = "",
        yes:   Annotated[bool, typer.Option("--yes", "-y",
               help="Skip the trust-on-first-use confirmation")] = False,
    ):
        """Run a skill by name, optionally pulling input from an indexed node.

        Skills execute arbitrary Python or shell as the current user. The first
        time a given skill source is run, you'll be asked to confirm — the SHA
        of the source is then stored in `trusted_skills` and re-runs are quiet
        until the source changes (any edit invalidates trust). Pass --yes to
        skip the prompt for scripted use.
        """
        import hashlib
        from .skills import Skill, run_skill
        from .db import Config as Cfg
        engine = _engine()
        with get_session(engine) as session:
            sk = session.query(Skill).filter_by(name=name).first()
            if not sk:
                # Fuzzy-match against existing skills before bailing.
                import difflib as _dl
                all_names = [s.name for s in session.query(Skill).all()]
                guess = _dl.get_close_matches(name, all_names, n=1, cutoff=0.6)
                if guess:
                    on_screen(f"[yellow]Skill {name!r} not found — "
                              f"using closest match {guess[0]!r}.[/yellow]")
                    sk = session.query(Skill).filter_by(name=guess[0]).first()
                if not sk:
                    red_alert(f"Skill {name!r} not found. Run `org-llm skills` to list.")
                    if all_names:
                        suggestions = _dl.get_close_matches(name, all_names, n=3, cutoff=0.4)
                        if suggestions:
                            on_screen(f"[dim]Closest names: {', '.join(suggestions)}[/dim]")
                    raise typer.Exit(1)

            # Trust-on-first-use: hash the source, compare to stored allow-list
            sig = hashlib.sha256(sk.source.encode()).hexdigest()[:16]
            trusted_row = session.get(Cfg, "trusted_skills")
            trusted = set((trusted_row.value if trusted_row else "").split(",")) - {""}
            entry = f"{name}:{sig}"
            if entry not in trusted and not yes:
                console.print()
                from rich.panel import Panel as _P
                from rich.syntax import Syntax as _S
                console.print(_P(
                    f"[bold yellow]⚠  Trust check[/bold yellow]\n\n"
                    f"This is the first time skill [bold]{name}[/bold] (lang: {sk.lang}, "
                    f"model_key: {sk.model_key}) has been run with this source.\n\n"
                    f"Skills execute arbitrary code as [bold]{__import__('os').environ.get('USER','you')}[/bold]. "
                    f"Inspect the source below before approving — anything that ships in a shared org file "
                    f"could be hostile.\n\n"
                    f"  source file: {sk.file_path}\n"
                    f"  heading:     {sk.heading}\n"
                    f"  sha-256:     {sig}",
                    border_style="yellow", padding=(1, 2),
                ))
                console.print(_S(sk.source, sk.lang if sk.lang in ("python","sh","bash","shell") else "text",
                                 theme="monokai", line_numbers=True))
                if not typer.confirm("Trust this skill source and run it?", default=False):
                    on_screen("Aborted. Edit the skill or pass --yes to bypass next time.")
                    raise typer.Exit(1)
                # Record trust
                new_trusted = ",".join(sorted(trusted | {entry}))
                if trusted_row:
                    trusted_row.value = new_trusted
                else:
                    session.add(Cfg(key="trusted_skills", value=new_trusted))
                session.commit()
                hail(f"Trust recorded for {name}@{sig}")

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

    @app.command("skill-index", rich_help_panel="Skills")
    def skill_index():
        """Scan org files and register skills into the database."""
        from pathlib import Path
        from .skills import index_skills
        engine = _engine()
        with get_session(engine) as session:
            org_dir = _org_dir(session)
            with warp(TREK_MSGS["index"] + " for skills"):
                count = index_skills(session, org_dir)
        hail(f"Registered {count} skills.")
        make_it_so()

    @app.command("skill-examples", rich_help_panel="Skills")
    def skill_examples(
        file:    Annotated[str,  typer.Option("--file", "-f",
                  help="Target org file (relative to org_dir). Default: "
                       "starter-skills.org so the bundle has its own "
                       "namespace and won't collide with skill-new.")
                ] = "starter-skills.org",
        force:   Annotated[bool, typer.Option("--force", "-F",
                  help="Overwrite the target file if it exists")] = False,
        no_index: Annotated[bool, typer.Option("--no-index",
                  help="Skip the auto skill-index after install")] = False,
    ):
        """Install a bundle of working starter skills into your vault.

        Copies =org_llm/skill_examples/starter.org= (shipped with the
        package) into your org_dir + auto-runs =skill-index= so the
        skills are immediately runnable. Seven examples included:
        =summarize=, =action_items=, =brainstorm=, =proofread=,
        =recent_grep= (sh, no LLM), =emojify=, =captains_log=. Edit
        any of them as templates for your own.
        """
        from pathlib import Path
        import shutil

        engine = _engine()
        with get_session(engine) as session:
            org_dir = _org_dir(session)

        # Locate the bundled examples — works for editable installs
        # AND wheel installs (the file ships under org_llm/skill_examples/).
        try:
            bundled = Path(__file__).parent / "skill_examples" / "starter.org"
        except Exception:
            bundled = None
        if not bundled or not bundled.exists():
            red_alert("Bundled starter.org not found. Reinstall with "
                      "[bold]uv tool install --reinstall org-llm[/bold].")
            raise typer.Exit(1)

        target = org_dir / file
        if target.exists() and not force:
            red_alert(
                f"{target} already exists. Use --force to overwrite, "
                "or --file to pick a different name (e.g. "
                f"--file phase3-starter.org)."
            )
            raise typer.Exit(1)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(bundled, target)
        hail(f"Copied 7 starter skills to {target}")

        if no_index:
            on_screen("[dim]Skipping auto-index. "
                      "Run [bold]org-llm skill-index[/bold] to register them.[/dim]")
            return

        from .skills import index_skills
        with get_session(engine) as session:
            with warp("Indexing the new skills"):
                count = index_skills(session, org_dir)
        hail(f"Indexed {count} skills total. "
             f"Try: [bold]org-llm skill summarize 'meeting notes here'[/bold]")
        make_it_so()


    @app.command("skill-new", rich_help_panel="Skills")
    def skill_new(
        name:      Annotated[str, typer.Argument(help="Skill name (snake_case)")],
        lang:      Annotated[str, typer.Option("--lang", "-l",
                   help="Language: python | sh")] = "python",
        model_key: Annotated[str, typer.Option("--model-key", "-m",
                   help="Model config key to use")] = "text_model",
        file:      Annotated[str, typer.Option("--file", "-f",
                   help="Org file to append to (relative to org_dir)")] = "skills.org",
        llm_brief: Annotated[str, typer.Option("--llm",
                   help="Free-form description; the LLM writes the skill body "
                        "for you instead of a generic placeholder. "
                        "e.g. --llm 'pull URLs out of clipboard text'")] = "",
    ):
        """Scaffold a new skill block into an org file and open instructions."""
        from pathlib import Path
        from rich.panel import Panel

        engine = _engine()
        with get_session(engine) as session:
            org_dir = _org_dir(session)

        heading = name.replace("_", " ").title()
        description = "Describe what this skill does."

        # --llm path: have the configured chat_model write the body
        # for us, grounded in the brief. Cold path falls back to the
        # generic placeholder if the LLM is unreachable.
        body = ""
        if llm_brief:
            try:
                from .llm import chat as _chat
                with get_session(_engine()) as _s:
                    _url = _ollama_url(_s)
                    _mdl = (_cfg(_s, "chat_model") or "llama3.2")
                _sys = (
                    f"You write minimal {lang} skill bodies for org-llm. "
                    "Output ONLY runnable code — no markdown fences, no "
                    "explanation, no preamble. Available variables in "
                    "Python skills: {{input}} (string from CLI), "
                    "llm.chat(prompt) (calls the configured model), "
                    "cfg (dict of all config). For sh skills: {{input}} "
                    "is substituted at runtime. Keep it under 25 lines."
                )
                _user = (
                    f"Skill name: {name}\n"
                    f"Brief: {llm_brief}\n\n"
                    f"Write the {lang} body that does this. Use {{input}} "
                    f"as the placeholder."
                )
                with thinking(f"Writing {name}", model=_mdl):
                    body = (_chat(_user, model=_mdl, base_url=_url,
                                    system=_sys, timeout=60.0) or "").strip()
                # Strip stray markdown fences if the model added them anyway
                if body.startswith("```"):
                    body = body.split("\n", 1)[-1]
                if body.endswith("```"):
                    body = body.rsplit("\n", 1)[0]
                body = body.rstrip() + "\n"
                description = llm_brief
            except Exception:
                body = ""

        if not body:
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
            f"{description}\n\n"
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
