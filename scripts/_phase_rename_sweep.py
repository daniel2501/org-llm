#!/usr/bin/env python3
"""TEMPORARY helper for the 2026-05-06 phase-rename sweep.

This file is part of Wave 4 of the rename work documented in
docs/wiki/2026-05-06-phase-rename-sweep.org. It is meant to be
deleted at the end of the sweep — left in scripts/ for the user
to inspect or rerun.

Usage:
  python3 scripts/_phase_rename_sweep.py FILE [FILE...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RAW_MAP = [
    ("Phase 12.1", "Phase 2026-04.07.01 — pre-mount skeleton"),
    ("Phase 12.2", "Phase 2026-04.07.02 — LLM narration"),
    ("Phase 12.3", "Phase 2026-04.07.03 — more generators"),
    ("Phase 12.4", "Phase 2026-04.07.04 — inject cards"),
    ("Phase 12.5", "Phase 2026-04.07.05 — engagement table"),
    ("Phase 12.6", "Phase 2026-07.05 — cached-gather persistence"),
    ("Phase 12.7", "Phase 2026-07.06 — dbt analytics"),
    ("Phase 12", "Phase 2026-04.07 — insight cards"),
    ("Phase 13.1", "Phase 2026-04.08 — walk + opencode surfacing"),
    ("Phase 13.2", "Phase 2026-07.07 — dailies as walk targets"),
    ("Phase 13.4", "Phase 2026-07.08 — per-slug routing"),
    ("Phase 13.5", "Phase 2026-07.09 — follow-up questions"),
    ("Phase 13", "Phase 2026-04.08 — walk + opencode surfacing"),
    ("Phase 15", "Phase 2026-04.09 — provider config audit"),
    ("Phase 16.0", "Phase 2026-04.10.01 — extensions scaffolding"),
    ("Phase 16.1", "Phase 2026-04.10.02 — opencode TUI plugin"),
    ("Phase 16.2", "Phase 2026-04.10.03 — launch wires plugin"),
    ("Phase 16.3", "Phase 2026-05.03 — Emacs companion"),
    ("Phase 16.4", "Phase 2026-05.19 — extension polish"),
    ("Phase 16.5", "Phase 2026-05.18 — Doom config introspection"),
    ("Phase 16", "Phase 2026-04.10 — extensions"),
    ("Phase 17.1", "Phase 2026-05.05 — LCARS panel + LLM proxy"),
    ("Phase 17", "Phase 2026-04.11 — live sidebar status panel"),
    ("Phase 18.4", "Phase 2026-05.06 — proxy expansion"),
    ("Phase 18.6", "Phase 2026-05.06.02 — cloud-first + synth tools"),
    ("Phase 18", "Phase 2026-05.06 — proxy expansion"),
    ("Phase 19", "Phase 2026-05.15 — built-in vault git-sync"),
    ("Phase 20.x", "Phase 2026-05.99 — full agent crew (PARKED)"),
    ("Phase 20", "Phase 2026-05.99 — full agent crew (PARKED)"),
    ("Phase 21.1", "Phase 2026-05.13.01 — DB-backed vault fact store"),
    ("Phase 21.2", "Phase 2026-05.13.02 — five-inferrer first cut"),
    ("Phase 21.3", "Phase 2026-05.13.03 — vault_profile aggregate"),
    ("Phase 21", "Phase 2026-05.13 — deterministic vault inferrers"),
    ("Phase 22.5a", "Phase 2026-05.11.01 — token-stream excerpt"),
    ("Phase 22.5b", "Phase 2026-05.11.02 — explicit reasoning"),
    ("Phase 22.5c", "Phase 2026-05.11.03 — chain visualisation"),
    ("Phase 22.5", "Phase 2026-05.11 — thought-process capture"),
    ("Phase 22.6.1", "Phase 2026-05.09.01 — pure-python tools"),
    ("Phase 22.6.2", "Phase 2026-05.09.02 — CLOCK + drill + attach"),
    ("Phase 22.6.3", "Phase 2026-05.09.03 — templating + Doom config"),
    ("Phase 22.6.4", "Phase 2026-05.09.04 — wire into agent prompts"),
    ("Phase 22.6", "Phase 2026-05.08 — markdown-JSON repair"),
    ("Phase 22 v2", "Phase 2026-05.07 — manager pre-fetch + agent narrates"),
    ("Phase 22", "Phase 2026-05.12 — orchestration v2"),
    ("Phase 23.1", "Phase 2026-05.14.01 — dataclass"),
    ("Phase 23.5", "Phase 2026-05.14.02 — DB-backed registry"),
    ("Phase 23.6.1", "Phase 2026-05.16.01 — customize tier"),
    ("Phase 23.6.2", "Phase 2026-05.16.02 — manage tier"),
    ("Phase 23.6.3", "Phase 2026-05.16.03 — shell-wrapped user tools"),
    ("Phase 23.6.4", "Phase 2026-05.16.04 — HTTP-wrapped user tools"),
    ("Phase 23.6", "Phase 2026-05.16 — literate MCP tools"),
    ("Phase 23", "Phase 2026-05.14 — agent-framework refinements"),
    ("Phase 24.1", "Phase 2026-05.02.01 — pre-flight resolvers"),
    ("Phase 24.2", "Phase 2026-05.02.02 — recovery hooks"),
    ("Phase 24.3", "Phase 2026-05.02.03 — confusion detector"),
    ("Phase 24.4", "Phase 2026-05.02.04 — manager quorum"),
    ("Phase 24", "Phase 2026-05.02 — supervision trinity"),
    ("Phase 25.1", "Phase 2026-05.17.01 — DB-as-cache foundations"),
    ("Phase 25.2", "Phase 2026-05.17.02 — DB-as-cache step 2"),
    ("Phase 25.3", "Phase 2026-05.17.03 — DB-as-cache step 3"),
    ("Phase 25.4", "Phase 2026-05.17.04 — DB-as-cache step 4"),
    ("Phase 25.5", "Phase 2026-05.17.05 — DB-as-cache step 5"),
    ("Phase 25.6", "Phase 2026-05.17.06 — DB-as-cache step 6"),
    ("Phase 25.7", "Phase 2026-05.17.07 — DB-as-cache step 7"),
    ("Phase 25", "Phase 2026-05.17 — DB-as-cache + git-canonical pipeline"),
    ("Phase 26.1", "Phase 2026-05.04 — cull walk"),
    ("Phase 26.2", "Phase 2026-05.01 — physical extraction"),
    ("Phase 26", "Phase 2026-05.04 — cull walk"),
    ("Phase 27", "Phase 2026-07.01 — Apache Superset dashboards"),
    ("Phase 28", "Phase 2026-07.02 — containerization strategy"),
    ("Phase 29.0", "Phase 2026-05.20 — sync verb POC"),
    ("Phase 29.1", "Phase 2026-06.01 — pattern detection"),
    ("Phase 29.2", "Phase 2026-06.02 — tool synthesis with validation"),
    ("Phase 29.3", "Phase 2026-06.03 — registration + replacement"),
    ("Phase 29.x", "Phase 2026-06.01–.03 — self-coded tools agent ecosystem"),
    ("Phase 29", "Phase 2026-05.20 — sync verb POC"),
    ("Phase 30.1", "Phase 2026-06.04 — local-only flows"),
    ("Phase 30.2", "Phase 2026-06.05 — specialized small models"),
    ("Phase 30.3", "Phase 2026-06.06 — background continuous work"),
    ("Phase 30.4", "Phase 2026-05.21 — multi-judge validation"),
    ("Phase 30.5", "Phase 2026-06.07 — self-validation"),
    ("Phase 30", "Phase 2026-06.04 — FOSS agent value-adds"),
    ("Phase 31.1", "Phase 2026-07.03 — agent visual metadata"),
    ("Phase 31.2", "Phase 2026-07.04 — themes as literate config"),
    ("Phase 31.3", "Phase 2026-08.01 — image-gen backend"),
    ("Phase 31.4", "Phase 2026-08.02 — Emacs display path"),
    ("Phase 31.5", "Phase 2026-08.03 — terminal protocol path"),
    ("Phase 31.6", "Phase 2026-08.04 — conversational tuning"),
    ("Phase 31", "Phase 2026-07.03 — theme-driven agent avatars"),
    ("Phase 1–3", "Phase 2026-04.01 — initial implementation"),
    ("Phase 1-3", "Phase 2026-04.01 — initial implementation"),
    ("Phase 7", "Phase 2026-04.03 — opencode wiring audit"),
    ("Phase 5", "Phase 2026-04.02 — opencode workspace"),
    ("Phase 9.7", "Phase 2026-04.04 — =.opencode/= dotfile"),
    ("Phase 10", "Phase 2026-04.05 — surface polish + tutor"),
    ("Phase 11", "Phase 2026-04.06 — embed migration + tag --apply"),
    # Sub-phases NOT in the canonical map but live in body prose.
    # They map to the parent's new name with a residual sub-tag in
    # the slug so future wiki readers can follow the trail back to
    # legacy NN.M numbering. Pattern: parent's new name + " (legacy
    # NN.M)" so readers can search by the old number if needed.
    ("Phase 13.3", "Phase 2026-04.08 — walk + opencode surfacing (legacy 13.3 — by-project walk)"),
    ("Phase 16.x", "Phase 2026-04.10 — extensions (legacy 16.x family)"),
    ("Phase 18.7", "Phase 2026-05.06 — proxy expansion (legacy 18.7 — prefix interceptor family)"),
    ("Phase 19.1", "Phase 2026-05.15 — built-in vault git-sync (legacy 19.1 — MVP)"),
    ("Phase 19.2", "Phase 2026-05.15 — built-in vault git-sync (legacy 19.2 — auto-embedder)"),
    ("Phase 19.3", "Phase 2026-05.15 — built-in vault git-sync (legacy 19.3 — conflict UX)"),
    ("Phase 23.0", "Phase 2026-05.14 — agent-framework refinements (legacy 23.0 — rationalization)"),
    ("Phase 23.2", "Phase 2026-05.14 — agent-framework refinements (legacy 23.2 — capability enforcement)"),
    ("Phase 23.3", "Phase 2026-05.14 — agent-framework refinements (legacy 23.3 — hygiene scans)"),
    ("Phase 23.4", "Phase 2026-05.14 — agent-framework refinements (legacy 23.4 — recipes/inferrers as agent properties)"),
    ("Phase 24.5a", "Phase 2026-05.02 — supervision trinity (legacy 24.5a — orientation primer registry seed)"),
    ("Phase 24.5b", "Phase 2026-05.02 — supervision trinity (legacy 24.5b — agent-report orientation primer)"),
    ("Phase 24.5", "Phase 2026-05.02 — supervision trinity (legacy 24.5 — orientation primer registry)"),
    ("Phase 25.x", "Phase 2026-05.17 — DB-as-cache + git-canonical pipeline (legacy 25.x family)"),
    ("Phase 14+", "Phase 2026-05.13 — deterministic vault inferrers (legacy 14+ — analytics layer)"),
    ("Phase 32", "Phase 2026-08+ — out-month placeholder (legacy 32)"),
    ("Phase 23.x", "Phase 2026-05.14 — agent-framework refinements (legacy 23.x family)"),
    ("Phase 1–16", "Phase 2026-04.01 through 2026-04.10 — pre-beta foundations (legacy 1–16)"),
    ("Phase 1-16", "Phase 2026-04.01 through 2026-04.10 — pre-beta foundations (legacy 1-16)"),
]

RAW_MAP_SORTED = sorted(RAW_MAP, key=lambda p: -len(p[0]))


def is_commit_quotation(line: str) -> bool:
    if re.search(r"=\s*(feat|fix|docs|test|chore|refactor|build|ci|perf|style)[(:]", line):
        return True
    if re.match(r"^\s*[0-9a-f]{7,12}\s+\w+\(", line):
        return True
    # Table row that starts with =hash= | — a "commits" log table
    if re.match(r"^\s*\|\s*=[0-9a-f]{6,12}=\s*\|", line):
        return True
    return False


def is_legacy_preservation(line: str) -> bool:
    """Lines that intentionally preserve the legacy NN.M form should
    not be rewritten. Patterns:
      :LEGACY_NUMBER: Phase NN.M
      legacy: Phase NN.M (in a table cell)
      "(legacy NN.M ...)" parens already inserted by an earlier pass
    """
    # :LEGACY_NUMBER: property line
    if re.search(r":LEGACY_NUMBER:\s*Phase\s+\d", line):
        return True
    # "legacy: Phase NN" or "legacy NN" inside a table cell. The
    # table cells in the index look like `... | legacy: Phase 26.2 |`
    # — match colon-form. Allow embedded text.
    if re.search(r"\blegacy:\s*Phase\s+\d", line, re.IGNORECASE):
        return True
    return False


def is_in_git_log_block(lines, idx):
    for i in range(idx - 1, -1, -1):
        s = lines[i].strip().lower()
        if s.startswith("#+end_src"):
            return False
        if s.startswith("#+begin_src"):
            return "git log" in s
    return False


def process_file(path):
    text = path.read_text()
    lines = text.split("\n")
    new_lines = []
    total_subs = 0
    skipped = []
    for idx, line in enumerate(lines):
        if is_commit_quotation(line):
            new_lines.append(line)
            if re.search(r"Phase\s+\d", line):
                skipped.append((idx + 1, "commit-quote"))
            continue
        if is_in_git_log_block(lines, idx):
            new_lines.append(line)
            if re.search(r"Phase\s+\d", line):
                skipped.append((idx + 1, "git-log-block"))
            continue
        if is_legacy_preservation(line):
            new_lines.append(line)
            continue
        new_line = line
        for old, new in RAW_MAP_SORTED:
            # Right boundary: don't match `Phase 26` if followed by
            # `.<digit>` (i.e. it's actually `Phase 26.1`). But DO
            # match if followed by a bare `.` (sentence end), space,
            # comma, etc. Also don't match if followed by another
            # digit (e.g. `Phase 12` mid `Phase 120`).
            pattern = re.escape(old) + r"(?!\.\d)(?!\d)"
            new_line, n = re.subn(pattern, new, new_line)
            total_subs += n
        new_lines.append(new_line)
    if total_subs:
        path.write_text("\n".join(new_lines))
    return total_subs, skipped


def find_residuals(path):
    """Return list of (lineno, line) tuples where Phase NN (legacy) shows up."""
    if not path.exists():
        return []
    out = []
    text = path.read_text()
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if is_commit_quotation(line) or is_legacy_preservation(line) or is_in_git_log_block(lines, idx):
            continue
        # Match 'Phase ' followed by 1-2 digit (legacy), but not Phase 2026-...
        # Also catch 'Phase 22 v2', 'Phase 20.x'
        for m in re.finditer(r"\bPhase\s+(\d{1,3}(?:\.\d+)*(?:\.\w)?(?:\s+v\d+)?)", line):
            num = m.group(1)
            # Skip if it's 2026 or other already-new form
            if num.startswith("20") or num.startswith("21") and len(num) == 4:
                continue
            # Heuristic: legacy if it's a 1-2 digit head <= 31
            head = num.split(".")[0].split()[0]
            try:
                if 1 <= int(head) <= 31:
                    out.append((idx + 1, line.rstrip()))
                    break
            except ValueError:
                pass
    return out


def main(argv):
    if len(argv) < 2:
        sys.exit("usage: _phase_rename_sweep.py [--check] FILE [FILE...]")
    if argv[1] == "--check":
        for p in argv[2:]:
            path = Path(p)
            for ln, line in find_residuals(path):
                print(f"{p}:{ln}:{line[:200]}")
        return
    if argv[1] == "--count":
        # Per-file count of legacy Phase NN residuals
        # If no files given, read from stdin
        if len(argv) == 2:
            files = [line.strip() for line in sys.stdin if line.strip()]
        else:
            files = argv[2:]
        per_file = []
        for p in files:
            path = Path(p)
            try:
                n = len(find_residuals(path))
            except (UnicodeDecodeError, OSError):
                continue
            if n:
                per_file.append((n, p))
        per_file.sort(reverse=True)
        for n, p in per_file:
            print(f"{n:5d}  {p}")
        print(f"--- total: {sum(n for n, _ in per_file)} ---")
        return
    grand = 0
    all_skipped = []
    for p in argv[1:]:
        path = Path(p)
        if not path.exists():
            print(f"SKIP (missing): {p}")
            continue
        n, skipped = process_file(path)
        grand += n
        marker = " (skip in file: " + str(len(skipped)) + ")" if skipped else ""
        print(f"{n:4d}  {p}{marker}")
        for ln, why in skipped:
            all_skipped.append((p, ln, why))
    print(f"--- total: {grand} substitutions ---")
    if all_skipped:
        print("--- skipped (commit-quotes / git-log) ---")
        for f, ln, why in all_skipped:
            print(f"  {f}:{ln}  ({why})")


if __name__ == "__main__":
    main(sys.argv)
