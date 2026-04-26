"""Proactive filesystem discovery for org-llm.

The app should be aware of what the user *actually has* on disk, not just
what their config table claims. This module probes common locations and
returns structured findings that other commands can surface or feed into
prompts.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import NamedTuple


# Standard locations we always look at, in priority order.
_PROBE_DIRS: tuple[tuple[str, str], ...] = (
    # (path, kind hint)
    ("~/org",                "vault"),
    ("~/repos",              "repos-root"),
    ("~/code",               "repos-root"),
    ("~/projects",           "repos-root"),
    ("~/work",               "repos-root"),
    ("~/dotfiles",           "dotfiles"),
    ("~/repos/dotfiles",     "dotfiles"),
    ("~/.config/doom",       "doom-config"),
    ("~/.doom.d",            "doom-config"),
    ("~/.config/emacs",      "vanilla-emacs"),
    ("~/.emacs.d",           "vanilla-emacs"),
    ("~/.password-store",    "pass-store"),
    ("~/.local/share/ollama", "ollama-data"),
)


# Language detection based on file extension; mirrors the more aggressive
# code_index.CODE_EXTENSIONS but kept lean for stat-only sweeps.
_LANG_EXT = {
    ".py": "python",  ".el": "elisp",      ".rs": "rust",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",       ".lua": "lua",       ".sh": "shell",
    ".rb": "ruby",     ".c": "c",           ".cpp": "cpp",
    ".java": "java",   ".kt": "kotlin",     ".scala": "scala",
    ".clj": "clojure", ".ex": "elixir",     ".hs": "haskell",
    ".sql": "sql",     ".scm": "scheme",    ".nix": "nix",
    ".html": "html",   ".css": "css",
    ".md": "markdown", ".org": "org",
    ".yaml": "yaml",   ".yml": "yaml",      ".toml": "toml",
}


class FoundPath(NamedTuple):
    path:         Path
    kind:         str          # vault | repos-root | dotfiles | doom-config | …
    file_count:   int          # ~ count of code-shaped files (capped)
    top_lang:     str          # most-frequent extension's language, "" if none
    description:  str


def _count_code_files(root: Path, cap: int = 5000) -> tuple[int, str]:
    """Walk root cheaply; return (count, top_language)."""
    count = 0
    langs = Counter()
    SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__",
            "target", "dist", "build", "site-packages", ".cache"}
    for cur, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in _LANG_EXT:
                count += 1
                langs[_LANG_EXT[ext]] += 1
                if count >= cap:
                    top = langs.most_common(1)[0][0] if langs else ""
                    return count, top
    top = langs.most_common(1)[0][0] if langs else ""
    return count, top


def discover(extra_dirs: list[str] | None = None) -> list[FoundPath]:
    """Probe known + user-supplied locations and return what we find."""
    out: list[FoundPath] = []
    seen: set[Path] = set()
    candidates = list(_PROBE_DIRS) + [(d, "user-supplied") for d in (extra_dirs or [])]
    for raw, kind in candidates:
        try:
            p = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if p in seen or not p.exists() or not p.is_dir():
            continue
        seen.add(p)
        if kind == "vault":
            n_org = sum(1 for _ in p.rglob("*.org"))
            out.append(FoundPath(p, kind, n_org, "org",
                                  f"{n_org} .org files"))
        elif kind == "pass-store":
            has_gpg_id = (p / ".gpg-id").exists()
            n_secrets  = sum(1 for _ in p.rglob("*.gpg"))
            out.append(FoundPath(
                p, kind, n_secrets, "",
                f"{n_secrets} secret(s); "
                + ("initialised" if has_gpg_id else "[red]not initialised[/red]"),
            ))
        elif kind in ("repos-root",):
            children = [c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")]
            count, top = _count_code_files(p)
            out.append(FoundPath(
                p, kind, count, top,
                f"{len(children)} subdirs · {count} code-shaped files"
                + (f" · top: {top}" if top else ""),
            ))
        else:
            count, top = _count_code_files(p)
            out.append(FoundPath(
                p, kind, count, top,
                f"{count} code-shaped files"
                + (f" · top: {top}" if top else ""),
            ))
    return out


def suggest_code_dirs(found: list[FoundPath] | None = None) -> list[Path]:
    """Pick the most useful indexable roots for `code-index`."""
    found = found if found is not None else discover()
    out: list[Path] = []
    # Prefer repos-root locations (have the most code) then doom/vanilla configs
    for f in found:
        if f.kind in ("repos-root",) and f.file_count > 5:
            out.append(f.path)
    for f in found:
        if f.kind in ("doom-config", "vanilla-emacs") and f.file_count > 0:
            out.append(f.path)
    for f in found:
        if f.kind == "dotfiles" and f.file_count > 0 and f.path not in out:
            out.append(f.path)
    return out


def suggest_grant_roots(found: list[FoundPath] | None = None) -> list[Path]:
    """Pick the safest/most-useful candidate auto-grant roots for MCP file access."""
    found = found if found is not None else discover()
    out: list[Path] = []
    for f in found:
        if f.kind in ("vault", "repos-root", "doom-config", "vanilla-emacs",
                       "dotfiles"):
            out.append(f.path)
    return out


def detect_preferred_language(repos_roots: list[Path] | None = None) -> str:
    """Skim ~/repos for the most-frequent code language."""
    roots = repos_roots if repos_roots is not None else [
        p for p in (Path("~/repos").expanduser(),) if p.exists()
    ]
    langs = Counter()
    for r in roots:
        try:
            _, top = _count_code_files(r, cap=3000)
            if top:
                langs[top] += 1
        except Exception:
            continue
    if not langs:
        return ""
    return langs.most_common(1)[0][0]
