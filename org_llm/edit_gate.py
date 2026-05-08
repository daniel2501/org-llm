"""R26 P2-6 STARTER — pre-emit grammar gate for edit_file.

Currently quality_lint.py penalizes /post-emit/: model produces broken
output, then we subtract score. The pre-emit gate REJECTS structural
defects BEFORE they land in the diff so the cell loop can force a retry.

This is the STARTER scope per the R26 launch checklist:
  - nested_brackets  > 0  (depth > 1 within a single line)
  - fab_uuids        > 0  (placeholder prefixes like 12345678-...)
  - heading_duplicates > 0 (same heading appears twice in added text)
  - table_pipe_loss  > 0  (org table rows missing leading `|`)

Reuses the existing detector functions in scripts.quality_lint so the
post-emit penalty math and the pre-emit gate stay in lockstep.

Wired into specialist._dispatch_tool_call's edit_file branch behind the
EDIT_GATE_ENABLED env var, default OFF for R26 (don't risk breaking the
round). R27 P2-6 will flip ON + extend with table validation, multiline
link continuations, and source-block-aware checks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make scripts/ importable when this module runs from inside the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.quality_lint import (  # noqa: E402
    count_fabricated_uuids,
    count_heading_duplicates,
    count_nested_bracket_depth,
    count_table_pipe_loss,
)


def gate_edit_file(path: str, old_string: str, new_string: str) -> tuple[bool, str]:
    """Reject structural defects in new_string before the edit lands.

    Returns (True, "") for a clean edit; (False, reason) when one of the
    starter detectors fires. Reason is a compact `name=count, ...` string
    suitable for handing back to the model as a retry hint.
    """
    issues: list[str] = []

    nested = count_nested_bracket_depth(new_string)
    if nested > 0:
        issues.append(f"nested_brackets={nested}")

    fab = count_fabricated_uuids(new_string)
    if fab > 0:
        issues.append(f"fab_uuids={fab}")

    dups = count_heading_duplicates(new_string)
    if dups > 0:
        issues.append(f"heading_duplicates={dups}")

    pipe_loss = count_table_pipe_loss(new_string)
    if pipe_loss > 0:
        issues.append(f"table_pipe_loss={pipe_loss}")

    if issues:
        return False, ", ".join(issues)
    return True, ""


def edit_gate_enabled() -> bool:
    """Read EDIT_GATE_ENABLED env var. Default OFF for R26."""
    val = os.environ.get("EDIT_GATE_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")
