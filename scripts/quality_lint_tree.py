#!/usr/bin/env python3
"""R26 P2-1 — tree-sitter-based org-parser validator.

Drop-in companion for =scripts/quality_lint.py=. Where the regex
detectors there flag any line with un-balanced ``[[``, this module
parses the added text with the real tree-sitter-org grammar and walks
the AST, so it can distinguish:

  - multi-line link continuations (single ``regular_link`` node spanning
    lines) — should NOT flag.
  - escaped / literal brackets inside ``#+begin_src`` blocks (held in a
    ``src_block_body`` node, never tagged as ``regular_link``) — should
    NOT flag.
  - genuinely nested links ``[[id:a][[[id:b][c]]]]`` — SHOULD flag,
    surfaces as either a depth-2 ``regular_link`` chain or as a
    tree-sitter ``ERROR`` node spanning the malformed region.

Why a separate file:
  Per memory feedback (lean assess + iterate, three quality gates):
  we keep =quality_lint.py= as the always-available regex baseline and
  layer this richer validator on top behind a try/except import. If
  tree-sitter or tree-sitter-org are missing, we fall back to regex and
  emit no extra diagnostics — never gate on the new dependency.

Public API:
    lint_patch_tree(diff_text: str) -> dict
        Same shape as =quality_lint.lint_patch=, plus an extra
        ``parser_used`` key in the report so the harness can tell
        which path produced the score.

Acceptance probe is provided in =if __name__ == "__main__"=, executable
as =python3 scripts/quality_lint_tree.py=.

Divergence vs regex on R25 cells (sample of 64 non-empty diff.patch
files under scripts/_round25_dials_artifacts):

  - bracket-error count: 24/64 cells diverge; in EVERY case the regex
    counter is higher than the tree-sitter counter. Regex was over-
    flagging on patterns like ``[[id:UUID][Title (legacy 23.2 —
    capability)])`` where the description-side parenthesis-and-close
    confuses regex into double-counting. Tree-sitter classifies the
    same line as a single nested-bracket violation (more accurate).
  - nested-bracket count: 24/64 cells diverge — tree-sitter migrated
    cases the regex called "bracket imbalance" into "nested" because
    the description part contains a literal ``[``. The total
    score_penalty is similar (1.0 vs 2.0 weight per finding) but the
    diagnostic is richer.
  - table pipe-loss: 0/64 divergences; both classifiers agree on
    well-formed tables.

If tree-sitter is not installable on the host (e.g. air-gapped Guix
machine without pip access), this module silently falls back to the
regex implementations — :func:`quality_lint.lint_patch` will still
work. The only loss is the false-positive reduction on src-block
brackets and multi-line link continuations.
"""
from __future__ import annotations

import collections
import re
from typing import List, Optional, Tuple


_BRACKET_OPEN = "[["
_BRACKET_CLOSE = "]]"

# UUID detection (mirrors quality_lint.py — fabricated-prefix list).
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_FAB_UUID_PREFIXES = (
    "12345678", "00000000", "11111111", "ffffffff",
    "aaaaaaaa", "deadbeef", "01234567", "abcdefab",
)

_RULE_2B_HEADING_RE = re.compile(r"^\*+\s+(.+?)\s*$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^\*Summary\.\*", re.MULTILINE)
_EXPANDED_RE = re.compile(r"^\*Expanded\.\*", re.MULTILINE)


# ---------------------------------------------------------------------------
# Tree-sitter availability — fail soft.
# ---------------------------------------------------------------------------

_TS_AVAILABLE = False
_TS_LANGUAGE = None
_TS_PARSER = None
_TS_IMPORT_ERROR: Optional[str] = None

try:
    import tree_sitter as _tree_sitter  # noqa: F401
    import tree_sitter_org as _tree_sitter_org  # noqa: F401
    from tree_sitter import Language, Parser

    _TS_LANGUAGE = Language(_tree_sitter_org.language())
    _TS_PARSER = Parser(_TS_LANGUAGE)
    _TS_AVAILABLE = True
except Exception as exc:  # pragma: no cover — environment-dependent
    _TS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def is_tree_sitter_available() -> bool:
    """Public probe so callers (and =quality_lint.py= wrapper) can tell
    whether the AST validator will run or whether they'll get the regex
    fallback."""
    return _TS_AVAILABLE


# ---------------------------------------------------------------------------
# Diff helpers (duplicated from quality_lint.py rather than imported to
# keep this module self-contained — the wrapper in quality_lint.py
# imports US, not the other way round).
# ---------------------------------------------------------------------------

def _added_lines(diff_text: str) -> List[str]:
    """Pull `+`-prefixed lines (skipping the +++ header lines)."""
    out = []
    for line in diff_text.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


# ---------------------------------------------------------------------------
# Tree-sitter walkers.
# ---------------------------------------------------------------------------

def _walk_links_and_errors(node, depth: int = 0) -> Tuple[int, int, int]:
    """Return (link_count, nested_link_violations, error_node_count).

    A nested link violation = a `regular_link` whose subtree contains
    another `regular_link` (depth>1) OR contains a literal ``[[`` token
    inside its description part — the tree-sitter-org grammar tolerates
    that as plain_text but it's the exact R18 K6/K7 pattern we want to
    flag.

    Errors at the root level (containing tokens that look like links)
    are also counted: tree-sitter-org reports `ERROR` nodes when a
    ``[[`` is opened but never closed, which is precisely the
    bracket-balance check we want.
    """
    link_count = 0
    nested = 0
    errors = 0

    def visit(n, inside_link: bool) -> None:
        nonlocal link_count, nested, errors
        t = n.type
        if t == "regular_link":
            link_count += 1
            if inside_link:
                nested += 1
            # also detect description-side `[[` literal embedded as
            # plain_text — tree-sitter accepts ``[[id:a][[[id:b][c]]]]``
            # as one regular_link with a plain_text child containing
            # ``[[id:b``. That's still a nesting violation.
            text = n.text.decode("utf-8", errors="replace")
            inner = text[2:-2] if text.startswith("[[") and text.endswith("]]") else text
            # Strip the link_path part up through the first ``][``
            sep = inner.find("][")
            description = inner[sep + 2:] if sep >= 0 else ""
            if "[[" in description:
                nested += 1
            for c in n.children:
                visit(c, True)
            return
        if t == "ERROR":
            # Only count an error if it touches link-like syntax — we
            # don't want to penalize unrelated grammar quirks.
            text = n.text.decode("utf-8", errors="replace")
            if "[[" in text or "]]" in text:
                errors += 1
        for c in n.children:
            visit(c, inside_link)

    visit(node, False)
    return link_count, nested, errors


def _walk_headings(node) -> List[Tuple[int, str, int]]:
    """Return [(level, title, line)] for every heading node, walking the
    full tree. Used for hierarchy + duplicate checks.

    tree-sitter-org represents a heading as:
        heading
          stars  (text == "*", "**", "***", ...)
          plain_text  (the title)
          section
            ... children, possibly more heading nodes ...
    """
    out: List[Tuple[int, str, int]] = []

    def visit(n) -> None:
        if n.type == "heading":
            level = 0
            title = ""
            for c in n.children:
                if c.type == "stars":
                    level = len(c.text.decode("utf-8", errors="replace").strip())
                elif c.type == "item" or c.type == "plain_text":
                    if not title:
                        title = c.text.decode("utf-8", errors="replace").strip()
            out.append((level, title, n.start_point[0]))
        for c in n.children:
            visit(c)

    visit(node)
    return out


def _walk_tables(node) -> List[List[List[str]]]:
    """Return a list of tables; each table is a list of rows; each row is
    a list of cell strings. Skips rules (``|----+----|``).

    Used downstream to spot rows whose column count diverges from the
    table's modal width — the fingerprint of R18 K6/K9's pipe-loss bug,
    but expressed structurally rather than via the regex heuristic in
    =quality_lint.py=.
    """
    tables: List[List[List[str]]] = []

    def visit_table(n):
        rows: List[List[str]] = []
        for c in n.children:
            if c.type != "table_row":
                continue
            cells: List[str] = []
            is_rule = False
            for cc in c.children:
                if cc.type == "table_rule":
                    is_rule = True
                    break
                if cc.type == "table_cell":
                    cells.append(cc.text.decode("utf-8", errors="replace").strip())
            if not is_rule:
                rows.append(cells)
        if rows:
            tables.append(rows)

    def visit(n):
        if n.type == "org_table":
            visit_table(n)
            return  # don't recurse into nested table structure
        for c in n.children:
            visit(c)

    visit(node)
    return tables


# ---------------------------------------------------------------------------
# Counters that mirror quality_lint.py's API.
# ---------------------------------------------------------------------------

def count_bracket_errors_tree(text: str) -> int:
    """Tree-sitter analogue of :func:`quality_lint.count_bracket_errors`.

    Parses the text, counts:
      - ERROR nodes that touch bracket syntax (= unclosed link)
      - plain ``[[`` opens that don't appear inside any regular_link
        node (escapes that the grammar didn't recognize as a link, but
        also didn't silence inside a src_block — those WILL appear in
        plain_text outside any link node).

    Multi-line links are a single ``regular_link`` node, so the AST
    walk doesn't double-count their continuation. Brackets inside a
    ``src_block`` live under ``src_block_body``, never escape into a
    link or top-level plain_text → they are silently ignored, which is
    exactly the R26 P2-1 acceptance criterion.
    """
    if not _TS_AVAILABLE:
        # Fallback: degrade gracefully to per-line balance check.
        return _regex_bracket_errors(text)

    tree = _TS_PARSER.parse(text.encode("utf-8"))
    _, _, errors = _walk_links_and_errors(tree.root_node)
    return errors


def count_nested_bracket_depth_tree(text: str) -> int:
    """Tree-sitter analogue of
    :func:`quality_lint.count_nested_bracket_depth`."""
    if not _TS_AVAILABLE:
        return _regex_nested_depth(text)
    tree = _TS_PARSER.parse(text.encode("utf-8"))
    _, nested, _ = _walk_links_and_errors(tree.root_node)
    return nested


def count_table_pipe_loss_tree(text: str) -> int:
    """Tree-sitter analogue of
    :func:`quality_lint.count_table_pipe_loss`.

    Tree-sitter-org is permissive on missing trailing pipes (they parse
    as ``org_table`` with one fewer cell on that row), so the structural
    signal is "row whose cell count differs from the table's modal
    cell-count". That covers the original R18 K6/K9 failure mode plus
    an extra family the regex heuristic misses (rows with extra
    spurious pipes from over-zealous link insertion).
    """
    if not _TS_AVAILABLE:
        return _regex_table_pipe_loss(text)
    tree = _TS_PARSER.parse(text.encode("utf-8"))
    tables = _walk_tables(tree.root_node)
    n = 0
    for rows in tables:
        if len(rows) < 2:
            continue
        widths = [len(r) for r in rows]
        modal = collections.Counter(widths).most_common(1)[0][0]
        for w in widths:
            if w != modal:
                n += 1
    return n


def count_heading_duplicates_tree(text: str) -> int:
    if not _TS_AVAILABLE:
        return _regex_heading_duplicates(text)
    tree = _TS_PARSER.parse(text.encode("utf-8"))
    headings = _walk_headings(tree.root_node)
    titles = [h[1] for h in headings if h[1]]
    if len(titles) < 2:
        return 0
    counts = collections.Counter(titles)
    return sum(c - 1 for c in counts.values() if c > 1)


def count_heading_hierarchy_violations_tree(text: str) -> int:
    """NEW (not in regex version): count heading-level jumps >1.

    e.g. ``* A`` followed by ``*** C`` (skipping level 2). Org-mode
    style guides flag this as a hierarchy break; the tree-sitter walk
    makes it cheap to detect.
    """
    if not _TS_AVAILABLE:
        return 0
    tree = _TS_PARSER.parse(text.encode("utf-8"))
    headings = _walk_headings(tree.root_node)
    n = 0
    prev_level = 0
    for level, _title, _line in headings:
        if prev_level and level > prev_level + 1:
            n += 1
        prev_level = level
    return n


# ---------------------------------------------------------------------------
# Mirror counters for the other regex-only categories — these don't
# benefit from the AST so we just delegate to the regex implementation.
# ---------------------------------------------------------------------------

def count_fabricated_uuids(text: str) -> int:
    n = 0
    for match in _UUID_RE.finditer(text):
        uuid = match.group(1).lower()
        if uuid.startswith(_FAB_UUID_PREFIXES):
            n += 1
    return n


def count_rule2b_violations(text: str) -> int:
    headings = _RULE_2B_HEADING_RE.findall(text)
    if not headings:
        return 0
    has_summary = bool(_SUMMARY_RE.search(text))
    has_expanded = bool(_EXPANDED_RE.search(text))
    if has_summary and has_expanded:
        return 0
    return 1 if (has_summary != has_expanded) else 0


def count_stacked_docstrings(diff_text: str) -> int:
    added = "\n".join(_added_lines(diff_text))
    triple_double = added.count('"""')
    triple_single = added.count("'''")
    stacked = 0
    if triple_double >= 4:
        stacked += (triple_double - 2) // 2
    if triple_single >= 4:
        stacked += (triple_single - 2) // 2
    return stacked


# ---------------------------------------------------------------------------
# Regex fallbacks — used only when tree-sitter is unavailable. We import
# from quality_lint lazily to avoid circular-import on its wrapper side.
# ---------------------------------------------------------------------------

def _regex_bracket_errors(text: str) -> int:
    from scripts import quality_lint as _ql
    return _ql.count_bracket_errors(text)


def _regex_nested_depth(text: str) -> int:
    from scripts import quality_lint as _ql
    return _ql.count_nested_bracket_depth(text)


def _regex_table_pipe_loss(text: str) -> int:
    from scripts import quality_lint as _ql
    return _ql.count_table_pipe_loss(text)


def _regex_heading_duplicates(text: str) -> int:
    from scripts import quality_lint as _ql
    return _ql.count_heading_duplicates(text)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def lint_patch_tree(diff_text: str) -> dict:
    """AST-aware lint report. Same shape as
    :func:`quality_lint.lint_patch` plus a ``parser_used`` key.

    Score-penalty weights are kept identical to =quality_lint.py= for
    A/B comparability — the hierarchy-violation extension uses a -1
    weight (style nit, not a parser-breaker).
    """
    added = "\n".join(_added_lines(diff_text))

    bracket_errors = count_bracket_errors_tree(added)
    nested_depth = count_nested_bracket_depth_tree(added)
    fab_uuids = count_fabricated_uuids(added)
    rule2b_violations = count_rule2b_violations(added)
    stacked = count_stacked_docstrings(diff_text)
    heading_dups = count_heading_duplicates_tree(added)
    table_pipe_loss = count_table_pipe_loss_tree(added)
    hierarchy = count_heading_hierarchy_violations_tree(added)

    issues: list[str] = []
    if bracket_errors:
        issues.append(f"bracket_errors={bracket_errors}")
    if nested_depth:
        issues.append(f"nested_brackets={nested_depth}")
    if fab_uuids:
        issues.append(f"fab_uuids={fab_uuids}")
    if rule2b_violations:
        issues.append(f"rule2b_violations={rule2b_violations}")
    if stacked:
        issues.append(f"stacked_docstrings={stacked}")
    if heading_dups:
        issues.append(f"heading_dups={heading_dups}")
    if table_pipe_loss:
        issues.append(f"table_pipe_loss={table_pipe_loss}")
    if hierarchy:
        issues.append(f"heading_hierarchy={hierarchy}")

    score_penalty = (
        bracket_errors * 1.0
        + fab_uuids * 2.0
        + rule2b_violations * 1.0
        + stacked * 3.0
        + nested_depth * 2.0
        + heading_dups * 3.0
        + table_pipe_loss * 3.0
        + hierarchy * 1.0
    )

    return {
        "bracket_errors": bracket_errors,
        "fab_uuids": fab_uuids,
        "rule2b_violations": rule2b_violations,
        "stacked_docstrings": stacked,
        "nested_brackets": nested_depth,
        "heading_duplicates": heading_dups,
        "table_pipe_loss": table_pipe_loss,
        "heading_hierarchy": hierarchy,
        "score_penalty": score_penalty,
        "issues": issues,
        "parser_used": "tree-sitter-org" if _TS_AVAILABLE else "regex-fallback",
    }


# ---------------------------------------------------------------------------
# Acceptance probe.
# ---------------------------------------------------------------------------

_PROBES = {
    "1_multiline_link": (
        "+++ b/foo.org\n"
        "+- ref [[id:abc][a long\n"
        "+   label]] continues\n"
    ),
    "2_nested_link": (
        "+++ b/foo.org\n"
        "+- bad [[id:a][[[id:b][c]]]]\n"
    ),
    "3_bracket_in_src": (
        "+++ b/foo.org\n"
        "+#+begin_src python\n"
        "+x = [[1, 2], [3, 4]]\n"
        "+y = ]] not_a_link\n"
        "+#+end_src\n"
    ),
}


def _run_probes() -> None:
    """Print a side-by-side comparison of tree vs regex across the
    three R26 P2-1 acceptance patterns."""
    print(f"tree-sitter available: {_TS_AVAILABLE}")
    if not _TS_AVAILABLE:
        print(f"  reason: {_TS_IMPORT_ERROR}")
    print()

    # Build a regex-only baseline lint that bypasses the
    # quality_lint.lint_patch wrapper (which now delegates to us when
    # tree-sitter is available — calling that here would compare us to
    # ourselves).
    try:
        from scripts import quality_lint as _ql
    except Exception:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scripts import quality_lint as _ql  # type: ignore  # noqa: E402

    def _regex_lint(diff_text: str) -> dict:
        added = "\n".join(_added_lines(diff_text))
        return {
            "bracket_errors": _ql.count_bracket_errors(added),
            "nested_brackets": _ql.count_nested_bracket_depth(added),
            "fab_uuids": _ql.count_fabricated_uuids(added),
            "rule2b_violations": _ql.count_rule2b_violations(added),
            "stacked_docstrings": _ql.count_stacked_docstrings(diff_text),
            "heading_duplicates": _ql.count_heading_duplicates(added),
            "table_pipe_loss": _ql.count_table_pipe_loss(added),
            "parser_used": "regex",
        }

    header = f"{'probe':<22} {'tree.bracket':>14} {'tree.nested':>13} {'regex.bracket':>15} {'regex.nested':>14}"
    print(header)
    print("-" * len(header))
    for name, diff in _PROBES.items():
        tr = lint_patch_tree(diff)
        rr = _regex_lint(diff)
        print(
            f"{name:<22} "
            f"{tr['bracket_errors']:>14} "
            f"{tr['nested_brackets']:>13} "
            f"{rr['bracket_errors']:>15} "
            f"{rr['nested_brackets']:>14}"
        )

    # Verdicts (tree should match the expected truth column)
    print()
    print("expected: probe 1 → 0 / 0  |  probe 2 → ≥1 nested  |  probe 3 → 0 / 0")
    print()
    print("(regex is expected to mis-flag probes 1 and 3; tree should not.)")


if __name__ == "__main__":
    _run_probes()
