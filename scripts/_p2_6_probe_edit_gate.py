#!/usr/bin/env python3
"""R26 P2-6 STARTER acceptance probe — pre-emit edit gate.

Synthesizes 3 edits + asserts:
  1. Clean edit (`foo = 1`)            -> (True, "")
  2. Nested-bracket edit               -> (False, contains "nested_brackets")
  3. Fabricated-UUID edit              -> (False, contains "fab_uuids")

Run:
    python3 scripts/_p2_6_probe_edit_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from org_llm.edit_gate import gate_edit_file


def main() -> int:
    failures: list[str] = []

    # 1. Clean edit — should pass.
    ok, reason = gate_edit_file("foo.py", "x = 0", "foo = 1")
    if not (ok is True and reason == ""):
        failures.append(
            f"case 1 (clean): expected (True, ''); got ({ok!r}, {reason!r})"
        )
    else:
        print("case 1 PASS — clean edit accepted")

    # 2. Nested-bracket edit — should reject.
    nested_new = "[[id:abc][[[id:def][text]]]]"
    ok, reason = gate_edit_file("foo.org", "old text", nested_new)
    if not (ok is False and "nested_brackets" in reason):
        failures.append(
            f"case 2 (nested): expected (False, contains 'nested_brackets'); "
            f"got ({ok!r}, {reason!r})"
        )
    else:
        print(f"case 2 PASS — nested-bracket edit rejected ({reason})")

    # 3. Fabricated-UUID edit — should reject.
    fab_new = ":ID: 12345678-1234-1234-1234-123456789abc:"
    ok, reason = gate_edit_file("foo.org", "old text", fab_new)
    if not (ok is False and "fab_uuids" in reason):
        failures.append(
            f"case 3 (fab uuid): expected (False, contains 'fab_uuids'); "
            f"got ({ok!r}, {reason!r})"
        )
    else:
        print(f"case 3 PASS — fab-UUID edit rejected ({reason})")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nALL 3 CASES PASS — pre-emit edit gate starter is working")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
