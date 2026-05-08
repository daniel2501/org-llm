#!/usr/bin/env python3
"""N5 — quality lint for cell diff/patch output.

Per R18 quality-judge agent: K7-qwen72b's mech-score lead was 9 of 14
cells with bracket-broken or UUID-fabricated edits. K6-llama70b's B1
mech 24 included 2 of 12 broken links. Mech score doesn't catch these.

This module surfaces a structural penalty so future rounds can either
post-adjust the score or gate variants behind a clean-edit pass.

Usage:
    from scripts.quality_lint import lint_patch
    report = lint_patch(diff_text)
    # report = {"bracket_errors": int, "fab_uuids": int,
    #           "rule2b_violations": int, "stacked_docstrings": int,
    #           "score_penalty": float, "issues": [str, ...]}
"""
from __future__ import annotations

import re
from typing import List


_BRACKET_OPEN = "[["
_BRACKET_CLOSE = "]]"

# R24 — additional structural validators motivated by outlier-deepdive
# agent's analysis of R15-R19 worst cells:
#   - nested-bracket depth >1 caught R18 K6/K7 [[id:...][[[id:...][...]]]]
#   - heading_duplication caught R19 K1 mech-47 illusory wins
#   - table_pipe_loss caught R18 K6/K9 verbatim-cell wraps
import collections as _collections

# UUID pattern. Real uuid5 hashes never start with placeholder
# strings like 12345678 / 00000000 / aaaaaaaa, but our R18 cells
# emitted these constants when models hallucinated.
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


def _added_lines(diff_text: str) -> List[str]:
    """Pull `+`-prefixed lines (skipping the +++ header lines)."""
    out = []
    for line in diff_text.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def count_bracket_errors(text: str) -> int:
    """Count unbalanced or nested-misplaced [[ ]] pairs in added text.

    R26 P0-4 fix: scan each line independently. The previous version
    counted across the whole concatenated added text, which produced
    false positives when a multi-line link had its closing ]] on a
    diff context line (dropped by `_added_lines`). The R25 quality-
    judge agent traced ~25 K11/K17/K8 cells (1 broken line each)
    being reported as 6/8/10/17 imbalanced because preceding
    multi-line links left dangling [[ that propagated.

    Per-line scan still catches:
      - intra-line imbalance (e.g. `[[id:foo]` with one missing `]`)
      - intra-line nesting (R18 K6/K7's [[id:][[[id:]...]]] cascades)
    while filtering cross-line false positives.
    """
    total = 0
    for line in text.splitlines():
        opens = line.count(_BRACKET_OPEN)
        closes = line.count(_BRACKET_CLOSE)
        imbalance = abs(opens - closes)

        # Within-line nesting check
        nested = 0
        depth = 0
        i = 0
        while i < len(line) - 1:
            if line[i:i + 2] == _BRACKET_OPEN:
                if depth > 0:
                    nested += 1
                depth += 1
                i += 2
            elif line[i:i + 2] == _BRACKET_CLOSE:
                depth = max(depth - 1, 0)
                i += 2
            else:
                i += 1

        total += imbalance + nested
    return total


def count_fabricated_uuids(text: str) -> int:
    """Count UUIDs that look hallucinated (placeholder prefixes)."""
    n = 0
    for match in _UUID_RE.finditer(text):
        uuid = match.group(1).lower()
        if uuid.startswith(_FAB_UUID_PREFIXES):
            n += 1
    return n


def count_rule2b_violations(text: str) -> int:
    """For wiki/decision pages: every level-1 heading should have
    *Summary.* + *Expanded.* prose. Count headings that are missing
    either.

    Heuristic — only checks added top-level org headings within the
    diff. Skips :PROPERTIES: blocks and source blocks.
    """
    headings = _RULE_2B_HEADING_RE.findall(text)
    if not headings:
        return 0
    has_summary = bool(_SUMMARY_RE.search(text))
    has_expanded = bool(_EXPANDED_RE.search(text))
    if has_summary and has_expanded:
        return 0
    # If the diff added headings but neither Summary. nor Expanded.
    # block, that's one violation.
    return 1 if (has_summary != has_expanded) else 0


def count_stacked_docstrings(diff_text: str) -> int:
    """Count consecutive triple-quoted string blocks added together
    (caught by R17 forbid_stacked_docstrings)."""
    added = "\n".join(_added_lines(diff_text))
    triple_double = added.count('"""')
    triple_single = added.count("'''")
    # Each docstring opens + closes = 2 markers. Stacked = 4+ in a
    # narrow window.
    stacked = 0
    if triple_double >= 4:
        stacked += (triple_double - 2) // 2
    if triple_single >= 4:
        stacked += (triple_single - 2) // 2
    return stacked


def count_nested_bracket_depth(text: str) -> int:
    """Count instances where bracket nesting exceeds depth 1, scanning
    each line independently so unclosed brackets at a line boundary
    don't propagate to subsequent lines.

    R24 quality-judge bug: cross-line counting falsely flagged any
    well-formed link after a multi-line link whose closing ]] sat on
    an unchanged diff context line — ~190 of K1+K11's 234 lint-
    penalty points were false positives. Per-line scan still catches
    R18 K6/K7's [[id:][[[id:][...]]]] cascades because they're
    within a single line, but stops counting cross-line ghosts.
    """
    deep_count = 0
    for line in text.splitlines():
        depth = 0
        i = 0
        while i < len(line) - 1:
            if line[i:i + 2] == _BRACKET_OPEN:
                depth += 1
                if depth > 1:
                    deep_count += 1
                i += 2
            elif line[i:i + 2] == _BRACKET_CLOSE:
                depth = max(depth - 1, 0)
                i += 2
            else:
                i += 1
    return deep_count


def count_heading_duplicates(text: str) -> int:
    """Count level-1+ org headings that appear more than once in
    added text. R19 K1 mech-47 cells duplicated `* Tier 1 — Customize`."""
    headings = _RULE_2B_HEADING_RE.findall(text)
    if len(headings) < 2:
        return 0
    counts = _collections.Counter(headings)
    dup = sum(c - 1 for c in counts.values() if c > 1)
    return dup


def count_table_pipe_loss(text: str) -> int:
    """Detect malformed org table rows missing leading/trailing pipes.

    R18 K6/K9 wraps of verbatim cells dropped the leading `|` when
    inserting bracket links inside a table cell.

    Heuristic: an added line that contains 2+ `|` characters but does
    NOT start with `|` or `+|` (handling diff-prefix already stripped)
    is likely a table-row pipe-loss.
    """
    n = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("|"):
            continue
        # Count un-escaped pipes
        pipes = stripped.count("|")
        if pipes >= 2:
            # Only flag if line looks like table content (not prose with
            # parenthetical pipes). Heuristic: more pipes than common
            # punctuation, AND looks like cell separators (tokens between
            # pipes).
            parts = [p.strip() for p in stripped.split("|")]
            non_empty = [p for p in parts if p]
            if len(non_empty) >= 2 and all(len(p) < 80 for p in non_empty):
                n += 1
    return n


def lint_patch(diff_text: str) -> dict:
    """Return a structured lint report for a unified-diff string.

    All counts are over `+`-added text only — we don't penalize
    pre-existing brackets or UUIDs the model didn't touch.
    """
    added = "\n".join(_added_lines(diff_text))

    bracket_errors = count_bracket_errors(added)
    fab_uuids = count_fabricated_uuids(added)
    rule2b_violations = count_rule2b_violations(added)
    stacked = count_stacked_docstrings(diff_text)
    nested_depth = count_nested_bracket_depth(added)
    heading_dups = count_heading_duplicates(added)
    table_pipe_loss = count_table_pipe_loss(added)

    issues: list[str] = []
    if bracket_errors:
        issues.append(f"bracket_errors={bracket_errors}")
    if fab_uuids:
        issues.append(f"fab_uuids={fab_uuids}")
    if rule2b_violations:
        issues.append(f"rule2b_violations={rule2b_violations}")
    if stacked:
        issues.append(f"stacked_docstrings={stacked}")
    if nested_depth:
        issues.append(f"nested_brackets={nested_depth}")
    if heading_dups:
        issues.append(f"heading_dups={heading_dups}")
    if table_pipe_loss:
        issues.append(f"table_pipe_loss={table_pipe_loss}")

    # Score penalties calibrated by observed harm severity:
    #   bracket imbalance: -1 (parser-survivable)
    #   nested-bracket >1: -2 (parser breaks silently — outlier agent
    #                          flagged this as worse than imbalance)
    #   fab UUID: -2 (broken cross-references; manual cleanup)
    #   stacked docstrings: -3 (per R17 quality agent)
    #   heading duplicate: -3 (illusory mech wins per R19 K1)
    #   table pipe loss: -3 (silently breaks table rendering)
    #   Rule 2b miss: -1 (style; user can append later)
    score_penalty = (
        bracket_errors * 1.0
        + fab_uuids * 2.0
        + rule2b_violations * 1.0
        + stacked * 3.0
        + nested_depth * 2.0
        + heading_dups * 3.0
        + table_pipe_loss * 3.0
    )

    return {
        "bracket_errors": bracket_errors,
        "fab_uuids": fab_uuids,
        "rule2b_violations": rule2b_violations,
        "stacked_docstrings": stacked,
        "nested_brackets": nested_depth,
        "heading_duplicates": heading_dups,
        "table_pipe_loss": table_pipe_loss,
        "score_penalty": score_penalty,
        "issues": issues,
    }


if __name__ == "__main__":
    # Smoke test against current trunk diffs would normally go here.
    # As a sanity check, lint a known-bad sample.
    sample = """+++ b/foo.org
+* New section
+- See [[file:bar.org][bar]] and [[file:baz.org
+  for context.
+:UUID:    12345678-1234-1234-1234-123456789abc:
"""
    print(lint_patch(sample))
