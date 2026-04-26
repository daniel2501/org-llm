# [[file:../../../org/20260425230731-org_llm.org::*code_index.py][code_index.py:1]]
"""Index source-code repos so `ask` can answer across notes AND code.

Reuses the existing File/Node/embedding pipeline — each indexed file becomes
one File row + one Node (whole-file body, capped at MAX_BODY_BYTES so a
single huge generated file doesn't dominate the embedding budget). The Node
gets a tag like ``code:python`` so the user can scope retrieval if they want.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from .db import File, Node


# ── What we index ─────────────────────────────────────────────────────────────

# (extension, language tag). Ordered roughly by usefulness for an org-roam
# user — you can extend this list, but new entries should map to a Pygments
# / Tree-sitter language name so the LLM can reason about them.
CODE_EXTENSIONS: dict[str, str] = {
    ".py":   "python",
    ".el":   "elisp",
    ".rs":   "rust",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".go":   "go",
    ".lua":  "lua",
    ".sh":   "shell",
    ".bash": "shell",
    ".fish": "shell",
    ".zsh":  "shell",
    ".rb":   "ruby",
    ".c":    "c",
    ".h":    "c",
    ".cpp":  "cpp",
    ".hpp":  "cpp",
    ".java": "java",
    ".kt":   "kotlin",
    ".scala":"scala",
    ".clj":  "clojure",
    ".ex":   "elixir",
    ".exs":  "elixir",
    ".hs":   "haskell",
    ".ml":   "ocaml",
    ".sql":  "sql",
    ".html": "html",
    ".css":  "css",
    ".scss": "css",
    ".vue":  "vue",
    ".svelte":"svelte",
    # Docs and configs
    ".md":   "markdown",
    ".rst":  "restructuredtext",
    ".org":  "org",
    ".yaml": "yaml",
    ".yml":  "yaml",
    ".toml": "toml",
    ".json": "json",
    ".dhall":"dhall",
    ".nix":  "nix",
    ".scm":  "scheme",
    ".lisp": "lisp",
    # Project metadata that often carries useful project context
    ".cfg":  "ini",
    ".ini":  "ini",
    ".conf": "conf",
}

# Skip these directories everywhere — they're noisy or huge.
SKIP_DIRS: set[str] = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "target", "dist", "build",
    ".next", ".nuxt", "coverage", "htmlcov", "vendor",
    "site-packages", ".gradle", ".idea", ".vscode", ".direnv", "result",
    ".cache",
}

# Files larger than this are truncated; helps the embedding step stay tractable
# on auto-generated lockfiles, bundled JS, etc.
MAX_BODY_BYTES = 24_000


def _walk(root: Path) -> Iterable[Path]:
    """Recursively yield every code-shaped file under root, skipping SKIP_DIRS."""
    for cur, dirnames, filenames in os.walk(root, followlinks=False):
        # Mutate dirnames in place to prune the walk
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in CODE_EXTENSIONS:
                yield Path(cur) / name


def _read_truncated(path: Path) -> str:
    """Read a file as text; truncate to MAX_BODY_BYTES; never raise."""
    try:
        # 'errors=replace' so non-utf8 bytes don't kill the indexing run.
        text = path.read_text(errors="replace")
    except Exception:
        return ""
    if len(text) > MAX_BODY_BYTES:
        text = text[:MAX_BODY_BYTES] + f"\n\n... [truncated; full file {len(text)} bytes]"
    return text


def index_code_dir(root: Path, session: Session) -> tuple[int, int]:
    """Index all code-shaped files under root. Returns (files, nodes).

    Per-file isolation: any single-file failure rolls back only that file's
    changes; the rest of the walk continues.
    """
    root = root.expanduser().resolve()
    if not root.exists():
        return (0, 0)

    files_indexed = 0
    nodes_indexed = 0
    now_iso = datetime.now().isoformat()

    for path in _walk(root):
        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue
        existing: File | None = (
            session.query(File).filter_by(path=str(path)).first()
        )
        if existing and existing.mtime >= mtime:
            continue
        body = _read_truncated(path)
        if not body.strip():
            continue
        ext = path.suffix.lower()
        lang = CODE_EXTENSIONS.get(ext, "text")
        # Tags: lang plus a "code" sentinel. User-friendly substring filters
        # like `lower(tags) LIKE '%code%'` will all hit.
        tags = f"code code:{lang}"
        title = str(path.relative_to(root.parent) if root.parent.exists() else path.name)

        try:
            if existing:
                existing.mtime = mtime
                existing.indexed_at = now_iso
                existing.node_count = 1
                session.query(Node).filter_by(file_id=existing.id).delete()
                file_rec = existing
            else:
                file_rec = File(path=str(path), indexed_at=now_iso,
                                node_count=1, mtime=mtime)
                session.add(file_rec)
                session.flush()
            session.add(Node(
                file_id=file_rec.id, node_id=None,
                title=title, body=body, tags=tags, mtime=mtime,
            ))
            session.commit()
            files_indexed += 1
            nodes_indexed += 1
        except Exception:
            session.rollback()
            continue

    return (files_indexed, nodes_indexed)


def index_code_dirs(roots: list[Path], session: Session,
                    progress_cb=None) -> tuple[int, int]:
    """Index every directory in `roots`. Sums per-dir counts."""
    total_files = total_nodes = 0
    for r in roots:
        f, n = index_code_dir(r, session)
        total_files += f
        total_nodes += n
        if progress_cb:
            progress_cb(r, f, n)
    return total_files, total_nodes
# code_index.py:1 ends here
