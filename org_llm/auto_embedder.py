"""Background auto-embedder.

The story: keep the vault index + embeddings fresh without making the
user remember to run `org-llm index` and `org-llm embed`. A tiny
daemon thread polls org file mtimes, runs incremental indexing on
changes, then embeds any new unembedded nodes.

Why a thread, not a separate process:
  - org-llm is already running inside `org-llm launch` (which spawns
    opencode as a child) and inside `org-llm watch` (when invoked as
    a standalone watcher). Both keep the parent Python process alive,
    so a daemon thread is the simplest deployment shape that works.
  - No IPC, no orphaned processes, no "is it still running" question.
    The thread dies with the parent.

State surfaced via:
  - SQLite History table (kind=embed, command=auto-embed-batch) — so
    Captain's Log + dbt analytics see every batch.
  - A tiny status JSON at ~/.local/share/org-llm/auto-embedder.json
    so any CLI invocation can read "has the watcher done anything
    recently" in O(1) and mention it in the footer.
  - Optional Rich heartbeat to stderr when `auto_embed_quiet=false`.

Configurable knobs (db.MODEL_DEFAULTS):
  auto_embed_enabled        true|false       (default false — opt-in)
  auto_embed_interval_secs  poll interval    (default 60)
  auto_embed_quiet          true|false       (default true)

Manual `org-llm embed` continues to work unchanged — the auto-embedder
just keeps things current between manual runs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


_STATUS_PATH = Path("~/.local/share/org-llm/auto-embedder.json").expanduser()
_STALE_NEWS_SECS = 300   # status counts as "recent" within this window


def status_path() -> Path:
    """Where the watcher stashes its rolling status. Env override for
    tests."""
    return Path(os.environ.get("ORG_LLM_AUTO_EMBEDDER_STATUS")
                  or str(_STATUS_PATH))


def write_status(state: dict) -> None:
    """Atomic write — partial state never visible to readers."""
    p = status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True,
                                    default=str))
        tmp.replace(p)
    except Exception:
        pass


def read_status() -> dict:
    """Latest status dict, or {} when no watcher has run yet."""
    p = status_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def status_is_fresh() -> bool:
    """True when the watcher has reported in within _STALE_NEWS_SECS."""
    s = read_status()
    last = s.get("last_check_at")
    if not last:
        return False
    try:
        return (time.time() - float(last)) < _STALE_NEWS_SECS
    except Exception:
        return False


def _config_or(key: str, default: str) -> str:
    """Read a config key with a swallow-everything fallback to the
    default — never raises during background polling."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return default
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, key)
            return row.value if row and row.value else default
    except Exception:
        return default


def _is_truthy(s: str) -> bool:
    return (s or "").strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return _is_truthy(_config_or("auto_embed_enabled", "false"))


def _interval_secs() -> int:
    try:
        return max(15, int(_config_or("auto_embed_interval_secs", "60")))
    except ValueError:
        return 60


def _quiet() -> bool:
    return _is_truthy(_config_or("auto_embed_quiet", "true"))


def _do_one_pass(stop_event: threading.Event) -> dict:
    """One sweep: index incrementally, then embed any new nodes.

    Returns a small status dict the caller persists. NEVER raises into
    the caller — failures get surfaced via the status `last_error` field.
    """
    started = time.monotonic()
    state: dict = {
        "last_check_at": time.time(),
        "started_at_iso": datetime.now().isoformat(timespec="seconds"),
        "files_indexed": 0,
        "nodes_added":   0,
        "nodes_embedded": 0,
        "last_error":   "",
    }
    try:
        from .db import Config, get_session, make_engine, DB_PATH
        from .indexer import index_directory, embed_nodes
        from .logbook import write_event as _log_event
        from . import logbook as _lb

        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            state["last_error"] = "DB not initialized — run `org-llm init`"
            return state
        engine = make_engine(path)

        with get_session(engine) as session:
            # Honour ORG_LLM_ORG_DIR env override exactly like the
            # CLI's _org_dir() helper. Phase 11 Day 4 surfaced the
            # divergence: a user running with ORG_LLM_DB pointed at
            # an isolated test corpus DB but ORG_LLM_ORG_DIR pointed
            # at the test corpus directory had `walk watch` happily
            # walking ~/org and indexing 549 real-vault files into
            # the test DB. The CLI honoured the env var; the watcher
            # didn't. Now both do.
            org_dir_row = session.get(Config, "org_dir")
            org_dir = Path(
                os.environ.get("ORG_LLM_ORG_DIR")
                or (org_dir_row.value if org_dir_row else "~/org")
            ).expanduser()
            url       = (session.get(Config, "ollama_url").value
                          if session.get(Config, "ollama_url")
                          else "http://localhost:11434")
            model_row = session.get(Config, "embed_model")
            model     = (model_row.value if model_row else "nomic-embed-text")

        if not org_dir.is_dir():
            state["last_error"] = f"org_dir doesn't exist: {org_dir}"
            return state

        # Index pass — index_directory is already incremental (skips
        # files unchanged since last index_at).
        with get_session(engine) as session:
            files_indexed, nodes_added = index_directory(org_dir, session)
            session.commit()
        state["files_indexed"] = files_indexed
        state["nodes_added"]   = nodes_added

        # Embed pass — only fires when there's something new.
        if nodes_added > 0:
            with get_session(engine) as session:
                count = embed_nodes(session, model=model, base_url=url,
                                      force=False)
            state["nodes_embedded"] = count
        # Log to Captain's Log if anything changed — silent passes
        # would be noise.
        if files_indexed or state["nodes_embedded"]:
            _log_event("embed", "auto-embed-batch",
                        args=f"interval={_interval_secs()}s",
                        model=model,
                        response=(f"indexed {files_indexed} files, "
                                  f"added {nodes_added} nodes, "
                                  f"embedded {state['nodes_embedded']}"),
                        duration_ms=int(
                            (time.monotonic() - started) * 1000),
                        outcome="ok")
    except Exception as e:
        state["last_error"] = f"{type(e).__name__}: {e}"
        try:
            from .logbook import write_event as _log_event
            _log_event("embed", "auto-embed-batch",
                        outcome="error",
                        response=state["last_error"])
        except Exception:
            pass
    state["duration_ms"] = int((time.monotonic() - started) * 1000)
    return state


@contextmanager
def watcher_thread():
    """Context manager: start the watcher daemon, run until exit, then
    signal stop. Use from `org-llm launch` so the watcher dies cleanly
    when opencode does.

    Yields a stop_event the caller can set to terminate early.
    """
    if not is_enabled():
        yield None
        return
    stop_event = threading.Event()

    def _loop():
        # Skew first poll by half-interval so a freshly-started session
        # doesn't immediately try to write to the DB while the rest of
        # launch is still bringing things up.
        interval = _interval_secs()
        if stop_event.wait(interval / 2):
            return
        while not stop_event.is_set():
            state = _do_one_pass(stop_event)
            write_status(state)
            if stop_event.wait(interval):
                return

    t = threading.Thread(target=_loop, daemon=True, name="org-llm-auto-embed")
    t.start()
    try:
        yield stop_event
    finally:
        stop_event.set()
        t.join(timeout=2.0)


def run_forever(quiet: bool | None = None) -> None:
    """Standalone foreground watcher — used by `org-llm watch`. Blocks
    until Ctrl-C; respects auto_embed_interval_secs from config."""
    quiet = _quiet() if quiet is None else quiet
    interval = _interval_secs()
    if not quiet:
        from .ui import on_screen
        on_screen(f"[lcars1]Auto-embedder running.[/lcars1] "
                  f"Polling every {interval}s. Ctrl-C to stop.")
    stop_event = threading.Event()
    try:
        while not stop_event.is_set():
            state = _do_one_pass(stop_event)
            write_status(state)
            if not quiet and (state["files_indexed"] or
                                state["nodes_embedded"]):
                from .ui import on_screen
                on_screen(
                    f"[dim]auto-embed:[/dim] "
                    f"+{state['files_indexed']} files, "
                    f"+{state['nodes_added']} nodes, "
                    f"+{state['nodes_embedded']} embedded "
                    f"in {state['duration_ms']}ms")
            if stop_event.wait(interval):
                return
    except KeyboardInterrupt:
        if not quiet:
            from .ui import on_screen
            on_screen("[yellow]Stopping auto-embedder.[/yellow]")


def status_summary() -> str:
    """One-line summary of the watcher's last activity for footer use."""
    s = read_status()
    if not s:
        return ""
    last = s.get("last_check_at")
    try:
        ago = int(time.time() - float(last)) if last else None
    except Exception:
        ago = None
    files = s.get("files_indexed", 0)
    nodes = s.get("nodes_added", 0)
    embed = s.get("nodes_embedded", 0)
    err   = s.get("last_error", "")
    if err:
        return f"auto-embed error: {err[:100]}"
    if files or nodes or embed:
        bits = []
        if files: bits.append(f"+{files}f")
        if nodes: bits.append(f"+{nodes}n")
        if embed: bits.append(f"+{embed}e")
        when = f"{ago}s ago" if ago is not None else "recent"
        return f"auto-embed {when}: {' '.join(bits)}"
    return f"auto-embed idle ({ago}s ago)" if ago is not None else ""
