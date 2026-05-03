"""Deterministic org-mode helpers for all agents.

The pattern (same as `style_infer.py`): the user has the files
on disk, so most "what does my vault look like?" questions can
be answered by parsing the .org files directly — no LLM
round-trip needed. Each helper here is a few-millisecond regex
or shell-out, returning structured output the agent can read at
its leisure.

Exposed as MCP tools in `mcp_server.py` (org_grep,
org_file_meta, org_outline, org_tag_index, org_agenda,
org_count_matches, org_tag_suggest).

Design constraints:
- Read-only. None of these helpers write to the vault.
- No LLM calls. Pure Python + optional ripgrep shell-out.
- Cheap. Caller can call dozens of these without thinking.
- Path-resolved against `org_dir` config; refuse paths outside.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


# ── Path safety ───────────────────────────────────────────────────────────────

def _org_dir() -> Path:
    """Resolve the user's org_dir from env or config."""
    from .db import Config, make_engine
    from sqlalchemy.orm import Session
    env = os.environ.get("ORG_LLM_ORG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    try:
        with Session(make_engine()) as s:
            row = s.get(Config, "org_dir")
            if row and row.value:
                return Path(row.value).expanduser().resolve()
    except Exception:
        pass
    return Path("~/org").expanduser().resolve()


def _resolve_under_org(path: str) -> Path | None:
    """Resolve `path` and ensure it sits under `org_dir`. Returns
    None if the path escapes."""
    if not path:
        return None
    org = _org_dir()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = org / p
    p = p.resolve()
    try:
        p.relative_to(org)
    except ValueError:
        return None
    return p


# ── Text search (ripgrep with python fallback) ────────────────────────────────

def org_grep(pattern: str, *, path_glob: str = "**/*.org",
              max_results: int = 50, ignore_case: bool = True
              ) -> list[dict]:
    """Search the vault for `pattern` across org files. Each hit:
    {file, line, text}. Uses ripgrep when available, falls back to
    a python re scan."""
    if not pattern:
        return []
    org = _org_dir()
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "--color=never",
                "-g", path_glob,
                "--max-count", str(max_results),
                "-e", pattern, str(org)]
        if ignore_case:
            cmd.insert(2, "-i")
        try:
            out = subprocess.run(cmd, capture_output=True,
                                  text=True, timeout=15)
            results: list[dict] = []
            for line in (out.stdout or "").splitlines():
                # rg format: <path>:<line>:<text>
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                results.append({
                    "file": parts[0],
                    "line": int(parts[1]) if parts[1].isdigit() else 0,
                    "text": parts[2].strip()[:200],
                })
                if len(results) >= max_results:
                    break
            return results
        except (subprocess.TimeoutExpired, OSError):
            pass
    # Python fallback
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return []
    results: list[dict] = []
    for f in org.glob(path_glob):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                results.append({
                    "file": str(f),
                    "line": i,
                    "text": line[:200],
                })
                if len(results) >= max_results:
                    return results
    return results


def org_count_matches(pattern: str, *, path_glob: str = "**/*.org",
                       ignore_case: bool = True) -> dict:
    """Count regex matches across the vault. Returns
    {pattern, total_hits, files_with_hits, top_files: [(path, count), ...]}.

    Designed for questions like 'how many times have I checked
    off making the bed?' — the LLM passes
    pattern=r'\\[X\\].*make bed' and gets a number, no per-file
    inspection needed."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return {"pattern": pattern, "error": f"bad regex: {e}",
                 "total_hits": 0, "files_with_hits": 0, "top_files": []}
    org = _org_dir()
    per_file: Counter = Counter()
    for f in org.glob(path_glob):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        n = len(compiled.findall(text))
        if n > 0:
            per_file[str(f)] = n
    total = sum(per_file.values())
    return {
        "pattern":          pattern,
        "total_hits":       total,
        "files_with_hits":  len(per_file),
        "top_files":        per_file.most_common(10),
    }


# ── File metadata ─────────────────────────────────────────────────────────────

_TITLE_RE      = re.compile(r"^#\+title:\s*(.+?)\s*$",  re.MULTILINE | re.IGNORECASE)
_FILE_ID_RE    = re.compile(r":ID:\s+([A-Za-z0-9-]+)",  re.IGNORECASE)
_FILE_TAGS_RE  = re.compile(r"^#\+filetags:\s*(.+?)\s*$",
                              re.MULTILINE | re.IGNORECASE)
_HEADING_RE    = re.compile(r"^(\*+)\s+(.+?)$", re.MULTILINE)
# Headline trailing tags: org tags only live at end of heading line
# as :tag1:tag2:…: preceded by whitespace. Anything else (property
# drawer keys like :PROPERTIES:/:ID:/:END:, link syntax) is NOT a tag.
_HEADLINE_TAGS_RE = re.compile(
    r"^\*+[ \t]+[^\n]*?[ \t]+(:[A-Za-z][\w@-]*(?::[A-Za-z][\w@-]*)*:)[ \t]*$",
    re.MULTILINE,
)
_TAG_TOKEN_RE     = re.compile(r"[A-Za-z][\w@-]*")


def org_file_meta(path: str) -> dict:
    """Return file-level metadata: title, id, file-tags, headline
    counts (per level), mtime, byte size, link count, todo count.
    Returns {} when the path resolves outside org_dir or is missing."""
    p = _resolve_under_org(path)
    if not p or not p.is_file():
        return {}
    try:
        text = p.read_text(errors="replace")
    except Exception:
        return {}
    title_m = _TITLE_RE.search(text)
    id_m    = _FILE_ID_RE.search(text)
    tags_m  = _FILE_TAGS_RE.search(text)
    file_tags = []
    if tags_m:
        # filetags: :tag1:tag2: OR  tag1 tag2 — handle both
        raw = tags_m.group(1).strip()
        if ":" in raw:
            file_tags = [t for t in raw.split(":") if t]
        else:
            file_tags = raw.split()
    inline_tags: set[str] = set()
    for tag_block in _HEADLINE_TAGS_RE.findall(text):
        for t in _TAG_TOKEN_RE.findall(tag_block):
            inline_tags.add(t)
    inline_tags_list = sorted(inline_tags)
    headings = _HEADING_RE.findall(text)
    levels: Counter = Counter(len(h[0]) for h in headings)
    todo_count = sum(1 for stars, body in headings
                       if re.match(r"(?:TODO|NEXT|WAITING|HOLD)\b", body))
    done_count = sum(1 for stars, body in headings
                       if re.match(r"DONE\b", body))
    cookie_open = sum(1 for stars, body in headings
                       if re.match(r"\[\s\]", body))
    cookie_done = sum(1 for stars, body in headings
                       if re.match(r"\[X\]", body))
    link_count = len(re.findall(r"\[\[id:[^\]]+\]\[", text))
    stat = p.stat()
    return {
        "path":         str(p),
        "title":        title_m.group(1).strip() if title_m else p.stem,
        "id":           id_m.group(1) if id_m else "",
        "file_tags":    file_tags,
        "inline_tags":  inline_tags_list,
        "headings":     dict(levels),
        "headings_total": sum(levels.values()),
        "todos":        todo_count,
        "dones":        done_count,
        "cookies_open": cookie_open,
        "cookies_done": cookie_done,
        "roam_links":   link_count,
        "bytes":        stat.st_size,
        "mtime":        stat.st_mtime,
        "modified":     datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def org_outline(path: str, max_depth: int = 99) -> list[dict]:
    """Return the heading tree of `path` as a flat list:
    [{level, text, line, id?, tags?}, …]. `level` is the asterisk
    count. Useful for the LLM to pick a sub-tree to read without
    reading the whole file."""
    p = _resolve_under_org(path)
    if not p or not p.is_file():
        return []
    try:
        text = p.read_text(errors="replace")
    except Exception:
        return []
    out: list[dict] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(\*+)\s+(.+?)$", line)
        if not m:
            continue
        level = len(m.group(1))
        if level > max_depth:
            continue
        body = m.group(2)
        # strip trailing :tags:tags:
        tags: list[str] = []
        tm = re.search(r":([A-Za-z][\w@-]*(?::[A-Za-z][\w@-]*)+):\s*$", body)
        if tm:
            tags = [t for t in tm.group(0).strip(":").split(":") if t]
            body = body[:tm.start()].rstrip()
        out.append({
            "level": level,
            "text":  body,
            "line":  i,
            "tags":  tags,
        })
    return out


# ── Tag taxonomy ──────────────────────────────────────────────────────────────

def org_tag_index(limit: int = 50) -> list[tuple[str, int]]:
    """Return [(tag, count), …] sorted by count desc, capped at
    `limit`. Aggregates :inline: tags + #+filetags: across the
    whole vault. Use this so the LLM picks tags from the user's
    existing taxonomy instead of inventing new ones."""
    org = _org_dir()
    counts: Counter = Counter()
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # filetags
        ft = _FILE_TAGS_RE.search(text)
        if ft:
            raw = ft.group(1).strip()
            for t in (raw.split(":") if ":" in raw else raw.split()):
                if t:
                    counts[t] += 1
        # inline tags (headline trailing only — never property drawer keys)
        for tag_block in _HEADLINE_TAGS_RE.findall(text):
            for t in _TAG_TOKEN_RE.findall(tag_block):
                counts[t] += 1
    return counts.most_common(max(1, limit))


def org_tag_suggest(text: str, *, top_n: int = 5) -> list[str]:
    """Suggest up to `top_n` tags for arbitrary `text`, drawn
    ONLY from tags that already exist in the vault.

    Heuristic: rank existing tags by (frequency in vault) ×
    (literal-substring match against `text` ∪ word-stem match).
    Returns the top names, no scores. Caller can show them as
    'these are your existing tags that fit this content.'"""
    if not text:
        return []
    text_lower = text.lower()
    words = set(re.findall(r"[a-z][a-z0-9_-]{2,}", text_lower))
    scored: list[tuple[str, float]] = []
    for tag, count in org_tag_index(limit=200):
        tl = tag.lower()
        score = 0.0
        if tl in text_lower:
            score += count * 2.0      # literal hit
        elif tl in words:
            score += count * 1.5
        elif any(tl in w or w in tl for w in words):
            score += count * 0.5      # stem-ish
        if score > 0:
            scored.append((tag, score))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in scored[:top_n]]


# ── Agenda ────────────────────────────────────────────────────────────────────

_SCHEDULED_RE  = re.compile(r"SCHEDULED:\s*<(\d{4}-\d{2}-\d{2})(?:[^>]*)>")
_DEADLINE_RE   = re.compile(r"DEADLINE:\s*<(\d{4}-\d{2}-\d{2})(?:[^>]*)>")
_TODO_HEAD_RE  = re.compile(r"^(\*+)\s+(TODO|NEXT|WAITING|HOLD)\b\s+(.+?)$",
                              re.MULTILINE)


def org_agenda(window_days: int = 7) -> dict:
    """Build a window agenda from the vault. Returns
    {today: [...], upcoming: [...], overdue: [...], stale_todo: [...]}.

    Each item: {file, line, level, state, text, scheduled?, deadline?}.

    `today` = SCHEDULED or DEADLINE within today.
    `upcoming` = within next `window_days` (default 7).
    `overdue` = SCHEDULED or DEADLINE in the past, still TODO/NEXT/WAITING.
    `stale_todo` = TODO/NEXT/WAITING with NO date, in files unmodified
    >30 days (caller can lower the bar via repeat-search).

    No LLM round-trip; pure scan. ~50ms on a 700-file vault."""
    today_d = datetime.now().date()
    soon_d  = today_d + timedelta(days=max(0, window_days))
    out = {"today": [], "upcoming": [], "overdue": [], "stale_todo": []}
    org = _org_dir()
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        lines = text.splitlines()
        f_mtime = datetime.fromtimestamp(f.stat().st_mtime).date()
        for m in _TODO_HEAD_RE.finditer(text):
            head_start = m.start()
            head_line  = text.count("\n", 0, head_start) + 1
            level      = len(m.group(1))
            state      = m.group(2)
            head_text  = m.group(3).strip()
            # Look at the next 5 lines for SCHEDULED/DEADLINE
            ctx_end = head_start
            for _ in range(5):
                nxt = text.find("\n", ctx_end + 1)
                if nxt == -1:
                    break
                ctx_end = nxt
            ctx = text[head_start:ctx_end + 1]
            sched_m = _SCHEDULED_RE.search(ctx)
            dead_m  = _DEADLINE_RE.search(ctx)
            sched = (datetime.strptime(sched_m.group(1), "%Y-%m-%d").date()
                     if sched_m else None)
            dead  = (datetime.strptime(dead_m.group(1), "%Y-%m-%d").date()
                     if dead_m else None)
            item = {
                "file": str(f),
                "line": head_line,
                "level": level,
                "state": state,
                "text":  head_text,
                "scheduled": sched.isoformat() if sched else None,
                "deadline":  dead.isoformat() if dead else None,
            }
            soonest = min((d for d in (sched, dead) if d), default=None)
            if soonest is None:
                if (today_d - f_mtime).days > 30:
                    out["stale_todo"].append(item)
            elif soonest < today_d:
                out["overdue"].append(item)
            elif soonest == today_d:
                out["today"].append(item)
            elif soonest <= soon_d:
                out["upcoming"].append(item)
    return out
