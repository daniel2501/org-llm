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


# ── Roam link graph (Phase 22.6.1) ────────────────────────────────────────────

# Compiled once, reused across helpers below.
_ID_LINK_RE       = re.compile(r"\[\[id:([A-Za-z0-9-]+)\](?:\[([^\]]*)\])?\]")
_ANY_HEADING_ID_RE = re.compile(
    r"^\*+\s+.*?\n(?:\s*:PROPERTIES:\s*\n(?:[^\n]*\n)*?\s*:ID:\s+"
    r"([A-Za-z0-9-]+)\s*\n)?",
    re.MULTILINE,
)
_HEADING_ID_BLOCK_RE = re.compile(
    r"^\*+\s+(?P<title>[^\n]+)\n"
    r"(?:[^*\n][^\n]*\n)*?"
    r"\s*:ID:\s+(?P<id>[A-Za-z0-9-]+)",
    re.MULTILINE,
)


def _vault_id_index() -> dict[str, dict]:
    """Scan the vault and build {id: {file, title, kind}}.

    `kind` is "file" for file-level IDs (declared in the file's
    :PROPERTIES: drawer at the top) and "heading" for IDs declared
    on heading drawers. ~30-80ms on a 700-file vault."""
    org = _org_dir()
    idx: dict[str, dict] = {}
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # File-level ID: lives in the FIRST :PROPERTIES: drawer
        # before any heading.
        first_heading = re.search(r"^\*+\s+", text, re.MULTILINE)
        head = text[: first_heading.start()] if first_heading else text
        fid_m = _FILE_ID_RE.search(head)
        if fid_m:
            title_m = _TITLE_RE.search(head)
            idx[fid_m.group(1)] = {
                "file":  str(f),
                "title": (title_m.group(1).strip()
                            if title_m else f.stem),
                "kind":  "file",
            }
        # Heading-level IDs.
        for hm in _HEADING_ID_BLOCK_RE.finditer(text):
            hid = hm.group("id")
            if hid in idx:
                continue
            title = hm.group("title").strip()
            # strip trailing :tags:tags:
            title = re.sub(r"\s+:[A-Za-z][\w@:-]*:\s*$", "", title)
            idx[hid] = {
                "file":  str(f),
                "title": title,
                "kind":  "heading",
            }
    return idx


def _file_outlinks(path: Path) -> list[str]:
    """Return the list of `[[id:…]]` targets from `path` (raw IDs)."""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []
    return [m.group(1) for m in _ID_LINK_RE.finditer(text)]


def org_link_graph(node_id: str, *, hops: int = 2,
                    max_nodes: int = 100) -> dict:
    """Walk roam `[[id:…]]` links starting from `node_id` up to
    `hops` steps. Returns
    {root, hops, nodes: [...], edges: [(from_id, to_id), ...],
     truncated: bool}. Each `nodes` entry: {id, title, file, kind, depth}.

    BFS — each node visited once, capped at `max_nodes`. Use to
    answer 'what is this node connected to?' without re-reading
    every file.
    """
    idx = _vault_id_index()
    if node_id not in idx:
        return {"root": node_id, "error": "id not in vault",
                 "nodes": [], "edges": [], "truncated": False}
    seen: set[str]               = {node_id}
    nodes: list[dict]            = [
        {**idx[node_id], "id": node_id, "depth": 0},
    ]
    edges: list[tuple[str, str]] = []
    frontier: list[tuple[str, int]] = [(node_id, 0)]
    truncated = False
    while frontier:
        cur, depth = frontier.pop(0)
        if depth >= hops:
            continue
        info = idx.get(cur)
        if not info:
            continue
        for target in _file_outlinks(Path(info["file"])):
            edges.append((cur, target))
            if target in seen:
                continue
            seen.add(target)
            tinfo = idx.get(target)
            nodes.append({
                "id":     target,
                "title":  tinfo["title"] if tinfo else "",
                "file":   tinfo["file"]  if tinfo else "",
                "kind":   tinfo["kind"]  if tinfo else "missing",
                "depth":  depth + 1,
            })
            if len(nodes) >= max_nodes:
                truncated = True
                break
            frontier.append((target, depth + 1))
        if truncated:
            break
    return {"root": node_id, "hops": hops,
             "nodes": nodes, "edges": edges,
             "truncated": truncated}


def org_backlinks(target: str) -> list[dict]:
    """List incoming roam links pointing at `target` (id or file path).

    Each hit: {from_id, from_title, from_file, line, anchor_text}.
    `target` accepts a node ID OR a file path; for file paths the
    file's primary ID is resolved first."""
    idx = _vault_id_index()
    target_id = target
    if "/" in target or target.endswith(".org"):
        p = _resolve_under_org(target)
        if p:
            for nid, info in idx.items():
                if info["file"] == str(p) and info["kind"] == "file":
                    target_id = nid
                    break
    org = _org_dir()
    out: list[dict] = []
    pat = re.compile(rf"\[\[id:{re.escape(target_id)}\](?:\[([^\]]*)\])?\]")
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            m = pat.search(line)
            if not m:
                continue
            # Find which file/heading this LINE belongs to.
            from_id = ""
            from_title = ""
            from_kind  = "file"
            # Walk up to nearest heading or fall back to file ID.
            for nid, info in idx.items():
                if info["file"] == str(f) and info["kind"] == "file":
                    from_id    = nid
                    from_title = info["title"]
                    break
            out.append({
                "from_id":     from_id,
                "from_title":  from_title,
                "from_file":   str(f),
                "line":        i,
                "anchor_text": (m.group(1) or "").strip(),
            })
    return out


def org_orphans(*, max_results: int = 50) -> list[dict]:
    """Files with NO incoming roam links, NO outgoing roam links,
    AND no inline tags / file-tags. Triage candidates for prune /
    refile / tag.

    Returns up to `max_results` entries: {file, title, bytes, mtime,
    headings_total}. ~80ms on a 700-file vault."""
    idx = _vault_id_index()
    org = _org_dir()
    in_degree: Counter = Counter()
    out_degree: Counter = Counter()
    file_id: dict[str, str] = {}
    for nid, info in idx.items():
        if info["kind"] == "file":
            file_id[info["file"]] = nid
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        outs = _ID_LINK_RE.findall(text)
        out_degree[str(f)] = len(outs)
        for tid, _ in outs:
            tinfo = idx.get(tid)
            if tinfo:
                in_degree[tinfo["file"]] += 1
    out: list[dict] = []
    for f in sorted(org.glob("**/*.org")):
        if not f.is_file():
            continue
        path = str(f)
        if in_degree.get(path, 0) > 0 or out_degree.get(path, 0) > 0:
            continue
        meta = org_file_meta(path) or {}
        if meta.get("file_tags") or meta.get("inline_tags"):
            continue
        stat = f.stat()
        out.append({
            "file":           path,
            "title":          meta.get("title", f.stem),
            "bytes":          stat.st_size,
            "mtime":          stat.st_mtime,
            "headings_total": meta.get("headings_total", 0),
        })
        if len(out) >= max_results:
            break
    return out


# ── Property drawer search ────────────────────────────────────────────────────

_PROPERTY_BLOCK_RE = re.compile(
    r"^\*+\s+(?P<title>[^\n]+)\n"
    r"\s*:PROPERTIES:\s*\n(?P<body>(?:[^\n]+\n)*?)\s*:END:",
    re.MULTILINE,
)
_PROPERTY_LINE_RE = re.compile(
    r"^\s*:(?P<key>[A-Za-z][\w@-]*):\s*(?P<val>.*?)\s*$", re.MULTILINE,
)


def org_property_search(prop: str, *, value: str | None = None,
                         path_glob: str = "**/*.org",
                         max_results: int = 50) -> list[dict]:
    """Find headings whose :PROPERTIES: drawer contains `prop`
    (case-insensitive). When `value` is provided, also require the
    value to match (substring, case-insensitive).

    Returns up to `max_results` rows: {file, line, title, prop, value}."""
    if not prop:
        return []
    org = _org_dir()
    out: list[dict] = []
    val_l = (value or "").lower() or None
    prop_l = prop.lower()
    for f in org.glob(path_glob):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for m in _PROPERTY_BLOCK_RE.finditer(text):
            body = m.group("body")
            for pm in _PROPERTY_LINE_RE.finditer(body):
                if pm.group("key").lower() != prop_l:
                    continue
                v = pm.group("val")
                if val_l is not None and val_l not in v.lower():
                    continue
                head_line = text.count("\n", 0, m.start()) + 1
                title = re.sub(r"\s+:[A-Za-z][\w@:-]*:\s*$", "",
                                 m.group("title").strip())
                out.append({
                    "file":  str(f),
                    "line":  head_line,
                    "title": title,
                    "prop":  pm.group("key"),
                    "value": v,
                })
                if len(out) >= max_results:
                    return out
    return out


# ── Fuzzy node-ID lookup ──────────────────────────────────────────────────────

def org_id_find(query: str, *, top_n: int = 10) -> list[dict]:
    """Fuzzy-find node IDs by partial title match. Cheaper than
    search_notes when you just want the ID for a known title.

    Returns up to `top_n` rows ranked by match-score:
    {id, title, file, kind, score}. Score is the number of query
    tokens that appear in the title (case-insensitive), tied broken
    by shorter titles preferred."""
    if not query:
        return []
    qtokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t]
    if not qtokens:
        return []
    idx = _vault_id_index()
    scored: list[tuple[int, int, str, dict]] = []
    for nid, info in idx.items():
        title_l = (info.get("title") or "").lower()
        if not title_l:
            continue
        hits = sum(1 for t in qtokens if t in title_l)
        if hits == 0:
            continue
        # Tie-break: shorter title is more specific.
        scored.append((hits, -len(title_l), nid, info))
    scored.sort(reverse=True)
    out = []
    for hits, neg_len, nid, info in scored[:top_n]:
        out.append({
            "id":    nid,
            "title": info["title"],
            "file":  info["file"],
            "kind":  info["kind"],
            "score": hits,
        })
    return out


# ── Refile destination proposals ──────────────────────────────────────────────

def _file_token_set(path: Path) -> set[str]:
    """Tokens drawn from filename + #+title — used for path/title
    overlap scoring in refile suggestions."""
    tokens = set(re.findall(r"[a-z0-9]+", path.stem.lower()))
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return tokens
    title_m = _TITLE_RE.search(text)
    if title_m:
        tokens.update(re.findall(r"[a-z0-9]+", title_m.group(1).lower()))
    return tokens


def org_refile_candidates(heading_text: str, *, heading_tags: list[str] | None = None,
                            top_n: int = 5) -> list[dict]:
    """Propose refile destinations for a heading. Scores existing
    org files by:
      1. Tag overlap between `heading_tags` and the file's
         #+filetags / inline tags (weight 3 per match).
      2. Token overlap between the heading text and file title /
         filename (weight 1 per match).

    Returns up to `top_n` rows: {file, title, score, reasons:
    [...]}. Use BEFORE asking the user 'where should this go?' so
    the agent has good defaults."""
    if not heading_text:
        return []
    tags = set(t.lower() for t in (heading_tags or []) if t)
    head_tokens = set(re.findall(r"[a-z0-9]+", heading_text.lower()))
    head_tokens = {t for t in head_tokens if len(t) >= 3}
    org = _org_dir()
    scored: list[tuple[float, dict]] = []
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        meta = org_file_meta(str(f)) or {}
        f_tags = set(t.lower() for t in
                      (meta.get("file_tags") or []) +
                      (meta.get("inline_tags") or []))
        tag_hits   = len(tags & f_tags) if tags else 0
        f_tokens   = _file_token_set(f)
        token_hits = len(head_tokens & f_tokens)
        score = tag_hits * 3 + token_hits
        if score == 0:
            continue
        reasons: list[str] = []
        if tag_hits:
            reasons.append(f"shared tags: {sorted(tags & f_tags)}")
        if token_hits:
            reasons.append(f"shared tokens: {sorted(head_tokens & f_tokens)}")
        scored.append((score, {
            "file":    str(f),
            "title":   meta.get("title", f.stem),
            "score":   score,
            "reasons": reasons,
        }))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [row for _, row in scored[:top_n]]


# ── CLOCK / drill / attach (Phase 22.6.2) ─────────────────────────────────────

_CLOCK_RE = re.compile(
    # `CLOCK: [2026-05-03 Sat 09:00]--[2026-05-03 Sat 10:30] =>  1:30`
    r"CLOCK:\s*\[(\d{4}-\d{2}-\d{2})[^\]]*\]"
    r"--\[(\d{4}-\d{2}-\d{2})[^\]]*\]"
    r"\s*=>\s*(\d+):(\d+)"
)


def org_clock_summary(*, since_days: int = 7,
                       per_tag: bool = False) -> dict:
    """Aggregate CLOCK time across the vault.

    Parses `CLOCK: [start]--[end] => H:MM` lines and totals minutes
    per heading (always) and per file-tag (when `per_tag=True`).
    Returns:
      {
        "since_days":   N,
        "total_minutes": int,
        "by_heading":   [{heading, file, line, minutes}, ...],
        "by_tag":       {tag: minutes, ...} (present iff per_tag),
      }

    Use to answer "where did my week go?" — local-only, no LLM.
    """
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.now() - _td(days=max(1, since_days))).date()
    org = _org_dir()
    by_heading: list[dict] = []
    by_tag:     Counter    = Counter()
    total_min  = 0
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # Walk through CLOCK matches; for each, find the nearest
        # heading above for attribution.
        f_meta = org_file_meta(str(f)) or {}
        f_tags = set((f_meta.get("file_tags") or [])
                      + (f_meta.get("inline_tags") or []))
        head_starts: list[tuple[int, str]] = []  # [(offset, heading_text)]
        for hm in _HEADING_RE.finditer(text):
            head_starts.append((hm.start(), hm.group(2)))
        for cm in _CLOCK_RE.finditer(text):
            start_d = datetime.strptime(cm.group(1), "%Y-%m-%d").date()
            if start_d < cutoff:
                continue
            mins = int(cm.group(3)) * 60 + int(cm.group(4))
            total_min += mins
            # Find the most recent heading above this CLOCK match.
            heading = ""
            line_no = 0
            for hs, ht in reversed(head_starts):
                if hs < cm.start():
                    heading = ht.strip()
                    line_no = text.count("\n", 0, hs) + 1
                    break
            by_heading.append({
                "heading": heading,
                "file":    str(f),
                "line":    line_no,
                "minutes": mins,
            })
            if per_tag:
                for tag in f_tags:
                    by_tag[tag] += mins
    return {
        "since_days":    since_days,
        "total_minutes": total_min,
        "by_heading":    sorted(by_heading,
                                  key=lambda r: r["minutes"],
                                  reverse=True)[:25],
        **({"by_tag": dict(by_tag.most_common(20))}
            if per_tag else {}),
    }


_DRILL_LAST_RE = re.compile(
    r":DRILL_LAST_REVIEWED:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE,
)
_DRILL_NEXT_RE = re.compile(
    r":DRILL_NEXT_REVIEW:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE,
)


def org_drill_review_due(*, max_results: int = 50) -> list[dict]:
    """Find org-drill / org-fc cards whose next review is overdue.

    Scans every heading drawer for `:DRILL_NEXT_REVIEW:` (or its
    dash variant `:DRILL-NEXT-REVIEW:`); returns the headings whose
    next-review date is on or before today, sorted by most-overdue
    first.

    Each row: {file, line, title, last_review, next_review,
    overdue_days}.
    """
    from datetime import datetime as _dt
    today = _dt.now().date()
    org = _org_dir()
    out: list[dict] = []
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for m in _PROPERTY_BLOCK_RE.finditer(text):
            body = m.group("body")
            nm = _DRILL_NEXT_RE.search(body)
            if not nm:
                continue
            try:
                next_d = datetime.strptime(nm.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if next_d > today:
                continue
            lm = _DRILL_LAST_RE.search(body)
            last = lm.group(1) if lm else ""
            head_line = text.count("\n", 0, m.start()) + 1
            title = re.sub(r"\s+:[A-Za-z][\w@:-]*:\s*$", "",
                             m.group("title").strip())
            out.append({
                "file":         str(f),
                "line":         head_line,
                "title":        title,
                "last_review":  last,
                "next_review":  nm.group(1),
                "overdue_days": (today - next_d).days,
            })
            if len(out) >= max_results * 2:   # collect, sort, trim
                break
    out.sort(key=lambda r: r["overdue_days"], reverse=True)
    return out[:max_results]


def org_attach_list(node_id_or_path: str) -> list[dict]:
    """List the attachments belonging to a given node.

    org-attach stores a node's attachments under a directory derived
    from its ID — typically `<org_dir>/.attach/<id[0:2]>/<id[2:]>/`
    (the default `org-attach-id-ts-folder-format`). When `node_id_or_path`
    is an ID, resolve via that scheme; when it's a file path, look up
    the file's primary ID first.

    Each row: {name, size, mtime, path}.
    """
    org = _org_dir()
    nid = node_id_or_path
    # Normalise: if it's a file path, find the file's primary ID.
    if "/" in nid or nid.endswith(".org"):
        idx = _vault_id_index()
        p = _resolve_under_org(nid)
        target_path = str(p) if p else nid
        for k, info in idx.items():
            if info["file"] == target_path and info["kind"] == "file":
                nid = k
                break
        else:
            return []
    if not re.fullmatch(r"[A-Za-z0-9-]{6,}", nid):
        return []
    # Try the two most common org-attach layouts.
    candidates = [
        org / ".attach" / nid[:2] / nid[2:],
        org / "data"    / nid[:2] / nid[2:],
        org / nid[:2]   / nid[2:],
    ]
    for d in candidates:
        if not d.is_dir():
            continue
        out: list[dict] = []
        try:
            for child in sorted(d.iterdir()):
                try:
                    st = child.stat()
                    out.append({
                        "name":  child.name,
                        "size":  st.st_size,
                        "mtime": st.st_mtime,
                        "path":  str(child),
                    })
                except OSError:
                    continue
        except OSError:
            continue
        if out:
            return out
    return []


# ── Capture template render (Phase 22.6.3) ────────────────────────────────────

# Org capture template syntax (subset): %t (date), %T (timestamp),
# %u/%U (inactive [date]/[timestamp]), %i (initial), %a (link),
# %?  (cursor — left as-is), %^{prompt} (left as-is, caller fills).
_TEMPLATE_TODAY     = re.compile(r"%t")
_TEMPLATE_NOW       = re.compile(r"%T")
_TEMPLATE_INACTIVE  = re.compile(r"%u")
_TEMPLATE_INACTIVE_TS = re.compile(r"%U")
_TEMPLATE_INITIAL   = re.compile(r"%i")
_TEMPLATE_LINK      = re.compile(r"%a")


def org_template_apply(name: str, *, initial: str = "",
                          link: str = "") -> str:
    """Render a saved capture template by name into a string.

    Templates live in `db.Config('capture_templates')` as JSON:
        {"todo":"* TODO %?\\n  %t\\n  %i", ...}

    Substitutes %t/%T/%u/%U/%i/%a; leaves %? and %^{prompt} for the
    caller (org-capture's interactive surface). Returns "" when the
    name isn't registered or the JSON is malformed.
    """
    if not name:
        return ""
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
        import json as _json
    except Exception:
        return ""
    raw: str = ""
    try:
        with Session(make_engine()) as s:
            row = s.get(Config, "capture_templates")
            raw = (row.value if row else "") or ""
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        templates = _json.loads(raw)
    except Exception:
        return ""
    body = templates.get(name)
    if not isinstance(body, str):
        return ""
    now = datetime.now()
    today_active   = now.strftime("<%Y-%m-%d %a>")
    now_active     = now.strftime("<%Y-%m-%d %a %H:%M>")
    today_inactive = now.strftime("[%Y-%m-%d %a]")
    now_inactive   = now.strftime("[%Y-%m-%d %a %H:%M]")
    body = _TEMPLATE_NOW.sub(now_active,           body)
    body = _TEMPLATE_INACTIVE_TS.sub(now_inactive, body)
    body = _TEMPLATE_TODAY.sub(today_active,       body)
    body = _TEMPLATE_INACTIVE.sub(today_inactive,  body)
    body = _TEMPLATE_INITIAL.sub(initial,          body)
    body = _TEMPLATE_LINK.sub(link,                body)
    return body


# ── Doom config introspection (Phase 22.6.3) ──────────────────────────────────

_DOOM_DIR_CANDIDATES = [
    "~/.doom.d",
    "~/.config/doom",
]
_PACKAGE_RE = re.compile(
    r"^\s*\(package!\s+([A-Za-z][\w-]*)", re.MULTILINE,
)
_UNPIN_RE = re.compile(
    r"^\s*\(unpin!\s+([A-Za-z][\w-]*)", re.MULTILINE,
)
_DISABLED_RE = re.compile(
    r":disable\s+t", re.IGNORECASE,
)
# `(map! :leader [optional :desc "..."] "key" <command form>...)`.
# Permissive — the user's bindings come in many shapes:
#   (map! :leader "d <left>" (cmd! (windmove-delete-left)))   ← no :desc
#   (map! :leader :desc "Save" "f a" #'save-buffer)           ← named cmd
#   (map! :desc "Snip" "M-s" #'snippet-cmd)                   ← non-leader
_LEADER_BIND_RE = re.compile(
    r'\(map!\s+:leader'
    r'(?:\s+:desc\s+"([^"]*)")?'        # group 1: optional :desc
    r'\s+"([^"\n]+)"\s+'                 # group 2: key
    r"([^\n]+)",                         # group 3: rest of the line
    re.MULTILINE,
)
_BIND_RE = re.compile(
    r'\(map!\s+'
    r'(?!:leader\b)'                     # not the leader form (covered above)
    r'(?:[^"\n]*?:desc\s+"([^"]*)")?'   # group 1: optional :desc
    r'\s+"([^"\n]+)"\s+'                 # group 2: key
    r"([^\n]+)",                         # group 3: rest of the line
    re.MULTILINE,
)


def _doom_dir() -> Path | None:
    """Return the user's Doom config directory or None if missing."""
    for c in _DOOM_DIR_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_dir():
            return p
    return None


def doom_packages() -> list[dict]:
    """Parse ~/.doom.d/packages.el for `(package! …)` declarations.

    Each row: {name, recipe?, disabled, source_line}. Disabled is
    True when the entry has `:disable t`. Returns [] when the file
    is missing.
    """
    d = _doom_dir()
    if not d:
        return []
    pf = d / "packages.el"
    if not pf.is_file():
        return []
    try:
        text = pf.read_text(errors="replace")
    except Exception:
        return []
    out: list[dict] = []
    for m in _PACKAGE_RE.finditer(text):
        name = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        # Look at the line for `:disable t`
        line_text = text.split("\n")[line - 1] if line > 0 else ""
        out.append({
            "name":        name,
            "disabled":    bool(_DISABLED_RE.search(line_text)),
            "source_line": line_text.strip()[:200],
            "line":        line,
        })
    return out


def doom_keybinds() -> list[dict]:
    """Parse ~/.doom.d/config.el (and bindings.el if present) for
    leader-key bindings declared via `(map! :leader …)` or `(map!
    … :desc …)`.

    Each row: {key, desc, command, file, line}. Best-effort regex
    parse — won't catch everything Doom's macros allow but covers
    the common case in user configs.
    """
    d = _doom_dir()
    if not d:
        return []
    out: list[dict] = []
    for fname in ("config.el", "bindings.el"):
        fp = d / fname
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(errors="replace")
        except Exception:
            continue
        for m in _LEADER_BIND_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            cmd = (m.group(3) or "").strip().rstrip(")")
            out.append({
                "key":     m.group(2),
                "desc":    m.group(1) or "",
                "command": cmd[:120],
                "scope":   "leader",
                "file":    str(fp),
                "line":    line,
            })
        for m in _BIND_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            cmd = (m.group(3) or "").strip().rstrip(")")
            out.append({
                "key":     m.group(2),
                "desc":    m.group(1) or "",
                "command": cmd[:120],
                "scope":   "global",
                "file":    str(fp),
                "line":    line,
            })
    return out
