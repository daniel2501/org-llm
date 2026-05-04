"""Internal helpers for reading authoritative source files.

Primers must read from the repo at call time (always-fresh rule
in ``docs/wiki/lazy-loading.org``). Centralizing the repo-root
resolution + section extraction keeps every primer module a few
lines long.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def read(rel_path: str) -> str:
    """Read a file at a path relative to the org-llm repo root.

    Raises ``FileNotFoundError`` if the path is missing — callers
    should let it propagate rather than catching, so primer drift
    (e.g. a renamed source file) surfaces loudly in tests.
    """
    return (_REPO_ROOT / rel_path).read_text(encoding="utf-8")


def extract_section(text: str, heading: str) -> str:
    """Return the body of a top-level org-mode section.

    ``heading`` is the title text after the leading stars
    (e.g. ``"What this is"``). Match is exact and case-sensitive.
    The returned slice runs from the heading line up to the next
    same-or-higher-level heading. Subheadings are kept.

    Raises ``LookupError`` if the heading is not found, so primer
    output never silently degrades to an empty string.
    """
    lines = text.splitlines(keepends=True)
    start = None
    start_level = None
    for i, line in enumerate(lines):
        if line.startswith("*") and " " in line:
            stars, _, title = line.partition(" ")
            if set(stars) == {"*"} and title.rstrip("\n").strip() == heading:
                start = i
                start_level = len(stars)
                break
    if start is None:
        raise LookupError(f"section not found: {heading!r}")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.startswith("*") and " " in line:
            stars, _, _ = line.partition(" ")
            if set(stars) == {"*"} and len(stars) <= start_level:
                end = j
                break
    return "".join(lines[start:end]).rstrip() + "\n"
