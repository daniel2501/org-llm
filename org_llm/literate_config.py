"""Literate config — DB ↔ ~/org/org-llm-config.org round-trip.

Mirrors every "user-tweakable" config key from the SQLite `config`
table to a single org file you can edit by hand, version-control, or
read alongside your other notes. The org file is the SAME shape as
context.org / llm-history.org — one heading per key, a PROPERTIES
drawer with metadata, and a `#+begin_src text :tangle …` block whose
content IS the value.

Two sync directions:

  tangle_db_to_org()   — write the org file from current DB state
  apply_org_to_db()    — read the org file, write changes back to DB
  diff_db_vs_org()     — show what would change in either direction

What's INCLUDED:
  • Every key in MODEL_DEFAULTS that the user is meant to tweak
    (chat_model, embed_model, ollama_url, theme dials, doctor knobs,
    log knobs, auto_embed knobs, etc.)
  • Custom keys the user has added to the config table.

What's EXCLUDED:
  • Runtime state masquerading as config (cloud_usage = a JSON event
    log; user_theme_knobs = managed via `org-llm knob`). These are
    too structured + churn too much for a literate file.
  • db_version (internal; round-tripping it is a footgun).

Auto-sync on writes is opt-in via `config_org_autosync` — when true,
every set_config call also re-tangles the org file. Off by default
because writing files on every config tweak feels surprising.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_LITERATE_PATH = Path("~/org/org-llm-config.org").expanduser()
_TANGLE_DIR    = Path("~/.local/share/org-llm/config").expanduser()


# Keys that round-trip cleanly. JSON state + internal counters excluded.
EXCLUDED_KEYS = {
    "cloud_usage",        # event log, not config
    "user_theme_knobs",   # managed by `org-llm knob`
    "db_version",         # internal
}


# Tiny human-readable description per key. Falls back to "(no description)"
# for keys not listed here. Keep these short — the org heading title
# already holds the key name.
KEY_DESCRIPTIONS = {
    "org_dir":           "Where your org-roam vault lives.",
    "ollama_url":        "Local Ollama API URL (default :11434).",
    "embed_model":       "Model used for vector embeddings.",
    "chat_model":        "Model used for ask / chat.",
    "code_model":        "Model used for code generation.",
    "reason_model":      "Model used for planning + complex reasoning.",
    "fast_model":        "Model used for tagging + classification.",
    "instruct_model":    "Model used for capture + instruction following.",
    "text_model":        "Model used for summarization + text analysis.",
    "review_model":      "Model used by `org-llm review-emacs`.",
    "tag_model":         "Override model used by `org-llm tag` (else fast_model).",
    "fixer_model":       "Cloud model used for SRE-style fix recovery.",
    "embed_dim":         "Embedding dimensionality (matches your embed_model).",
    "context_window":    "Default context window in tokens.",
    "temperature":       "Default chat sampling temperature.",
    "top_p":             "Default chat top-p sampling.",
    "code_dirs":         "Comma-separated paths for `code-index`.",
    "theme":             "UI color mode: dark | light.",
    "trek_level":        "Star Trek voice intensity (0-3).",
    "commie_level":      "Solidarity / collective-action voice (0-3).",
    "queer_level":       "Pride / care voice (0-3).",
    "cloud_provider":    "Active cloud provider slug (e.g. openrouter).",
    "cloud_endpoint_url":"Cloud provider's OpenAI-compatible endpoint.",
    "cloud_model":       "Cloud model name to route chat through.",
    "doctor_proactive_mode":   "off | passive | active | aggressive.",
    "doctor_stuck_threshold":  "N tool calls before LLM self-doctors.",
    "doctor_intervene_in":     "Comma-sep triggers for proactive_doctor.",
    "doctor_auto_apply":       "True = power-boost applies without --apply.",
    "log_level":               "off | minimal | normal | verbose.",
    "log_kinds":               "Comma-sep event kinds the logbook records.",
    "log_max_rows_per_kind":   "Per-kind cap in the History table.",
    "log_auto_reflect_every":  "Run LLM reflection every Nth invocation. 0 = off.",
    "auto_embed_enabled":      "Background auto-embedder daemon thread.",
    "auto_embed_interval_secs":"Watcher poll interval (≥15s).",
    "auto_embed_quiet":        "Suppress per-batch terminal output.",
    "config_org_autosync":     "Re-tangle org-llm-config.org on every set.",
}


def literate_path() -> Path:
    """Where the literate config file lives. Env override for tests."""
    return Path(os.environ.get("ORG_LLM_LITERATE_CONFIG_PATH")
                  or str(_LITERATE_PATH))


def tangle_dir() -> Path:
    return Path(os.environ.get("ORG_LLM_LITERATE_CONFIG_TANGLE_DIR")
                  or str(_TANGLE_DIR))


@dataclass
class _ConfigEntry:
    key:          str
    value:        str
    default:      str
    description:  str

    @property
    def is_modified(self) -> bool:
        return (self.value or "") != (self.default or "")


def _gather_entries() -> list[_ConfigEntry]:
    """Pull every relevant key from DB + MODEL_DEFAULTS, merged."""
    from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
    from sqlalchemy.orm import Session
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return []
    engine = make_engine(path)
    rows: dict[str, str] = {}
    with Session(engine) as s:
        for r in s.query(Config).all():
            rows[r.key] = r.value or ""
    entries: list[_ConfigEntry] = []
    all_keys = (set(rows) | set(MODEL_DEFAULTS)) - EXCLUDED_KEYS
    for k in sorted(all_keys):
        entries.append(_ConfigEntry(
            key=k,
            value=rows.get(k, MODEL_DEFAULTS.get(k, "")),
            default=MODEL_DEFAULTS.get(k, ""),
            description=KEY_DESCRIPTIONS.get(k, "(no description)"),
        ))
    return entries


def _render_org(entries: list[_ConfigEntry]) -> str:
    """Build the full org file body from entries.

    Output shape (per entry):

        * <key>
        :PROPERTIES:
        :KEY:         <key>
        :DEFAULT:     <default>
        :MODIFIED:    yes|no
        :DESCRIPTION: <one-line>
        :END:

        #+name: cfg-<key>
        #+begin_src text :tangle ~/.local/share/org-llm/config/<key>
        <value>
        #+end_src
    """
    td = tangle_dir()
    head = (
        "#+title: org-llm — literate config\n"
        "#+filetags: :org-llm:config:noexport:\n"
        "#+startup: showall\n\n"
        "Round-trip mirror of the SQLite `config` table.\n"
        "  • [bold]org-llm config --tangle[/bold]   → write this file from DB\n"
        "  • [bold]org-llm config --apply-from-org[/bold] → write DB from this file\n"
        "  • [bold]org-llm config --diff-org[/bold]  → show what would change\n\n"
        f"Tangled per-key plaintext mirrors land at {td}/<key>.\n"
        "Edit any block's body, then `org-llm config --apply-from-org`\n"
        "to push your changes back to the DB.\n\n"
    )
    body_parts = [head]
    for e in entries:
        # Multi-line / multi-word values stay verbatim — the tangle
        # block grabs everything between begin_src and end_src.
        body_parts.append(
            f"* {e.key}\n"
            f":PROPERTIES:\n"
            f":KEY:         {e.key}\n"
            f":DEFAULT:     {e.default}\n"
            f":MODIFIED:    {'yes' if e.is_modified else 'no'}\n"
            f":DESCRIPTION: {e.description}\n"
            f":END:\n\n"
            f"#+name: cfg-{e.key}\n"
            f"#+begin_src text :tangle {td}/{e.key}\n"
            f"{e.value}\n"
            f"#+end_src\n\n"
        )
    return "".join(body_parts)


_HEADING_RE = re.compile(r"^\* (\S.*)$", re.MULTILINE)
_BLOCK_RE   = re.compile(
    r"#\+begin_src text :tangle [^\n]+\n(.*?)\n#\+end_src",
    re.DOTALL,
)


def _parse_org(text: str) -> dict[str, str]:
    """Reverse of _render_org: extract {key: value} from the org file.

    Robust to user edits: relies on heading + the immediately-following
    src block. Comments / extra text between are tolerated; only the
    src block's contents become the value. Multi-line values preserved.
    """
    out: dict[str, str] = {}
    # Walk headings + the next src block after each.
    headings = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        key = m.group(1).strip()
        # Look at the slice between this heading and the next.
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        slab = text[m.end():end]
        block = _BLOCK_RE.search(slab)
        if not block:
            continue
        value = block.group(1)
        # Strip the trailing newline we emit at write time.
        if value.endswith("\n"):
            value = value[:-1]
        out[key] = value
    return out


def tangle_db_to_org() -> Path:
    """Render the literate file from the current DB state. Returns
    the path written. Idempotent — safe to call repeatedly."""
    entries = _gather_entries()
    p = literate_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_render_org(entries))
    return p


def apply_org_to_db(*, dry_run: bool = False) -> tuple[int, list[tuple[str, str, str]]]:
    """Read the literate file, write changes back to the DB.

    Returns (n_changed, [(key, old, new), ...]). When `dry_run=True`,
    nothing is written; the change list is the diff that *would* apply.
    """
    p = literate_path()
    if not p.exists():
        return (0, [])
    parsed = _parse_org(p.read_text())
    changes: list[tuple[str, str, str]] = []
    from .db import DB_PATH, Config, make_engine
    from sqlalchemy.orm import Session
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return (0, [])
    engine = make_engine(path)
    with Session(engine) as s:
        for key, new_val in parsed.items():
            if key in EXCLUDED_KEYS:
                continue
            row = s.get(Config, key)
            old_val = row.value if row else ""
            if (old_val or "") == (new_val or ""):
                continue
            changes.append((key, old_val or "", new_val))
            if dry_run:
                continue
            if row:
                row.value = new_val
            else:
                s.add(Config(key=key, value=new_val))
        if not dry_run:
            s.commit()
    return (len(changes), changes)


def diff_db_vs_org() -> list[tuple[str, str, str, str]]:
    """Show every difference between DB and the literate file.

    Returns [(key, db_value, org_value, direction), ...] where
    direction is one of: "db-only" (key in DB, not in org file),
    "org-only" (vice versa), "differ" (both sides have different values).
    """
    p = literate_path()
    parsed = _parse_org(p.read_text()) if p.exists() else {}
    from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
    from sqlalchemy.orm import Session
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return []
    engine = make_engine(path)
    with Session(engine) as s:
        db_rows = {r.key: r.value or "" for r in s.query(Config).all()}
    out: list[tuple[str, str, str, str]] = []
    keys = (set(db_rows) | set(parsed) | set(MODEL_DEFAULTS)) - EXCLUDED_KEYS
    for k in sorted(keys):
        db_v  = db_rows.get(k, "")
        org_v = parsed.get(k, "")
        if k not in parsed and k in db_rows:
            out.append((k, db_v, "", "db-only"))
        elif k not in db_rows and k in parsed:
            out.append((k, "", org_v, "org-only"))
        elif (db_v or "") != (org_v or "") and k in parsed and k in db_rows:
            out.append((k, db_v, org_v, "differ"))
    return out


def autosync_enabled() -> bool:
    """Read the config_org_autosync key with safe fallback to False."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return False
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, "config_org_autosync")
            return (row.value or "").lower() in {"1", "true", "yes", "on"} \
                if row else False
    except Exception:
        return False


def maybe_autosync() -> None:
    """If autosync is enabled, re-tangle the org file. Best-effort."""
    if not autosync_enabled():
        return
    try:
        tangle_db_to_org()
    except Exception:
        pass
