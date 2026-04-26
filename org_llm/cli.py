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


def _opencode_bin() -> Path | None:
    """Return path to opencode binary if it exists anywhere on PATH or known locations."""
    import shutil
    found = shutil.which("opencode")
    if found:
        return Path(found)
    for candidate in [
        Path("~/.opencode/bin/opencode").expanduser(),
        Path("~/.local/bin/opencode").expanduser(),
    ]:
        if candidate.exists():
            return candidate
    return None


def _install_opencode_bin(bin_dir: Path) -> Path | None:
    """Download and install opencode using its official installer. Returns path or None."""
    import subprocess
    import os
    # The opencode installer always installs to ~/.opencode/bin regardless of env vars
    default_install = Path("~/.opencode/bin/opencode").expanduser()
    install_sh = bin_dir / "_opencode_install.sh"
    try:
        dl = subprocess.run(
            ["curl", "-fsSL", "-o", str(install_sh), "https://opencode.ai/install"],
            capture_output=True, text=True, timeout=30,
        )
        if dl.returncode != 0:
            red_alert(f"opencode download failed: {dl.stderr.strip()[:120]}")
            return None
        install_sh.chmod(0o755)
        run = subprocess.run(
            ["sh", str(install_sh)],
            capture_output=True, text=True,
            env={**os.environ},
        )
        install_sh.unlink(missing_ok=True)
        result = _opencode_bin()
        if result:
            hail(f"opencode installed at {result}")
            return result
        red_alert(f"opencode install failed: {run.stderr.strip()[:200]}")
        return None
    except Exception as exc:
        install_sh.unlink(missing_ok=True)
        red_alert(f"opencode install error: {exc}")
        return None


def _gh_bin() -> str | None:
    """Return path to gh CLI, or None if not available."""
    import shutil
    return shutil.which("gh") or (
        str(Path("~/.local/bin/gh").expanduser())
        if Path("~/.local/bin/gh").expanduser().exists() else None
    )


def _claude_bin() -> Path | None:
    """Return path to claude CLI if installed anywhere."""
    import shutil
    found = shutil.which("claude")
    if found:
        return Path(found)
    for candidate in [
        Path("~/.claude/bin/claude").expanduser(),
        Path("~/.local/bin/claude").expanduser(),
        Path("~/.npm-global/bin/claude").expanduser(),
        Path("~/.local/share/npm/bin/claude").expanduser(),
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.exists():
            return candidate
    return None


def _install_claude_bin() -> Path | None:
    """Install claude CLI via npm. Returns path or None."""
    import subprocess, shutil
    npm = shutil.which("npm")
    if not npm:
        red_alert("npm not found — install Node.js to get Claude Code  (nodejs.org)")
        return None
    hail("Installing @anthropic-ai/claude-code via npm…")
    result = subprocess.run(
        [npm, "install", "-g", "@anthropic-ai/claude-code"],
        timeout=120,
    )
    if result.returncode != 0:
        red_alert("claude install failed — check npm output above")
        return None
    return _claude_bin()


def _install_gh(bin_dir: Path) -> Path | None:
    """Download and install gh CLI to bin_dir. Returns path or None on failure."""
    import platform
    import urllib.request
    import json

    arch = platform.machine().lower()
    arch_slug = "amd64" if arch in ("x86_64", "amd64") else "arm64"
    try:
        with urllib.request.urlopen(
            "https://api.github.com/repos/cli/cli/releases/latest", timeout=10
        ) as r:
            release = json.loads(r.read())
        version = release["tag_name"].lstrip("v")
        url = (
            f"https://github.com/cli/cli/releases/download/v{version}/"
            f"gh_{version}_linux_{arch_slug}.tar.gz"
        )
        tmp = Path("/tmp/gh.tar.gz")
        with warp("Downloading gh CLI"):
            urllib.request.urlretrieve(url, tmp)
        import subprocess, tarfile
        with tarfile.open(tmp) as tf:
            for member in tf.getmembers():
                if member.name.endswith("/bin/gh"):
                    member.name = "gh"
                    tf.extract(member, path=bin_dir)
                    break
        gh = bin_dir / "gh"
        gh.chmod(0o755)
        tmp.unlink(missing_ok=True)
        hail(f"gh CLI v{version} installed at {gh}")
        return gh
    except Exception as exc:
        red_alert(f"gh install failed: {exc}")
        return None


@app.command()
def install(
    skip_ollama:   Annotated[bool, typer.Option("--skip-ollama")]   = False,
    skip_models:   Annotated[bool, typer.Option("--skip-models")]   = False,
    skip_fonts:    Annotated[bool, typer.Option("--skip-fonts")]    = False,
    skip_opencode: Annotated[bool, typer.Option("--skip-opencode")] = False,
    skip_gh:       Annotated[bool, typer.Option("--skip-gh")]       = False,
    skip_claude:   Annotated[bool, typer.Option("--skip-claude")]   = False,
):
    """Install Ollama, models, Nerd Fonts, opencode, gh CLI, and Claude Code."""
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
        existing = _opencode_bin()
        if existing:
            hail(f"opencode already installed — skipping. ({existing})")
        else:
            hail("Installing opencode (AI coding agent)…")
            _install_opencode_bin(bin_dir)

    # ── gh CLI ────────────────────────────────────────────────────────────────
    if not skip_gh:
        if _gh_bin():
            hail(f"gh CLI already installed — skipping. ({_gh_bin()})")
        else:
            hail("Installing gh CLI (GitHub CLI)…")
            _install_gh(bin_dir)

    # ── Claude Code CLI ───────────────────────────────────────────────────────
    if not skip_claude:
        existing = _claude_bin()
        if existing:
            hail(f"Claude Code already installed — skipping. ({existing})")
        else:
            hail("Installing Claude Code CLI…")
            _install_claude_bin()

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
def doctor(
    diagnose: Annotated[bool, typer.Option("--diagnose", "-d",
              help="Use LLM to explain failures and suggest fixes")] = False,
    fix:      Annotated[bool, typer.Option("--fix",
              help="Auto-apply safe fixes (init DB, start Ollama)")] = False,
):
    """Deep health check: system, DB, index, Ollama, fonts — with LLM diagnosis."""
    import os
    import shutil
    import subprocess
    import sys
    import time
    from datetime import datetime
    from rich.panel import Panel
    from rich.rule  import Rule
    from rich.text  import Text
    from rich.table import Table
    from .db import Node, File
    from .ui import trans_stripe, PRIDE_BANNER, NERD_FONTS

    PASS = "[bold green]✓[/bold green]"
    FAIL = "[bold red]✗[/bold red]"
    WARN = "[bold yellow]⚠[/bold yellow]"
    INFO = "[dim]·[/dim]"

    issues:   list[str] = []   # failures for LLM diagnosis
    warnings: list[str] = []   # non-fatal
    checks:   list[tuple[str, str, str]] = []  # (status, label, detail)

    def ok(label: str, detail: str = "") -> None:
        checks.append((PASS, label, detail))

    def fail(label: str, detail: str = "", fix_hint: str = "") -> None:
        checks.append((FAIL, label, detail))
        msg = f"FAIL: {label}"
        if detail:
            msg += f" — {detail}"
        if fix_hint:
            msg += f". Fix: {fix_hint}"
        issues.append(msg)

    def warn(label: str, detail: str = "") -> None:
        checks.append((WARN, label, detail))
        warnings.append(f"WARN: {label} — {detail}")

    def info(label: str, detail: str = "") -> None:
        checks.append((INFO, label, detail))

    def section(title: str) -> None:
        checks.append(("", f"[lcars1]{title}[/lcars1]", ""))

    # ── System ─────────────────────────────────────────────────────────────────
    section("System")
    py = sys.version.split()[0]
    ok("Python", py) if tuple(int(x) for x in py.split(".")[:2]) >= (3, 11) else \
        fail("Python ≥ 3.11 required", py)

    uv_bin = shutil.which("uv")
    ok("uv", uv_bin or "") if uv_bin else warn("uv not on PATH", "install: pip install uv")

    ollama_bin = shutil.which("ollama") or str(Path("~/.local/bin/ollama").expanduser())
    ok("ollama binary", ollama_bin) if Path(ollama_bin).exists() else \
        fail("Ollama not installed", "~/.local/bin/ollama missing",
             "org-llm install --skip-models --skip-fonts")

    oc_path = _opencode_bin()
    if oc_path:
        ok("opencode", str(oc_path))
    else:
        warn("opencode not installed", "run: org-llm install --skip-ollama --skip-models --skip-fonts")

    # Disk space for DB and org_dir
    try:
        import shutil as _sh
        db_dir = DB_PATH.parent
        db_dir.mkdir(parents=True, exist_ok=True)
        usage = _sh.disk_usage(db_dir)
        free_gb = usage.free / 1_073_741_824
        db_size = DB_PATH.stat().st_size / 1_048_576 if DB_PATH.exists() else 0
        if free_gb < 1:
            fail("Disk space", f"{free_gb:.1f} GB free — very low",
                 "free up disk space")
        elif free_gb < 5:
            warn("Disk space", f"{free_gb:.1f} GB free; DB is {db_size:.0f} MB")
        else:
            ok("Disk space", f"{free_gb:.0f} GB free; DB is {db_size:.0f} MB")
    except Exception as e:
        warn("Disk space check failed", str(e))

    # ── Database ───────────────────────────────────────────────────────────────
    section("Database")
    engine = _engine()
    db_ok = False
    try:
        init_db(engine)
        db_ok = True
        ok("DB reachable", str(DB_PATH))
        if fix:
            hail("DB initialised (--fix)")
    except Exception as e:
        fail("DB not reachable", str(e), "org-llm init")
        if fix:
            hail("Attempting org-llm init …")
            try:
                init_db(make_engine(DB_PATH))
                ok("DB init (auto-fixed)", str(DB_PATH))
                db_ok = True
            except Exception as e2:
                fail("DB init failed", str(e2))

    if db_ok:
        # sqlite-vec
        try:
            import sqlite_vec  # noqa: F401
            ok("sqlite-vec extension")
        except Exception as e:
            fail("sqlite-vec not loadable", str(e),
                 "uv add sqlite-vec && uv run org-llm init")

        # PRAGMA integrity_check
        try:
            from sqlalchemy import text as _text
            with get_session(engine) as _s:
                result = _s.execute(_text("PRAGMA integrity_check")).scalar()
            if result == "ok":
                ok("DB integrity", "PRAGMA integrity_check = ok")
            else:
                fail("DB integrity", result or "unknown",
                     "backup DB, then: org-llm init --force")
        except Exception as e:
            warn("DB integrity check failed", str(e))

        # Index state
        with get_session(engine) as session:
            file_count  = session.query(File).count()
            node_count  = session.query(Node).count()
            embed_count = session.query(Node).filter(Node.embedding.isnot(None)).count()

            # Last index time
            last_file = session.query(File).order_by(File.indexed_at.desc()).first()
            last_ts = last_file.indexed_at if last_file else None

            # Stale files (in DB but not on disk)
            all_paths = [f.path for f in session.query(File).all()]
            stale = [p for p in all_paths if not Path(p).exists()]

        if node_count > 0:
            ok("Index populated", f"{file_count} files / {node_count} nodes")
        else:
            fail("Index empty", "no nodes found", "org-llm index")

        if last_ts:
            info("Last indexed", last_ts)

        if stale:
            warn("Stale DB records",
                 f"{len(stale)} file(s) in DB no longer exist on disk — "
                 "run: org-llm index --force")

        if node_count > 0:
            pct = int(embed_count / node_count * 100)
            if pct == 100:
                ok("Embeddings", f"{embed_count}/{node_count} (100%)")
            elif pct >= 80:
                warn("Embeddings partial", f"{embed_count}/{node_count} ({pct}%) — run: org-llm embed")
            else:
                fail("Embeddings low", f"{embed_count}/{node_count} ({pct}%)",
                     "org-llm embed")

    # ── Org Files ──────────────────────────────────────────────────────────────
    section("Org Files")
    with get_session(engine) as session:
        org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
    if org_dir.exists():
        org_files = list(org_dir.rglob("*.org"))
        ok("org_dir", str(org_dir))
        if org_files:
            ok("org files found", f"{len(org_files)} .org files")
            if db_ok:
                with get_session(engine) as session:
                    db_paths = {f.path for f in session.query(File).all()}
                unindexed = [p for p in org_files if str(p) not in db_paths]
                if unindexed:
                    warn("Unindexed files",
                         f"{len(unindexed)} .org file(s) not yet in DB — run: org-llm index")
                else:
                    ok("All org files indexed")
        else:
            fail("No org files", f"no .org files in {org_dir}", f"add .org files to {org_dir}")
    else:
        fail("org_dir missing", str(org_dir),
             f"mkdir -p {org_dir}  or  org-llm config org_dir /path/to/your/org")

    # ── Ollama ─────────────────────────────────────────────────────────────────
    section("Ollama")
    with get_session(engine) as session:
        url = _ollama_url(session)
    pulled_models: list[str] = []
    ollama_live = False
    try:
        from .llm import list_models
        pulled_models = list_models(url)
        ollama_live = True
        ok("Ollama API", url)
    except Exception as e:
        fail("Ollama not reachable", f"{url} — {e}",
             "ollama serve &  (or: org-llm install --skip-models --skip-fonts)")
        if fix and Path(ollama_bin).exists():
            hail("Starting ollama serve (--fix) …")
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            try:
                pulled_models = list_models(url)
                ollama_live = True
                ok("Ollama API (auto-started)", url)
            except Exception:
                fail("Ollama still unreachable after start attempt", url)

    if ollama_live:
        info("Pulled models", ", ".join(pulled_models) or "none")
        model_keys = [
            "embed_model", "chat_model", "code_model",
            "reason_model", "fast_model", "instruct_model", "text_model",
        ]
        with get_session(engine) as session:
            missing_models = []
            for key in model_keys:
                model = _cfg(session, key)
                if any(model in m for m in pulled_models):
                    ok(f"  {key}", model)
                else:
                    fail(f"  {key} not pulled", model,
                         f"ollama pull {model}")
                    missing_models.append(model)

        # Ping embed model to verify it actually responds
        with get_session(engine) as session:
            embed_mdl = _cfg(session, "embed_model") or "nomic-embed-text"
        if any(embed_mdl in m for m in pulled_models):
            try:
                from .llm import embed as _embed
                _embed("ping", model=embed_mdl, base_url=url)
                ok("  embed model ping", f"{embed_mdl} responded")
            except Exception as e:
                fail("  embed model unresponsive", str(e)[:80],
                     f"ollama pull {embed_mdl}")

    # ── Cloud / RunPod ─────────────────────────────────────────────────────────
    section("Cloud / RunPod")
    with get_session(engine) as session:
        cloud_provider = _cfg(session, "cloud_provider")
        cloud_endpoint = _cfg(session, "cloud_endpoint_url")
        cloud_api_key  = _cfg(session, "runpod_api_key")
        cloud_model_   = _cfg(session, "cloud_model") or _cfg(session, "chat_model") or "llama3.2"
    if cloud_endpoint:
        try:
            from .cloud import check_connection, assess_local_capability
            cs = check_connection(cloud_endpoint, cloud_api_key, cloud_model_)
            if cs.reachable and cs.auth_ok:
                ok("Cloud endpoint", f"{cloud_endpoint}  ({cs.latency_ms:.0f}ms)")
            elif cs.reachable:
                warn("Cloud auth failed", "check runpod_api_key config")
            else:
                fail("Cloud endpoint unreachable", cloud_endpoint,
                     "check pod is running: org-llm cloud --console")
        except Exception as e:
            warn("Cloud check failed", str(e)[:60])
    else:
        info("Cloud (RunPod)", "not configured — org-llm cloud --signup to set up")

    with get_session(engine) as session:
        all_models = [_cfg(session, k) for _, k, _ in _TASK_MODEL_KEYS if _cfg(session, k)]
    try:
        from .cloud import assess_local_capability, local_vram_gb
        vram = local_vram_gb()
        results = assess_local_capability(list(set(all_models)))
        cloud_needed = [r["model"] for r in results if not r["can_local"]]
        if cloud_needed:
            if cloud_endpoint:
                ok("Compute coverage", f"{len(cloud_needed)} model(s) offloaded to cloud")
            else:
                warn("Compute gap",
                     f"{len(cloud_needed)} model(s) need cloud: {', '.join(cloud_needed)}")
        else:
            gpu_str = f"{vram:.0f}GB VRAM" if vram else "CPU/RAM"
            ok("Local compute sufficient", f"{gpu_str} handles all models")
    except Exception as e:
        warn("Compute assessment failed", str(e)[:60])

    # ── gh CLI ─────────────────────────────────────────────────────────────────
    section("gh CLI")
    gh = _gh_bin()
    if gh:
        ok("gh installed", gh)
        try:
            result = subprocess.run(
                [gh, "auth", "status"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Extract logged-in account from output
                for line in (result.stdout + result.stderr).splitlines():
                    if "Logged in to" in line or "account" in line.lower():
                        info("gh auth", line.strip())
                        break
                else:
                    ok("gh auth", "authenticated")
            else:
                warn("gh not authenticated",
                     "run: gh auth login  (or: org-llm install --skip-ollama ...)")
        except Exception as e:
            warn("gh auth check failed", str(e))
    else:
        warn("gh CLI not installed",
             "run: org-llm install --skip-ollama --skip-models --skip-fonts --skip-opencode")

    # ── Claude Code ────────────────────────────────────────────────────────────
    section("Claude Code")
    claude = _claude_bin()
    if claude:
        ok("claude installed", str(claude))
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            ok("ANTHROPIC_API_KEY", f"set ({len(api_key)} chars)")
        else:
            warn("ANTHROPIC_API_KEY not set",
                 "needed for claude CLI — set in shell or ~/.profile")
    else:
        warn("Claude Code not installed",
             "run: org-llm install --skip-ollama --skip-models --skip-fonts --skip-opencode --skip-gh")

    # ── Fonts / UI ─────────────────────────────────────────────────────────────
    section("Fonts & UI")
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
        ok("Nerd Font files", f"{len(nf_files)} found")
    else:
        fail("Nerd Font not installed",
             "icons will show as □",
             "org-llm install --skip-ollama --skip-models")
    if NERD_FONTS:
        ok("Nerd Font detection", "icons enabled")
    else:
        warn("Nerd Font detection off",
             "icons disabled — set ORG_LLM_NERD_FONTS=1 to force-enable")

    # ── Render table ──────────────────────────────────────────────────────────
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("St", width=3)
    table.add_column("Check", style="lcars2")
    table.add_column("Detail", style="dim")
    for status, label, detail in checks:
        table.add_row(status, label, detail)

    console.print()
    console.print(trans_stripe(52))
    console.print(PRIDE_BANNER)
    console.print(Panel(table, title="[lcars1]org-llm doctor[/lcars1]",
                        border_style="lcars1"))

    fail_count = len(issues)
    warn_count = len(warnings)
    summary = (
        f"[bold green]{fail_count == 0 and 'All checks passed' or ''}[/bold green]"
        f"[bold red]{fail_count} failure(s)[/bold red]  " if fail_count else ""
    ) + (f"[bold yellow]{warn_count} warning(s)[/bold yellow]" if warn_count else "")
    if summary:
        console.print(f"  {summary.strip()}")

    console.print(trans_stripe(52))
    console.print()

    # ── LLM Diagnosis ─────────────────────────────────────────────────────────
    if (issues or diagnose) and ollama_live:
        with get_session(engine) as session:
            url = _ollama_url(session)
        # Use whichever model is actually pulled, prefer chat > fast > any
        with get_session(engine) as session:
            preferred = [
                _cfg(session, "chat_model"),
                _cfg(session, "fast_model"),
                _cfg(session, "text_model"),
            ]
        diag_model = next(
            (m for m in preferred if any(m in p for p in pulled_models)),
            pulled_models[0] if pulled_models else None,
        )
        if not diag_model:
            red_alert("No chat models pulled — cannot run LLM diagnosis.")
            on_screen("Pull a model first:  [bold]ollama pull llama3.2[/bold]  (small, fast)")
            return

        state_summary = "\n".join(
            [f"- {i}" for i in issues] +
            [f"- {w}" for w in warnings]
        ) or "All checks passed — user requested diagnosis anyway."

        system = (
            "You are an expert assistant for org-llm, a Python CLI tool that indexes org-roam "
            "notes and provides LLM-powered search and Q&A using Ollama. "
            "You are given a health-check report. Respond with:\n"
            "1. A brief plain-English explanation of each failure/warning\n"
            "2. Ordered fix steps with exact commands\n"
            "3. Any follow-up checks the user should run after fixing\n"
            "Be concise, specific, and helpful. Use plain text (no markdown)."
        )
        prompt = (
            f"org-llm health check results:\n\n{state_summary}\n\n"
            f"System: Python {sys.version.split()[0]}, Ollama at {url}\n"
            f"DB: {DB_PATH}\n"
            f"Org dir: {org_dir}\n"
        )

        from .llm import chat
        try:
            with warp(f"[lcars2]{diag_model}[/lcars2] diagnosing …"):
                diagnosis = chat(prompt, model=diag_model, base_url=url, system=system)
            console.print(Panel(
                diagnosis,
                title=f"[lcars1]LLM Diagnosis ({diag_model})[/lcars1]",
                border_style="lcars2",
                padding=(1, 2),
            ))
        except Exception as e:
            red_alert(f"LLM diagnosis failed ({diag_model}): {e}")
            on_screen("Pull a chat-capable model:  [bold]ollama pull llama3.2[/bold]")
        console.print()
    elif issues and not ollama_live:
        red_alert(
            f"{len(issues)} issue(s) found but Ollama is not running — "
            "start it with 'ollama serve' then run 'org-llm doctor --diagnose'"
        )


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
        "       → config → skills → report → doctor → install → dbt → opencode\n"
        "       → source → cloud → launch → emacs → claude → done",
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
        "  [bold]opencode[/bold]    AI coding agent from opencode.ai → ~/.local/bin/\n"
        "  [bold]gh CLI[/bold]      GitHub CLI from cli/cli releases → ~/.local/bin/\n\n"
        "[lcars1]Flags:[/lcars1]\n"
        "  --skip-ollama     skip Ollama binary download\n"
        "  --skip-models     skip model pulls\n"
        "  --skip-fonts      skip font download\n"
        "  --skip-opencode   skip opencode installation\n"
        "  --skip-gh         skip gh CLI installation\n\n"
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
        "[lcars2]org-llm launch[/lcars2] — opencode workspace with full vault context\n\n"
        "opencode (opencode.ai) is an interactive terminal AI coding agent.\n"
        "[bold]org-llm launch[/bold] transforms it into a fully-configured second brain workspace:\n\n"
        "[lcars1]What launch does:[/lcars1]\n"
        "  1. Writes [bold]{org_dir}/.opencode.json[/bold] with:\n"
        "       • Ollama provider (uses your configured chat_model)\n"
        "       • A rich system prompt injecting vault stats, recent nodes, skills\n"
        "       • MCP server: org-llm mcp (stdio) — all org-llm tools available natively\n"
        "  2. Prints a themed launch panel (LCARS)\n"
        "  3. exec-replaces itself with opencode in org_dir\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]\n"
        "  [lcars2]SPC l o[/lcars2]  → opens a vterm buffer running [bold]org-llm launch[/bold]\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]              — full vault context + opencode\n"
        "  [bold]org-llm launch --no-context[/bold] — minimal system prompt\n"
        "  [bold]org-llm launch --dry-run[/bold]    — preview .opencode.json without launching\n"
        "  [bold]org-llm launch --model phi4[/bold] — override model\n\n"
        "[lcars1]MCP tools available inside opencode:[/lcars1]\n"
        "  search_notes   ask_notes    capture_note   get_node\n"
        "  list_skills    run_skill    tangle_file    get_vault_stats\n"
        "  list_recent_nodes  list_nodes_by_tag  get_config\n"
        "  get_tutor_step  list_tutor_steps\n\n"
        "[lcars1]Use cases inside opencode:[/lcars1]\n"
        "  • 'What did I write about Python last month?'  → search_notes\n"
        "  • 'Summarise my project notes'                → ask_notes\n"
        "  • 'Save this idea to inbox.org'               → capture_note\n"
        "  • 'Run my summarise skill on this text'       → run_skill\n"
        "  • 'Tangle my config.org'                      → tangle_file\n"
        "  • 'Show me how embeddings work'               → get_tutor_step\n\n"
        "[dim]org-llm mcp   starts the MCP server standalone (for other clients too).[/dim]",
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
        "  [lcars2]SPC l o[/lcars2]   org-llm-launch       — open opencode workspace in full vterm\n"
        "  [lcars2]SPC l a[/lcars2]   org-llm-ask          — ask a question, answer in side window\n"
        "  [lcars2]SPC l A[/lcars2]   ask (reason model)   — use deepseek-r1 for hard questions\n"
        "  [lcars2]SPC l s[/lcars2]   org-llm-search       — semantic search, results in side window\n"
        "  [lcars2]SPC l S[/lcars2]   keyword search       — fast SQL-based search\n"
        "  [lcars2]SPC l c[/lcars2]   org-llm-capture      — capture note (prompts title + body)\n"
        "  [lcars2]SPC l r[/lcars2]   org-llm-report       — open report in vterm\n"
        "  [lcars2]SPC l i[/lcars2]   org-llm-index        — re-index org files\n"
        "  [lcars2]SPC l e[/lcars2]   org-llm-embed        — generate embeddings\n"
        "  [lcars2]SPC l m[/lcars2]   org-llm-models       — list models in side window\n"
        "  [lcars2]SPC l d[/lcars2]   org-llm-doctor       — health check in vterm\n"
        "  [lcars2]SPC l .[/lcars2]   org-llm-ask-dwim     — ask about region or sentence at point\n\n"
        "[lcars1]How results appear:[/lcars1]\n"
        "  SPC l o  → full window vterm (opencode takes over — q to quit)\n"
        "  ask / search / models → side window (right, 45% width), ANSI-coloured\n"
        "  index / embed / report / doctor → vterm side window (bottom, 35% height)\n\n"
        "[dim]Source: doom/org-llm.el  |  org-llm source cli[/dim]",
    ),
    (
        "cloud",
        "[lcars2]org-llm cloud[/lcars2] — RunPod cloud LLM backend\n\n"
        "When local Ollama can't run a model (not enough VRAM/RAM), the app can\n"
        "transparently fall back to a RunPod cloud endpoint.\n\n"
        "[lcars1]Setup flow:[/lcars1]\n"
        "  1. [bold]org-llm cloud --signup[/bold]      — opens runpod.io in browser\n"
        "  2. Deploy an Ollama template pod from their marketplace\n"
        "  3. [bold]org-llm cloud --configure[/bold]   — enter your pod URL + API key\n"
        "  4. [bold]org-llm cloud --test[/bold]         — verify connection\n\n"
        "[lcars1]Assessment:[/lcars1]\n"
        "  [bold]org-llm cloud --assess[/bold]  — shows which configured models fit locally\n"
        "  vs which require cloud. Checks GPU VRAM and RAM.\n\n"
        "[lcars1]Cost estimation:[/lcars1]\n"
        "  [bold]org-llm cloud --cost[/bold]   — RunPod GPU pricing + tokens/$ table\n\n"
        "[lcars1]Config keys set by --configure:[/lcars1]\n"
        "  cloud_provider      runpod\n"
        "  cloud_endpoint_url  https://{pod_id}-11434.proxy.runpod.net\n"
        "  runpod_api_key      (optional, for private pods)\n"
        "  cloud_model         model to use on cloud (default: chat_model)\n\n"
        "[lcars1]Doctor integration:[/lcars1]\n"
        "  org-llm doctor now shows a Cloud section with connection status.\n\n"
        "[lcars1]Theme levels (env vars):[/lcars1]\n"
        "  ORG_LLM_TREK_LEVEL=0..3    — Trek references intensity (default: 2)\n"
        "  ORG_LLM_COMMIE_LEVEL=0..3  — Solidarity messaging intensity (default: 2)\n\n"
        "[dim]Source: org_llm/cloud.py  |  org-llm source cloud[/dim]",
    ),
    (
        "launch",
        "[lcars2]org-llm launch[/lcars2] — opencode workspace with full vault context\n\n"
        "Transforms opencode into a second brain interface by:\n\n"
        "  1. Writing [bold]{org_dir}/.opencode.json[/bold] — Ollama provider, MCP server,\n"
        "     and a rich system prompt injected with vault stats + recent activity\n"
        "  2. Launching opencode (installs if missing) in org_dir\n\n"
        "[lcars1]The MCP server (org-llm mcp):[/lcars1]\n"
        "  org-llm starts an MCP server over stdio that opencode connects to.\n"
        "  Every org-llm capability is a native tool opencode can call:\n\n"
        "    search_notes    ask_notes     capture_note\n"
        "    get_node        list_skills   run_skill\n"
        "    tangle_file     get_config    get_vault_stats\n"
        "    list_recent_nodes  list_nodes_by_tag\n"
        "    get_tutor_step  list_tutor_steps\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]  [lcars2]SPC l o[/lcars2] — opens vterm + launches workspace\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]              — full context workspace\n"
        "  [bold]org-llm launch --no-context[/bold] — minimal prompt\n"
        "  [bold]org-llm launch --dry-run[/bold]    — preview .opencode.json\n"
        "  [bold]org-llm launch --model phi4[/bold] — use a different model\n"
        "  [bold]org-llm mcp[/bold]                 — start MCP server standalone\n\n"
        "[lcars1]Tutor in opencode:[/lcars1]\n"
        "  Inside opencode, ask: 'show me the tutor step for embeddings'\n"
        "  opencode calls [bold]get_tutor_step('embed')[/bold] and presents the content.\n\n"
        "[dim]Source: org_llm/mcp_server.py + cli.py → launch()  |  org-llm source mcp_server[/dim]",
    ),
    (
        "claude",
        "[lcars2]org-llm claude[/lcars2] — Claude Code as interactive org-roam workspace\n\n"
        "Connects the Anthropic Claude CLI to your vault via MCP tools.\n"
        "Unlike opencode (Ollama-backed), Claude Code uses the Anthropic API\n"
        "or a claude.ai Pro subscription — state-of-the-art models, no local GPU needed.\n\n"
        "[lcars1]What it does:[/lcars1]\n"
        "  1. Installs [bold]claude[/bold] CLI via npm if missing\n"
        "  2. Writes [bold]{org_dir}/.claude/settings.json[/bold] — MCP server entry\n"
        "  3. Writes [bold]{org_dir}/.claude/CLAUDE.md[/bold] — vault context instructions\n"
        "  4. exec-replaces itself with [bold]claude[/bold] in org_dir\n\n"
        "[lcars1]Requirements:[/lcars1]\n"
        "  ANTHROPIC_API_KEY  — get one at console.anthropic.com\n"
        "  OR claude.ai Pro subscription (claude.ai/login)\n"
        "  Node.js / npm       — for installing the claude package\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]  [lcars2]SPC l C[/lcars2] — opens vterm + launches Claude Code\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm claude[/bold]              — full context workspace\n"
        "  [bold]org-llm claude --no-context[/bold] — minimal .claude/CLAUDE.md\n"
        "  [bold]org-llm claude --dry-run[/bold]    — preview config without launching\n\n"
        "[lcars1]vs opencode:[/lcars1]\n"
        "  opencode → Ollama models (local, private, free after hardware)\n"
        "  claude   → Anthropic API (cloud, frontier models, per-token cost)\n\n"
        "[dim]Source: cli.py → claude_frontend()  |  config: .claude/settings.json[/dim]",
    ),
    (
        "done",
        "[bold lcars1]You're ready to explore your second brain.[/bold lcars1]\n\n"
        "[lcars1]Recommended first flight:[/lcars1]\n\n"
        "  1. [bold]org-llm install[/bold]         — Ollama + models + fonts + opencode + claude\n"
        "  2. [bold]org-llm init[/bold]             — create DB + default config\n"
        "  3. [bold]org-llm index[/bold]            — parse all org files into DB\n"
        "  4. [bold]org-llm embed[/bold]            — generate embeddings (takes a while)\n"
        "  5. [bold]org-llm doctor[/bold]           — verify everything is green\n"
        "  6. [bold]org-llm ask 'What did I write about X?'[/bold]\n\n"
        "[lcars1]Explore further:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]              — opencode workspace (SPC l o in Emacs)\n"
        "  [bold]org-llm claude[/bold]              — Claude Code workspace (SPC l C in Emacs)\n"
        "  [bold]org-llm cloud --assess[/bold]      — check which models need RunPod cloud\n"
        "  [bold]org-llm report all[/bold]          — analytics on your vault\n"
        "  [bold]org-llm skill-new my_skill[/bold]  — create your first skill\n"
        "  [bold]org-llm tag[/bold]                 — auto-tag untagged nodes\n"
        "  [bold]org-llm source mcp_server[/bold]   — see all MCP tools\n"
        "  [bold]org-llm tutor --all[/bold]          — read the whole manual\n\n"
        "Engage. ☭ ✊ 🏳️‍🌈 — Queer, collective, free.",
    ),
]


@app.command()
def tutor(
    step: Annotated[str, typer.Argument(
        help="Step name to jump to (welcome/init/index/embed/search/ask/capture/"
             "tag/code/config/skills/report/doctor/install/dbt/opencode/source/cloud/launch/emacs/claude/done)"
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
    "mcp_server": "org_llm.mcp_server",
    "cloud":      "org_llm.cloud",
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


@app.command()
def launch(
    model:      Annotated[str,  typer.Option("--model", "-m",
                help="Override chat model (default: chat_model from config)")] = "",
    no_context: Annotated[bool, typer.Option("--no-context",
                help="Skip vault context injection into system prompt")] = False,
    dry_run:    Annotated[bool, typer.Option("--dry-run",
                help="Print opencode config only, do not launch")] = False,
):
    """Launch opencode as an interactive org-roam workspace with vault context and MCP tools."""
    import json
    import os
    import shutil
    import subprocess
    from rich.panel  import Panel
    from rich.table  import Table
    from rich.syntax import Syntax
    from .ui         import trans_stripe, PRIDE_BANNER, solidarity
    from .db         import Node, File
    from .skills     import Skill

    # ── Locate or install opencode ────────────────────────────────────────────
    oc_path = _opencode_bin()
    if not dry_run and oc_path is None:
        hail("opencode not found — installing via curl…")
        oc_path = _install_opencode_bin(Path("~/.local/bin").expanduser())
        if not oc_path:
            raise typer.Exit(1)
    oc_bin = str(oc_path) if oc_path else "opencode"

    # ── Gather vault context ──────────────────────────────────────────────────
    engine = _engine()
    with get_session(engine) as session:
        org_dir    = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        ollama_url = _ollama_url(session)
        chat_mdl   = model or _cfg(session, "chat_model") or "llama3.2"
        n_files    = session.query(File).count()
        n_nodes    = session.query(Node).count()
        n_embedded = session.query(Node).filter(Node.embedding.isnot(None)).count()
        pct_e      = int(n_embedded / n_nodes * 100) if n_nodes else 0
        skill_names = [s.name for s in session.query(Skill).all()]
        from datetime import datetime, timedelta
        since  = (datetime.now() - timedelta(days=7)).isoformat()
        recent = (
            session.query(Node)
            .filter(Node.mtime >= since)
            .order_by(Node.mtime.desc())
            .limit(8).all()
        )

    org_llm_dir = Path(__file__).parent.parent.resolve()

    # ── Build system prompt ───────────────────────────────────────────────────
    recent_str = "\n".join(
        f"  - {n.title} ({n.mtime[:10] if n.mtime else '?'})" for n in recent
    ) or "  (no recent activity)"
    skill_str = ", ".join(skill_names) if skill_names else "none — run org-llm skill-index"

    if no_context:
        instructions = (
            "You are an intelligent assistant connected to an org-roam knowledge base "
            "via org-llm MCP tools. Use the tools to help with note management, Q&A, "
            "and org-babel automation workflows."
        )
    else:
        instructions = f"""You are an intelligent personal assistant and knowledge worker with full access to the user's org-roam second brain via org-llm MCP tools.

VAULT SUMMARY
  Location: {org_dir}
  Files:    {n_files}  |  Nodes: {n_nodes}  |  Embedded: {n_embedded}/{n_nodes} ({pct_e}%)
  Skills:   {skill_str}

RECENT ACTIVITY (last 7 days)
{recent_str}

AVAILABLE MCP TOOLS
  search_notes(query, limit, keyword)  — semantic or keyword search over all notes
  ask_notes(question, top_k)           — RAG Q&A grounded in org notes
  capture_note(title, body, file)      — add a new note to the vault
  get_node(title)                      — fetch full note content by title
  list_nodes_by_tag(tag, limit)        — browse notes by tag
  list_recent_nodes(days)              — see recent activity
  get_vault_stats()                    — vault statistics
  list_skills()                        — available org-babel skill workflows
  run_skill(name, input)               — execute a skill workflow
  tangle_file(file_path)               — org-babel-tangle via emacsclient
  get_config()                         — current model/config assignments

BEHAVIOUR
  - Always call search_notes or ask_notes before answering questions about the user's notes
  - When saving something, use capture_note and confirm file path and ID
  - When discussing automation, check list_skills first
  - Cite note titles when drawing from the knowledge base
  - Use tangle_file to materialise org-babel workflows after editing"""

    # ── Write .opencode.json ──────────────────────────────────────────────────
    oc_config: dict = {
        "model": f"ollama/{chat_mdl}",
        "provider": {
            "ollama": {
                "name": "Ollama",
                "options": {"baseURL": f"{ollama_url.rstrip('/')}/v1"},
            }
        },
        "instructions": instructions,
        "mcp": {
            "org-llm": {
                "type": "local",
                "command": [
                    "uv", "--directory", str(org_llm_dir),
                    "run", "org-llm", "mcp",
                ],
                "env": {"ORG_LLM_DB": str(DB_PATH)},
            }
        },
    }

    config_path = org_dir / ".opencode.json"

    if dry_run:
        console.print()
        console.rule("[lcars1]opencode config (dry-run)[/lcars1]")
        console.print(Syntax(json.dumps(oc_config, indent=2), "json", theme="monokai"))
        console.rule()
        on_screen(f"Would write to: {config_path}")
        return

    config_path.write_text(json.dumps(oc_config, indent=2))

    # ── Launch banner ─────────────────────────────────────────────────────────
    solidarity()
    console.print()

    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=20)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Model",       f"{chat_mdl}  (Ollama)")
    tbl.add_row("Vault",       str(org_dir))
    tbl.add_row("Nodes",       f"{n_nodes}  ({pct_e}% embedded)")
    tbl.add_row("Skills",      f"{len(skill_names)} registered")
    tbl.add_row("MCP server",  "org-llm mcp  (stdio)")
    tbl.add_row("Config",      str(config_path))
    console.print(Panel(
        tbl,
        title="[lcars1]org-llm  ×  opencode  workspace[/lcars1]",
        border_style="lcars2",
        padding=(1, 2),
    ))
    console.print()
    hail("Engaging opencode… (q to quit, Ctrl-C to abort)")
    console.print()

    # ── Hand off to opencode ──────────────────────────────────────────────────
    os.chdir(org_dir)
    os.execvp(oc_bin, [oc_bin])


@app.command(name="claude")
def claude_frontend(
    no_context: Annotated[bool, typer.Option("--no-context",
                help="Skip vault context in CLAUDE.md instructions")] = False,
    dry_run:    Annotated[bool, typer.Option("--dry-run",
                help="Print config only, do not launch")] = False,
):
    """Launch Claude Code as an interactive org-roam workspace with vault context and MCP tools."""
    import json
    import os
    import shutil
    from rich.panel  import Panel
    from rich.syntax import Syntax
    from rich.table  import Table
    from .db    import Node, File
    from .skills import Skill
    from .ui    import solidarity, trans_stripe

    # ── Locate or install claude ──────────────────────────────────────────────
    claude_path = _claude_bin()
    if not dry_run and claude_path is None:
        hail("Claude Code not found — installing via npm…")
        claude_path = _install_claude_bin()
        if not claude_path:
            raise typer.Exit(1)
    claude_bin = str(claude_path) if claude_path else "claude"

    # ── Gather vault context (same as launch) ─────────────────────────────────
    engine = _engine()
    with get_session(engine) as session:
        org_dir     = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        n_files     = session.query(File).count()
        n_nodes     = session.query(Node).count()
        n_embedded  = session.query(Node).filter(Node.embedding.isnot(None)).count()
        pct_e       = int(n_embedded / n_nodes * 100) if n_nodes else 0
        skill_names = [s.name for s in session.query(Skill).all()]
        from datetime import datetime, timedelta
        since  = (datetime.now() - timedelta(days=7)).isoformat()
        recent = (
            session.query(Node)
            .filter(Node.mtime >= since)
            .order_by(Node.mtime.desc())
            .limit(8).all()
        )

    org_llm_dir = Path(__file__).parent.parent.resolve()

    recent_str = "\n".join(
        f"  - {n.title} ({n.mtime[:10] if n.mtime else '?'})" for n in recent
    ) or "  (no recent activity)"
    skill_str = ", ".join(skill_names) if skill_names else "none — run org-llm skill-index"

    # ── MCP server config → .claude/settings.json ────────────────────────────
    claude_dir = org_dir / ".claude"
    settings_path = claude_dir / "settings.json"

    # Merge with existing settings if present (preserve user's own config)
    existing_settings: dict = {}
    if settings_path.exists():
        try:
            existing_settings = json.loads(settings_path.read_text())
        except Exception:
            pass

    mcp_entry = {
        "command": "uv",
        "args": ["--directory", str(org_llm_dir), "run", "org-llm", "mcp"],
        "env": {"ORG_LLM_DB": str(DB_PATH)},
    }
    existing_settings.setdefault("mcpServers", {})["org-llm"] = mcp_entry

    # ── Vault instructions → .claude/CLAUDE.md ───────────────────────────────
    if no_context:
        claude_md = (
            "# org-llm Vault Assistant\n\n"
            "You have access to the org-roam knowledge base via MCP tools.\n"
            "Use `search_notes`, `ask_notes`, `capture_note` and other tools to "
            "assist with note management, Q&A, and automation workflows.\n"
        )
    else:
        claude_md = f"""# org-llm Vault Assistant

You are an intelligent personal assistant with full access to the user's
org-roam second brain via org-llm MCP tools.

## Vault Summary
- Location: {org_dir}
- Files: {n_files}  |  Nodes: {n_nodes}  |  Embedded: {n_embedded}/{n_nodes} ({pct_e}%)
- Skills: {skill_str}

## Recent Activity (last 7 days)
{recent_str}

## Available MCP Tools
- `search_notes(query, limit, keyword)` — semantic or keyword search
- `ask_notes(question, top_k)` — RAG Q&A grounded in org notes
- `capture_note(title, body, file)` — add a new note to the vault
- `get_node(title)` — fetch full note content by title
- `list_nodes_by_tag(tag, limit)` — browse notes by tag
- `list_recent_nodes(days)` — see recent activity
- `get_vault_stats()` — vault statistics
- `list_skills()` — available org-babel skill workflows
- `run_skill(name, input)` — execute a skill workflow
- `tangle_file(file_path)` — org-babel-tangle via emacsclient
- `get_config()` — current model/config assignments
- `list_tutor_steps()` / `get_tutor_step(step)` — interactive tutorial

## Behaviour
- Always call `search_notes` or `ask_notes` before answering questions about notes
- When saving something, use `capture_note` and confirm file path
- When discussing automation, check `list_skills` first
- Cite note titles when drawing from the knowledge base
- Use `tangle_file` to materialise org-babel workflows after editing
"""

    if dry_run:
        console.print()
        console.rule("[lcars1].claude/settings.json (dry-run)[/lcars1]")
        console.print(Syntax(json.dumps(existing_settings, indent=2), "json", theme="monokai"))
        console.rule()
        console.print()
        console.rule("[lcars1].claude/CLAUDE.md (dry-run)[/lcars1]")
        console.print(claude_md)
        console.rule()
        on_screen(f"Would write to: {settings_path}")
        on_screen(f"Would write to: {claude_dir / 'CLAUDE.md'}")
        return

    # ── Write config files ────────────────────────────────────────────────────
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing_settings, indent=2))
    (claude_dir / "CLAUDE.md").write_text(claude_md)

    # ── Check for API key ─────────────────────────────────────────────────────
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print()
        console.print("[bold yellow]⚠  ANTHROPIC_API_KEY not set[/bold yellow]")
        console.print(
            "  Claude Code needs an API key (or claude.ai Pro subscription).\n"
            "  Get one at [bold]console.anthropic.com[/bold] and set:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...\n"
        )

    # ── Launch banner ─────────────────────────────────────────────────────────
    solidarity()
    console.print()

    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=20)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Model",       "claude (Anthropic API / claude.ai Pro)")
    tbl.add_row("Vault",       str(org_dir))
    tbl.add_row("Nodes",       f"{n_nodes}  ({pct_e}% embedded)")
    tbl.add_row("Skills",      f"{len(skill_names)} registered")
    tbl.add_row("MCP server",  "org-llm mcp  (stdio)")
    tbl.add_row("Settings",    str(settings_path))
    console.print(Panel(
        tbl,
        title="[lcars1]org-llm  ×  Claude Code  workspace[/lcars1]",
        border_style="lcars2",
        padding=(1, 2),
    ))
    console.print()
    hail("Engaging Claude Code… (Ctrl-C to abort)")
    console.print()

    # ── Hand off to claude ────────────────────────────────────────────────────
    os.chdir(org_dir)
    os.execvp(claude_bin, [claude_bin])


@app.command()
def cloud(
    status:    Annotated[bool, typer.Option("--status",    "-s",  help="Show cloud config and connection status")] = False,
    signup:    Annotated[bool, typer.Option("--signup",          help="Open RunPod signup in browser")] = False,
    console_:  Annotated[bool, typer.Option("--console",         help="Open RunPod console in browser")] = False,
    configure: Annotated[bool, typer.Option("--configure", "-c",  help="Set up RunPod API key and endpoint")] = False,
    test:      Annotated[bool, typer.Option("--test",      "-t",  help="Ping the cloud endpoint")] = False,
    assess:    Annotated[bool, typer.Option("--assess",    "-a",  help="Assess which models need cloud vs local")] = False,
    cost:      Annotated[bool, typer.Option("--cost",             help="Show cost estimates for configured models")] = False,
):
    """Manage RunPod cloud LLM backend — expand beyond local Ollama when needed."""
    from rich.panel import Panel
    from rich.table import Table
    from .cloud import (
        assess_local_capability, check_connection, cost_per_1k_tokens,
        local_vram_gb, local_ram_gb,
        open_signup, open_console,
        RUNPOD_GPU_COSTS, RUNPOD_SIGNUP_URL,
    )
    from .ui import TREK_MSGS, stardate, lcars_panel, COMRADE_STAR

    engine = _engine()

    # ── default: show status ──────────────────────────────────────────────────
    if not any([status, signup, console_, configure, test, assess, cost]):
        status = True

    if signup:
        hail(f"Opening RunPod signup: {RUNPOD_SIGNUP_URL}")
        open_signup()
        on_screen("Create an account, then deploy an Ollama pod from the template gallery.")
        on_screen("Run [bold]org-llm cloud --configure[/bold] once you have your pod URL.")
        return

    if console_:
        hail("Opening RunPod console…")
        open_console()
        return

    if configure:
        console.print()
        console.rule("[lcars1]RunPod Configuration[/lcars1]")
        endpoint = typer.prompt(
            "RunPod Ollama endpoint URL (e.g. https://abc123-11434.proxy.runpod.net)",
            default="",
        )
        if endpoint:
            with get_session(engine) as session:
                from .db import Config
                for key, val in [
                    ("cloud_provider", "runpod"),
                    ("cloud_endpoint_url", endpoint.rstrip("/")),
                ]:
                    row = session.get(Config, key)
                    if row:
                        row.value = val
                    else:
                        session.add(Config(key=key, value=val))
                session.commit()
            hail(f"Cloud endpoint saved: {endpoint}")

        api_key = typer.prompt("RunPod API key (leave blank if endpoint is public)", default="")
        if api_key:
            with get_session(engine) as session:
                from .db import Config
                row = session.get(Config, "runpod_api_key")
                if row:
                    row.value = api_key
                else:
                    session.add(Config(key="runpod_api_key", value=api_key))
                session.commit()
            hail("API key saved.")

        cloud_model = typer.prompt(
            "Model name on cloud endpoint (e.g. llama3.2, leave blank = use chat_model)",
            default="",
        )
        if cloud_model:
            with get_session(engine) as session:
                from .db import Config
                row = session.get(Config, "cloud_model")
                if row:
                    row.value = cloud_model
                else:
                    session.add(Config(key="cloud_model", value=cloud_model))
                session.commit()
            hail(f"Cloud model saved: {cloud_model}")

        make_it_so()
        return

    # Load cloud config
    with get_session(engine) as session:
        provider     = _cfg(session, "cloud_provider")
        endpoint_url = _cfg(session, "cloud_endpoint_url")
        api_key      = _cfg(session, "runpod_api_key")
        cloud_model  = _cfg(session, "cloud_model") or _cfg(session, "chat_model") or "llama3.2"
        all_models   = [_cfg(session, k) for _, k, _ in _TASK_MODEL_KEYS if _cfg(session, k)]

    if test:
        if not endpoint_url:
            red_alert("No cloud endpoint configured. Run: org-llm cloud --configure")
            raise typer.Exit(1)
        with warp(f"{TREK_MSGS['cloud']}: {endpoint_url}"):
            cs = check_connection(endpoint_url, api_key, cloud_model)
        if cs.reachable and cs.auth_ok:
            hail(f"Cloud endpoint reachable  ({cs.latency_ms:.0f}ms)")
        elif cs.reachable:
            red_alert("Endpoint reachable but authentication failed — check API key.")
        else:
            red_alert(f"Cloud endpoint unreachable: {endpoint_url}")
        return

    if assess:
        console.print()
        console.rule(f"[lcars1]{TREK_MSGS['assess']}[/lcars1]")
        vram = local_vram_gb()
        ram  = local_ram_gb()
        hail(f"Hardware: {'%.0f GB VRAM' % vram if vram else 'no GPU detected'}  |  {ram:.0f} GB RAM")
        hail(f"Stardate: {stardate()}")
        console.print()

        results = assess_local_capability(list(set(all_models)))
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("Model",        style="lcars2")
        tbl.add_column("VRAM needed",  style="lcars3", justify="right")
        tbl.add_column("Local?",       justify="center")
        tbl.add_column("Verdict",      style="dim")
        needs_cloud = []
        for r in results:
            status_str = "[bold green]✓[/bold green]" if r["can_local"] else "[bold red]→ cloud[/bold red]"
            tbl.add_row(r["model"], f"{r['vram_needed']:.0f} GB", status_str, r["reason"])
            if not r["can_local"]:
                needs_cloud.append(r["model"])
        console.print(tbl)

        if needs_cloud:
            console.print()
            if endpoint_url:
                on_screen(f"Cloud endpoint ready for: {', '.join(needs_cloud)}")
            else:
                on_screen(f"[bold]{len(needs_cloud)} model(s) need cloud.[/bold] "
                          "Run: [bold]org-llm cloud --signup[/bold]  then  "
                          "[bold]org-llm cloud --configure[/bold]")
        return

    if cost:
        console.print()
        console.rule("[lcars1]Cloud Cost Estimates[/lcars1]")
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("GPU",        style="lcars2")
        tbl.add_column("$/hr",       style="lcars3", justify="right")
        tbl.add_column("¢/1k tok",   style="lcars1", justify="right")
        for gpu, hourly in RUNPOD_GPU_COSTS.items():
            cpp = cost_per_1k_tokens(gpu, tokens_per_sec=25.0)
            tbl.add_row(gpu, f"${hourly:.2f}", f"{cpp*100:.3f}¢")
        console.print(tbl)
        console.print()
        on_screen("Prices are approximate spot rates. Check runpod.io for live pricing.")
        return

    # ── Status panel ─────────────────────────────────────────────────────────
    console.print()
    console.rule(f"[lcars1]Cloud Status  ·  stardate {stardate()}[/lcars1]")

    cs = None
    if endpoint_url:
        with warp(f"{TREK_MSGS['cloud']}: {endpoint_url}"):
            cs = check_connection(endpoint_url, api_key, cloud_model)

    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=22)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Provider",     provider or "not configured")
    tbl.add_row("Endpoint",     endpoint_url or "—")
    tbl.add_row("API key",      "set" if api_key else "—")
    tbl.add_row("Cloud model",  cloud_model)
    if cs:
        if cs.reachable and cs.auth_ok:
            tbl.add_row("Connection",  f"[bold green]✓ reachable ({cs.latency_ms:.0f}ms)[/bold green]")
        elif cs.reachable:
            tbl.add_row("Connection",  "[bold yellow]⚠ reachable, auth failed[/bold yellow]")
        else:
            tbl.add_row("Connection",  "[bold red]✗ unreachable[/bold red]")

    vram = local_vram_gb()
    ram  = local_ram_gb()
    tbl.add_row("Local GPU",    f"{vram:.0f} GB VRAM" if vram else "none detected")
    tbl.add_row("Local RAM",    f"{ram:.0f} GB")

    console.print(Panel(
        tbl,
        title=f"[lcars1]{COMRADE_STAR}  RunPod Cloud  {COMRADE_STAR}[/lcars1]",
        border_style="lcars2", padding=(1, 2),
    ))

    if not endpoint_url:
        console.print()
        on_screen("No cloud backend configured. Options:")
        on_screen("  [bold]org-llm cloud --signup[/bold]     — create RunPod account")
        on_screen("  [bold]org-llm cloud --configure[/bold]  — enter endpoint URL + API key")
        on_screen("  [bold]org-llm cloud --assess[/bold]     — see which models need cloud")
    console.print()


@app.command()
def mcp():
    """Start the org-llm MCP server over stdio (for opencode and other MCP clients)."""
    from .mcp_server import main as _mcp_main
    _mcp_main()


# Register skill commands at import time so they appear in --help
from . import cli_skills as _cs
_cs.register(app)


def main():
    app()
# cli.py:1 ends here
