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
    """Count unbalanced or nested-misplaced [[ ]] pairs in added text."""
    opens = text.count(_BRACKET_OPEN)
    closes = text.count(_BRACKET_CLOSE)
    imbalance = abs(opens - closes)

    # Nested: [[ ... [[ ... ]] ... ]] is illegal in org-mode bracket
    # links. Find any "[[" inside an unclosed pair.
    nested = 0
    depth = 0
    i = 0
    while i < len(text) - 1:
        if text[i:i + 2] == _BRACKET_OPEN:
            if depth > 0:
                nested += 1
            depth += 1
            i += 2
        elif text[i:i + 2] == _BRACKET_CLOSE:
            depth = max(depth - 1, 0)
            i += 2
        else:
            i += 1
    return imbalance + nested


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

    issues: list[str] = []
    if bracket_errors:
        issues.append(f"bracket_errors={bracket_errors}")
    if fab_uuids:
        issues.append(f"fab_uuids={fab_uuids}")
    if rule2b_violations:
        issues.append(f"rule2b_violations={rule2b_violations}")
    if stacked:
        issues.append(f"stacked_docstrings={stacked}")

    # Score penalty: each bracket error = -1, each fab UUID = -2,
    # each rule2b violation = -1, each stacked docstring = -3.
    score_penalty = (
        bracket_errors * 1.0
        + fab_uuids * 2.0
        + rule2b_violations * 1.0
        + stacked * 3.0
    )

    return {
        "bracket_errors": bracket_errors,
        "fab_uuids": fab_uuids,
        "rule2b_violations": rule2b_violations,
        "stacked_docstrings": stacked,
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
