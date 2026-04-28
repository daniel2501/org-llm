"""Askbook — literate Q/A scratchpad across all model backends.

Pose a question to any backend (chat / reason / fast / code / text /
cloud / claude / pi), have the answer materialise back into the same
org file as a tangle-friendly src block. Multiple questions can stack
in one file; `askbook run` walks the pending ones and fills in answers.

Backends:
  chat      — Ollama via llm.chat with config.chat_model
  reason    — Ollama with config.reason_model (planning-grade)
  fast      — Ollama with config.fast_model (cheap classifier)
  code      — Ollama with config.code_model
  text      — Ollama with config.text_model
  cloud     — cloud.cloud_chat with the configured cloud_provider
  claude    — shell out to `claude -p QUESTION` (needs ANTHROPIC_API_KEY)
  pi        — shell out to `pi -p QUESTION` (needs Pi installed)

Why one file with multiple backends instead of per-backend files:
  Comparing answers side-by-side is the obvious thing to want when
  you're picking which model to lean on. Same vault search has to
  serve all of them; same context. One file makes that natural.

File format:
  Each question is one `* TODO Q: <one-line>` heading with PROPERTIES
  drawer (BACKEND / MODEL / STATUS / TIMESTAMP) and two src blocks —
  one for the question, one for the answer. STATUS=pending → askbook
  run picks it up; STATUS=done → skipped. Heading TODO/DONE keyword
  mirrors the status so org-mode's agenda + global counts work.

Read more: org-llm tutor askbook (after the tutor sweep).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_DEFAULT_PATH = Path("~/org/llm-askbook.org").expanduser()
_TANGLE_DIR   = Path("~/.local/share/org-llm/askbook").expanduser()


def askbook_path() -> Path:
    """Where the askbook lives. Override with $ORG_LLM_ASKBOOK_PATH."""
    return Path(os.environ.get("ORG_LLM_ASKBOOK_PATH")
                  or str(_DEFAULT_PATH))


def tangle_dir() -> Path:
    return Path(os.environ.get("ORG_LLM_ASKBOOK_TANGLE_DIR")
                  or str(_TANGLE_DIR))


# Recognised backends. The map is the source of truth; CLI validates
# against its keys, README references it, and `run_one` dispatches off it.
SUPPORTED_BACKENDS = (
    "chat", "reason", "fast", "code", "text",
    "cloud", "claude", "pi",
)


@dataclass
class AskEntry:
    """One askbook entry — question + (optional) answer + metadata."""
    timestamp:  str
    backend:    str
    model:      str
    title:      str
    status:     str        # "pending" | "done" | "error"
    question:   str
    answer:     str = ""


_HEADING_RE = re.compile(
    r"^\* (?P<keyword>TODO|DONE) Q\[(?P<ts>[^\]]+)\] (?P<title>.+)$",
    re.MULTILINE,
)
_PROPS_RE   = re.compile(
    # Anchored at start-of-line + REQUIRED whitespace between :KEY: and
    # the value. Without these guards, :PROPERTIES: (which has no value
    # on the line) would greedy-match the NEXT line's content as its
    # value via \s*[^\n]*.
    r"^:(?P<key>[A-Z_]+):[ \t]+(?P<value>[^\n]+)$",
    re.MULTILINE,
)
# Tangle src blocks named question / answer.
_QBLOCK_RE  = re.compile(
    r"#\+name: q-(?P<ts>[^\n]+)\n"
    r"#\+begin_src text :tangle [^\n]+\n(?P<body>.*?)\n#\+end_src",
    re.DOTALL,
)
_ABLOCK_RE  = re.compile(
    r"#\+name: a-(?P<ts>[^\n]+)\n"
    r"#\+begin_src text :tangle [^\n]+\n(?P<body>.*?)\n#\+end_src",
    re.DOTALL,
)


def _ensure_header(p: Path) -> None:
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "#+title: org-llm — LLM askbook\n"
        "#+filetags: :org-llm:askbook:noexport:\n"
        "#+todo: TODO | DONE\n"
        "#+startup: showall\n\n"
        "Literate Q/A scratchpad across model backends. Each entry is\n"
        "ONE question with PROPERTIES (backend, model, status) plus\n"
        "two `:tangle` blocks (question + answer). Pending entries get\n"
        "filled in by `org-llm askbook run`.\n\n"
        "Add a question:\n"
        "  org-llm askbook add 'How does dbt fit org-llm?' \\\n"
        "                      --backend chat --model gemma3\n"
        "  org-llm askbook add 'Plan a refactor' --backend reason\n"
        "  org-llm askbook add 'cloud second-opinion' --backend cloud\n\n"
        "Run pending entries:\n"
        "  org-llm askbook run                — process every pending\n"
        "  org-llm askbook run --backend cloud — only cloud entries\n\n"
    )


def _normalise_title(text: str, max_len: int = 70) -> str:
    """Squish a question down to a one-line heading title."""
    flat = " ".join(text.split())
    return (flat[:max_len - 1] + "…") if len(flat) > max_len else flat


def add_entry(question: str, *, backend: str, model: str = "",
                title: str = "") -> AskEntry:
    """Append one new pending entry to the askbook file. Returns the
    entry. `model` defaults to the backend's configured model."""
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; choose from {', '.join(SUPPORTED_BACKENDS)}")
    p = askbook_path()
    _ensure_header(p)
    # Microsecond precision keeps timestamps unique even when add_entry
    # is called twice in rapid succession (tests do this; users usually
    # don't, but the cost is one extra digit string).
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    safe_ts = ts.replace(":", "-")
    td = tangle_dir()
    td.mkdir(parents=True, exist_ok=True)
    title = title or _normalise_title(question)
    entry = AskEntry(
        timestamp=ts, backend=backend,
        model=model or _default_model_for(backend),
        title=title, status="pending",
        question=question, answer="(pending — run `org-llm askbook run`)",
    )
    block = _render_entry(entry, safe_ts)
    with open(p, "a") as f:
        f.write(block)
    return entry


def _render_entry(e: AskEntry, safe_ts: str) -> str:
    td = tangle_dir()
    return (
        f"\n* {('DONE' if e.status == 'done' else 'TODO')} Q[{e.timestamp}] {e.title}\n"
        f":PROPERTIES:\n"
        f":BACKEND:   {e.backend}\n"
        f":MODEL:     {e.model}\n"
        f":STATUS:    {e.status}\n"
        f":TIMESTAMP: {e.timestamp}\n"
        f":END:\n\n"
        f"#+name: q-{safe_ts}\n"
        f"#+begin_src text :tangle {td}/q-{safe_ts}.txt\n"
        f"{e.question}\n"
        f"#+end_src\n\n"
        f"#+name: a-{safe_ts}\n"
        f"#+begin_src text :tangle {td}/a-{safe_ts}.txt\n"
        f"{e.answer}\n"
        f"#+end_src\n\n"
    )


def _default_model_for(backend: str) -> str:
    """Look up the configured model for a backend. Falls back to a
    safe default when the DB isn't there yet."""
    cfg_key = {
        "chat":   "chat_model",
        "reason": "reason_model",
        "fast":   "fast_model",
        "code":   "code_model",
        "text":   "text_model",
        "cloud":  "cloud_model",
    }.get(backend)
    if not cfg_key:
        return ""    # claude / pi resolve their own models
    try:
        from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return MODEL_DEFAULTS.get(cfg_key, "")
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, cfg_key)
            return (row.value if row and row.value
                      else MODEL_DEFAULTS.get(cfg_key, ""))
    except Exception:
        return ""


def parse_entries(text: str | None = None) -> list[AskEntry]:
    """Parse every entry out of the askbook file (or supplied text)."""
    if text is None:
        p = askbook_path()
        if not p.exists():
            return []
        text = p.read_text()
    entries: list[AskEntry] = []
    headings = list(_HEADING_RE.finditer(text))
    qblocks = {m.group("ts"): m.group("body").rstrip("\n")
                for m in _QBLOCK_RE.finditer(text)}
    ablocks = {m.group("ts"): m.group("body").rstrip("\n")
                for m in _ABLOCK_RE.finditer(text)}
    for i, m in enumerate(headings):
        ts = m.group("ts")
        safe_ts = ts.replace(":", "-")
        title = m.group("title").strip()
        slab_end = (headings[i + 1].start() if i + 1 < len(headings)
                      else len(text))
        slab = text[m.end():slab_end]
        props = {pm.group("key"): pm.group("value").strip()
                   for pm in _PROPS_RE.finditer(slab)}
        entries.append(AskEntry(
            timestamp=ts,
            backend=props.get("BACKEND", "chat"),
            model=props.get("MODEL", ""),
            title=title,
            status=props.get("STATUS", "pending"),
            question=qblocks.get(safe_ts, ""),
            answer=ablocks.get(safe_ts, ""),
        ))
    return entries


def _write_back(entries: list[AskEntry]) -> None:
    """Rewrite the entire askbook file from a list of entries. Preserves
    the header preamble (above the first heading)."""
    p = askbook_path()
    _ensure_header(p)
    text = p.read_text()
    first = _HEADING_RE.search(text)
    head = text[:first.start()] if first else text
    body_parts = [head.rstrip("\n") + "\n"]
    for e in entries:
        body_parts.append(_render_entry(e, e.timestamp.replace(":", "-")))
    p.write_text("".join(body_parts))


def run_one(entry: AskEntry) -> AskEntry:
    """Dispatch one entry to its backend. Returns the updated entry."""
    started = time.monotonic()
    try:
        if entry.backend in ("chat", "reason", "fast", "code", "text"):
            entry.answer = _run_ollama(entry)
        elif entry.backend == "cloud":
            entry.answer = _run_cloud(entry)
        elif entry.backend == "claude":
            entry.answer = _run_subprocess_chat(["claude", "-p"], entry.question)
        elif entry.backend == "pi":
            entry.answer = _run_subprocess_chat(["pi", "-p"], entry.question)
        else:
            entry.answer = f"(unknown backend {entry.backend!r})"
            entry.status = "error"
            return entry
        entry.status = "done"
    except Exception as e:
        entry.answer = f"(error: {type(e).__name__}: {e})"
        entry.status = "error"
    # Fold a footer with timing so the user can see latency at a glance.
    elapsed = int((time.monotonic() - started) * 1000)
    if not entry.answer.endswith(")"):
        entry.answer = entry.answer.rstrip() + f"\n\n[ran in {elapsed}ms]"
    # Write to logbook so dbt sees it as an LLM call too.
    try:
        from .logbook import write_event as _log
        _log("llm", f"askbook-{entry.backend}",
              args=f"backend={entry.backend} model={entry.model}",
              model=entry.model,
              response=entry.answer[:1500],
              outcome=entry.status,
              duration_ms=elapsed)
    except Exception:
        pass
    return entry


def _ollama_url() -> str:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        p = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not p.exists():
            return "http://localhost:11434"
        engine = make_engine(p)
        with Session(engine) as s:
            row = s.get(Config, "ollama_url")
            return row.value if row and row.value else "http://localhost:11434"
    except Exception:
        return "http://localhost:11434"


def _run_ollama(entry: AskEntry) -> str:
    from .llm import chat
    return chat(entry.question, model=entry.model,
                  base_url=_ollama_url(), timeout=120.0)


def _run_cloud(entry: AskEntry) -> str:
    from .cloud import cloud_chat
    from . import creds
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        p = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        engine = make_engine(p)
        with Session(engine) as s:
            provider = (s.get(Config, "cloud_provider") or
                          type("R", (), {"value": "openrouter"})()).value
            endpoint = (s.get(Config, "cloud_endpoint_url") or
                          type("R", (), {"value": ""})()).value
        api_key = creds.read_secret(creds.cloud_slug(provider)) or ""
    except Exception:
        provider, endpoint, api_key = "openrouter", "", ""
    if not endpoint or not api_key:
        return ("(cloud not configured — run `org-llm cloud --configure`)")
    return cloud_chat(entry.question, model=entry.model,
                        endpoint_url=endpoint, api_key=api_key)


def _run_subprocess_chat(cmd_prefix: list[str], question: str) -> str:
    """Shell out to claude / pi for a one-shot answer."""
    import shutil, subprocess
    if not shutil.which(cmd_prefix[0]):
        return f"({cmd_prefix[0]} not on PATH — install it first)"
    try:
        r = subprocess.run([*cmd_prefix, question],
                             capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return f"({cmd_prefix[0]} timed out after 180s)"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return f"(exit {r.returncode}) {err[:500]}"
    return out or err or "(no output)"


def run_pending(*, backend_filter: str = "",
                  limit: int = 0) -> list[AskEntry]:
    """Find every pending entry, run it, persist results. Returns the
    entries that ran (in order)."""
    entries = parse_entries()
    targets: list[int] = []
    for i, e in enumerate(entries):
        if e.status != "pending":
            continue
        if backend_filter and e.backend != backend_filter:
            continue
        targets.append(i)
        if limit and len(targets) >= limit:
            break
    ran: list[AskEntry] = []
    for i in targets:
        entries[i] = run_one(entries[i])
        ran.append(entries[i])
    if ran:
        _write_back(entries)
    return ran


def export_to(path: Path, *, backend_filter: str = "",
                status_filter: str = "") -> int:
    """Copy entries (optionally filtered) to a new org file. Useful for
    saving conversation history per topic / per model. Returns count."""
    entries = parse_entries()
    if backend_filter:
        entries = [e for e in entries if e.backend == backend_filter]
    if status_filter:
        entries = [e for e in entries if e.status == status_filter]
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        f"#+title: org-llm askbook export — "
        f"{len(entries)} entries\n"
        f"#+filetags: :org-llm:askbook:export:noexport:\n"
        f"#+date: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"Exported from {askbook_path()}.\n"
    ]
    if backend_filter:
        parts.append(f"Filter: backend = {backend_filter}\n")
    if status_filter:
        parts.append(f"Filter: status = {status_filter}\n")
    parts.append("\n")
    for e in entries:
        parts.append(_render_entry(e, e.timestamp.replace(":", "-")))
    path.write_text("".join(parts))
    return len(entries)
