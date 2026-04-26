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

    # Nerd Font — check multiple locations
    from .ui import NERD_FONTS
    font_dirs = [
        Path("~/.local/share/fonts/NerdFonts").expanduser(),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    nf_files: list = []
    for fd in font_dirs:
        if fd.exists():
            nf_files += list(fd.rglob("*Nerd*")) + list(fd.rglob("*NFM*"))
    if nf_files:
        row(True,  "Nerd Font installed", f"{len(nf_files)} file(s) found")
    else:
        row(False, "Nerd Font installed", "run: org-llm install --skip-ollama --skip-models")
    if not NERD_FONTS:
        warn_row("Nerd Font detection", "icons may show as □ — set ORG_LLM_NERD_FONTS=1 to override")

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
        "[bold lcars1]Welcome aboard, officer.[/bold lcars1]\n\n"
        "[lcars1]org-llm[/lcars1] is your personal LLM-powered second brain, "
        "built entirely on your org-roam notes.\n\n"
        "  ✦ Runs 100% locally — Ollama serves all models, nothing leaves your machine.\n"
        "  ✦ SQLite stores the index and config — one file, zero infra.\n"
        "  ✦ sqlite-vec provides vector search inside that same file.\n"
        "  ✦ Skills let you define LLM workflows as org-babel blocks.\n"
        "  ✦ dbt transforms raw indexed data into analytics-ready views.\n"
        "  ✦ Doom Emacs integration gives you SPC l bindings for everything.\n\n"
        "Navigate with: [bold]org-llm tutor <step>[/bold]\n"
        "All steps:     [bold]org-llm tutor --all[/bold]\n"
        "Steps: welcome → init → index → embed → search → ask → capture → tag → code\n"
        "       → config → skills → report → doctor → install → source → emacs → done",
    ),
    (
        "init",
        "[lcars2]org-llm init[/lcars2] — bootstrap your installation\n\n"
        "Creates the SQLite database at [bold]~/.local/share/org-llm/org-llm.db[/bold]\n"
        "and writes default config values into it. Safe to re-run (idempotent).\n\n"
        "[lcars1]What gets created:[/lcars1]\n"
        "  tables: files, nodes, history, config, skills\n"
        "  config defaults: org_dir=~/org, ollama_url, all model assignments\n\n"
        "[lcars1]Try it:[/lcars1]  [bold]org-llm init[/bold]\n\n"
        "[lcars1]Override DB path:[/lcars1]\n"
        "  [bold]ORG_LLM_DB=/tmp/test.db org-llm init[/bold]\n"
        "  (useful for testing without touching your real DB)\n\n"
        "[dim]Source: org_llm/db.py → init_db()  |  org-llm source db[/dim]",
    ),
    (
        "index",
        "[lcars2]org-llm index[/lcars2] — parse org files into the database\n\n"
        "Walks every .org file under org_dir, extracts:\n"
        "  • file-level #+TITLE and body\n"
        "  • each heading: title, body text, :ID: property, tags\n"
        "  • file mtime (used to skip unchanged files on re-runs)\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm index[/bold]          — incremental (skip unchanged files)\n"
        "  [bold]org-llm index --force[/bold]  — wipe and re-index everything\n\n"
        "[lcars1]What goes in the DB:[/lcars1]\n"
        "  files table  → one row per .org file (path, mtime, node_count)\n"
        "  nodes table  → one row per heading/file (title, body, tags, mtime)\n\n"
        "[lcars1]Check result:[/lcars1]  [bold]org-llm report overview[/bold]\n\n"
        "[dim]Source: org_llm/indexer.py  |  org-llm source indexer[/dim]",
    ),
    (
        "embed",
        "[lcars2]org-llm embed[/lcars2] — generate vector embeddings\n\n"
        "For every node in the DB, calls the embed_model (default: nomic-embed-text)\n"
        "and stores a 768-dimensional float32 vector in the nodes.embedding column.\n\n"
        "[lcars1]Why embeddings?[/lcars1]\n"
        "  An embedding turns text into a point in high-dimensional space.\n"
        "  Semantically similar text lands near each other. This is what powers\n"
        "  'search' and 'ask' — instead of matching words, we match meaning.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm embed[/bold]          — embed only new/unembedded nodes\n"
        "  [bold]org-llm embed --force[/bold]  — re-embed everything\n\n"
        "[lcars1]Check progress:[/lcars1]  [bold]org-llm doctor[/bold]\n\n"
        "[dim]Large vaults (10k+ nodes) take 10-30 min on first run.[/dim]\n"
        "[dim]Source: org_llm/indexer.py → embed_nodes()  |  org-llm source indexer[/dim]",
    ),
    (
        "search",
        "[lcars2]org-llm search <query>[/lcars2] — find relevant notes\n\n"
        "[lcars1]Semantic search (default):[/lcars1]\n"
        "  Embeds your query → finds nearest nodes by cosine distance.\n"
        "  Works even if no words overlap — it matches meaning.\n\n"
        "[lcars1]Keyword search:[/lcars1]\n"
        "  SQL LIKE match on title, body, and tags. Fast, exact.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm search 'Emacs configuration'[/bold]          — semantic\n"
        "  [bold]org-llm search --keyword python[/bold]               — keyword\n"
        "  [bold]org-llm search 'machine learning' --limit 20[/bold]  — top 20\n\n"
        "[lcars1]Output columns:[/lcars1]  Score | Title | Tags | File\n"
        "  Score = cosine distance (lower = more similar, 0.0 = identical)\n\n"
        "[dim]Requires embeddings. Run 'org-llm embed' first for semantic search.[/dim]\n"
        "[dim]Source: org_llm/search.py  |  org-llm source search[/dim]",
    ),
    (
        "ask",
        "[lcars2]org-llm ask <question>[/lcars2] — RAG over your org notes\n\n"
        "[lcars1]How it works (RAG = Retrieval-Augmented Generation):[/lcars1]\n"
        "  1. Your question is embedded into a vector\n"
        "  2. The top-K nearest nodes are retrieved from the DB\n"
        "  3. Their titles + bodies are injected into a prompt\n"
        "  4. The chat_model answers using your notes as context\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm ask 'What did I write about Emacs?'[/bold]\n"
        "  [bold]org-llm ask 'Summarise my notes on Python' --top-k 10[/bold]\n"
        "  [bold]org-llm ask 'Plan my week' --reason[/bold]   ← uses deepseek-r1\n"
        "  [bold]org-llm ask 'X' --context[/bold]             ← shows which nodes were used\n\n"
        "[lcars1]Models used:[/lcars1]\n"
        "  embed_model   → query embedding (nomic-embed-text)\n"
        "  chat_model    → answer generation (llama3.3)\n"
        "  reason_model  → used with --reason (deepseek-r1)\n\n"
        "[dim]Source: org_llm/cli.py → ask()  |  org-llm source cli[/dim]",
    ),
    (
        "capture",
        "[lcars2]org-llm capture[/lcars2] — add a new note to your vault\n\n"
        "Prompts for a title and body, optionally polishes the content with an LLM\n"
        "(instruct_model), then appends a properly-formatted org heading with a\n"
        "UUID :ID: property to a target file.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm capture[/bold]                           — interactive prompts\n"
        "  [bold]org-llm capture --title 'Meeting notes' --body 'Discussed X'[/bold]\n"
        "  [bold]org-llm capture --no-polish[/bold]               — skip LLM formatting\n"
        "  [bold]org-llm capture --file 'projects/work.org'[/bold] — custom target file\n\n"
        "[lcars1]Output format (appended to inbox.org):[/lcars1]\n"
        "  * Your Title\n"
        "  :PROPERTIES:\n"
        "  :ID: <uuid4>\n"
        "  :CREATED: [20260426120000]\n"
        "  :END:\n\n"
        "  <polished org-mode content>\n\n"
        "[dim]Run 'org-llm index' after capturing to add the new node to the DB.[/dim]\n"
        "[dim]Source: org_llm/cli.py → capture()  |  org-llm source cli[/dim]",
    ),
    (
        "tag",
        "[lcars2]org-llm tag[/lcars2] — auto-tag nodes with the fast_model\n\n"
        "Finds untagged nodes (or all nodes with --force), sends each node's\n"
        "title + body to the fast_model, and stores the suggested tags in the DB.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm tag[/bold]              — tag up to 50 untagged nodes\n"
        "  [bold]org-llm tag --limit 200[/bold]  — process more at once\n"
        "  [bold]org-llm tag --force[/bold]      — re-tag even already-tagged nodes\n\n"
        "[lcars1]How it works:[/lcars1]\n"
        "  The model is asked to output ONLY space-separated lowercase tags.\n"
        "  Tags are stored in nodes.tags in the DB.\n"
        "  Use 'org-llm tag --apply' (planned) to write them back to .org files.\n\n"
        "[lcars1]Check results:[/lcars1]  [bold]org-llm report tags[/bold]\n\n"
        "[dim]Model used: fast_model (phi4 by default) — fast, low memory.[/dim]\n"
        "[dim]Source: org_llm/cli.py → tag()  |  org-llm source cli[/dim]",
    ),
    (
        "code",
        "[lcars2]org-llm code <task>[/lcars2] — generate code from org context\n\n"
        "Describes a coding task, optionally retrieves relevant org notes as context,\n"
        "then generates code using the code_model. Output is syntax-highlighted.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm code 'parse an org file and list all headings'[/bold]\n"
        "  [bold]org-llm code 'backup my org dir' --lang sh[/bold]\n"
        "  [bold]org-llm code 'roam node query' --lang elisp[/bold]\n"
        "  [bold]org-llm code 'X' --output my_script.py[/bold]   ← write to file\n"
        "  [bold]org-llm code 'X' --no-context[/bold]            ← no org retrieval\n\n"
        "[lcars1]Supported languages:[/lcars1]  python | sh | elisp | sql | rust\n\n"
        "[lcars1]How context works:[/lcars1]\n"
        "  Your task description is embedded → top-4 nearest nodes are fetched\n"
        "  and prepended as context for the code_model.\n\n"
        "[dim]Model: code_model (qwen2.5-coder by default).[/dim]\n"
        "[dim]Source: org_llm/cli.py → code()  |  org-llm source cli[/dim]",
    ),
    (
        "config",
        "[lcars2]org-llm config[/lcars2] — view and change settings\n\n"
        "All config is stored in the SQLite DB. No config files to edit.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm config[/bold]                         — show all keys\n"
        "  [bold]org-llm config chat_model[/bold]              — show one value\n"
        "  [bold]org-llm config chat_model llama3.2[/bold]     — set a value\n\n"
        "[lcars1]All config keys:[/lcars1]\n"
        "  org_dir         path to scan for .org files  (default: ~/org)\n"
        "  ollama_url      Ollama API endpoint           (default: localhost:11434)\n"
        "  embed_model     embedding model               (nomic-embed-text)\n"
        "  chat_model      ask / general Q&A             (llama3.3)\n"
        "  code_model      code generation               (qwen2.5-coder)\n"
        "  reason_model    planning, complex reasoning   (deepseek-r1)\n"
        "  fast_model      tagging, classification       (phi4)\n"
        "  instruct_model  capture, instruction follow   (mistral-nemo)\n"
        "  text_model      summarisation, analysis       (gemma3)\n"
        "  embed_dim       embedding dimensions          (768)\n\n"
        "[dim]Source: org_llm/db.py → MODEL_DEFAULTS  |  org-llm source db[/dim]",
    ),
    (
        "skills",
        "[lcars2]Skills[/lcars2] — define reusable LLM workflows in org-mode\n\n"
        "Skills are org-babel source blocks tagged [bold]:skill:[/bold].\n"
        "You write them in your org notes, and org-llm discovers and runs them.\n\n"
        "[lcars1]Anatomy of a skill:[/lcars1]\n\n"
        "  * Summarise a note                        :skill:\n"
        "  :PROPERTIES:\n"
        "  :SKILL_NAME:  summarise\n"
        "  :SKILL_LANG:  python       ← python or sh\n"
        "  :SKILL_MODEL: text_model   ← which model config key to use\n"
        "  :END:\n\n"
        "  #+begin_src python\n"
        "  # {{input}} → replaced with your CLI argument at runtime\n"
        "  # llm.chat(prompt) → calls the configured model, returns a string\n"
        "  # cfg → dict of all config values (keys from 'org-llm config')\n"
        "  prompt = f'Summarise in 2 sentences:\\n\\n{{input}}'\n"
        "  print(llm.chat(prompt))\n"
        "  #+end_src\n\n"
        "[lcars1]Scaffold → register → run:[/lcars1]\n"
        "  [bold]org-llm skill-new my_skill[/bold]              — create template in skills.org\n"
        "  [bold]org-llm skill-new my_skill --lang sh[/bold]    — shell skill template\n"
        "  [bold]org-llm skill-index[/bold]                     — scan files, register in DB\n"
        "  [bold]org-llm skills[/bold]                          — list all registered skills\n"
        "  [bold]org-llm skill my_skill 'input text'[/bold]     — run with literal input\n"
        "  [bold]org-llm skill my_skill --node 'Emacs'[/bold]   — use a node body as input\n\n"
        "[lcars1]Shell skill example:[/lcars1]\n\n"
        "  :SKILL_LANG: sh\n"
        "  #+begin_src sh\n"
        "  echo 'Input was: {{input}}'\n"
        "  #+end_src\n\n"
        "[dim]Skills live in your org vault — version-control them with your notes.[/dim]\n"
        "[dim]Source: org_llm/skills.py  |  org-llm source skills[/dim]",
    ),
    (
        "report",
        "[lcars2]org-llm report[/lcars2] — rich text analytics on your vault\n\n"
        "[lcars1]Sections:[/lcars1]\n"
        "  [bold]overview[/bold]  files indexed, node count, embedding %, tag sets\n"
        "  [bold]tags[/bold]      tag frequency leaderboard with bar chart\n"
        "  [bold]recent[/bold]    nodes modified in the last N days (--days 14)\n"
        "  [bold]orphans[/bold]   nodes with org-roam IDs but no outgoing links\n"
        "  [bold]daily[/bold]     recent daily notes with preview\n"
        "  [bold]all[/bold]       all of the above in sequence\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm report[/bold]                 — all sections\n"
        "  [bold]org-llm report overview[/bold]        — just the stat panels\n"
        "  [bold]org-llm report recent --days 30[/bold] — 30-day window\n\n"
        "[lcars1]dbt integration:[/lcars1]\n"
        "  The raw tables are also transformed by dbt into mart views:\n"
        "  nodes_by_tag, recent_nodes, orphan_nodes, daily_notes.\n"
        "  Run [bold]dbt run[/bold] from ~/repos/org-llm/dbt/ after indexing.\n\n"
        "[dim]Source: org_llm/report.py  |  org-llm source report[/dim]",
    ),
    (
        "doctor",
        "[lcars2]org-llm doctor[/lcars2] — health check\n\n"
        "Runs a battery of checks and displays ✓ / ✗ for each:\n\n"
        "  [bold]Database reachable[/bold]   — SQLite file exists and is readable\n"
        "  [bold]sqlite-vec loaded[/bold]    — vector extension imported OK\n"
        "  [bold]org_dir exists[/bold]       — your org directory is found\n"
        "  [bold]org files found[/bold]      — .org files present in org_dir\n"
        "  [bold]Index populated[/bold]      — files + nodes in DB\n"
        "  [bold]Embeddings present[/bold]   — % of nodes with embeddings\n"
        "  [bold]Ollama reachable[/bold]     — API responding at ollama_url\n"
        "  [bold]model: <key>[/bold]         — each configured model is pulled\n"
        "  [bold]Nerd Font installed[/bold]  — fonts in ~/.local/share/fonts/\n\n"
        "[lcars1]Command:[/lcars1]  [bold]org-llm doctor[/bold]\n\n"
        "Run this first when something seems wrong. Every ✗ has a fix.\n\n"
        "[dim]Source: org_llm/cli.py → doctor()  |  org-llm source cli[/dim]",
    ),
    (
        "install",
        "[lcars2]org-llm install[/lcars2] — one-shot bootstrap\n\n"
        "Downloads and installs everything you need to ~/.local (no sudo):\n\n"
        "  [bold]Ollama[/bold]      binary → ~/.local/bin/ollama, starts 'ollama serve'\n"
        "  [bold]Models[/bold]      pulls all configured models via 'ollama pull'\n"
        "  [bold]Nerd Font[/bold]   NotoMono from ryanoasis/nerd-fonts → ~/.local/share/fonts/\n"
        "  [bold]opencode[/bold]    AI coding agent from opencode.ai → ~/.local/bin/\n\n"
        "[lcars1]Flags:[/lcars1]\n"
        "  --skip-ollama     skip Ollama binary download\n"
        "  --skip-models     skip model pulls\n"
        "  --skip-fonts      skip font download\n"
        "  --skip-opencode   skip opencode installation\n\n"
        "[lcars1]Command:[/lcars1]  [bold]org-llm install[/bold]\n\n"
        "[dim]After install, run: org-llm init → index → embed → doctor[/dim]\n"
        "[dim]Source: org_llm/cli.py → install()  |  org-llm source cli[/dim]",
    ),
    (
        "dbt",
        "[lcars2]dbt layer[/lcars2] — SQL transformations on your org index\n\n"
        "org-llm uses dbt (data build tool) to transform the raw indexed tables\n"
        "into analytics-ready views and tables in the same SQLite database.\n\n"
        "[lcars1]Architecture:[/lcars1]\n"
        "  Python indexer writes raw data into: files, nodes, history, config\n"
        "  dbt reads those raw tables and produces:\n\n"
        "  [bold]staging/[/bold]\n"
        "    stg_nodes    — clean nodes: relative paths, formatted dates, has_embedding flag\n"
        "    stg_files    — files with days_since_modified, formatted indexed_at\n\n"
        "  [bold]marts/[/bold]\n"
        "    nodes_by_tag   — tag → node count + titles (powers report tags)\n"
        "    recent_nodes   — nodes modified in last 30 days\n"
        "    orphan_nodes   — nodes with IDs but no incoming links\n"
        "    daily_notes    — files under /daily/ path\n\n"
        "[lcars1]How to run dbt:[/lcars1]\n"
        "  [bold]cd ~/repos/org-llm/dbt[/bold]\n"
        "  [bold]dbt run[/bold]            — build all models\n"
        "  [bold]dbt run --select staging[/bold]  — only staging models\n"
        "  [bold]dbt test[/bold]           — run data tests\n\n"
        "[lcars1]Configuration:[/lcars1]\n"
        "  dbt/profiles.yml points at ORG_LLM_DB (default: ~/.local/share/org-llm/org-llm.db)\n"
        "  Override with: ORG_LLM_DB=/path/to/other.db dbt run\n\n"
        "[dim]dbt is installed as a dependency — 'uv run dbt run' also works.[/dim]",
    ),
    (
        "opencode",
        "[lcars2]opencode integration[/lcars2] — AI coding agent\n\n"
        "opencode (opencode.ai) is an open-source terminal AI coding agent.\n"
        "org-llm installs it and complements it in several ways:\n\n"
        "[lcars1]How they work together:[/lcars1]\n"
        "  org-llm code   → generates code from your org notes as context\n"
        "  opencode       → interactive agent that edits files, runs tests\n\n"
        "  Typical flow:\n"
        "  1. [bold]org-llm code 'parse org files into JSON' --output task.py[/bold]\n"
        "     org-llm uses your notes as context and generates a starting point\n"
        "  2. [bold]opencode[/bold]\n"
        "     opencode picks up task.py, you describe refinements interactively\n\n"
        "[lcars1]Install opencode:[/lcars1]  [bold]org-llm install --skip-ollama --skip-models --skip-fonts[/bold]\n\n"
        "[lcars1]Use opencode with your org vault context:[/lcars1]\n"
        "  org-llm's DB knows your entire note history. You can generate\n"
        "  relevant context snippets: [bold]org-llm search 'X' --keyword[/bold]\n"
        "  and paste them into an opencode session for richer coding help.\n\n"
        "[dim]opencode is installed to ~/.local/bin/opencode — no sudo needed.[/dim]",
    ),
    (
        "source",
        "[lcars2]org-llm source <module>[/lcars2] — inspect org-llm's own code\n\n"
        "Shows the source of any org-llm module with syntax highlighting.\n"
        "With --explain, the LLM explains what the code does.\n\n"
        "[lcars1]Available modules:[/lcars1]\n"
        "  cli        — all CLI commands (Typer app)\n"
        "  db         — SQLAlchemy models + init_db\n"
        "  indexer    — org file parser + embed_nodes\n"
        "  llm        — Ollama client wrapper\n"
        "  search     — vector_search + keyword_search\n"
        "  skills     — skill discovery + execution engine\n"
        "  cli_skills — skill CLI commands\n"
        "  report     — report_overview, report_tags, etc.\n"
        "  ui         — Rich theming + progress bars\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm source db[/bold]               — show db.py with line numbers\n"
        "  [bold]org-llm source indexer --explain[/bold] — show + LLM explanation\n"
        "  [bold]org-llm source llm[/bold]               — the Ollama wrapper (it's tiny!)\n\n"
        "[dim]This command uses Python's inspect module — always shows live code.[/dim]",
    ),
    (
        "emacs",
        "[lcars2]Doom Emacs integration[/lcars2]\n\n"
        "Load from config.el:  [bold](load! \"~/repos/org-llm/doom/org-llm\")[/bold]\n\n"
        "[lcars1]Keybindings (all under SPC l):[/lcars1]\n\n"
        "  [lcars2]SPC l a[/lcars2]   org-llm-ask          — ask a question, answer in side window\n"
        "  [lcars2]SPC l A[/lcars2]   ask (reason model)   — use deepseek-r1 for hard questions\n"
        "  [lcars2]SPC l s[/lcars2]   org-llm-search       — semantic search, results in side window\n"
        "  [lcars2]SPC l S[/lcars2]   keyword search       — fast SQL-based search\n"
        "  [lcars2]SPC l r[/lcars2]   org-llm-report       — open report in vterm\n"
        "  [lcars2]SPC l i[/lcars2]   org-llm-index        — re-index org files\n"
        "  [lcars2]SPC l e[/lcars2]   org-llm-embed        — generate embeddings\n"
        "  [lcars2]SPC l m[/lcars2]   org-llm-models       — list models in side window\n"
        "  [lcars2]SPC l .[/lcars2]   org-llm-ask-dwim     — ask about region or sentence at point\n\n"
        "[lcars1]How results appear:[/lcars1]\n"
        "  ask / search / models → side window (right, 45% width), ANSI-coloured\n"
        "  index / embed / report → dedicated vterm buffer (bottom, 35% height)\n\n"
        "[dim]Source: doom/org-llm.el  |  org-llm source cli (org-llm-ask etc.)[/dim]",
    ),
    (
        "done",
        "[bold lcars1]You're ready to explore your second brain.[/bold lcars1]\n\n"
        "[lcars1]Recommended first flight:[/lcars1]\n\n"
        "  1. [bold]org-llm install[/bold]         — Ollama + models + fonts + opencode\n"
        "  2. [bold]org-llm init[/bold]             — create DB + default config\n"
        "  3. [bold]org-llm index[/bold]            — parse all org files into DB\n"
        "  4. [bold]org-llm embed[/bold]            — generate embeddings (takes a while)\n"
        "  5. [bold]org-llm doctor[/bold]           — verify everything is green\n"
        "  6. [bold]org-llm ask 'What did I write about X?'[/bold]\n\n"
        "[lcars1]Explore further:[/lcars1]\n"
        "  [bold]org-llm report all[/bold]          — analytics on your vault\n"
        "  [bold]org-llm skill-new my_skill[/bold]  — create your first skill\n"
        "  [bold]org-llm tag[/bold]                 — auto-tag untagged nodes\n"
        "  [bold]org-llm source indexer --explain[/bold] — understand how it works\n"
        "  [bold]org-llm tutor --all[/bold]          — read the whole manual\n\n"
        "Engage. ☭ ✊ 🏳️‍🌈 — Queer, collective, free.",
    ),
]


@app.command()
def tutor(
    step: Annotated[str, typer.Argument(
        help="Step name to jump to (welcome/init/index/embed/search/ask/capture/"
             "tag/code/config/skills/report/doctor/install/source/emacs/done)"
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


_MODULE_MAP = {
    "cli":        "org_llm.cli",
    "db":         "org_llm.db",
    "indexer":    "org_llm.indexer",
    "llm":        "org_llm.llm",
    "search":     "org_llm.search",
    "skills":     "org_llm.skills",
    "cli_skills": "org_llm.cli_skills",
    "report":     "org_llm.report",
    "ui":         "org_llm.ui",
}


@app.command()
def source(
    module:  Annotated[str,  typer.Argument(
             help="Module to show: cli | db | indexer | llm | search | skills | report | ui"
    )],
    explain: Annotated[bool, typer.Option("--explain", "-e",
             help="Use LLM to explain the source")] = False,
    model:   Annotated[str,  typer.Option("--model", "-m",
             help="Override model for --explain")] = "",
):
    """Show org-llm's own source code with syntax highlighting, optionally explained by LLM."""
    import importlib
    import inspect
    from rich.syntax import Syntax
    from rich.panel import Panel

    if module not in _MODULE_MAP:
        red_alert(
            f"Unknown module {module!r}. "
            f"Available: {', '.join(_MODULE_MAP)}"
        )
        raise typer.Exit(1)

    mod = importlib.import_module(_MODULE_MAP[module])
    src = inspect.getsource(mod)
    path = inspect.getfile(mod)

    console.print()
    console.rule(f"[lcars2]{path}[/lcars2]")
    console.print(Syntax(src, "python", theme="monokai", line_numbers=True))
    console.rule(f"[dim]{len(src.splitlines())} lines[/dim]")

    if explain:
        engine = _engine()
        with get_session(engine) as session:
            url      = _ollama_url(session)
            chat_mdl = model or _cfg(session, "chat_model") or "llama3.3"

        from .llm import chat
        system = (
            "You are an expert Python developer and Emacs/org-mode enthusiast. "
            "Explain the following Python module from the org-llm codebase. "
            "Cover: what it does, its key functions/classes, design decisions, "
            "and how it fits into the overall system. Be clear and concrete."
        )
        prompt = f"Module: {module}\n\n```python\n{src[:6000]}\n```"
        with warp(f"Explaining {module} with {chat_mdl}"):
            explanation = chat(prompt, model=chat_mdl, base_url=url, system=system)

        console.print()
        console.rule(f"[lcars1]{chat_mdl} explains {module}[/lcars1]")
        console.print(explanation)
        console.rule()


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
