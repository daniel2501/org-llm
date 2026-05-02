"""Sweep docs/wiki/*.org and convert verbatim file-path refs into
org-mode `[[file:...][=...=]]` links.

Only touches refs with a prefix we recognise (org_llm/, extensions/,
scripts/, tests/, docs/) — bare filenames like `=cli.py=` are left
alone because the right path is ambiguous.

Usage::

    uv run python scripts/link_wiki_files.py

Idempotent: re-running on already-linked files is a no-op (existing
[[file:...][=...=]] links are masked before the regex runs).

Convention is documented in =docs/wiki/wiki-conventions.org= § Rule
2a; the runtime helper lives at =org_llm.logbook.org_file_link= for
generators emitting new org content programmatically.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "docs" / "wiki"

# Match =<path>= where path starts with one of our top-level dirs
# and ends with a code/doc extension. Allow optional :<line> suffix.
PREFIX = r"(?:org_llm|extensions|scripts|tests|docs)"
EXT = r"(?:py|tsx|ts|el|md|json|sql)"
PATH_RE = re.compile(
    r"=(" + PREFIX + r"/[a-zA-Z0-9_/\-\.]+\." + EXT + r")(:[0-9]+(?:,[0-9]+)*)?="
)

LINK_RE = re.compile(r"\[\[file:[^\]]+\]\[=[^=]+=\]\]")

# Skip anything inside org example/src blocks — those are showing the
# convention itself (a verbatim BAD example) and must not be rewritten.
EXAMPLE_BLOCK_RE = re.compile(
    r"#\+begin_(example|src)[^\n]*\n.*?\n#\+end_\1",
    re.DOTALL | re.IGNORECASE,
)


def transform(text: str) -> tuple[str, int]:
    """Replace each verbatim path ref with a link, preserving optional
    line-suffix in the label."""
    # First mask out any existing [[file:...][=...=]] links so we
    # don't re-link the inner verbatim copy. Also mask out
    # #+begin_example / #+begin_src blocks — verbatim refs inside
    # them are documenting the convention, not violating it.
    masks: list[str] = []

    def _mask(m: re.Match) -> str:
        masks.append(m.group(0))
        return f"\x00MASK{len(masks)-1}\x00"

    masked = EXAMPLE_BLOCK_RE.sub(_mask, text)
    masked = LINK_RE.sub(_mask, masked)

    n = 0

    def _link(m: re.Match) -> str:
        nonlocal n
        path = m.group(1)
        line = m.group(2) or ""
        # Only link if the file actually exists in the repo. Skip
        # otherwise to avoid broken links from stale references.
        if not (ROOT / path).exists():
            return m.group(0)
        n += 1
        label = f"={path}{line}="
        return f"[[file:../../{path}][{label}]]"

    out = PATH_RE.sub(_link, masked)

    # Restore masks.
    def _unmask(m: re.Match) -> str:
        return masks[int(m.group(1))]

    out = re.sub(r"\x00MASK([0-9]+)\x00", _unmask, out)
    return out, n


def main() -> None:
    total = 0
    for org in sorted(WIKI.glob("*.org")):
        text = org.read_text()
        new, n = transform(text)
        if n:
            org.write_text(new)
            print(f"  {org.name}: linked {n} ref(s)")
            total += n
    print(f"Total: {total} link(s) added across {WIKI}")


if __name__ == "__main__":
    main()
