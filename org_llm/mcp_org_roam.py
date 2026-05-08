"""org-roam-mcp wrapper — Python access to the user's org-roam knowledge base.

R26 P1-17 — wires three of the upstream
[aserranoni/org-roam-mcp](https://github.com/aserranoni/org-roam-mcp) v0.2.0
tools into specialist.py:

  - search_nodes(query, limit=10) → list[dict]
  - get_node(node_id) → dict
  - get_backlinks(node_id) → list[dict]

Implementation choice: this wrapper queries org-roam's SQLite database
*directly* rather than spawning the upstream MCP server as a subprocess.
That upstream package is itself a thin SQLite wrapper (see
src/org_roam_mcp/database.py in the cloned repo) and our context is an
in-process tool dispatch, not a stdio/MCP transport. Direct SQL gives:

  - zero new dependencies (sqlite3 ships with Python)
  - no subprocess startup penalty per tool call
  - same query semantics as the upstream tools

The DB-storage quirk we replicate: org-roam stores ids/titles wrapped in
double-quotes, so before a SELECT we add quotes if missing, and on the
way out we strip them. (Upstream calls these helpers _clean_string /
_clean_path; we inline the equivalent.)

Auto-detection scans the same paths upstream does, plus Doom Emacs's
default cache location which upstream missed:

    ~/.config/emacs/.local/cache/org-roam.db    ← Doom default
    ~/.emacs.d/org-roam.db
    ~/.config/emacs/org-roam.db
    ~/org-roam.db
    ~/Documents/org-roam/org-roam.db

Override with env var ORG_ROAM_DB_PATH.

Sister to org_llm.vault_rag.vault_search (R19 Track D, Qdrant-backed).
The two are complementary — org-roam-mcp answers structural questions
("what links to this node?") + exact-title lookups; vault_search answers
semantic-prose questions. Both are wired as specialist tools; let cells
pick.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional


# ── DB auto-detection ───────────────────────────────────────────────────
DEFAULT_DB_CANDIDATES = (
    # Doom Emacs default — upstream org-roam-mcp v0.2.0 misses this one,
    # which is the location actually in use by the org-llm developer.
    "~/.config/emacs/.local/cache/org-roam.db",
    "~/.emacs.d/org-roam.db",
    "~/.config/emacs/org-roam.db",
    "~/org-roam.db",
    "~/Documents/org-roam/org-roam.db",
)

# Module-level connection cache. org-roam writes the DB sporadically
# (manual M-x org-roam-db-sync); we open read-only for safety.
_CONN: Optional[sqlite3.Connection] = None
_CONN_PATH: Optional[str] = None


def _find_db_path() -> str:
    """Locate org-roam.db. Honors ORG_ROAM_DB_PATH env var, else scans defaults."""
    override = os.environ.get("ORG_ROAM_DB_PATH")
    if override:
        if Path(override).exists():
            return override
        raise FileNotFoundError(
            f"ORG_ROAM_DB_PATH={override} but file does not exist")
    for cand in DEFAULT_DB_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists() and p.stat().st_size > 0:
            return str(p)
    raise FileNotFoundError(
        "No org-roam.db found in default locations. Set ORG_ROAM_DB_PATH "
        "or run M-x org-roam-db-sync in Emacs.")


def _connect() -> sqlite3.Connection:
    """Lazy connect, reused across calls. Read-only via uri=true."""
    global _CONN, _CONN_PATH
    db_path = _find_db_path()
    if _CONN is not None and _CONN_PATH == db_path:
        return _CONN
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
    # Read-only URI form keeps us safe even if the model misroutes a write.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _CONN = conn
    _CONN_PATH = db_path
    return conn


def _strip_quotes(s: Any) -> str:
    """org-roam stores ids/titles as quoted strings; strip the wrapping quotes."""
    if s is None:
        return ""
    return str(s).strip('"')


def _quote_id(node_id: str) -> str:
    """Add wrapping quotes if absent, since the DB stores quoted ids."""
    if node_id.startswith('"') and node_id.endswith('"'):
        return node_id
    return f'"{node_id}"'


# ── Public: search_nodes ─────────────────────────────────────────────────
def search_nodes(query: str, limit: int = 10) -> list[dict]:
    """Search org-roam nodes by title, alias, or tag (LIKE %query%).

    Returns a list of dicts with keys: id, file, title, level, tags,
    aliases. Mirrors the upstream search_nodes tool but returns plain
    JSON-serializable dicts (not OrgRoamNode dataclasses).
    """
    if not query or not query.strip():
        return []
    limit = max(1, min(100, int(limit)))
    conn = _connect()
    sql = """
    SELECT DISTINCT n.id, n.file, n.level, n.title
    FROM nodes n
    LEFT JOIN aliases a ON n.id = a.node_id
    LEFT JOIN tags t ON n.id = t.node_id
    WHERE n.title LIKE ?
       OR a.alias LIKE ?
       OR t.tag LIKE ?
    ORDER BY n.title
    LIMIT ?
    """
    pat = f"%{query}%"
    cur = conn.execute(sql, (pat, pat, pat, limit))
    rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        nid_clean = _strip_quotes(r["id"])
        out.append({
            "id": nid_clean,
            "file": _strip_quotes(r["file"]),
            "title": _strip_quotes(r["title"]),
            "level": r["level"],
            "tags": _node_tags(conn, r["id"]),
            "aliases": _node_aliases(conn, r["id"]),
        })
    return out


def _node_tags(conn: sqlite3.Connection, raw_id: str) -> list[str]:
    """Tags for a node (raw_id is the quoted form from the nodes row)."""
    cur = conn.execute("SELECT tag FROM tags WHERE node_id = ?", (raw_id,))
    return [_strip_quotes(r["tag"]) for r in cur.fetchall()]


def _node_aliases(conn: sqlite3.Connection, raw_id: str) -> list[str]:
    cur = conn.execute("SELECT alias FROM aliases WHERE node_id = ?", (raw_id,))
    return [_strip_quotes(r["alias"]) for r in cur.fetchall()]


# ── Public: get_node ─────────────────────────────────────────────────────
def get_node(node_id: str) -> dict:
    """Fetch full node by ID. Returns {} if not found.

    Includes the nodes-table fields plus tags + aliases. The upstream
    MCP get_node tool also reads file content; we leave that to the
    standard read_file tool to avoid duplicating the read pipeline.
    """
    if not node_id or not node_id.strip():
        return {}
    conn = _connect()
    qid = _quote_id(node_id.strip())
    sql = """
    SELECT id, file, level, pos, todo, priority, scheduled, deadline,
           title, properties, olp
    FROM nodes
    WHERE id = ?
    """
    cur = conn.execute(sql, (qid,))
    row = cur.fetchone()
    if not row:
        return {}
    return {
        "id": _strip_quotes(row["id"]),
        "file": _strip_quotes(row["file"]),
        "level": row["level"],
        "pos": row["pos"],
        "todo": row["todo"],
        "priority": row["priority"],
        "scheduled": row["scheduled"],
        "deadline": row["deadline"],
        "title": _strip_quotes(row["title"]),
        "properties": row["properties"],
        "olp": row["olp"],
        "tags": _node_tags(conn, row["id"]),
        "aliases": _node_aliases(conn, row["id"]),
    }


# ── Public: get_backlinks ───────────────────────────────────────────────
def get_backlinks(node_id: str) -> list[dict]:
    """Return list of nodes that link TO node_id.

    Output dicts: {source_id, source_title, source_file, type, pos}.
    Joins links table back to nodes so callers don't need a second
    round-trip per backlink.
    """
    if not node_id or not node_id.strip():
        return []
    conn = _connect()
    qid = _quote_id(node_id.strip())
    sql = """
    SELECT l.pos, l.source, l.dest, l.type, n.title AS source_title,
           n.file AS source_file
    FROM links l
    LEFT JOIN nodes n ON n.id = l.source
    WHERE l.dest = ?
    ORDER BY n.title
    """
    cur = conn.execute(sql, (qid,))
    rows = cur.fetchall()
    return [
        {
            "source_id": _strip_quotes(r["source"]),
            "source_title": _strip_quotes(r["source_title"]),
            "source_file": _strip_quotes(r["source_file"]),
            "type": _strip_quotes(r["type"]),
            "pos": r["pos"],
        }
        for r in rows
    ]


# ── Tool specs for specialist.py dispatch ───────────────────────────────
SEARCH_NODES_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_roam_search_nodes",
        "description": (
            "Search the user's org-roam knowledge base by title, alias, "
            "or tag. Returns matching nodes with id, file, title, level, "
            "tags, aliases. Use this when you need to find a canonical "
            "node by name (exact-title-ish matching, not semantic) or "
            "discover the real :ID: UUID for a concept before linking. "
            "Backed by the org-roam SQLite database — fast, deterministic. "
            "For semantic prose-content search prefer vault_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match against node titles, aliases, and tags.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (1-100). Default 10.",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
}


GET_NODE_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_roam_get_node",
        "description": (
            "Fetch a single org-roam node by its :ID: UUID. Returns "
            "id, file, title, level, pos, todo, priority, properties, "
            "olp, tags, aliases. Empty dict if not found. Use after "
            "mcp_roam_search_nodes to verify a candidate id, or with "
            "an id you've already extracted from a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "Org-roam :ID: UUID (with or without surrounding quotes).",
                },
            },
            "required": ["node_id"],
        },
    },
}


GET_BACKLINKS_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_roam_get_backlinks",
        "description": (
            "Return nodes that LINK TO the given node id. Each result: "
            "source_id, source_title, source_file, type, pos. Use to "
            "audit cross-references before edits, or to ground @atoz "
            "claims in real graph state instead of guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "Org-roam :ID: UUID of the target node.",
                },
            },
            "required": ["node_id"],
        },
    },
}


MCP_ROAM_TOOLS = [SEARCH_NODES_TOOL, GET_NODE_TOOL, GET_BACKLINKS_TOOL]


# ── Health check ─────────────────────────────────────────────────────────
def healthcheck() -> dict:
    """Smoke-test for preflight scripts. Returns counts + db path."""
    try:
        conn = _connect()
        n = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        ln = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
        return {
            "ok": True,
            "db_path": _CONN_PATH,
            "nodes": n,
            "links": ln,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
