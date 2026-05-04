"""Repo resolver — turns repo-name mentions in a prompt into
absolute repo roots.

When the user says "in org-llm" or "@curator in org-llm,
fix …", this resolver maps the bare name to the repo root on
disk (e.g. `/home/user/repos/org-llm/`). The downstream agent
then knows where to glob, where `wiki/` actually means
`docs/wiki/`, and which repo-relative paths to interpret
correctly.

Discovery is `~/repos/<*>` with a `.git` directory. Phase 25's
DB-authoritative inversion will replace this with a registry
table; today it's a one-shot scandir cached for the process.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import ResolvedFact


# Patterns that introduce a repo mention. Order matters — first
# match wins on a given pass over the text, but we run all
# patterns to catch multiple mentions in one prompt.
#
#   "in org-llm"          → in <name>
#   "in the org-llm repo" → in the <name> repo
#   "@<agent> in <name>"  → routed-mention (also matches first)
#   "<name>:"             → label-style intro
_REPO_MENTION_RES = (
    re.compile(r"\bin\s+(?:the\s+)?([\w][\w-]+)(?:\s+repo)?\b", re.IGNORECASE),
    re.compile(r"@\w+\s+in\s+([\w][\w-]+)", re.IGNORECASE),
    re.compile(r"\b([\w][\w-]+)\s+repo\b", re.IGNORECASE),
)

# Words that look like repo names but aren't — false positives
# from the "in <word>" pattern. Cheap stoplist.
_NOT_REPO_WORDS = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "my", "your", "our", "their", "his", "her",
    "general", "particular", "fact", "place", "order",
    "addition", "summary", "context", "case", "scope",
    "progress", "production", "memory", "code", "vault",
    "doom", "emacs", "python", "fish", "bash", "wiki",
    "dailies", "captain", "captains", "log",
})


_REPOS_CACHE: dict[str, Path] | None = None


def resolve_repos(prompt: str, context: str = "") -> list[ResolvedFact]:
    text = f"{prompt}\n{context}".strip()
    if not text:
        return []
    repos = _known_repos()
    if not repos:
        return []
    out: list[ResolvedFact] = []
    seen: set[str] = set()
    for rx in _REPO_MENTION_RES:
        for m in rx.finditer(text):
            name = m.group(1).lower()
            if name in _NOT_REPO_WORDS or name in seen:
                continue
            if name in repos:
                seen.add(name)
                out.append(ResolvedFact(
                    label=f'repo "{name}"',
                    value=str(repos[name]),
                    evidence="matched ~/repos/<name> with .git",
                    source="repo",
                ))
    return out


def _known_repos() -> dict[str, Path]:
    """Build a name → root map for ~/repos/* directories that
    contain a `.git`. Cached for the process to avoid scandir
    per delegate call."""
    global _REPOS_CACHE
    if _REPOS_CACHE is not None:
        return _REPOS_CACHE
    out: dict[str, Path] = {}
    repos_root = Path.home() / "repos"
    if not repos_root.is_dir():
        _REPOS_CACHE = out
        return out
    try:
        entries = list(repos_root.iterdir())
    except OSError:
        _REPOS_CACHE = out
        return out
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if not (entry / ".git").exists():
                continue
        except OSError:
            continue
        out[entry.name.lower()] = entry.resolve()
    _REPOS_CACHE = out
    return out


def _reset_cache_for_tests() -> None:
    """Tests poke this to re-scan after creating repos in a tmp
    dir. Not called from runtime."""
    global _REPOS_CACHE
    _REPOS_CACHE = None


# Allow tests to override the home dir cleanly without touching
# the filesystem under the real ~/repos.
def _scan_with_home(home: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    repos_root = home / "repos"
    if not repos_root.is_dir():
        return out
    for entry in repos_root.iterdir():
        try:
            if entry.is_dir() and (entry / ".git").exists():
                out[entry.name.lower()] = entry.resolve()
        except OSError:
            continue
    return out
# end
