# [[file:../../../org/20260425230731-org_llm.org::*cli.py][cli.py:1]]
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from .db import DB_PATH, get_session, init_db, make_engine
from .indexer import index_directory
from .ui import TREK_MSGS, console, hail, impulse, make_it_so, on_screen, red_alert, warp

app = typer.Typer(
    help="org-llm: LLM-powered org-roam CLI",
    rich_markup_mode="rich",
)


def _engine():
    import os
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key: str) -> str:
    from .db import Config
    row = session.get(Config, key)
    return row.value if row else ""


def _ollama_url(session) -> str:
    return _cfg(session, "ollama_url") or "http://localhost:11434"


@app.command()
def init():
    """Initialize database and write default config."""
    import os
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    with warp(TREK_MSGS["init"]):
        engine = make_engine(path)
        init_db(engine)
    hail(f"Database ready at {path}")
    make_it_so()


@app.command()
def index(
    force: Annotated[bool, typer.Option("--force", help="Re-index all files")] = False,
):
    """Scan org files and populate the index."""
    engine = _engine()
    with get_session(engine) as session:
        org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        if not org_dir.exists():
            red_alert(f"org_dir not found: {org_dir}")
            raise typer.Exit(1)

        if force:
            from .db import File, Node
            session.query(Node).delete()
            session.query(File).delete()
            session.commit()
            hail("Cleared existing index.")

    with warp(TREK_MSGS["index"] + f": {org_dir}"):
        engine = _engine()
        with get_session(engine) as session:
            files, nodes = index_directory(org_dir, session)

    hail(f"Indexed {files} files, {nodes} nodes.")
    make_it_so()


@app.command()
def embed(
    force: Annotated[bool, typer.Option("--force", help="Re-embed all nodes")] = False,
):
    """Generate embeddings for indexed nodes (requires Ollama)."""
    from .indexer import embed_nodes

    from .db import Node
    engine = _engine()
    with get_session(engine) as session:
        url   = _ollama_url(session)
        model = _cfg(session, "embed_model") or "nomic-embed-text"
        if force:
            total = session.query(Node).count()
        else:
            total = session.query(Node).filter(Node.embedding.is_(None)).count()

    hail(f"Embedding {total} nodes with [bold]{model}[/bold]")

    with get_session(engine) as session:
        with impulse(TREK_MSGS["embed"], total=total) as (prog, task):
            def tick():
                prog.advance(task)
            count = embed_nodes(session, model=model, base_url=url,
                                force=force, progress_cb=tick)

    hail(f"Embedded {count} nodes.")
    make_it_so()


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int,  typer.Option("--limit", "-n")] = 10,
    keyword: Annotated[bool, typer.Option("--keyword", "-k",
             help="Keyword search instead of semantic")] = False,
):
    """Search your org notes semantically or by keyword."""
    from .search import keyword_search, vector_search

    engine = _engine()
    with get_session(engine) as session:
        url   = _ollama_url(session)
        model = _cfg(session, "embed_model") or "nomic-embed-text"

        if keyword:
            results = keyword_search(session, query, limit=limit)
        else:
            with warp(TREK_MSGS["search"] + f": {query!r}"):
                from .llm import embed
                qvec = embed(query, model=model, base_url=url)
                results = vector_search(session, qvec, limit=limit)

    if not results:
        on_screen("No results found.")
        return

    table = Table(box=None, pad_edge=False, show_header=True)
    table.add_column("Score", style="lcars1", width=6, no_wrap=True)
    table.add_column("Title", style="lcars2")
    table.add_column("Tags",  style="dim", width=20)
    table.add_column("File",  style="dim")

    for r in results:
        score = f"{r.score:.3f}" if not keyword else "—"
        fname = Path(r.file_path).name
        table.add_row(score, r.title, r.tags or "—", fname)

    console.print(table)


@app.command()
def ask(
    query:   Annotated[str,  typer.Argument(help="Your question")],
    top_k:   Annotated[int,  typer.Option("--top-k", "-k")] = 6,
    model:   Annotated[str,  typer.Option("--model", "-m",
             help="Override chat model")] = "",
    context: Annotated[bool, typer.Option("--context", "-c",
             help="Show retrieved context nodes")] = False,
    reason:  Annotated[bool, typer.Option("--reason", "-r",
             help="Use reason_model (deepseek-r1) instead of chat_model")] = False,
):
    """Ask a question answered from your org notes (RAG)."""
    from .llm import chat, embed
    from .search import vector_search

    engine = _engine()
    with get_session(engine) as session:
        url        = _ollama_url(session)
        embed_mdl  = _cfg(session, "embed_model") or "nomic-embed-text"
        chat_mdl   = model or (
            _cfg(session, "reason_model") if reason
            else _cfg(session, "chat_model")
        ) or "llama3.3"

        with warp(TREK_MSGS["ask"] + " — retrieving context"):
            qvec    = embed(query, model=embed_mdl, base_url=url)
            results = vector_search(session, qvec, limit=top_k)

    if not results:
        red_alert("No indexed nodes found. Run `org-llm embed` first.")
        raise typer.Exit(1)

    if context:
        hail("Context nodes retrieved:")
        for r in results:
            on_screen(f"  [{r.score:.3f}] {r.title} ({Path(r.file_path).name})")
        console.print()

    ctx_text = "\n\n---\n\n".join(
        f"# {r.title}\n{r.body[:800]}" for r in results
    )

    system = (
        "You are an assistant with access to a personal org-mode knowledge base. "
        "Answer using only the provided notes. Be concise. "
        "Cite note titles when relevant."
    )
    prompt = f"Notes from my org files:\n\n{ctx_text}\n\n---\n\nQuestion: {query}"

    with warp(f"Hailing {chat_mdl}"):
        answer = chat(prompt, model=chat_mdl, base_url=url, system=system)

    console.print()
    console.rule(f"[lcars2]{chat_mdl}[/lcars2]")
    console.print(answer)
    console.rule()


_TASK_MODEL_KEYS = [
    ("embed",    "embed_model",    "Semantic search embeddings"),
    ("chat",     "chat_model",     "ask / general Q&A"),
    ("code",     "code_model",     "Code generation"),
    ("reason",   "reason_model",   "Planning & complex reasoning"),
    ("fast",     "fast_model",     "Tagging & classification"),
    ("instruct", "instruct_model", "Capture & instruction following"),
    ("text",     "text_model",     "Summarization & text analysis"),
]


@app.command()
def models():
    """Show task→model assignments and available Ollama models."""
    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)

        assign = Table(title="Task → Model", box=None, pad_edge=False)
        assign.add_column("Task",       style="lcars1")
        assign.add_column("Config key", style="dim")
        assign.add_column("Model",      style="lcars2")
        assign.add_column("Purpose")
        for task, key, purpose in _TASK_MODEL_KEYS:
            assign.add_row(task, key, _cfg(session, key) or "—", purpose)
        console.print(assign)

    console.print()
    try:
        from .llm import list_models
        names = list_models(url)
        avail = Table(title="Pulled in Ollama", box=None, pad_edge=False)
        avail.add_column("Model", style="lcars3")
        for name in names:
            avail.add_row(name)
        console.print(avail)
    except Exception as e:
        console.print(f"[warn]Ollama not reachable ({url}):[/warn] {e}")


@app.command()
def config(
    key:   Annotated[str, typer.Argument(help="Config key")] = "",
    value: Annotated[str, typer.Argument(help="Value to set")] = "",
):
    """Get or set a config value. No args = show all."""
    from .db import Config as Cfg
    engine = _engine()
    with get_session(engine) as session:
        if not key:
            table = Table(box=None, pad_edge=False)
            table.add_column("Key",   style="lcars1")
            table.add_column("Value", style="lcars2")
            for row in session.query(Cfg).order_by(Cfg.key):
                table.add_row(row.key, row.value)
            console.print(table)
        elif not value:
            row = session.get(Cfg, key)
            console.print(row.value if row else "[error]not set[/error]")
        else:
            row = session.get(Cfg, key)
            if row:
                row.value = value
            else:
                session.add(Cfg(key=key, value=value))
            session.commit()
            hail(f"{key} = {value}")
            make_it_so()


@app.command()
def install(
    skip_ollama:   Annotated[bool, typer.Option("--skip-ollama")]   = False,
    skip_models:   Annotated[bool, typer.Option("--skip-models")]   = False,
    skip_fonts:    Annotated[bool, typer.Option("--skip-fonts")]    = False,
    skip_opencode: Annotated[bool, typer.Option("--skip-opencode")] = False,
):
    """Install Ollama, pull all configured models, install Nerd Fonts, and opencode."""
    import platform
    import shutil
    import subprocess
    import urllib.request
    from .ui import solidarity, trans_stripe

    solidarity()
    console.print()

    bin_dir   = Path("~/.local/bin").expanduser()
    font_dir  = Path("~/.local/share/fonts/NerdFonts").expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    font_dir.mkdir(parents=True, exist_ok=True)

    ollama_bin = bin_dir / "ollama"

    # ── Ollama ────────────────────────────────────────────────────────────────
    if not skip_ollama:
        if shutil.which("ollama") or ollama_bin.exists():
            hail("Ollama already installed — skipping binary download.")
        else:
            arch = platform.machine().lower()
            arch_slug = "amd64" if arch in ("x86_64", "amd64") else "arm64"
            url = (
                f"https://github.com/ollama/ollama/releases/latest/download/"
                f"ollama-linux-{arch_slug}"
            )
            hail(f"Downloading Ollama ({arch_slug}) → {ollama_bin}")
            with warp("Beaming Ollama aboard"):
                urllib.request.urlretrieve(url, ollama_bin)
            ollama_bin.chmod(0o755)
            hail(f"Ollama installed at {ollama_bin}")

        # Start ollama serve in background if not already running
        try:
            import ollama as _ol
            _ol.Client(host="http://localhost:11434").list()
            hail("Ollama is already running.")
        except Exception:
            hail("Starting ollama serve in background…")
            subprocess.Popen(
                [str(ollama_bin if ollama_bin.exists() else shutil.which("ollama")),
                 "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import time; time.sleep(2)

    # ── Models ────────────────────────────────────────────────────────────────
    if not skip_models:
        engine = _engine()
        with get_session(engine) as session:
            url = _ollama_url(session)
            model_keys = [
                "embed_model", "chat_model", "code_model",
                "reason_model", "fast_model", "instruct_model", "text_model",
            ]
            models_to_pull = list({_cfg(session, k) for k in model_keys
                                   if _cfg(session, k)})

        hail(f"Pulling {len(models_to_pull)} models via Ollama…")
        ollama_exe = shutil.which("ollama") or str(ollama_bin)
        with impulse("Pulling models", total=len(models_to_pull)) as (prog, task):
            for model in models_to_pull:
                on_screen(f"  ollama pull {model}")
                result = subprocess.run(
                    [ollama_exe, "pull", model],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    red_alert(f"Failed to pull {model}: {result.stderr.strip()}")
                prog.advance(task)

    # ── Nerd Fonts ────────────────────────────────────────────────────────────
    if not skip_fonts:
        font_name = "NotoMono"
        font_zip  = font_dir / f"{font_name}.tar.xz"
        font_url  = (
            f"https://github.com/ryanoasis/nerd-fonts/releases/latest/download/"
            f"{font_name}.tar.xz"
        )
        existing = list(font_dir.glob("*.ttf")) + list(font_dir.glob("*.otf"))
        if existing:
            hail(f"Nerd Fonts already present in {font_dir} — skipping.")
        else:
            hail(f"Downloading {font_name} Nerd Font…")
            with warp("Transporting font data"):
                urllib.request.urlretrieve(font_url, font_zip)
            subprocess.run(
                ["tar", "-xf", str(font_zip), "-C", str(font_dir)],
                check=True,
            )
            font_zip.unlink(missing_ok=True)
            subprocess.run(["fc-cache", "-fv"], capture_output=True)
            hail(f"NotoMono Nerd Font installed in {font_dir}")

    # ── opencode ──────────────────────────────────────────────────────────────
    if not skip_opencode:
        opencode_bin = bin_dir / "opencode"
        if shutil.which("opencode") or opencode_bin.exists():
            hail("opencode already installed — skipping.")
        else:
            hail("Installing opencode (AI coding agent)…")
            try:
                install_sh = bin_dir / "_opencode_install.sh"
                urllib.request.urlretrieve(
                    "https://opencode.ai/install", install_sh
                )
                install_sh.chmod(0o755)
                result = subprocess.run(
                    ["sh", str(install_sh)],
                    capture_output=True, text=True,
                    env={**__import__("os").environ,
                         "OPENCODE_INSTALL": str(bin_dir)},
                )
                install_sh.unlink(missing_ok=True)
                if result.returncode == 0:
                    hail(f"opencode installed at {opencode_bin}")
                else:
                    red_alert(f"opencode install failed: {result.stderr.strip()[:200]}")
            except Exception as exc:
                red_alert(f"opencode install error: {exc}")

    console.print()
    console.print(trans_stripe(52))
    make_it_so()


@app.command()
def report(
    section: Annotated[str, typer.Argument(
        help="Section: overview | tags | recent | orphans | daily | all"
    )] = "all",
    days: Annotated[int, typer.Option("--days", "-d", help="Days back for recent")] = 14,
):
    """Render rich text reports on your org-roam library."""
    from .report import (
        report_daily, report_orphans, report_overview,
        report_recent, report_top_tags, NF, REPORT_HEADER,
    )
    engine = _engine()
    with get_session(engine) as session:
        console.rule(REPORT_HEADER)
        match section:
            case "overview": report_overview(session)
            case "tags":     report_top_tags(session)
            case "recent":   report_recent(session, days=days)
            case "orphans":  report_orphans(session)
            case "daily":    report_daily(session)
            case "all":
                report_overview(session)
                report_top_tags(session)
                console.print()
                report_recent(session, days=days)
                console.print()
                report_daily(session)
                console.print()
                report_orphans(session)
            case _:
                red_alert(f"Unknown section: {section!r}. "
                          "Use: overview | tags | recent | orphans | daily | all")
                raise typer.Exit(1)
        console.rule("[dim]end of report[/dim]")


@app.command()
def doctor():
    """Run a health check on the org-llm installation."""
    import shutil
    from rich.table import Table
    from rich.text import Text
    from .ui import trans_stripe, PRIDE_BANNER

    PASS = "[bold green]✓ OK[/bold green]"
    FAIL = "[bold red]✗ FAIL[/bold red]"
    WARN = "[bold yellow]⚠ WARN[/bold yellow]"

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("Status", width=10)
    table.add_column("Check", style="lcars2")
    table.add_column("Detail", style="dim")

    def row(ok, label, detail=""):
        table.add_row(PASS if ok else FAIL, label, detail)

    def warn_row(label, detail=""):
        table.add_row(WARN, label, detail)

    # DB
    engine = _engine()
    try:
        init_db(engine)
        row(True, "Database reachable", str(engine.url))
    except Exception as e:
        row(False, "Database", str(e))

    # sqlite-vec
    try:
        import sqlite_vec  # noqa: F401
        row(True, "sqlite-vec loaded")
    except Exception as e:
        row(False, "sqlite-vec", str(e))

    # org_dir
    with get_session(engine) as session:
        org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        org_files = list(org_dir.rglob("*.org")) if org_dir.exists() else []
        row(org_dir.exists(), "org_dir exists", str(org_dir))
        if org_dir.exists():
            row(bool(org_files), "org files found", f"{len(org_files)} .org files")

        # index counts
        from .db import Node, File
        with get_session(engine) as s2:
            file_count  = s2.query(File).count()
            node_count  = s2.query(Node).count()
            embed_count = s2.query(Node).filter(Node.embedding.isnot(None)).count()
        row(node_count > 0, "Index populated",
            f"{file_count} files / {node_count} nodes")
        if node_count > 0:
            pct = int(embed_count / node_count * 100)
            row(embed_count > 0, "Embeddings present",
                f"{embed_count}/{node_count} ({pct}%)")

        # Ollama
        url = _ollama_url(session)
        try:
            from .llm import list_models
            pulled = list_models(url)
            row(True, "Ollama reachable", url)
            model_keys = [
                "embed_model", "chat_model", "code_model",
                "reason_model", "fast_model", "instruct_model", "text_model",
            ]
            for key in model_keys:
                model = _cfg(session, key)
                ok = any(model in m for m in pulled)
                row(ok, f"  model: {key}", model)
        except Exception as e:
            row(False, "Ollama reachable", f"{url} — {e}")
            warn_row("Models not checked", "Ollama must be running")

    # Nerd Font
    font_dir = Path("~/.local/share/fonts/NerdFonts").expanduser()
    fonts = list(font_dir.glob("*.ttf")) + list(font_dir.glob("*.otf")) if font_dir.exists() else []
    row(bool(fonts), "Nerd Font installed",
        f"{len(fonts)} font(s) in {font_dir}" if fonts else str(font_dir))

    console.print()
    console.print(trans_stripe(52))
    console.print(PRIDE_BANNER)
    from rich.panel import Panel
    console.print(Panel(table, title="[lcars1]org-llm doctor[/lcars1]",
                        border_style="lcars1"))
    console.print(trans_stripe(52))
    console.print()


_TUTOR_STEPS = [
    (
        "welcome",
        "Welcome aboard, officer. [lcars1]org-llm[/lcars1] is your personal "
        "LLM-powered second brain, built on your org-roam notes.\n\n"
        "It runs entirely on your machine using open-source models via Ollama. "
        "No cloud. No surveillance. Queer, collective, free.\n\n"
        "This tutor will walk you through the key commands one at a time.",
    ),
    (
        "init",
        "Step 1: [lcars2]org-llm init[/lcars2]\n\n"
        "Initializes the SQLite database at ~/.local/share/org-llm/org-llm.db.\n"
        "Writes default config (model assignments, org_dir, etc.).\n"
        "Safe to run multiple times — idempotent.\n\n"
        "[dim]Try it:[/dim]  [bold]org-llm init[/bold]",
    ),
    (
        "index",
        "Step 2: [lcars2]org-llm index[/lcars2]\n\n"
        "Scans your org_dir (default: ~/org) for all .org files.\n"
        "Extracts headings, body text, tags, and org-roam IDs into the DB.\n"
        "Skips files that haven't changed (mtime check).\n\n"
        "[dim]Force full re-index:[/dim]  [bold]org-llm index --force[/bold]",
    ),
    (
        "embed",
        "Step 3: [lcars2]org-llm embed[/lcars2]\n\n"
        "Generates vector embeddings for every indexed node using the embed_model "
        "(default: nomic-embed-text via Ollama).\n"
        "Required before semantic search and ask work.\n\n"
        "[dim]This may take a while for large vaults.[/dim]\n"
        "[dim]Check progress:[/dim]  [bold]org-llm doctor[/bold]",
    ),
    (
        "search",
        "Step 4: [lcars2]org-llm search <query>[/lcars2]\n\n"
        "Semantic search (default): embeds your query, finds the nearest nodes.\n"
        "Keyword search:  [bold]org-llm search --keyword <term>[/bold]\n\n"
        "Results show score, title, tags, and filename.",
    ),
    (
        "ask",
        "Step 5: [lcars2]org-llm ask <question>[/lcars2]\n\n"
        "RAG pipeline: embed query → retrieve top-K nodes → feed to chat_model.\n"
        "Uses your notes as context; cites them in the answer.\n\n"
        "[dim]Options:[/dim]\n"
        "  --top-k N     how many context nodes to retrieve (default 6)\n"
        "  --context     show which nodes were used\n"
        "  --reason      use reason_model (deepseek-r1) for harder questions",
    ),
    (
        "config",
        "Step 6: [lcars2]org-llm config[/lcars2]\n\n"
        "Show or change configuration values stored in the DB.\n\n"
        "  [bold]org-llm config[/bold]               — show all\n"
        "  [bold]org-llm config chat_model[/bold]    — show one value\n"
        "  [bold]org-llm config chat_model phi4[/bold] — set a value\n\n"
        "[dim]Key config keys:[/dim] org_dir, ollama_url, chat_model, embed_model, "
        "code_model, reason_model, text_model, fast_model, instruct_model",
    ),
    (
        "skills",
        "Step 7: [lcars2]Skills[/lcars2] — define LLM workflows in org-mode\n\n"
        "Skills are org-babel blocks tagged [bold]:skill:[/bold] in any org file.\n"
        "They let you define reusable LLM-powered workflows alongside your notes.\n\n"
        "[lcars1]Anatomy of a skill:[/lcars1]\n\n"
        "  * Summarise a note                        :skill:\n"
        "  :PROPERTIES:\n"
        "  :SKILL_NAME:  summarise\n"
        "  :SKILL_LANG:  python       ← python or sh\n"
        "  :SKILL_MODEL: text_model   ← config key for the model to use\n"
        "  :END:\n\n"
        "  #+begin_src python\n"
        "  # {{input}} → text passed at runtime\n"
        "  # llm.chat(prompt) → calls the configured model\n"
        "  # cfg → dict of all config values\n"
        "  prompt = f'Summarise in 2 sentences:\\n\\n{{input}}'\n"
        "  print(llm.chat(prompt))\n"
        "  #+end_src\n\n"
        "[lcars1]Scaffold a new skill:[/lcars1]\n\n"
        "  [bold]org-llm skill-new my_skill --lang python[/bold]\n"
        "  → appends a template to ~/org/skills.org and prints next steps\n\n"
        "[lcars1]Register & run:[/lcars1]\n\n"
        "  [bold]org-llm skill-index[/bold]              — scan org files, register skills in DB\n"
        "  [bold]org-llm skills[/bold]                   — list registered skills\n"
        "  [bold]org-llm skill summarise 'my note'[/bold] — run with literal input\n"
        "  [bold]org-llm skill summarise --node 'Emacs'[/bold] — use a node body as input\n\n"
        "[dim]Shell skills work the same way — just use SKILL_LANG: sh and write bash.[/dim]",
    ),
    (
        "report",
        "Step 8: [lcars2]org-llm report[/lcars2]\n\n"
        "Rich text reports on your knowledge base:\n\n"
        "  [bold]org-llm report overview[/bold]  — file/node/embedding counts\n"
        "  [bold]org-llm report tags[/bold]      — tag frequency leaderboard\n"
        "  [bold]org-llm report recent[/bold]    — recently modified nodes\n"
        "  [bold]org-llm report orphans[/bold]   — nodes with no outgoing links\n"
        "  [bold]org-llm report daily[/bold]     — recent daily notes\n"
        "  [bold]org-llm report all[/bold]       — all of the above",
    ),
    (
        "doctor",
        "Step 9: [lcars2]org-llm doctor[/lcars2]\n\n"
        "Health check for your installation:\n"
        "  ✓/✗ DB reachable, sqlite-vec, org_dir, index counts, "
        "embedding coverage, Ollama + all models, Nerd Font.\n\n"
        "Run this if something seems wrong.",
    ),
    (
        "install",
        "Step 10: [lcars2]org-llm install[/lcars2]\n\n"
        "One-shot bootstrap: downloads Ollama, pulls all configured models, "
        "and installs NotoMono Nerd Font — all to ~/.local, no sudo needed.\n\n"
        "[dim]Flags:[/dim]\n"
        "  --skip-ollama   skip Ollama download/start\n"
        "  --skip-models   skip model pulls\n"
        "  --skip-fonts    skip font download",
    ),
    (
        "emacs",
        "Doom Emacs integration\n\n"
        "All commands are available via [lcars1]SPC l[/lcars1]:\n\n"
        "  SPC l a   ask\n"
        "  SPC l A   ask with reason model\n"
        "  SPC l s   semantic search\n"
        "  SPC l S   keyword search\n"
        "  SPC l r   report\n"
        "  SPC l i   index\n"
        "  SPC l e   embed\n"
        "  SPC l m   models\n"
        "  SPC l .   ask-dwim (uses region or line at point)\n\n"
        "Results appear in a side window (ANSI-rendered) or vterm.",
    ),
    (
        "done",
        "[bold lcars1]You're ready to explore your second brain.[/bold lcars1]\n\n"
        "Recommended first flight:\n\n"
        "  1. [bold]org-llm init[/bold]\n"
        "  2. [bold]org-llm install[/bold]   (if Ollama not yet running)\n"
        "  3. [bold]org-llm index[/bold]\n"
        "  4. [bold]org-llm embed[/bold]\n"
        "  5. [bold]org-llm doctor[/bold]    (verify everything green)\n"
        "  6. [bold]org-llm ask 'What did I write about X?'[/bold]\n\n"
        "Engage. ☭ ✊ 🏳️‍🌈",
    ),
]


@app.command()
def tutor(
    step: Annotated[str, typer.Argument(
        help="Step name to jump to (welcome/init/index/embed/search/ask/"
             "config/skills/report/doctor/install/emacs/done)"
    )] = "welcome",
    all_steps: Annotated[bool, typer.Option("--all", "-a",
               help="Print all steps at once")] = False,
):
    """Interactive tutorial: learn org-llm features step by step."""
    from rich.panel import Panel
    from rich.markdown import Markdown
    from .ui import trans_stripe, PRIDE_BANNER, solidarity

    step_names = [s for s, _ in _TUTOR_STEPS]

    if all_steps:
        console.print()
        console.print(trans_stripe(52))
        console.print(PRIDE_BANNER)
        for name, body in _TUTOR_STEPS:
            idx = step_names.index(name) + 1
            console.print(Panel(
                body,
                title=f"[lcars1]{idx}/{len(_TUTOR_STEPS)}  {name}[/lcars1]",
                border_style="lcars2",
                padding=(1, 2),
            ))
        console.print(trans_stripe(52))
        return

    # find requested step
    match = next(((n, b) for n, b in _TUTOR_STEPS if n == step), None)
    if not match:
        red_alert(f"Unknown step {step!r}. Valid: {', '.join(step_names)}")
        raise typer.Exit(1)

    name, body = match
    idx = step_names.index(name) + 1
    total = len(_TUTOR_STEPS)

    prev_step = step_names[idx - 2] if idx > 1 else None
    next_step = step_names[idx] if idx < total else None

    console.print()
    console.print(Panel(
        body,
        title=f"[lcars1]{idx}/{total}  {name}[/lcars1]",
        border_style="lcars2",
        padding=(1, 2),
    ))

    nav = []
    if prev_step:
        nav.append(f"← [dim]org-llm tutor {prev_step}[/dim]")
    if next_step:
        nav.append(f"→ [lcars2]org-llm tutor {next_step}[/lcars2]")
    if nav:
        console.print("  " + "    ".join(nav))
    console.print()


@app.command()
def capture(
    title:  Annotated[str,  typer.Option("--title",  "-t", help="Note title")] = "",
    body:   Annotated[str,  typer.Option("--body",   "-b", help="Raw content / prompt")] = "",
    file:   Annotated[str,  typer.Option("--file",   "-f", help="Target org file (relative to org_dir)")] = "inbox.org",
    polish: Annotated[bool, typer.Option("--polish",       help="Let LLM structure the note")] = True,
):
    """Capture a new note into your org vault, optionally polished by an LLM."""
    import uuid
    from datetime import datetime

    engine = _engine()
    with get_session(engine) as session:
        org_dir   = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        url       = _ollama_url(session)
        model     = _cfg(session, "instruct_model") or "mistral-nemo"

    if not title:
        title = typer.prompt("Note title")
    if not body:
        body = typer.prompt("Content (or prompt for LLM)")

    content = body
    if polish:
        system = (
            "You are an org-mode expert. Structure the following content as a clean org-mode "
            "note body. Use headings (* **), bullet points (- ), and code blocks (#+begin_src) "
            "where appropriate. Do not include the title. Output only the org-mode markup."
        )
        from .llm import chat
        with warp(f"Polishing with {model}"):
            content = chat(body, model=model, base_url=url, system=system)

    node_id  = str(uuid.uuid4())
    ts       = datetime.now().strftime("%Y%m%d%H%M%S")
    org_file = org_dir / file
    org_file.parent.mkdir(parents=True, exist_ok=True)

    entry = (
        f"\n* {title}\n"
        f":PROPERTIES:\n:ID: {node_id}\n:CREATED: [{ts}]\n:END:\n\n"
        f"{content.strip()}\n"
    )
    with open(org_file, "a") as fh:
        fh.write(entry)

    hail(f"Captured to {org_file}")
    on_screen(f"  Node ID: {node_id}")
    make_it_so()


@app.command()
def tag(
    force:  Annotated[bool, typer.Option("--force", help="Re-tag already-tagged nodes")] = False,
    limit:  Annotated[int,  typer.Option("--limit", "-n", help="Max nodes to tag")] = 50,
    apply:  Annotated[bool, typer.Option("--apply", help="Write tags back to org files")] = False,
):
    """Auto-tag untagged nodes using the fast_model."""
    from .db import Node
    from .llm import chat

    engine = _engine()
    with get_session(engine) as session:
        url   = _ollama_url(session)
        model = _cfg(session, "fast_model") or "phi4"

        q = session.query(Node)
        if not force:
            q = q.filter((Node.tags == "") | (Node.tags.is_(None)))
        nodes = q.limit(limit).all()

    if not nodes:
        on_screen("No untagged nodes found.")
        return

    hail(f"Auto-tagging {len(nodes)} nodes with [bold]{model}[/bold]")
    system = (
        "You are an org-mode expert. Given a note title and body, output ONLY a space-separated "
        "list of lowercase org-mode tags (no colons, no explanation). "
        "Max 5 tags. Example: python programming tools"
    )

    tagged = 0
    with get_session(engine) as session:
        with impulse("Tagging", total=len(nodes)) as (prog, task):
            for node in nodes:
                text = f"{node.title}\n{node.body[:500]}"
                try:
                    result = chat(text, model=model, base_url=url, system=system)
                    new_tags = " ".join(
                        t.strip().lower().replace(":", "")
                        for t in result.strip().split()
                        if t.strip()
                    )
                    db_node = session.get(Node, node.id)
                    if db_node:
                        db_node.tags = new_tags
                        session.commit()
                    tagged += 1
                except Exception:
                    session.rollback()
                prog.advance(task)

    hail(f"Tagged {tagged} nodes.")
    if apply:
        on_screen("[warn]--apply (write to org files) not yet implemented.[/warn]")
    make_it_so()


@app.command()
def code(
    task:     Annotated[str,  typer.Argument(help="What to generate")],
    lang:     Annotated[str,  typer.Option("--lang", "-l",
              help="Language: python | sh | elisp | sql | rust")] = "python",
    model:    Annotated[str,  typer.Option("--model", "-m",
              help="Override model")] = "",
    context:  Annotated[bool, typer.Option("--context", "-c",
              help="Retrieve relevant org notes as context")] = True,
    output:   Annotated[str,  typer.Option("--output", "-o",
              help="Write generated code to file")] = "",
):
    """Generate code for an org/roam task using the code model."""
    from .llm import chat, embed
    from .search import vector_search
    from rich.syntax import Syntax

    engine = _engine()
    with get_session(engine) as session:
        url       = _ollama_url(session)
        embed_mdl = _cfg(session, "embed_model") or "nomic-embed-text"
        code_mdl  = model or _cfg(session, "code_model") or "qwen2.5-coder"

        ctx_text = ""
        if context:
            try:
                with warp("Retrieving org context"):
                    qvec    = embed(task, model=embed_mdl, base_url=url)
                    results = vector_search(session, qvec, limit=4)
                if results:
                    ctx_text = "\n\n".join(
                        f"# {r.title}\n{r.body[:600]}" for r in results
                    )
            except Exception:
                pass

    system = (
        f"You are an expert {lang} programmer who specialises in org-mode and Emacs tooling. "
        f"Output ONLY the {lang} code with no explanation or markdown fences. "
        f"The code should be complete and directly runnable."
    )
    prompt = task
    if ctx_text:
        prompt = f"Context from my org notes:\n\n{ctx_text}\n\n---\n\nTask: {task}"

    with warp(f"{TREK_MSGS['code']} [{code_mdl}]"):
        generated = chat(prompt, model=code_mdl, base_url=url, system=system)

    console.print()
    console.rule(f"[lcars2]{lang}  ·  {code_mdl}[/lcars2]")
    console.print(Syntax(generated, lang, theme="monokai", line_numbers=True))
    console.rule()

    if output:
        Path(output).write_text(generated)
        hail(f"Written to {output}")

    make_it_so()


# Register skill commands at import time so they appear in --help
from . import cli_skills as _cs
_cs.register(app)


def main():
    app()
# cli.py:1 ends here
