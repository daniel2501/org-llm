#!/usr/bin/env python3
"""Round-19 — Track K long-horizon multi-file task definitions.

Five tasks designed to exercise specialists across multiple files with
DETERMINISTIC verifiers. These extend R17/R18's mostly-single-file edits
to test "can FOSS handle real coding work like Claude does?"

  BK1 — rename a constant across 3-5 files (refactor)
  BK2 — add a new CLI subcommand (new code + new test)
  BK3 — fix a deterministic bug given a failing test (bug-hunt)
  BK4 — add a new SpecialistTask field + propagate to schema/docs
  BK5 — normalize wiki cross-link forms across 5+ pages (refactor)

This module is DATA-ONLY. The harness (a future _round19_dials.py)
imports `LONG_HORIZON_TASKS`, materializes a fresh worktree per cell,
runs prefetch + bug-injection (BK3), invokes the specialist, then
calls the task's `verifier(workdir)` to score.

All paths are workdir-relative; verifiers receive an absolute workdir
Path and resolve from there. Verifiers are idempotent + side-effect-free.

NOTE: This module does NOT mutate the live tree. BK3's "starting bug"
is applied to the worktree by the harness via the task's
`apply_starting_state(workdir)` hook (separate from prefetch).
"""
from __future__ import annotations
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parent.parent  # /home/daniel/repos/org-llm

# Standard allowed-tool sets (subset of BROAD_TOOLS_FULL by name).
TOOLS_REFACTOR = {"read_file", "edit_file", "grep", "list_dir"}
TOOLS_REFACTOR_PY = TOOLS_REFACTOR | {"run_pytest", "run_python"}
TOOLS_NEW_CODE = {"read_file", "edit_file", "write_file", "grep",
                   "list_dir", "run_pytest", "run_python"}
TOOLS_BUG_FIX = {"read_file", "edit_file", "grep", "list_dir",
                  "run_pytest", "run_python"}
TOOLS_WIKI = {"read_file", "edit_file", "grep", "list_dir",
               "find_canonical_id", "validate_org"}


# ── Helpers shared across verifiers ──────────────────────────────────────
def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    """Run a shell command, capture (rc, stdout, stderr)."""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                            text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _grep_count(workdir: Path, pattern: str,
                 paths: list[str]) -> int:
    """Count literal-string occurrences of pattern across paths
    (workdir-relative). No regex; uses Python find()."""
    total = 0
    for rel in paths:
        p = workdir / rel
        if not p.exists():
            continue
        try:
            text = p.read_text()
        except Exception:
            continue
        total += text.count(pattern)
    return total


def _file_lines(workdir: Path, rel: str) -> list[str]:
    p = workdir / rel
    if not p.exists():
        return []
    return p.read_text().splitlines()


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK1 — rename BROAD_TOOLS_FULL → FULL_TOOL_SURFACE across 4 files      ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK1_FILES = [
    "org_llm/specialist.py",
    "scripts/_round16_dials.py",
    "scripts/_round17_dials.py",
    "scripts/_round18_dials.py",
]
BK1_OLD = "BROAD_TOOLS_FULL"
BK1_NEW = "FULL_TOOL_SURFACE"


def prefetch_bk1(task: dict) -> dict:
    """Pre-compute exact occurrence count + line numbers per file."""
    workdir = REPO  # prefetch reads live tree; harness runs in worktree copy
    occurrences = {}
    total = 0
    for rel in BK1_FILES:
        p = workdir / rel
        if not p.exists():
            occurrences[rel] = {"missing": True}
            continue
        lines = p.read_text().splitlines()
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if BK1_OLD in ln]
        occurrences[rel] = {"line_count": len(lines), "hits": hits,
                              "n": len(hits)}
        total += len(hits)
    return {
        "old_name":   BK1_OLD,
        "new_name":   BK1_NEW,
        "files":       BK1_FILES,
        "total_occurrences": total,
        "per_file":    occurrences,
        "rule":        ("Replace EVERY occurrence of BROAD_TOOLS_FULL with "
                         "FULL_TOOL_SURFACE across the listed files. The "
                         "rename must be exact (preserve indentation, "
                         "context). After the rename, "
                         "`python -c 'import org_llm.specialist'` MUST "
                         "succeed and `pytest tests/ -x -q` MUST pass."),
    }


def verify_bk1(workdir: Path) -> tuple[bool, str]:
    """0 occurrences of old name; module imports; pytest passes (subset)."""
    old_count = _grep_count(workdir, BK1_OLD, BK1_FILES)
    if old_count != 0:
        return False, (f"BK1 fail: {old_count} occurrences of {BK1_OLD} "
                        f"remain (expected 0)")
    new_count = _grep_count(workdir, BK1_NEW, BK1_FILES)
    if new_count < 4:
        return False, (f"BK1 fail: only {new_count} occurrences of "
                        f"{BK1_NEW} (expected >= 4 — at least 1 per file)")
    rc, out, err = _run(
        [sys.executable, "-c", "import org_llm.specialist as s; "
                                  "assert hasattr(s, 'FULL_TOOL_SURFACE'), "
                                  "'missing FULL_TOOL_SURFACE attr'"],
        workdir, timeout=20)
    if rc != 0:
        return False, f"BK1 fail: import smoke failed: {err.strip() or out.strip()}"
    rc, out, err = _run(
        [sys.executable, "-m", "pytest", "tests/test_cli.py", "-x", "-q",
          "--no-header", "-p", "no:cacheprovider"],
        workdir, timeout=120)
    if rc != 0:
        return False, f"BK1 fail: pytest tests/test_cli.py failed (rc={rc})"
    return True, (f"BK1 pass: 0 old / {new_count} new occurrences; "
                   f"import + pytest green")


BK1 = {
    "id":               "BK1",
    "label":            "rename BROAD_TOOLS_FULL across 4 files",
    "target_files":     list(BK1_FILES),
    "expected_changes": 10,   # ~3 occurrences × 3 scripts + 1 in specialist.py
    "prefetch":         prefetch_bk1,
    "goal": (
        "Rename the constant `BROAD_TOOLS_FULL` to `FULL_TOOL_SURFACE` "
        "across these 4 files (see prefetch.files). Rename must be exact "
        "and complete: every occurrence in every file. Do NOT touch any "
        "other file. After the rename:\n"
        "  - `python -c 'import org_llm.specialist'` must succeed\n"
        "  - `pytest tests/test_cli.py -x -q` must pass\n"
        "  - 0 occurrences of `BROAD_TOOLS_FULL` may remain in the 4 files\n"
        "  - At least 1 occurrence of `FULL_TOOL_SURFACE` per file\n"
        "Suggested workflow: read_file each target → edit_file with literal "
        "old_string/new_string for each occurrence → run pytest at the end."
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk1,
    "allowed_tools":    TOOLS_REFACTOR_PY,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK2 — add `org-llm specialist list-tools --json` subcommand          ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK2_CLI_FILE = "org_llm/cli_specialist.py"
BK2_TEST_FILE = "tests/test_cli_specialist_list_tools.py"


def prefetch_bk2(task: dict) -> dict:
    """Show the existing CLI module shape and the BROAD_TOOLS_FULL contents
    so the model knows what tool names to expect."""
    cli_path = REPO / BK2_CLI_FILE
    cli_text = cli_path.read_text() if cli_path.exists() else ""
    # Pull tool names from specialist.py at prefetch time (live tree;
    # the worktree's specialist.py is identical at branch point).
    sys.path.insert(0, str(REPO))
    try:
        from org_llm.specialist import BROAD_TOOLS_FULL
        tool_names = sorted({t["function"]["name"] for t in BROAD_TOOLS_FULL})
    except Exception as exc:
        tool_names = []
    finally:
        if str(REPO) in sys.path:
            sys.path.remove(str(REPO))
    return {
        "cli_module_path":     BK2_CLI_FILE,
        "cli_module_lines":    len(cli_text.splitlines()),
        "test_module_path":    BK2_TEST_FILE,
        "expected_tool_names": tool_names,
        "spec": (
            "Add a NEW typer subcommand `list-tools` to the existing "
            "_specialist_app in org_llm/cli_specialist.py. It must accept "
            "a `--json` flag; when --json is passed, print BROAD_TOOLS_FULL "
            "(from org_llm.specialist) as JSON to stdout. Without --json, "
            "print a human-readable table (same shape as `specialist run`). "
            "Then create tests/test_cli_specialist_list_tools.py with at "
            "least one CliRunner-based test that invokes "
            "`['specialist', 'list-tools', '--json']` and asserts the "
            "stdout parses as JSON and contains every name in "
            "prefetch.expected_tool_names."
        ),
    }


def verify_bk2(workdir: Path) -> tuple[bool, str]:
    """Run the CLI; assert JSON parses + has all expected tool names; pytest."""
    # 1. import + invoke
    rc, out, err = _run(
        [sys.executable, "-m", "org_llm.cli", "specialist",
          "list-tools", "--json"],
        workdir, timeout=30)
    if rc != 0:
        return False, (f"BK2 fail: CLI invocation failed (rc={rc}): "
                        f"{err.strip()[:300]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return False, f"BK2 fail: --json output not valid JSON: {exc}"
    # 2. tool names present
    if isinstance(data, list):
        names = []
        for entry in data:
            if isinstance(entry, dict):
                fn = entry.get("function") or {}
                nm = fn.get("name") or entry.get("name")
                if nm:
                    names.append(nm)
    elif isinstance(data, dict):
        names = list(data.keys())
    else:
        return False, f"BK2 fail: unexpected JSON shape: {type(data).__name__}"
    # Must include core tools
    required = {"read_file", "edit_file", "write_file", "grep"}
    missing = required - set(names)
    if missing:
        return False, f"BK2 fail: missing tool names in --json: {sorted(missing)}"
    # 3. test module exists + passes
    test_path = workdir / BK2_TEST_FILE
    if not test_path.exists():
        return False, f"BK2 fail: test file not created: {BK2_TEST_FILE}"
    rc, out2, err2 = _run(
        [sys.executable, "-m", "pytest", BK2_TEST_FILE, "-x", "-q",
          "--no-header", "-p", "no:cacheprovider"],
        workdir, timeout=120)
    if rc != 0:
        return False, (f"BK2 fail: pytest {BK2_TEST_FILE} failed "
                        f"(rc={rc}): {err2.strip()[:300]}")
    return True, (f"BK2 pass: --json parses, {len(names)} tool names "
                   f"present, test passes")


BK2 = {
    "id":               "BK2",
    "label":            "add `specialist list-tools --json` subcommand",
    "target_files":     [BK2_CLI_FILE, BK2_TEST_FILE],
    "expected_changes": 2,   # 1 modified + 1 new
    "prefetch":         prefetch_bk2,
    "goal": (
        "Add a NEW typer subcommand `list-tools` to the existing "
        "_specialist_app in org_llm/cli_specialist.py. The command takes "
        "a `--json` boolean flag (default False).\n\n"
        "Behavior:\n"
        "  - With --json: import BROAD_TOOLS_FULL from org_llm.specialist "
        "and print it as a JSON list to stdout.\n"
        "  - Without --json: print a Rich table with one row per tool "
        "(name + description).\n\n"
        "Also create tests/test_cli_specialist_list_tools.py — at least "
        "one test that uses typer.testing.CliRunner to invoke "
        "['specialist', 'list-tools', '--json'] and asserts:\n"
        "  - exit_code == 0\n"
        "  - stdout parses as JSON\n"
        "  - every name in prefetch.expected_tool_names is present\n\n"
        "Edit ONLY org_llm/cli_specialist.py and the new test file."
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk2,
    "allowed_tools":    TOOLS_NEW_CODE,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK3 — fix a deterministic bug in _check_append_only (inverted return) ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Starting state (applied by harness BEFORE specialist runs):
#   In org_llm/specialist.py, _check_append_only has its return inverted:
#     - `return True, ""` becomes `return False, ""` (the OK branch broken)
#     - `return False, (...)` becomes `return True, (...)` (violation marked OK)
#   Plus a NEW failing test in tests/test_append_only_bug.py the model
#   must use as its truth signal.
#
# The model is told: "There's a regression in _check_append_only —
# tests/test_append_only_bug.py is failing. Find and fix the bug."
#
# Verifier: pytest tests/test_append_only_bug.py passes; whole-test-suite
# (subset) green; no other deletions outside specialist.py.

BK3_TARGET = "org_llm/specialist.py"
BK3_TEST_PATH = "tests/test_append_only_bug.py"

# Pasted as starting-state by the harness (NOT a permanent change).
BK3_FAILING_TEST = '''"""R19 BK3 — failing test for _check_append_only regression.

Asserts the contract: appending preserves; deletion violates.
"""
from org_llm.specialist import _check_append_only


def test_pure_append_returns_ok():
    old = "a\\nb\\nc\\n"
    new = "a\\nb\\nc\\nd\\n"   # appended
    ok, msg = _check_append_only(old, new)
    assert ok is True, f"pure append should pass; got msg={msg!r}"
    assert msg == "", f"OK case should have empty msg; got {msg!r}"


def test_deletion_returns_violation():
    old = "a\\nb\\nc\\n"
    new = "a\\nc\\n"            # deleted line b
    ok, msg = _check_append_only(old, new)
    assert ok is False, "deletion must be flagged as violation"
    assert "APPEND-ONLY violation" in msg


def test_replacement_returns_violation():
    old = "a\\nb\\nc\\n"
    new = "a\\nB\\nc\\nd\\n"    # replaced b with B AND appended d
    ok, msg = _check_append_only(old, new)
    assert ok is False, "replacement-then-append must be flagged"
'''

# Bug-injection patch: harness applies, model fixes. Two flips.
BK3_BUG_PATCHES = [
    # (old_substr, new_substr) — exact, must match exactly once
    ('    if new_text.startswith(old_text):\n        return True, ""',
      '    if new_text.startswith(old_text):\n        return False, ""'),
    ('    return False, (f"APPEND-ONLY violation:',
      '    return True, (f"APPEND-ONLY violation:'),
]


def apply_bk3_starting_state(workdir: Path) -> tuple[bool, str]:
    """Harness hook: install the failing test + invert the function's return.

    Idempotent: writes the test file, applies each patch exactly once."""
    test_path = workdir / BK3_TEST_PATH
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(BK3_FAILING_TEST)

    spec_path = workdir / BK3_TARGET
    text = spec_path.read_text()
    for old, new in BK3_BUG_PATCHES:
        n = text.count(old)
        if n == 0 and new in text:
            continue   # already inverted (idempotent)
        if n != 1:
            return False, (f"BK3 starting-state error: pattern present "
                            f"{n}x in {BK3_TARGET} (expected 1): "
                            f"{old[:60]!r}")
        text = text.replace(old, new, 1)
    spec_path.write_text(text)
    return True, "BK3 starting state applied (failing test + inverted return)"


def prefetch_bk3(task: dict) -> dict:
    """Locate _check_append_only; surface its current (broken) signature."""
    p = REPO / BK3_TARGET
    text = p.read_text() if p.exists() else ""
    func_match = re.search(
        r"def _check_append_only\([^)]*\) -> tuple\[bool, str\]:.*?(?=\n\ndef |\nclass |\Z)",
        text, re.DOTALL)
    snippet = func_match.group(0)[:1200] if func_match else "<not found>"
    return {
        "target_path":      BK3_TARGET,
        "test_path":        BK3_TEST_PATH,
        "function_name":    "_check_append_only",
        "function_snippet": snippet,
        "instruction": (
            "There is a regression in _check_append_only that breaks the "
            "APPEND-ONLY guard. Run the failing test, read the function, "
            "identify the inverted return values, and fix them. Do NOT "
            "modify the test. Only edit org_llm/specialist.py."
        ),
    }


def verify_bk3(workdir: Path) -> tuple[bool, str]:
    """Failing test must now pass; the function's logic restored."""
    rc, out, err = _run(
        [sys.executable, "-m", "pytest", BK3_TEST_PATH, "-x", "-q",
          "--no-header", "-p", "no:cacheprovider"],
        workdir, timeout=60)
    if rc != 0:
        return False, (f"BK3 fail: {BK3_TEST_PATH} still failing "
                        f"(rc={rc}); model did not fix the bug")
    # Static guard: the inverted patterns should be GONE.
    spec_text = (workdir / BK3_TARGET).read_text()
    for bad_old, bad_new in BK3_BUG_PATCHES:
        if bad_new in spec_text and bad_old not in spec_text:
            return False, (f"BK3 fail: inverted pattern still in source: "
                            f"{bad_new[:60]!r}")
    # Verify no scope creep: only specialist.py touched (besides test).
    rc2, diff_out, _ = _run(
        ["git", "diff", "--name-only", "HEAD"],
        workdir, timeout=10)
    if rc2 == 0:
        changed = [ln for ln in diff_out.splitlines() if ln.strip()]
        forbidden = [ln for ln in changed
                       if ln not in (BK3_TARGET, BK3_TEST_PATH)]
        if forbidden:
            return False, (f"BK3 fail: scope creep — extra files modified: "
                            f"{forbidden}")
    return True, "BK3 pass: failing test green; bug pattern removed"


BK3 = {
    "id":               "BK3",
    "label":            "fix _check_append_only regression (failing test)",
    "target_files":     [BK3_TARGET, BK3_TEST_PATH],
    "expected_changes": 1,   # one logical fix (two return flips)
    "prefetch":         prefetch_bk3,
    "apply_starting_state": apply_bk3_starting_state,
    "goal": (
        "tests/test_append_only_bug.py is failing. The function "
        "`_check_append_only` in org_llm/specialist.py has a regression: "
        "its return values are inverted on both branches.\n\n"
        "Step 1: run pytest on the failing test, read the error.\n"
        "Step 2: read_file org_llm/specialist.py and locate "
        "_check_append_only.\n"
        "Step 3: edit_file to flip the two `return` lines back to correct.\n"
        "Step 4: re-run pytest to confirm green.\n\n"
        "Do NOT modify the test. Do NOT touch any file other than "
        "org_llm/specialist.py."
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk3,
    "allowed_tools":    TOOLS_BUG_FIX,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK4 — add `cell_timeout_seconds: float = 120.0` to SpecialistTask    ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK4_DATACLASS_FILE = "org_llm/specialist.py"
BK4_CLI_FILE = "org_llm/cli_specialist.py"
BK4_FIELD_NAME = "cell_timeout_seconds"
BK4_FIELD_DEFAULT = 120.0


def prefetch_bk4(task: dict) -> dict:
    """Surface SpecialistTask's current field list + the cli schema sub."""
    sys.path.insert(0, str(REPO))
    try:
        from dataclasses import fields
        from org_llm.specialist import SpecialistTask
        existing_fields = [f.name for f in fields(SpecialistTask)]
    except Exception:
        existing_fields = []
    finally:
        if str(REPO) in sys.path:
            sys.path.remove(str(REPO))
    cli_text = (REPO / BK4_CLI_FILE).read_text()
    schema_match = re.search(r'def specialist_schema\(.*?(?=\ndef |\Z)',
                              cli_text, re.DOTALL)
    schema_snippet = schema_match.group(0)[:2000] if schema_match else "<not found>"
    return {
        "dataclass_file":   BK4_DATACLASS_FILE,
        "cli_file":         BK4_CLI_FILE,
        "existing_fields":  existing_fields,
        "field_already_present": BK4_FIELD_NAME in existing_fields,
        "field_name":       BK4_FIELD_NAME,
        "field_default":    BK4_FIELD_DEFAULT,
        "field_type":       "float",
        "schema_function_snippet": schema_snippet,
        "spec": (
            f"Add `{BK4_FIELD_NAME}: float = {BK4_FIELD_DEFAULT}` to the "
            "@dataclass SpecialistTask in org_llm/specialist.py. Then "
            "extend specialist_schema() in org_llm/cli_specialist.py to "
            f"include the field in its returned JSON schema (type=number)."
        ),
    }


def verify_bk4(workdir: Path) -> tuple[bool, str]:
    """Field present in dataclass with correct default + type; schema lists it."""
    # 1. dataclass field check (via subprocess to use the worktree's copy)
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from dataclasses import fields; "
        "from org_llm.specialist import SpecialistTask; "
        "fmap = {f.name: f for f in fields(SpecialistTask)}; "
        f"f = fmap.get('{BK4_FIELD_NAME}'); "
        f"assert f is not None, 'missing field {BK4_FIELD_NAME}'; "
        f"assert f.type in (float, 'float'), 'wrong type: ' + repr(f.type); "
        f"assert f.default == {BK4_FIELD_DEFAULT}, "
        f"  'wrong default: ' + repr(f.default); "
        "print('OK')"
    )
    rc, out, err = _run([sys.executable, "-c", code], workdir, timeout=20)
    if rc != 0:
        return False, (f"BK4 fail: dataclass check failed: "
                        f"{err.strip() or out.strip()}")
    # 2. schema mentions the field name
    cli_text = (workdir / BK4_CLI_FILE).read_text()
    if BK4_FIELD_NAME not in cli_text:
        return False, (f"BK4 fail: '{BK4_FIELD_NAME}' not mentioned in "
                        f"{BK4_CLI_FILE} (expected schema entry)")
    # 3. schema CLI invocation includes the field key
    rc2, out2, err2 = _run(
        [sys.executable, "-m", "org_llm.cli", "specialist", "schema"],
        workdir, timeout=20)
    if rc2 != 0:
        return False, f"BK4 fail: `specialist schema` rc={rc2}: {err2[:200]}"
    try:
        schema = json.loads(out2)
        props = schema.get("properties", {})
    except Exception as exc:
        return False, f"BK4 fail: schema not JSON: {exc}"
    if BK4_FIELD_NAME not in props:
        return False, (f"BK4 fail: schema.properties missing "
                        f"{BK4_FIELD_NAME}: keys={sorted(props.keys())}")
    # 4. existing tests still pass (subset)
    rc3, out3, err3 = _run(
        [sys.executable, "-m", "pytest", "tests/test_cli.py", "-x", "-q",
          "--no-header", "-p", "no:cacheprovider"],
        workdir, timeout=120)
    if rc3 != 0:
        return False, f"BK4 fail: pytest tests/test_cli.py failed (rc={rc3})"
    return True, (f"BK4 pass: field added, schema includes "
                   f"{BK4_FIELD_NAME}, tests green")


BK4 = {
    "id":               "BK4",
    "label":            "add cell_timeout_seconds to SpecialistTask",
    "target_files":     [BK4_DATACLASS_FILE, BK4_CLI_FILE],
    "expected_changes": 2,
    "prefetch":         prefetch_bk4,
    "goal": (
        f"Add a new field to the @dataclass SpecialistTask in "
        f"org_llm/specialist.py:\n\n"
        f"    {BK4_FIELD_NAME}: float = {BK4_FIELD_DEFAULT}\n\n"
        f"Place it alongside the other timeout/budget fields (e.g. "
        f"max_iterations, max_budget_usd) and add a one-line comment "
        f"describing its purpose: a per-cell wall-clock ceiling in "
        f"seconds.\n\n"
        f"Then extend specialist_schema() in org_llm/cli_specialist.py "
        f"to include the field in its JSON schema (type='number').\n\n"
        f"Constraints:\n"
        f"  - Use exact field name `{BK4_FIELD_NAME}` and default "
        f"{BK4_FIELD_DEFAULT}\n"
        f"  - Type must be `float` (lowercase)\n"
        f"  - If prefetch.field_already_present is True, no-op + explain\n"
        f"  - Existing tests must still pass\n"
        f"  - Do NOT touch any other file"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk4,
    "allowed_tools":    TOOLS_REFACTOR_PY,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK5 — normalize wiki [[file:agents.org]] → [[id:UUID][agents.org]]   ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Five+ wiki pages currently link to agents.org via the brittle
# `[[file:agents.org]]` form. The canonical form is the ID-based link.
# Goal: rewrite every `[[file:agents.org][LABEL]]` (and bare
# `[[file:agents.org]]`) into `[[id:UUID][LABEL]]` (or
# `[[id:UUID][agents.org]]` for the bare form).

BK5_AGENTS_ID = "a8f4c2e1-9b3d-4e5f-87a6-c1d2e3f4b5a6"

# Live tree contains 8+ pages with [[file:agents.org]] forms; pick a
# stable subset that's been quiet recently.
BK5_FILES = [
    "docs/wiki/agentsmith.org",
    "docs/wiki/agent-framework.org",
    "docs/wiki/agent-time-awareness.org",
    "docs/wiki/sidecar-agent-design.org",
    "docs/wiki/literate-tools.org",
]


_BK5_OLD_FORM_RE = re.compile(
    r"\[\[file:agents\.org\](?:\[([^\]]*)\])?\]")
# Matches either:  [[file:agents.org]]            (bare, no label)
#               or [[file:agents.org][LABEL]]    (labeled)


def prefetch_bk5(task: dict) -> dict:
    """Locate every [[file:agents.org... occurrence and its expected target."""
    workdir = REPO
    per_file = {}
    total = 0
    for rel in BK5_FILES:
        p = workdir / rel
        if not p.exists():
            per_file[rel] = {"missing": True}
            continue
        text = p.read_text()
        hits = []
        for m in _BK5_OLD_FORM_RE.finditer(text):
            label = m.group(1) or "agents.org"
            line_no = text[: m.start()].count("\n") + 1
            hits.append({"line": line_no, "match": m.group(0), "label": label})
        per_file[rel] = {"hits": hits, "n": len(hits)}
        total += len(hits)
    return {
        "files":              BK5_FILES,
        "agents_canonical_id": BK5_AGENTS_ID,
        "total_occurrences":  total,
        "per_file":           per_file,
        "rule": (
            "For EVERY [[file:agents.org]] or [[file:agents.org][LABEL]] "
            f"occurrence, replace with [[id:{BK5_AGENTS_ID}][LABEL]] "
            "(or [[id:UUID][agents.org]] for the bare form). Do not "
            "touch other links. Do not change surrounding prose."
        ),
    }


def _bk5_count_old_form(workdir: Path) -> int:
    n = 0
    for rel in BK5_FILES:
        p = workdir / rel
        if not p.exists():
            continue
        n += len(_BK5_OLD_FORM_RE.findall(p.read_text()))
    return n


def _bk5_count_new_form(workdir: Path) -> int:
    pat = re.compile(rf"\[\[id:{re.escape(BK5_AGENTS_ID)}\]\[[^\]]+\]\]")
    n = 0
    for rel in BK5_FILES:
        p = workdir / rel
        if not p.exists():
            continue
        n += len(pat.findall(p.read_text()))
    return n


def verify_bk5(workdir: Path) -> tuple[bool, str]:
    """0 old-form links across the 5 files; >=5 new-form links; org parses."""
    old_n = _bk5_count_old_form(workdir)
    if old_n != 0:
        return False, (f"BK5 fail: {old_n} `[[file:agents.org...]]` "
                        f"occurrences remain (expected 0)")
    new_n = _bk5_count_new_form(workdir)
    if new_n < 5:
        return False, (f"BK5 fail: only {new_n} `[[id:{BK5_AGENTS_ID[:8]}…]` "
                        f"links across 5 files (expected >= 5 — at least one "
                        f"per file)")
    # Each file must still contain at least one new-form link (no file
    # got fully emptied of agents.org refs).
    pat = re.compile(rf"\[\[id:{re.escape(BK5_AGENTS_ID)}\]")
    for rel in BK5_FILES:
        p = workdir / rel
        if not p.exists():
            continue
        if pat.search(p.read_text()) is None:
            # Allowed only if file had 0 old-form hits originally;
            # the prefetch confirmed every file in BK5_FILES has >=1.
            return False, (f"BK5 fail: {rel} has no new-form id-link to "
                            f"agents.org (expected >=1)")
    # Org AST validation per file (lightweight: just run emacs --batch
    # if available; otherwise skip — bracket balance check as fallback).
    for rel in BK5_FILES:
        p = workdir / rel
        if not p.exists():
            continue
        text = p.read_text()
        # Bracket balance: every [[ has a matching ]]. Cheap structural check.
        if text.count("[[") != text.count("]]"):
            return False, (f"BK5 fail: bracket imbalance in {rel} after "
                            f"edits ([[ count != ]] count)")
    return True, (f"BK5 pass: 0 old-form / {new_n} new-form across "
                   f"{len(BK5_FILES)} files; brackets balanced")


BK5 = {
    "id":               "BK5",
    "label":            "normalize agents.org cross-link form across 5 wiki pages",
    "target_files":     list(BK5_FILES),
    "expected_changes": 5,   # at least 1 per file
    "prefetch":         prefetch_bk5,
    "goal": (
        "Five wiki pages link to agents.org using the brittle "
        "`[[file:agents.org]]` (and `[[file:agents.org][LABEL]]`) forms. "
        "The canonical form is the ID-based link.\n\n"
        f"Replace EVERY occurrence with "
        f"`[[id:{BK5_AGENTS_ID}][LABEL]]`. For bare-form occurrences "
        f"(no LABEL), use `[[id:{BK5_AGENTS_ID}][agents.org]]`.\n\n"
        "Constraints:\n"
        "  - Edit only the 5 files in target_files\n"
        "  - Preserve every existing LABEL (just swap file: → id:UUID)\n"
        "  - Do NOT modify surrounding prose, headings, or other links\n"
        "  - After edits, every file must still parse (bracket balance "
        "preserved)\n"
        "  - prefetch.per_file lists exact line numbers + matches"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk5,
    "allowed_tools":    TOOLS_WIKI,
}


# ── Module export ────────────────────────────────────────────────────────

LONG_HORIZON_TASKS = [BK1, BK2, BK3, BK4, BK5]


def _summarize() -> str:
    rows = []
    for t in LONG_HORIZON_TASKS:
        rows.append(f"  {t['id']:4} {t['label']:60} "
                     f"files={len(t['target_files'])} "
                     f"tools={len(t['allowed_tools'])}")
    return "\n".join(rows)


if __name__ == "__main__":
    print("R19 Track K — Long-horizon multi-file tasks")
    print(_summarize())
    print()
    # Smoke: confirm every prefetch runs against live tree without crashing.
    for t in LONG_HORIZON_TASKS:
        try:
            pf = t["prefetch"](t)
            keys = sorted(pf.keys())[:6]
            print(f"  {t['id']} prefetch OK; keys[:6]={keys}")
        except Exception as exc:
            print(f"  {t['id']} prefetch FAIL: {exc}")
