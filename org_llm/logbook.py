"""Captain's Log — single source of truth for "what just happened" events.

(Module is named `logbook` for spelling/grep convenience; the user-facing
surface — file title, CLI panels, MCP tool descriptions — uses the
themed name throughout.)

Every event we choose to log is written to TWO places at once:

  1. The SQLite `history` table — so dbt can build analytics on top
     of it (stg_history → marts/llm_calls / cli_invocations / etc.)
     and so MCP tools can query it from inside opencode.

  2. ~/org/org-llm-log.org — so the user's vault has a primary-source
     record of how the app and the LLMs are behaving over time. Each
     event is one heading with a PROPERTIES drawer + body. High-volume
     event kinds get a `#+begin_src text :tangle …` block so a
     standard `org-babel-tangle` produces grep-friendly plaintext
     mirrors at ~/.local/share/org-llm/log/<kind>.log.

Why two surfaces:
  - DB is fast, structured, joinable, dbt-friendly.
  - Org file is human-readable, vault-searchable via search_notes,
    survives if the DB is wiped, and inherits the same backup/sync
    story as the rest of the user's notes.

The two stay in sync because every event flows through `write_event()`
or `track_event()` — there is no other path. The migration in db.py
adds the columns the new schema needs without breaking pre-logbook
rows.

Verbosity is gated by config: `log_level` (off|minimal|normal|verbose),
`log_kinds` (comma-sep filter), `log_max_rows_per_kind` (rotation cap).

Best-effort everywhere: a write failure NEVER raises into the caller.
The caller is doing real work; logging it isn't allowed to break that.
"""
from __future__ import annotations

import os
import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


_ORG_LOG_PATH = Path("~/org/captains-log.org").expanduser()
_TANGLE_DIR   = Path("~/.local/share/org-llm/log").expanduser()
_ROTATE_BYTES = 5 * 2**20    # 5 MB — rotate the org file past this size


# Anywhere we're emitting an org file that mentions a code/wiki path,
# wrap it with this helper so it renders as a clickable link in
# org-mode + agave + the wiki's `file:` convention. Bare verbatim
# `=path/to/file=` is fine for ad-hoc text inside src blocks (where
# org-mode wouldn't render the link anyway), but every "*See also*"
# / "where it lives" / hyperlink-friendly position should use this.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def org_file_link(path, *, label: str = "", relative_to=None) -> str:
    """Render `path` as an org-mode `[[file:...][=...=]]` link.

    Resolution rules — priority top to bottom:
      • If `relative_to` is given, link target is `path` relative to it.
      • Else if `path` is absolute and inside the repo root, target is
        relative to the repo root with a `../../` prefix (so wiki pages
        at `docs/wiki/<page>.org` resolve correctly).
      • Else target is `path` as-is (absolute, or whatever the caller
        passed — assumed already correct).

    `label` defaults to the verbatim path string in `=...=` style,
    matching the convention in `docs/wiki/*.org` set in 2026-05-02.

    Best-effort: never raises. Falls back to a plain verbatim
    `=path=` string if anything goes sideways — that still readably
    renders.
    """
    try:
        p = Path(path)
        rel = None
        if relative_to is not None:
            try:
                rel = "../../" + str(p.relative_to(Path(relative_to)))
            except ValueError:
                rel = None
        if rel is None and p.is_absolute():
            try:
                rel = "../../" + str(p.relative_to(_REPO_ROOT))
            except ValueError:
                rel = None
        if rel is None and not p.is_absolute():
            # Caller passed a repo-rooted path (e.g. "org_llm/cli.py").
            rel = "../../" + str(p)
        target = rel or str(p)
        text = label or f"={path}="
        return f"[[file:{target}][{text}]]"
    except Exception:
        return f"={path}="

# Per-kind cap defaults. Override via log_max_rows_per_kind config.
_DEFAULT_MAX_ROWS_PER_KIND = 1000

# Length cap on bodies stored in DB / written to org. Verbose mode uses
# 4× this cap.
_DEFAULT_BODY_CAP = 1500


def org_log_path() -> Path:
    """Where the human-readable org log lives. Override with
    ORG_LLM_LOG_PATH env var for tests."""
    return Path(os.environ.get("ORG_LLM_LOG_PATH") or str(_ORG_LOG_PATH))


def tangle_dir() -> Path:
    return Path(os.environ.get("ORG_LLM_LOG_TANGLE_DIR") or str(_TANGLE_DIR))


def _config_snapshot() -> dict:
    """Read the verbosity-control config keys with safe fallbacks.

    Inlined to avoid coupling the logbook to the broader config layer
    — if the DB isn't there yet (very first invocation), we degrade
    to defaults rather than raising.
    """
    out = {
        "log_level": "normal",
        "log_kinds": "cli,llm,mcp,config,doctor,dbt,alert",
        "log_max_rows_per_kind": str(_DEFAULT_MAX_ROWS_PER_KIND),
    }
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return out
        engine = make_engine(path)
        with Session(engine) as s:
            for k in list(out.keys()):
                row = s.get(Config, k)
                if row and row.value:
                    out[k] = row.value
    except Exception:
        pass
    return out


def _enabled(kind: str) -> tuple[bool, str]:
    """(should-write, current-level). Single decision point — keeps the
    write-event path side-effect free except when actually emitting."""
    cfg = _config_snapshot()
    level = cfg.get("log_level", "normal").lower()
    if level == "off":
        return (False, level)
    kinds = {k.strip() for k in cfg.get("log_kinds", "").split(",") if k.strip()}
    if kinds and kind not in kinds:
        return (False, level)
    # minimal level only allows cli + doctor + alert; normal allows everything
    if level == "minimal" and kind not in ("cli", "doctor", "alert"):
        return (False, level)
    return (True, level)


def _truncate(s: str, level: str) -> str:
    if not s:
        return ""
    cap = _DEFAULT_BODY_CAP * (4 if level == "verbose" else 1)
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n…(truncated, {len(s) - cap} more chars)"


def _prune_old_rows(session, kind: str, max_rows: int) -> None:
    """Trim the History table to at most max_rows rows for this kind.

    Cheap because History is small and queried by id (rowid). Called
    at write time so the table never grows unbounded.
    """
    from .db import History
    try:
        excess = (session.query(History)
                          .filter(History.kind == kind)
                          .order_by(History.id.desc())
                          .offset(max_rows).all())
        if excess:
            for row in excess:
                session.delete(row)
    except Exception:
        pass


def _rotate_org_log_if_needed(path: Path) -> None:
    """When the log file crosses _ROTATE_BYTES, rename it with a
    timestamp suffix and start fresh. Old logs stay searchable in the
    vault under the same prefix."""
    try:
        if not path.exists() or path.stat().st_size < _ROTATE_BYTES:
            return
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        rotated = path.with_name(f"{path.stem}-{ts}.org")
        path.rename(rotated)
    except Exception:
        pass


def _ensure_org_header(path: Path) -> None:
    """Write the file's preamble if it doesn't exist yet — title + tangle
    helper note + a `:noexport:` tag so it doesn't pollute exports."""
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#+title: Captain's Log — org-llm event log\n"
            "#+filetags: :captains-log:org-llm:log:noexport:\n"
            "#+startup: showall\n\n"
            "Stardate notwithstanding, this is the canonical event log\n"
            "for org-llm. Every heading below is one event; the PROPERTIES\n"
            "drawer carries the structured fields the dbt layer materialises\n"
            "into stg_history + marts (llm_calls, cli_invocations,\n"
            f"recent_activity). Tangled plaintext mirrors live at\n"
            f"{tangle_dir()}/<kind>.log after `org-babel-tangle` runs.\n\n"
            "When this file crosses 5 MB it rotates to\n"
            f"{org_log_path().stem}-<timestamp>.org and a fresh one is\n"
            "started — search across all of them via the vault index.\n\n"
            "Auto-written by org_llm/logbook.py. Open the captain's chair:\n"
            "  org-llm log              — recent entries\n"
            "  org-llm log --kind llm   — filter to LLM round-trips\n"
            "  org-llm log --grep <pat> — substring search\n"
            "  org-llm log -t           — tangle to plaintext mirrors\n\n"
        )
    except Exception:
        pass


def _append_org_entry(kind: str, command: str, args: str, response: str,
                       model: str, duration_ms: int | None,
                       outcome: str, level: str) -> None:
    """Append one heading to the org log. Uses :tangle to give each kind
    a plaintext sibling that ordinary grep can hit after tangling."""
    path = org_log_path()
    try:
        _rotate_org_log_if_needed(path)
        _ensure_org_header(path)
        ts = datetime.now().isoformat(timespec="seconds")
        tangle_target = tangle_dir() / f"{kind}.log"
        # PROPERTIES drawer keeps the structured record; bodies go in src
        # blocks so they tangle cleanly to plaintext per-kind logs.
        body_block = ""
        if response:
            safe = response.replace("\n#+end_src", "\n#+end_  src")  # un-nest
            body_block = (
                f"#+name: {kind}-{int(time.time()*1000)}\n"
                f"#+begin_src text :tangle {tangle_target}\n"
                f"{safe}\n"
                f"#+end_src\n"
            )
        entry = (
            f"\n* {ts} — {kind} — {command}\n"
            f":PROPERTIES:\n"
            f":KIND:        {kind}\n"
            f":COMMAND:     {command}\n"
            f":MODEL:       {model or ''}\n"
            f":DURATION_MS: {duration_ms if duration_ms is not None else ''}\n"
            f":OUTCOME:     {outcome}\n"
            f":ARGS:        {args.replace(chr(10), ' ')[:200] if args else ''}\n"
            f":END:\n\n"
            f"{body_block}"
        )
        with open(path, "a") as f:
            f.write(entry)
    except Exception:
        # Never raise from the logging path — the caller has real work.
        pass


def write_event(kind: str, command: str, *, args: str = "",
                  response: str = "", model: str = "",
                  duration_ms: int | None = None,
                  outcome: str = "ok") -> None:
    """Write one event to BOTH the History table AND the org log.

    Returns silently on any failure. Caller never has to wrap this in
    try/except — this is the boundary that holds the swallow guarantee.
    """
    ok, level = _enabled(kind)
    if not ok:
        return
    response = _truncate(response, level)
    args = _truncate(args, level)
    try:
        from .db import DB_PATH, History, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if path.exists():
            engine = make_engine(path)
            with Session(engine) as s:
                s.add(History(
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    command=command,
                    query=args,
                    response=response,
                    kind=kind,
                    model=model,
                    args=args,
                    duration_ms=duration_ms,
                    outcome=outcome,
                ))
                s.commit()
                cfg = _config_snapshot()
                try:
                    cap = int(cfg.get("log_max_rows_per_kind") or
                              _DEFAULT_MAX_ROWS_PER_KIND)
                except ValueError:
                    cap = _DEFAULT_MAX_ROWS_PER_KIND
                _prune_old_rows(s, kind, cap)
                s.commit()
    except Exception:
        pass
    _append_org_entry(kind, command, args, response, model,
                       duration_ms, outcome, level)


def export_rows_to_org(rows, dest: Path, *, title: str = "",
                          source_filter: str = "") -> int:
    """Append `rows` (sqlalchemy History records) to a user-chosen org
    file, formatted the same way as the canonical Captain's Log.

    Idempotency: the destination gets a per-export `* Captain's Log
    export — <iso ts>` parent heading so multiple exports stack
    cleanly. We do NOT dedupe by row id — same export twice gives two
    parents, which is the predictable behaviour.

    Returns the number of rows written. Never raises into the caller —
    on filesystem failure we return 0.
    """
    dest = Path(dest).expanduser()
    if not rows:
        return 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        is_new = not dest.exists()
        with open(dest, "a") as f:
            if is_new:
                f.write(
                    "#+TITLE: Captain's Log — exports\n"
                    "#+OPTIONS: toc:nil\n"
                    "#+STARTUP: showeverything\n\n"
                    "Captain's Log entries copied here by "
                    "[[shell:org-llm log --export][org-llm log "
                    "--export]]. Source of truth remains "
                    f"~{org_log_path().name}~.\n"
                )
            ts = datetime.now().isoformat(timespec="seconds")
            header = title or f"Captain's Log export — {ts}"
            filter_line = f" ({source_filter})" if source_filter else ""
            f.write(f"\n* {header}{filter_line}\n")
            f.write(f":PROPERTIES:\n:EXPORTED_AT: {ts}\n"
                     f":ROW_COUNT:   {len(rows)}\n")
            if source_filter:
                f.write(f":FILTER:      {source_filter}\n")
            f.write(":END:\n")
            for r in rows:
                kind     = getattr(r, "kind", "") or "?"
                command  = getattr(r, "command", "") or "?"
                model    = getattr(r, "model", "") or ""
                args     = getattr(r, "args", "") or ""
                response = getattr(r, "response", "") or ""
                outcome  = getattr(r, "outcome", "") or ""
                dur      = getattr(r, "duration_ms", None)
                row_ts   = (getattr(r, "timestamp", "") or "")[:19]
                f.write(f"\n** {row_ts} — {kind} — {command}\n")
                f.write(":PROPERTIES:\n")
                f.write(f":KIND:        {kind}\n")
                f.write(f":COMMAND:     {command}\n")
                f.write(f":MODEL:       {model}\n")
                f.write(f":DURATION_MS: {dur if dur is not None else ''}\n")
                f.write(f":OUTCOME:     {outcome}\n")
                f.write(f":ARGS:        "
                         f"{args.replace(chr(10), ' ')[:200]}\n")
                f.write(":END:\n")
                if response:
                    safe = response.replace("\n#+end_src", "\n#+end_  src")
                    f.write("#+begin_src text\n")
                    f.write(safe.rstrip() + "\n")
                    f.write("#+end_src\n")
        return len(rows)
    except Exception:
        return 0


@contextmanager
def track_event(kind: str, command: str, *, args: str = "", model: str = ""):
    """Context manager — measures duration, captures exceptions, writes
    one event on exit. Use for any block whose start/end matters.

    Yields a small mutable dict so the body can refine the response /
    model / outcome before the write happens:

        with track_event("llm", "chat", model="gemma3") as ev:
            ev["response"] = chat(...)
            # outcome stays "ok"
    """
    started = time.monotonic()
    ev = {"response": "", "outcome": "ok", "model": model}
    try:
        yield ev
    except Exception as e:
        ev["outcome"] = "error"
        ev["response"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        write_event(kind, command,
                     args=args,
                     response=ev.get("response", ""),
                     model=ev.get("model", model),
                     duration_ms=int((time.monotonic() - started) * 1000),
                     outcome=ev.get("outcome", "ok"))
