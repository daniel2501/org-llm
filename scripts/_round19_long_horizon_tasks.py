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
# Elisp / org-mode authoring (BK6-BK15). emacs --batch is invoked by the
# verifier, not by the specialist — the specialist still works through
# read/edit/write/grep on the source files.
TOOLS_ELISP = {"read_file", "edit_file", "write_file", "grep", "list_dir",
                "validate_org"}


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


# ── Emacs / elisp helpers (BK6-BK15) ─────────────────────────────────────
EMACS_BIN = os.environ.get("ORG_LLM_EMACS", "emacs")


def _emacs_batch(args: list[str], cwd: Path,
                  timeout: int = 60) -> tuple[int, str, str]:
    """Run `emacs --batch ARGS...` in `cwd`. Caller supplies post-batch args."""
    cmd = [EMACS_BIN, "-Q", "--batch"] + list(args)
    return _run(cmd, cwd, timeout=timeout)


def _emacs_available() -> bool:
    """True iff emacs binary resolves on PATH (smoke skip otherwise)."""
    rc, _, _ = _run(["which", EMACS_BIN], REPO, timeout=5)
    return rc == 0


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


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK6 — ERT test authoring: extend tests/test_org_llm_chat.el          ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Specialist must add 3 new ert-deftest blocks covering happy/edge/error
# paths for an existing org-llm-chat helper (e.g. parse-agent-prefix or
# markdown->org). Verifier runs emacs --batch with ert and asserts the
# new tests load + pass.

BK6_TEST_FILE = "tests/test_org_llm_chat.el"
BK6_SOURCE_FILE = "doom/org-llm-chat.el"
BK6_NEW_TEST_PREFIX = "org-llm-chat/r26-bk6-"   # required prefix on new tests


def prefetch_bk6(task: dict) -> dict:
    test_path = REPO / BK6_TEST_FILE
    src_path = REPO / BK6_SOURCE_FILE
    test_text = test_path.read_text() if test_path.exists() else ""
    test_lines = test_text.splitlines()
    # Find the existing `(provide '...)` line — new tests must go above it.
    provide_line = None
    for i, ln in enumerate(test_lines):
        if "(provide 'test_org_llm_chat)" in ln:
            provide_line = i + 1
            break
    # Surface a few helper function names the new tests can target.
    helper_names = []
    if src_path.exists():
        for m in re.finditer(r"^\(defun (org-llm-chat-[\w-]+)\b",
                               src_path.read_text(), re.MULTILINE):
            helper_names.append(m.group(1))
    return {
        "test_file":          BK6_TEST_FILE,
        "source_file":        BK6_SOURCE_FILE,
        "test_lines":         len(test_lines),
        "provide_line":       provide_line,
        "candidate_helpers":  helper_names[:8],
        "required_prefix":    BK6_NEW_TEST_PREFIX,
        "rule": (
            "Add at least 3 NEW `ert-deftest` blocks to tests/"
            "test_org_llm_chat.el. Each test name MUST start with "
            f"`{BK6_NEW_TEST_PREFIX}` (so the verifier can isolate them) "
            "and cover one of {happy-path, edge-case, error-case} of an "
            "existing helper (see candidate_helpers). Insert ABOVE the "
            "`(provide 'test_org_llm_chat)` line. Do NOT modify any "
            "existing test."
        ),
    }


def verify_bk6(workdir: Path) -> tuple[bool, str]:
    test_path = workdir / BK6_TEST_FILE
    if not test_path.exists():
        return False, f"BK6 fail: {BK6_TEST_FILE} missing"
    text = test_path.read_text()
    # 1. >= 3 new ert-deftest blocks with required prefix.
    pattern = re.compile(
        rf"\(ert-deftest\s+{re.escape(BK6_NEW_TEST_PREFIX)}[\w/-]+\s*\(\)",
        re.MULTILINE)
    new_tests = pattern.findall(text)
    if len(new_tests) < 3:
        return False, (f"BK6 fail: only {len(new_tests)} new ert-deftest "
                        f"with prefix `{BK6_NEW_TEST_PREFIX}` (need >=3)")
    # 2. emacs --batch ERT run must exit 0.
    rc, out, err = _emacs_batch(
        ["-L", "doom/", "-l", "ert", "-l", BK6_TEST_FILE,
          "-f", "ert-run-tests-batch-and-exit"],
        workdir, timeout=120)
    if rc != 0:
        tail = (err or out)[-400:]
        return False, f"BK6 fail: ert run rc={rc}; tail={tail!r}"
    return True, (f"BK6 pass: {len(new_tests)} new ert-deftest with "
                   f"prefix; full ert suite green")


BK6 = {
    "id":               "BK6",
    "label":            "ERT test authoring (3 new ert-deftest blocks)",
    "target_files":     [BK6_TEST_FILE, BK6_SOURCE_FILE],
    "expected_changes": 1,   # one logical change: extend the test file
    "prefetch":         prefetch_bk6,
    "goal": (
        "Extend tests/test_org_llm_chat.el with at least 3 NEW "
        "`ert-deftest` blocks. Each new test name MUST start with "
        f"`{BK6_NEW_TEST_PREFIX}` so the verifier can find them. "
        "Cover happy-path / edge-case / error-case of an existing "
        "helper from prefetch.candidate_helpers (e.g. "
        "`org-llm-chat-parse-agent-prefix`).\n\n"
        "Constraints:\n"
        "  - Insert ABOVE the `(provide 'test_org_llm_chat)` line\n"
        "  - Do NOT modify any existing test\n"
        "  - Each new test must be syntactically valid elisp\n"
        "  - The full file must still load + the full ert suite "
        "must exit 0 under `emacs --batch ... -f "
        "ert-run-tests-batch-and-exit`"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk6,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK7 — org-babel tangle: add a :tangle src block to literate-tools.org║
# ╚══════════════════════════════════════════════════════════════════════╝

BK7_TARGET = "docs/wiki/literate-tools.org"
BK7_TANGLE_RELPATH = "docs/wiki/_bk7_tangled.sh"   # specialist tangles to this


def prefetch_bk7(task: dict) -> dict:
    p = REPO / BK7_TARGET
    text = p.read_text() if p.exists() else ""
    existing_src = len(re.findall(r"^#\+begin_src\b", text,
                                     re.MULTILINE | re.IGNORECASE))
    return {
        "target_file":         BK7_TARGET,
        "expected_tangle_to":  BK7_TANGLE_RELPATH,
        "existing_src_blocks": existing_src,
        "rule": (
            "Append (do NOT delete) a NEW `#+begin_src sh :tangle "
            f"{BK7_TANGLE_RELPATH}` block to docs/wiki/literate-tools.org. "
            "Block contents: at least one shell command. Then verify the "
            "block tangles cleanly. The verifier runs "
            "`emacs --batch -l org --eval (org-babel-tangle)` and asserts "
            f"the file `{BK7_TANGLE_RELPATH}` is created with the block's "
            "contents."
        ),
    }


def verify_bk7(workdir: Path) -> tuple[bool, str]:
    src = workdir / BK7_TARGET
    if not src.exists():
        return False, f"BK7 fail: {BK7_TARGET} missing"
    text = src.read_text()
    # 1. New block with the expected :tangle target present.
    if BK7_TANGLE_RELPATH not in text:
        return False, (f"BK7 fail: no `:tangle {BK7_TANGLE_RELPATH}` in "
                        f"{BK7_TARGET}")
    # Make sure tangled file isn't pre-existing (force fresh).
    tangled = workdir / BK7_TANGLE_RELPATH
    if tangled.exists():
        try:
            tangled.unlink()
        except Exception:
            pass
    # 2. Run emacs to tangle.
    elisp = (
        f"(progn (require 'org) (require 'ob-tangle) "
        f"(find-file \"{BK7_TARGET}\") (org-babel-tangle))"
    )
    rc, out, err = _emacs_batch(
        ["--eval", elisp], workdir, timeout=60)
    if rc != 0:
        return False, f"BK7 fail: tangle rc={rc}; err={err[-300:]!r}"
    if not tangled.exists():
        return False, (f"BK7 fail: expected tangled output at "
                        f"{BK7_TANGLE_RELPATH} (not created)")
    if tangled.stat().st_size == 0:
        return False, f"BK7 fail: tangled file is empty"
    return True, (f"BK7 pass: src block added; tangle produced "
                   f"{tangled.stat().st_size} bytes at {BK7_TANGLE_RELPATH}")


BK7 = {
    "id":               "BK7",
    "label":            "org-babel tangle: add :tangle src block",
    "target_files":     [BK7_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk7,
    "goal": (
        f"Append a NEW `#+begin_src sh :tangle {BK7_TANGLE_RELPATH}` "
        f"block to {BK7_TARGET} (do not modify existing content). "
        "The block must contain at least one valid shell command "
        "(e.g. `echo 'r26 bk7'`). When tangled via "
        "`emacs --batch -l org --eval (org-babel-tangle)`, the block "
        f"must produce the file {BK7_TANGLE_RELPATH} with the block's "
        "contents.\n\n"
        "Constraints:\n"
        "  - Only edit docs/wiki/literate-tools.org\n"
        "  - Preserve all existing src blocks + prose\n"
        "  - The :tangle target must be exactly the path above"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk7,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK8 — org-element parse + assert: add a property drawer with new ID  ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK8_TARGET = "docs/wiki/agentsmith.org"
BK8_NEW_ID = "bk8-r26-test-id-deadbeef"   # required ID literal


def prefetch_bk8(task: dict) -> dict:
    p = REPO / BK8_TARGET
    text = p.read_text() if p.exists() else ""
    headings = re.findall(r"^\*+\s+.+$", text, re.MULTILINE)
    return {
        "target_file":     BK8_TARGET,
        "n_headings":      len(headings),
        "first_heading":   headings[0] if headings else None,
        "required_id":     BK8_NEW_ID,
        "rule": (
            f"Add a `:PROPERTIES:`/`:ID: {BK8_NEW_ID}`/`:END:` property "
            f"drawer to ANY second-level (or deeper) heading in "
            f"{BK8_TARGET}. The verifier parses the file with "
            f"`org-element-parse-buffer` and asserts at least one node "
            f"in the tree carries the ID `{BK8_NEW_ID}`. Do NOT modify "
            f"the existing top-level :PROPERTIES: block."
        ),
    }


def verify_bk8(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK8_TARGET
    if not p.exists():
        return False, f"BK8 fail: {BK8_TARGET} missing"
    text = p.read_text()
    if BK8_NEW_ID not in text:
        return False, f"BK8 fail: ID `{BK8_NEW_ID}` not present in file"
    # Use org-element-parse-buffer to confirm the ID is on a real node
    # (not just lurking in prose).
    elisp = (
        f"(progn (require 'org) (require 'org-element) "
        f"(find-file \"{BK8_TARGET}\") "
        f"(let* ((tree (org-element-parse-buffer)) "
        f"       (ids (org-element-map tree 'headline "
        f"               (lambda (h) (org-element-property :ID h)))) "
        f"       (target \"{BK8_NEW_ID}\")) "
        f"  (if (member target ids) "
        f"      (progn (princ \"OK \") (princ (length ids))) "
        f"    (error \"missing ID %s in tree (got %S)\" target ids))))"
    )
    rc, out, err = _emacs_batch(["--eval", elisp], workdir, timeout=60)
    if rc != 0:
        return False, (f"BK8 fail: org-element parse failed "
                        f"(rc={rc}): {err[-300:]!r}")
    if not out.startswith("OK"):
        return False, f"BK8 fail: parse OK but ID not on a headline: {out!r}"
    return True, f"BK8 pass: parse green, ID `{BK8_NEW_ID}` on headline"


BK8 = {
    "id":               "BK8",
    "label":            "org-element parse: add property drawer with new ID",
    "target_files":     [BK8_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk8,
    "goal": (
        f"In {BK8_TARGET}, pick any second-level (or deeper) heading "
        f"that does NOT already have a :PROPERTIES: drawer and add:\n\n"
        f"  :PROPERTIES:\n"
        f"  :ID:       {BK8_NEW_ID}\n"
        f"  :END:\n\n"
        f"Place the drawer immediately after the heading line. The "
        f"verifier parses the file with `org-element-parse-buffer` "
        f"and asserts at least one headline carries the ID "
        f"`{BK8_NEW_ID}`. Use the EXACT id literal above.\n\n"
        f"Constraints:\n"
        f"  - Only edit {BK8_TARGET}\n"
        f"  - Do NOT modify the file's existing top-level :ID: drawer\n"
        f"  - Preserve all surrounding prose"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk8,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK9 — org-roam-ish node + backlink (structural, no DB sync)          ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Original spec called for org-roam-db-sync. org-roam is an external
# package (not built-in to Emacs 30.2) AND requires a sqlite DB.
# Reviewer note: keep BK9 in the BK6-BK15 set but verify *structurally*
# (new file with :ID: + forward link from existing → new) so the
# fixture runs on any Emacs 30.2 host. The "roam-ish" shape is enough
# to score whether the specialist can author a node + backlink correctly.

BK9_NEW_FILE = "docs/wiki/_bk9_r26_node.org"   # specialist creates
BK9_EXISTING_FILE = "docs/wiki/agentsmith.org"  # specialist adds link
BK9_NEW_NODE_ID = "bk9-r26-roam-node-cafef00d"
BK9_LINK_LABEL = "BK9 R26 node"


def prefetch_bk9(task: dict) -> dict:
    return {
        "new_file":         BK9_NEW_FILE,
        "existing_file":    BK9_EXISTING_FILE,
        "new_node_id":      BK9_NEW_NODE_ID,
        "link_label":       BK9_LINK_LABEL,
        "rule": (
            f"Two-file change. (1) CREATE {BK9_NEW_FILE} with a top-level "
            f"`:PROPERTIES:`/`:ID: {BK9_NEW_NODE_ID}`/`:END:` drawer, a "
            f"`#+TITLE:` line, and at least one paragraph of prose. "
            f"(2) APPEND a paragraph to {BK9_EXISTING_FILE} containing "
            f"the link `[[id:{BK9_NEW_NODE_ID}][{BK9_LINK_LABEL}]]`. "
            f"This emulates an org-roam node + backlink without "
            f"requiring the org-roam package."
        ),
    }


def verify_bk9(workdir: Path) -> tuple[bool, str]:
    new = workdir / BK9_NEW_FILE
    if not new.exists():
        return False, f"BK9 fail: new file {BK9_NEW_FILE} not created"
    new_text = new.read_text()
    if BK9_NEW_NODE_ID not in new_text:
        return False, f"BK9 fail: new file missing :ID: {BK9_NEW_NODE_ID}"
    if not re.search(r":PROPERTIES:[\s\S]*:ID:\s+" +
                       re.escape(BK9_NEW_NODE_ID) +
                       r"[\s\S]*:END:", new_text):
        return False, f"BK9 fail: ID not inside a :PROPERTIES: drawer"
    if "#+TITLE:" not in new_text and "#+title:" not in new_text:
        return False, f"BK9 fail: new file missing #+TITLE: header"
    existing = workdir / BK9_EXISTING_FILE
    if not existing.exists():
        return False, f"BK9 fail: existing file {BK9_EXISTING_FILE} missing"
    ext_text = existing.read_text()
    expected_link = f"[[id:{BK9_NEW_NODE_ID}][{BK9_LINK_LABEL}]]"
    if expected_link not in ext_text:
        return False, f"BK9 fail: backlink {expected_link!r} not in existing file"
    # Structural sanity: brackets balanced in both.
    for rel, t in ((BK9_NEW_FILE, new_text), (BK9_EXISTING_FILE, ext_text)):
        if t.count("[[") != t.count("]]"):
            return False, f"BK9 fail: bracket imbalance in {rel}"
    return True, (f"BK9 pass: new node + backlink wired "
                   f"({len(new_text)} bytes new, link in existing)")


BK9 = {
    "id":               "BK9",
    "label":            "org-roam-ish node + backlink (structural)",
    "target_files":     [BK9_NEW_FILE, BK9_EXISTING_FILE],
    "expected_changes": 2,   # one new file + one edited file
    "prefetch":         prefetch_bk9,
    "goal": (
        f"Two-file change emulating an org-roam node + backlink:\n\n"
        f"(1) CREATE a NEW file {BK9_NEW_FILE} with:\n"
        f"    - A top-level `:PROPERTIES:` drawer containing "
        f"`:ID:       {BK9_NEW_NODE_ID}`\n"
        f"    - A `#+TITLE: BK9 R26 node` line\n"
        f"    - At least one paragraph of body prose\n\n"
        f"(2) EDIT the existing file {BK9_EXISTING_FILE}: append a "
        f"paragraph (anywhere safe) containing the link "
        f"`[[id:{BK9_NEW_NODE_ID}][{BK9_LINK_LABEL}]]`.\n\n"
        f"Constraints:\n"
        f"  - Use the EXACT ID literal `{BK9_NEW_NODE_ID}`\n"
        f"  - Use the EXACT link label `{BK9_LINK_LABEL}`\n"
        f"  - Bracket balance preserved in both files\n"
        f"  - Do NOT modify any other file"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk9,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK10 — defcustom + defface authoring; byte-compile clean              ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK10_TARGET = "doom/org-llm.el"
BK10_DEFCUSTOM_NAME = "org-llm-bk10-r26-flag"
BK10_DEFFACE_NAME = "org-llm-bk10-r26-face"


def prefetch_bk10(task: dict) -> dict:
    p = REPO / BK10_TARGET
    text = p.read_text() if p.exists() else ""
    has_defgroup = "(defgroup org-llm" in text
    return {
        "target_file":      BK10_TARGET,
        "lines":            len(text.splitlines()),
        "has_defgroup":     has_defgroup,
        "defcustom_name":   BK10_DEFCUSTOM_NAME,
        "defface_name":     BK10_DEFFACE_NAME,
        "rule": (
            f"In {BK10_TARGET}, add ONE new `defcustom` named "
            f"`{BK10_DEFCUSTOM_NAME}` (any reasonable :type, e.g. "
            f"boolean) AND ONE new `defface` named "
            f"`{BK10_DEFFACE_NAME}` (any reasonable spec). After the "
            f"edit, `emacs --batch -f batch-byte-compile {BK10_TARGET}` "
            f"must exit 0. Pick a sensible default + docstring for each."
        ),
    }


def verify_bk10(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK10_TARGET
    if not p.exists():
        return False, f"BK10 fail: {BK10_TARGET} missing"
    text = p.read_text()
    if not re.search(rf"\(defcustom\s+{re.escape(BK10_DEFCUSTOM_NAME)}\b",
                       text):
        return False, f"BK10 fail: defcustom {BK10_DEFCUSTOM_NAME} not found"
    if not re.search(rf"\(defface\s+{re.escape(BK10_DEFFACE_NAME)}\b",
                       text):
        return False, f"BK10 fail: defface {BK10_DEFFACE_NAME} not found"
    # byte-compile.
    rc, out, err = _emacs_batch(
        ["-f", "batch-byte-compile", BK10_TARGET],
        workdir, timeout=120)
    if rc != 0:
        return False, (f"BK10 fail: byte-compile rc={rc}: "
                        f"{(err or out)[-400:]!r}")
    return True, "BK10 pass: defcustom + defface added; byte-compile clean"


BK10 = {
    "id":               "BK10",
    "label":            "defcustom + defface authoring (byte-compile clean)",
    "target_files":     [BK10_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk10,
    "goal": (
        f"In {BK10_TARGET}, add ONE new `defcustom` and ONE new "
        f"`defface`:\n\n"
        f"  - defcustom: name=`{BK10_DEFCUSTOM_NAME}`, any reasonable "
        f"type (e.g. boolean), a docstring, a `:group 'org-llm` (the "
        f"defgroup is already declared elsewhere in the org-llm "
        f"distro), and a default value.\n"
        f"  - defface: name=`{BK10_DEFFACE_NAME}`, a sensible spec "
        f"(e.g. `'((t :inherit shadow))`), a docstring, optional "
        f"`:group 'org-llm`.\n\n"
        f"After the edit, `emacs --batch -Q -f batch-byte-compile "
        f"{BK10_TARGET}` MUST exit 0 (no warnings escalated to errors "
        f"in batch — but the file must compile cleanly enough to exit "
        f"0).\n\n"
        f"Constraints:\n"
        f"  - Use the EXACT names above\n"
        f"  - Place the new forms near other defcustom forms (search "
        f"`(defcustom ` to find them)\n"
        f"  - Do NOT modify any other file"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk10,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK11 — org-table validation: add 5x4 table with #+TBLFM:             ║
# ╚══════════════════════════════════════════════════════════════════════╝

BK11_TARGET = "docs/wiki/recipes.org"
BK11_TABLE_MARKER = "BK11 R26 sample table"   # required caption/header text


def prefetch_bk11(task: dict) -> dict:
    p = REPO / BK11_TARGET
    text = p.read_text() if p.exists() else ""
    pipe_lines = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("|"))
    return {
        "target_file":     BK11_TARGET,
        "lines":           len(text.splitlines()),
        "existing_pipes":  pipe_lines,
        "marker":          BK11_TABLE_MARKER,
        "rule": (
            f"Append (do NOT delete) a 5-row × 4-column org table to "
            f"{BK11_TARGET}. The 4th column must be computed via "
            f"`#+TBLFM:` (e.g. `$4=$2+$3`). Include the marker string "
            f"`{BK11_TABLE_MARKER}` on a comment/caption line "
            f"immediately above the table so the verifier can locate "
            f"it. The verifier runs `org-table-recalculate t` over the "
            f"file and asserts it exits cleanly (no eval errors)."
        ),
    }


def verify_bk11(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK11_TARGET
    if not p.exists():
        return False, f"BK11 fail: {BK11_TARGET} missing"
    text = p.read_text()
    if BK11_TABLE_MARKER not in text:
        return False, f"BK11 fail: marker {BK11_TABLE_MARKER!r} not in file"
    if "#+TBLFM:" not in text:
        return False, f"BK11 fail: no #+TBLFM: line found"
    # Pipe count must have grown by at least 5 (5 table rows).
    new_pipes = sum(1 for ln in text.splitlines()
                      if ln.lstrip().startswith("|"))
    # Cheap structural sanity: each table row should have at least 4 cells
    # ⇒ 5+ pipes per row. Just require >= 5 pipe-lines were added.
    # (The exact count differs because of separator rows; allow margin.)
    # 4 cells / row = 5 pipes per row; plus optional separator rows.
    # Find the table that contains TBLFM and count its rows.
    block = re.search(r"((?:^[ \t]*\|.*\n)+#\+TBLFM:.*$)",
                        text, re.MULTILINE)
    if not block:
        return False, f"BK11 fail: no contiguous |table|+#+TBLFM: block found"
    rows = [ln for ln in block.group(1).splitlines()
              if ln.lstrip().startswith("|") and not
              ln.lstrip().startswith("|-")]
    if len(rows) < 5:
        return False, (f"BK11 fail: table has {len(rows)} data/header rows "
                        f"(need >=5)")
    # Check column count on first non-separator row.
    cols = [c for c in rows[0].split("|") if c.strip()]
    if len(cols) < 4:
        return False, f"BK11 fail: first row has {len(cols)} cells (need >=4)"
    # Run org-table-recalculate.
    elisp = (
        f"(progn (require 'org) (require 'org-table) "
        f"(find-file \"{BK11_TARGET}\") "
        f"(goto-char (point-min)) "
        f"(re-search-forward \"#\\\\+TBLFM:\") "
        f"(forward-line -1) "
        f"(org-table-recalculate t) "
        f"(princ \"OK\"))"
    )
    rc, out, err = _emacs_batch(["--eval", elisp], workdir, timeout=60)
    if rc != 0 or "OK" not in out:
        return False, (f"BK11 fail: org-table-recalculate rc={rc}; "
                        f"err={err[-300:]!r}")
    return True, (f"BK11 pass: 5x4+ table with TBLFM added; "
                   f"recalculate green ({len(rows)} rows)")


BK11 = {
    "id":               "BK11",
    "label":            "org-table validation: 5x4 table + #+TBLFM:",
    "target_files":     [BK11_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk11,
    "goal": (
        f"Append a 5-row × 4-column org-mode table to {BK11_TARGET}. "
        f"The 4th column must be computed via `#+TBLFM:` (e.g. "
        f"`#+TBLFM: $4=$2+$3`). Layout suggestion:\n\n"
        f"  # {BK11_TABLE_MARKER}\n"
        f"  | label | a | b | sum |\n"
        f"  |-------+---+---+-----|\n"
        f"  | r1    | 1 | 2 |     |\n"
        f"  | r2    | 3 | 4 |     |\n"
        f"  | r3    | 5 | 6 |     |\n"
        f"  | r4    | 7 | 8 |     |\n"
        f"  #+TBLFM: $4=$2+$3\n\n"
        f"Constraints:\n"
        f"  - Marker line (`{BK11_TABLE_MARKER}`) must appear "
        f"immediately above the table\n"
        f"  - Append at end of file (preserve all existing content)\n"
        f"  - Table must have >=5 rows (header + separator + 4 data "
        f"is fine) and >=4 columns\n"
        f"  - `org-table-recalculate t` must exit cleanly"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk11,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK12 — capture template authoring (byte-compile-clean validator)     ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Reviewer note: (org-capture nil "key") is interactive and stalls
# under --batch. Validate via byte-compile-clean instead — i.e. the
# specialist must add a setq/add-to-list form for org-capture-templates
# that compiles cleanly without unbound-symbol errors.

BK12_TARGET = "doom/org-llm.el"
BK12_KEY_LITERAL = "bk12r26"   # capture key the specialist must use


def prefetch_bk12(task: dict) -> dict:
    p = REPO / BK12_TARGET
    text = p.read_text() if p.exists() else ""
    return {
        "target_file":      BK12_TARGET,
        "lines":            len(text.splitlines()),
        "key_literal":      BK12_KEY_LITERAL,
        "rule": (
            f"In {BK12_TARGET}, add a form that registers a NEW "
            f"`org-capture-templates` entry with key "
            f"`\"{BK12_KEY_LITERAL}\"` (e.g. via "
            f"`(add-to-list 'org-capture-templates ...)` or "
            f"`(with-eval-after-load 'org ...)`). The verifier runs "
            f"`emacs --batch -f batch-byte-compile {BK12_TARGET}` and "
            f"asserts exit 0; (org-capture nil ...) is NOT exercised "
            f"because it stalls under --batch."
        ),
    }


def verify_bk12(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK12_TARGET
    if not p.exists():
        return False, f"BK12 fail: {BK12_TARGET} missing"
    text = p.read_text()
    # 1. Capture-templates entry with the required key literal.
    if "org-capture-templates" not in text:
        return False, "BK12 fail: no `org-capture-templates` reference"
    if f"\"{BK12_KEY_LITERAL}\"" not in text:
        return False, (f"BK12 fail: capture key "
                        f"\"{BK12_KEY_LITERAL}\" not present")
    # 2. byte-compile clean.
    rc, out, err = _emacs_batch(
        ["-f", "batch-byte-compile", BK12_TARGET],
        workdir, timeout=120)
    if rc != 0:
        return False, (f"BK12 fail: byte-compile rc={rc}: "
                        f"{(err or out)[-400:]!r}")
    return True, "BK12 pass: capture template entry added; byte-compile clean"


BK12 = {
    "id":               "BK12",
    "label":            "capture template authoring (byte-compile clean)",
    "target_files":     [BK12_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk12,
    "goal": (
        f"In {BK12_TARGET}, add a form that registers a NEW entry in "
        f"`org-capture-templates`. The new entry's key must be exactly "
        f"the string `\"{BK12_KEY_LITERAL}\"`. Suggested shape (one "
        f"option of several valid forms):\n\n"
        f"  (with-eval-after-load 'org\n"
        f"    (add-to-list 'org-capture-templates\n"
        f"                 '(\"{BK12_KEY_LITERAL}\" \"BK12 R26 capture\"\n"
        f"                   entry (file \"~/org/inbox.org\")\n"
        f"                   \"* TODO %?\\n  %U\")))\n\n"
        f"After the edit, `emacs --batch -Q -f batch-byte-compile "
        f"{BK12_TARGET}` MUST exit 0. The verifier does NOT call "
        f"`org-capture` because it stalls interactively under --batch.\n\n"
        f"Constraints:\n"
        f"  - Key literal must be EXACTLY `\"{BK12_KEY_LITERAL}\"`\n"
        f"  - Use `add-to-list` or equivalent — do NOT clobber the "
        f"existing alist\n"
        f"  - Place near related capture/keymap setup if any exists"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk12,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK13 — org-id management: add :ID: drawers to N headings             ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Reviewer's spec calls for `org-id-get-create` on 5 headings. Running
# org-id-get-create from --batch mutates the buffer + writes the file.
# To make this deterministically scoreable: the specialist authors 5
# fresh `:PROPERTIES:`/`:ID: <unique>`/`:END:` drawers on 5 distinct
# headings of an existing wiki page. Verifier counts unique IDs +
# checks they're 36-char-ish UUIDs (or any non-empty unique tokens).

BK13_TARGET = "docs/wiki/agents.org"
BK13_ID_PREFIX = "bk13-r26-"          # required prefix for the 5 new IDs
BK13_REQUIRED_COUNT = 5


def prefetch_bk13(task: dict) -> dict:
    p = REPO / BK13_TARGET
    text = p.read_text() if p.exists() else ""
    headings = [m.group(0) for m in re.finditer(r"^\*+\s+.+$", text,
                                                    re.MULTILINE)]
    # Existing IDs (so model knows which headings to skip).
    existing_ids = re.findall(r"^:ID:\s+(\S+)\s*$", text, re.MULTILINE)
    return {
        "target_file":          BK13_TARGET,
        "n_headings":           len(headings),
        "existing_id_count":    len(existing_ids),
        "id_prefix":            BK13_ID_PREFIX,
        "required_new_count":   BK13_REQUIRED_COUNT,
        "rule": (
            f"In {BK13_TARGET}, add `:PROPERTIES:`/`:ID: "
            f"{BK13_ID_PREFIX}<uniq>`/`:END:` drawers to "
            f"{BK13_REQUIRED_COUNT} DISTINCT headings that don't "
            f"already have an :ID:. Each new ID must start with "
            f"`{BK13_ID_PREFIX}` and the 5 new IDs must be unique. "
            f"Pick headings at level >=2 to avoid clobbering the "
            f"file's top-level :ID:."
        ),
    }


def verify_bk13(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK13_TARGET
    if not p.exists():
        return False, f"BK13 fail: {BK13_TARGET} missing"
    text = p.read_text()
    new_ids = re.findall(rf"^:ID:\s+({re.escape(BK13_ID_PREFIX)}\S+)\s*$",
                          text, re.MULTILINE)
    if len(new_ids) < BK13_REQUIRED_COUNT:
        return False, (f"BK13 fail: only {len(new_ids)} new IDs with "
                        f"prefix `{BK13_ID_PREFIX}` (need "
                        f"{BK13_REQUIRED_COUNT})")
    if len(set(new_ids)) != len(new_ids):
        return False, f"BK13 fail: new IDs not unique: {sorted(new_ids)}"
    # Each new ID must sit inside a :PROPERTIES:...:END: drawer.
    for nid in new_ids:
        pat = re.compile(
            r":PROPERTIES:[\s\S]*?:ID:\s+" + re.escape(nid) +
            r"[\s\S]*?:END:")
        if not pat.search(text):
            return False, (f"BK13 fail: ID `{nid}` not inside a "
                            f":PROPERTIES: drawer")
    # Validate file still parses via org-element-parse-buffer.
    elisp = (
        f"(progn (require 'org) (require 'org-element) "
        f"(find-file \"{BK13_TARGET}\") "
        f"(let* ((tree (org-element-parse-buffer))) "
        f"  (princ (length (org-element-map tree 'headline #'identity)))))"
    )
    rc, out, err = _emacs_batch(["--eval", elisp], workdir, timeout=60)
    if rc != 0:
        return False, (f"BK13 fail: org-element parse failed after edits "
                        f"(rc={rc}): {err[-300:]!r}")
    return True, (f"BK13 pass: {len(new_ids)} unique new IDs in "
                   f":PROPERTIES: drawers; parse green")


BK13 = {
    "id":               "BK13",
    "label":            f"org-id management: add {BK13_REQUIRED_COUNT} new :ID: drawers",
    "target_files":     [BK13_TARGET],
    "expected_changes": BK13_REQUIRED_COUNT,
    "prefetch":         prefetch_bk13,
    "goal": (
        f"In {BK13_TARGET}, add `:PROPERTIES:`/`:ID:`/`:END:` drawers "
        f"to {BK13_REQUIRED_COUNT} DISTINCT headings that don't "
        f"already have an :ID:.\n\n"
        f"Each new ID must:\n"
        f"  - Start with the prefix `{BK13_ID_PREFIX}`\n"
        f"  - Be unique across the file\n"
        f"  - Sit inside a real `:PROPERTIES:`/`:END:` drawer placed "
        f"immediately after its heading line\n\n"
        f"Pick headings at level >=2 (do NOT touch the file's top-"
        f"level :ID:). Suggested suffix: a short slug derived from "
        f"the heading text, e.g. `{BK13_ID_PREFIX}architecture`.\n\n"
        f"After the edit, the file must still parse cleanly via "
        f"`org-element-parse-buffer`."
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk13,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK14 — checkdoc-clean elisp file (use checkdoc-file, not -batch)     ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Reviewer note: `checkdoc-batch` is not a function. The canonical
# entry point in Emacs 30.2 is `(checkdoc-file FILE)`. The validator
# captures checkdoc's output and asserts no warnings tagged with the
# specialist's section. To make this deterministic without rewriting
# the existing 1971-LOC file, the specialist must add (a) a Commentary
# preamble + (b) a single new defun WITH a complete docstring AND
# checkdoc-clean. Verifier runs `(checkdoc-file ...)` and asserts no
# new warnings reference the specialist's added defun.

BK14_TARGET = "doom/org-llm-chat.el"
BK14_NEW_DEFUN = "org-llm-chat-bk14-r26-stub"
BK14_COMMENTARY_TAG = "BK14 R26 commentary preamble"


def prefetch_bk14(task: dict) -> dict:
    p = REPO / BK14_TARGET
    text = p.read_text() if p.exists() else ""
    has_commentary = ";;; Commentary:" in text
    return {
        "target_file":         BK14_TARGET,
        "lines":               len(text.splitlines()),
        "has_commentary":      has_commentary,
        "new_defun_name":      BK14_NEW_DEFUN,
        "commentary_tag":      BK14_COMMENTARY_TAG,
        "rule": (
            f"In {BK14_TARGET}: (1) ensure a `;;; Commentary:` block "
            f"exists in the file header (add or extend), and the "
            f"Commentary text contains the marker `"
            f"{BK14_COMMENTARY_TAG}`. (2) Add ONE new defun named "
            f"`{BK14_NEW_DEFUN}` near the bottom of the file (above "
            f"`(provide '...)`) with a complete checkdoc-clean "
            f"docstring (capitalised first sentence ending in a period; "
            f"no checkdoc warnings). The verifier runs `(checkdoc-file "
            f"\"{BK14_TARGET}\")` and asserts no warning lines mention "
            f"the new defun's name."
        ),
    }


def verify_bk14(workdir: Path) -> tuple[bool, str]:
    p = workdir / BK14_TARGET
    if not p.exists():
        return False, f"BK14 fail: {BK14_TARGET} missing"
    text = p.read_text()
    # 1. Commentary preamble + tag present.
    if ";;; Commentary:" not in text:
        return False, "BK14 fail: no `;;; Commentary:` block"
    if BK14_COMMENTARY_TAG not in text:
        return False, f"BK14 fail: tag {BK14_COMMENTARY_TAG!r} not in file"
    # 2. New defun present.
    if not re.search(rf"\(defun\s+{re.escape(BK14_NEW_DEFUN)}\b", text):
        return False, f"BK14 fail: defun {BK14_NEW_DEFUN} not found"
    # 3. checkdoc-file run; capture warnings.
    elisp = (
        f"(progn (require 'checkdoc) "
        f"(let ((checkdoc-arguments-in-order-flag nil)) "
        f"  (with-current-buffer (find-file-noselect \"{BK14_TARGET}\") "
        f"    (let ((checkdoc-diagnostic-buffer "
        f"           (get-buffer-create \"*bk14-checkdoc*\"))) "
        f"      (checkdoc-current-buffer t) "
        f"      (with-current-buffer \"*bk14-checkdoc*\" "
        f"        (princ (buffer-string)))))))"
    )
    rc, out, err = _emacs_batch(["--eval", elisp], workdir, timeout=120)
    # checkdoc-current-buffer doesn't fail rc; we inspect output.
    if rc != 0:
        return False, (f"BK14 fail: checkdoc invocation rc={rc}: "
                        f"{err[-300:]!r}")
    # Any warnings referencing the new defun → fail.
    bad_lines = [ln for ln in out.splitlines() if BK14_NEW_DEFUN in ln]
    if bad_lines:
        return False, (f"BK14 fail: checkdoc warnings on "
                        f"{BK14_NEW_DEFUN}: {bad_lines[:3]}")
    return True, "BK14 pass: Commentary + defun added; checkdoc clean for new defun"


BK14 = {
    "id":               "BK14",
    "label":            "checkdoc-clean elisp (Commentary + new defun)",
    "target_files":     [BK14_TARGET],
    "expected_changes": 1,
    "prefetch":         prefetch_bk14,
    "goal": (
        f"In {BK14_TARGET}:\n\n"
        f"(1) Ensure a `;;; Commentary:` block exists in the file "
        f"header. If absent, add one. The Commentary text MUST "
        f"contain the literal marker `{BK14_COMMENTARY_TAG}`.\n\n"
        f"(2) Add ONE new defun named `{BK14_NEW_DEFUN}` immediately "
        f"above the trailing `(provide '...)` form. The defun must "
        f"have a checkdoc-clean docstring: first sentence capitalised, "
        f"ends with a period, fits on one line (or wraps cleanly). "
        f"The body can be trivial (e.g. return nil or a constant).\n\n"
        f"The verifier runs `(checkdoc-current-buffer t)` over the "
        f"file and asserts no warning lines mention "
        f"`{BK14_NEW_DEFUN}`.\n\n"
        f"Constraints:\n"
        f"  - Use the EXACT defun name `{BK14_NEW_DEFUN}`\n"
        f"  - Use `checkdoc-file` / `checkdoc-current-buffer` shape "
        f"(NOT `checkdoc-batch` — not a real function in Emacs 30.2)\n"
        f"  - Don't re-format unrelated existing functions"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk14,
    "allowed_tools":    TOOLS_ELISP,
}


# ╔══════════════════════════════════════════════════════════════════════╗
# ║ BK15 — introduce makem.sh + Makefile (NEW FILES at repo root)        ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Reviewer note: Makefile + makem.sh do NOT currently exist. Asking the
# specialist to add a target to a missing Makefile is meaningless;
# introducing the testing infrastructure as a new artifact tests
# whether the specialist can author CI surface from scratch.

BK15_MAKEFILE = "Makefile"
BK15_MAKEM = "makem.sh"
BK15_TEST_TARGET_NAME = "test-elisp"


def prefetch_bk15(task: dict) -> dict:
    repo_root = REPO
    return {
        "makefile_path":        BK15_MAKEFILE,
        "makem_path":           BK15_MAKEM,
        "makefile_exists":      (repo_root / BK15_MAKEFILE).exists(),
        "makem_exists":         (repo_root / BK15_MAKEM).exists(),
        "repo_root_writable":   os.access(str(repo_root), os.W_OK),
        "expected_target":      BK15_TEST_TARGET_NAME,
        "rule": (
            f"Create TWO new files at repo root:\n\n"
            f"  (1) {BK15_MAKEFILE}: a GNU Makefile with at least a "
            f"`{BK15_TEST_TARGET_NAME}:` target that invokes `./{BK15_MAKEM} "
            f"all` (or runs the existing emacs --batch ert command "
            f"directly). Tab-indented recipe lines (Make is whitespace-"
            f"sensitive).\n\n"
            f"  (2) {BK15_MAKEM}: a POSIX shell script with `#!/usr/bin/"
            f"env bash` shebang. Minimum body: run "
            f"`emacs --batch -L doom/ -l ert -l tests/test_org_llm_chat.el "
            f"-f ert-run-tests-batch-and-exit` and propagate its exit "
            f"code. Mark executable (specialist requests "
            f"`chmod +x {BK15_MAKEM}` or the verifier does it before "
            f"invoking)."
        ),
    }


def verify_bk15(workdir: Path) -> tuple[bool, str]:
    mf = workdir / BK15_MAKEFILE
    sh = workdir / BK15_MAKEM
    if not mf.exists():
        return False, f"BK15 fail: {BK15_MAKEFILE} not created"
    if not sh.exists():
        return False, f"BK15 fail: {BK15_MAKEM} not created"
    mf_text = mf.read_text()
    sh_text = sh.read_text()
    # Makefile must define the expected target.
    if not re.search(rf"^{re.escape(BK15_TEST_TARGET_NAME)}\s*:",
                       mf_text, re.MULTILINE):
        return False, (f"BK15 fail: Makefile has no `"
                        f"{BK15_TEST_TARGET_NAME}:` target")
    # Tabs in recipe lines (a TAB after a target is required by GNU Make).
    if "\n\t" not in mf_text:
        return False, "BK15 fail: Makefile has no tab-indented recipe lines"
    # makem.sh must have a shebang.
    if not sh_text.startswith("#!"):
        return False, "BK15 fail: makem.sh missing shebang"
    if "ert-run-tests-batch-and-exit" not in sh_text and \
        "emacs" not in sh_text:
        return False, "BK15 fail: makem.sh body doesn't invoke emacs/ert"
    # Ensure executable (set if not, then re-check).
    try:
        mode = sh.stat().st_mode
        if not (mode & 0o111):
            os.chmod(sh, mode | 0o755)
    except Exception as exc:
        return False, f"BK15 fail: chmod {BK15_MAKEM} error: {exc}"
    # Run `make -n test-elisp` (dry-run) to confirm the target parses.
    rc, out, err = _run(
        ["make", "-n", BK15_TEST_TARGET_NAME], workdir, timeout=20)
    if rc != 0:
        return False, (f"BK15 fail: `make -n {BK15_TEST_TARGET_NAME}` "
                        f"rc={rc}: {err[-300:]!r}")
    return True, (f"BK15 pass: Makefile + makem.sh created; "
                   f"`make -n {BK15_TEST_TARGET_NAME}` clean")


BK15 = {
    "id":               "BK15",
    "label":            "introduce makem.sh + Makefile (NEW infra)",
    "target_files":     [BK15_MAKEFILE, BK15_MAKEM],
    "expected_changes": 2,   # two new files
    "prefetch":         prefetch_bk15,
    "goal": (
        f"Create TWO new files at the repo root (neither exists yet):\n\n"
        f"(1) `{BK15_MAKEFILE}` — a GNU Makefile with at least a "
        f"`{BK15_TEST_TARGET_NAME}:` target. The recipe must run the "
        f"makem.sh script (or the equivalent emacs --batch ert "
        f"invocation directly). Recipe lines MUST be tab-indented "
        f"(GNU Make whitespace rule).\n\n"
        f"Suggested shape:\n\n"
        f"  .PHONY: {BK15_TEST_TARGET_NAME}\n"
        f"  {BK15_TEST_TARGET_NAME}:\n"
        f"  \\t./{BK15_MAKEM} all\n\n"
        f"(2) `{BK15_MAKEM}` — a POSIX bash script with `#!/usr/bin/"
        f"env bash` shebang. Minimum body: run\n\n"
        f"  emacs --batch -L doom/ -l ert -l tests/test_org_llm_chat.el "
        f"-f ert-run-tests-batch-and-exit\n\n"
        f"and propagate its exit code (`set -e` or explicit `exit $?`). "
        f"The verifier ensures the file is executable.\n\n"
        f"Constraints:\n"
        f"  - Both files at repo root (NOT in subdirs)\n"
        f"  - `make -n {BK15_TEST_TARGET_NAME}` must exit 0 after creation\n"
        f"  - Use `write_file` for new files; existing tools may not "
        f"create from nothing"
    ),
    "primary_metric":   "verifier_passes",
    "verifier":         verify_bk15,
    "allowed_tools":    TOOLS_ELISP,
}


# ── Module export ────────────────────────────────────────────────────────

LONG_HORIZON_TASKS = [BK1, BK2, BK3, BK4, BK5,
                       BK6, BK7, BK8, BK9, BK10,
                       BK11, BK12, BK13, BK14, BK15]


def _summarize() -> str:
    rows = []
    for t in LONG_HORIZON_TASKS:
        rows.append(f"  {t['id']:4} {t['label']:60} "
                     f"files={len(t['target_files'])} "
                     f"tools={len(t['allowed_tools'])}")
    return "\n".join(rows)


def _smoke_one(t: dict) -> tuple[bool, str]:
    """Per-fixture smoke: prefetch runs + every target_file exists or
    (for NEW-FILE tasks like BK15) parent dir exists + is writable."""
    # 1. prefetch
    try:
        pf = t["prefetch"](t)
    except Exception as exc:
        return False, f"prefetch raised: {exc!r}"
    if not isinstance(pf, dict):
        return False, f"prefetch returned non-dict: {type(pf).__name__}"
    # 2. target_files: each must exist OR (for new-file tasks) parent
    #     dir must exist + be writable.
    missing = []
    for rel in t.get("target_files", []):
        p = REPO / rel
        if p.exists():
            continue
        # NEW-FILE allowance: BK9 creates a new file; BK15 creates two;
        # tolerate any non-existent target whose parent dir is writable.
        parent = p.parent
        if parent.exists() and os.access(str(parent), os.W_OK):
            continue
        missing.append(rel)
    if missing:
        return False, f"missing target_files (no NEW-FILE excuse): {missing}"
    keys = sorted(pf.keys())[:6]
    return True, f"prefetch OK; keys[:6]={keys}"


if __name__ == "__main__":
    print("R19 Track K — Long-horizon multi-file tasks")
    print(_summarize())
    print()
    # Smoke: confirm every prefetch runs against live tree without crashing
    # AND every target_files entry is reachable (existing or NEW-FILE-able).
    n_pass = 0
    n_fail = 0
    for t in LONG_HORIZON_TASKS:
        ok, msg = _smoke_one(t)
        flag = "GREEN" if ok else "RED  "
        print(f"  [{flag}] {t['id']:5}  {msg}")
        if ok:
            n_pass += 1
        else:
            n_fail += 1
    print()
    print(f"Smoke summary: {n_pass} green / {n_fail} red "
           f"({len(LONG_HORIZON_TASKS)} total)")
    sys.exit(0 if n_fail == 0 else 1)
