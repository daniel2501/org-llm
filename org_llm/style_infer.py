"""Deterministic org-mode style detection with on-disk cache.

The scribe agent's old "read 3 sample dailies and infer the format"
flow cost 3 cloud LLM round-trips per turn (~30s). This module
does the same job in ~10ms via regex + counts, then caches the
result keyed by directory path with mtime invalidation so the
next call skips even the disk read.

The cache lives at ~/.cache/org-llm/style-cache.json. Entries
expire when the directory's newest .org file mtime changes — i.e.
the moment the user edits a daily, the next style probe re-runs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_CACHE_DIR  = Path.home() / ".cache" / "org-llm"
_CACHE_PATH = _CACHE_DIR / "style-cache.json"
_TTL_SECONDS = 24 * 3600   # hard expiration even if mtime hasn't changed


# Patterns we recognise. `cookie` = `[ ]` / `[X]` / `[-]` checkbox box;
# `todo`   = `TODO` / `DONE` / `WAITING` / `NEXT` / `HOLD` keyword;
# `prio`   = `[#A]` / `[#B]` / `[#C]` priority marker.
_HEADER_COOKIE_RE = re.compile(r"^(\*+)\s+\[[X\- ]\]\s+", re.MULTILINE)
_HEADER_TODO_RE   = re.compile(
    r"^(\*+)\s+(TODO|DONE|NEXT|WAITING|HOLD)\b", re.MULTILINE)
_BULLET_COOKIE_RE = re.compile(r"^\s*-\s+\[[X\- ]\]\s+", re.MULTILINE)
_BULLET_PLAIN_RE  = re.compile(r"^\s*-\s+(?!\[)", re.MULTILINE)
_PRIO_RE          = re.compile(r"\[#[ABC]\]")
_ROAM_LINK_RE     = re.compile(r"\[\[id:[a-f0-9-]+\]\[")


def _newest_mtime(dir_path: Path, sample: int = 5) -> float:
    """Return the max mtime across the N most recently modified
    .org files in the directory. Returns 0 if the dir doesn't
    exist or has no .org files."""
    if not dir_path.exists() or not dir_path.is_dir():
        return 0.0
    files = sorted(dir_path.glob("*.org"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return 0.0
    return max(f.stat().st_mtime for f in files[:sample])


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text()) or {}
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def infer_style(dir_path: str, sample_size: int = 3,
                 force_refresh: bool = False) -> dict:
    """Return a dict describing the dominant org-mode style in
    `dir_path`. Cached by directory path with mtime-aware
    invalidation. Schema:

        {"dir":              str,
         "samples_examined": int,
         "newest_mtime":     float,
         "cached":           bool,
         "dominant_shape":   "header_cookie" | "header_todo"
                              | "bullet_cookie" | "plain_bullet"
                              | "mixed" | "unknown",
         "uses_priority":    bool,
         "uses_roam_links":  bool,
         "example_line":     str,
         "raw_counts":       {pattern: int}}
    """
    p = Path(dir_path).expanduser().resolve()
    cache = _load_cache()
    key = str(p)
    now_mtime = _newest_mtime(p, sample_size)
    cached = cache.get(key)
    import time as _t
    if (cached and not force_refresh
            and cached.get("newest_mtime") == now_mtime
            and (_t.time() - cached.get("cached_at", 0)) < _TTL_SECONDS):
        cached["cached"] = True
        return cached

    if not p.exists() or not p.is_dir():
        result = {
            "dir": str(p), "samples_examined": 0,
            "newest_mtime": 0.0, "cached": False,
            "dominant_shape": "unknown",
            "uses_priority": False, "uses_roam_links": False,
            "example_line": "",
            "raw_counts": {},
            "cached_at": _t.time(),
        }
        return result

    files = sorted(p.glob("*.org"),
                    key=lambda f: f.stat().st_mtime, reverse=True
                    )[:sample_size]
    counts = {"header_cookie": 0, "header_todo": 0,
               "bullet_cookie": 0, "plain_bullet": 0}
    examples: dict[str, str] = {}
    has_priority = False
    has_roam_links = False
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # Counts
        for m in _HEADER_COOKIE_RE.finditer(text):
            counts["header_cookie"] += 1
            if "header_cookie" not in examples:
                start = m.start()
                eol = text.find("\n", start)
                examples["header_cookie"] = text[start:eol if eol > 0 else None].strip()
        for m in _HEADER_TODO_RE.finditer(text):
            counts["header_todo"] += 1
            if "header_todo" not in examples:
                start = m.start()
                eol = text.find("\n", start)
                examples["header_todo"] = text[start:eol if eol > 0 else None].strip()
        for m in _BULLET_COOKIE_RE.finditer(text):
            counts["bullet_cookie"] += 1
            if "bullet_cookie" not in examples:
                start = m.start()
                eol = text.find("\n", start)
                examples["bullet_cookie"] = text[start:eol if eol > 0 else None].strip()
        # Plain bullets are common in prose; only count when there
        # are no cookies in the file (otherwise they're just sub-
        # bullets under a checkbox heading).
        if not _BULLET_COOKIE_RE.search(text) and not _HEADER_COOKIE_RE.search(text):
            for m in _BULLET_PLAIN_RE.finditer(text):
                counts["plain_bullet"] += 1
                if "plain_bullet" not in examples:
                    start = m.start()
                    eol = text.find("\n", start)
                    examples["plain_bullet"] = text[start:eol if eol > 0 else None].strip()
        if _PRIO_RE.search(text):
            has_priority = True
        if _ROAM_LINK_RE.search(text):
            has_roam_links = True

    # Pick dominant. Require a 2× margin over the runner-up to
    # avoid declaring "header_cookie" dominant when "bullet_cookie"
    # is nearly as common.
    sorted_counts = sorted(counts.items(), key=lambda kv: kv[1],
                            reverse=True)
    top, top_n = sorted_counts[0]
    runner_n   = sorted_counts[1][1] if len(sorted_counts) > 1 else 0
    if top_n == 0:
        dominant = "unknown"
    elif top_n >= 2 * max(runner_n, 1):
        dominant = top
    else:
        dominant = "mixed"

    result = {
        "dir":              str(p),
        "samples_examined": len(files),
        "newest_mtime":     now_mtime,
        "cached":           False,
        "dominant_shape":   dominant,
        "uses_priority":    has_priority,
        "uses_roam_links":  has_roam_links,
        "example_line":     examples.get(dominant, ""),
        "raw_counts":       counts,
        "cached_at":        _t.time(),
    }
    cache[key] = result
    _save_cache(cache)
    return result


def style_summary(info: dict) -> str:
    """Human-readable one-paragraph summary of an `infer_style`
    result. Used by the MCP tool wrapper so the LLM gets terse
    actionable guidance, not a JSON blob to re-parse."""
    shape = info.get("dominant_shape") or "unknown"
    example = info.get("example_line") or ""
    extras = []
    if info.get("uses_priority"):
        extras.append("priorities (`[#A]`/`[#B]`)")
    if info.get("uses_roam_links"):
        extras.append("org-roam id links (`[[id:…][title]]`)")
    extras_str = " · ".join(extras)
    cached_marker = " (cached)" if info.get("cached") else ""

    if shape == "header_cookie":
        head = (
            "Format: header-cookie. Headings FLUSH LEFT (no "
            "leading spaces). States: `[ ]` open / `[X]` done / "
            "`[-]` wip.\n"
            "DEFAULT (flat — most cases):\n"
            "<<<\n* [ ] Make bed\n* [ ] Clean bathroom\n>>>\n"
            "PARENT + SUB-TASKS only for meaningful real "
            "categories (chores / project name etc). Don't invent "
            "wrapper parents ('Weekend', 'List'). Don't invent "
            "sub-tasks. Use `**` headings, not `- [ ]` bullets.\n"
            "<<<\n* [ ] chores\n** [ ] Make bed\n** [ ] Clean bathroom\n>>>")
    elif shape == "header_todo":
        head = (
            "Format: HEADERS-WITH-TODO.\n"
            "EXACT TEMPLATE — copy this shape verbatim:\n"
            "  * TODO Category heading\n"
            "  ** TODO Sub-task one\n"
            "  ** DONE Sub-task already finished\n"
            "RULES: TODO/DONE/NEXT/WAITING keywords on each headline. "
            "No `[ ]` cookies — they're redundant with TODO state.")
    elif shape == "bullet_cookie":
        head = (
            "Format: CHECKBOX-BULLETS — flat list under one heading.\n"
            "EXACT TEMPLATE:\n"
            "  * Heading\n"
            "  - [ ] First item\n"
            "  - [X] Done item\n"
            "RULES: ONE top-level heading; items are `-` bullets "
            "with cookies. No nested `**` sub-headings.")
    elif shape == "plain_bullet":
        head = (
            "Format: PLAIN BULLETS — `- item` lines, no cookies "
            "or TODO state. Used for prose / reference notes.")
    elif shape == "mixed":
        head = (
            "Format: MIXED — multiple shapes coexist. Default to "
            "HEADERS-WITH-COOKIE (`* [ ] Heading` / `** [ ] sub-task`) "
            "since it's the most expressive.\n"
            "EXACT TEMPLATE:\n"
            "  * [ ] Category heading\n"
            "  ** [ ] Sub-task one")
    else:
        head = (
            "Format: UNKNOWN — no samples or no recognisable pattern. "
            "Fall back to HEADERS-WITH-COOKIE.\n"
            "EXACT TEMPLATE:\n"
            "  * [ ] Category\n"
            "  ** [ ] Sub-task")

    lines = [head + cached_marker]
    if extras_str:
        lines.append(f"Conventions: {extras_str}.")
    if example:
        lines.append(f"Example from samples: `{example[:120]}`")
    return "\n".join(lines)
