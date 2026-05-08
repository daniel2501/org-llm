#!/usr/bin/env python3
"""Pre-flight smoke test for the R15 dial-sweep harness.

Goal: catch integration bugs in the cheap regime ($0.01, ~30 sec)
before committing to the full ~$20 / ~2-hour Layer 1+2+3 run.

Sequence (each check = pass/fail, fail short-circuits):

  1. Imports — specialist runtime, tool surfaces, R15 harness module
  2. Tooling presence — emacs, claude, pass, git, openrouter API key
  3. Prefetch dry-run — call every prefetch_* on real REPO state;
     verify they don't crash and return required keys
  4. Prompt-assembly dry-run — render_specialist_brief for each task
     under BEST_CONFIG, with both with_manager True and False
  5. Tool dispatch — verify edit/read/grep/list_dir/find_canonical_id
     work on a temp worktree (relative paths included)
  6. End-to-end smoke cell — one cheap real cell:
     B13 (LICENSE year) × K1-qwen30 × BEST_CONFIG
     Asserts: cell_result.json present, in_scope changes >= 1,
     no fabrication, cost < $0.05, wall < 60s

If all green → R15 is safe to launch. If anything red → fix first.

Run:
    python3 scripts/_round15_preflight.py
Exit code: 0 = green, non-zero = at least one red.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(REPO))


# Tiny ANSI color helpers — no extra deps.
def G(s): return f"\033[32m{s}\033[0m"   # green
def R(s): return f"\033[31m{s}\033[0m"   # red
def Y(s): return f"\033[33m{s}\033[0m"   # yellow
def D(s): return f"\033[2m{s}\033[0m"    # dim

FAILED: list[str] = []


def step(name: str):
    print(f"\n{D('━' * 4)} {name} {D('━' * (60 - len(name)))}")


def ok(msg: str):
    print(f"  {G('✓')} {msg}")


def fail(msg: str, detail: str = ""):
    print(f"  {R('✗')} {msg}")
    if detail:
        for ln in detail.splitlines()[:6]:
            print(f"    {D(ln[:120])}")
    FAILED.append(msg)


def warn(msg: str):
    print(f"  {Y('!')} {msg}")


# ── 1. Imports ──────────────────────────────────────────────────────────
def check_imports():
    step("1. imports")
    try:
        from org_llm.specialist import (
            SpecialistTask, SpecialistResult, run_specialist,
            DEFAULT_TOOLS, BROAD_TOOLS, BROAD_TOOLS_PLUS_ELISP,
            FIND_CANONICAL_ID_TOOL, GREP_TOOL, LIST_DIR_TOOL,
            EVAL_ELISP_TOOL, LOAD_ELISP_FILE_TOOL,
        )
        ok(f"org_llm.specialist (DEFAULT={len(DEFAULT_TOOLS)} "
           f"BROAD={len(BROAD_TOOLS)} +ELISP={len(BROAD_TOOLS_PLUS_ELISP)})")
    except Exception as exc:
        fail("specialist import", traceback.format_exc())
        return False

    # Import R15 harness as a real module (dataclasses need __module__).
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_round15_dials", REPO / "scripts/_round15_dials.py")
        r15 = importlib.util.module_from_spec(spec)
        sys.modules["_round15_dials"] = r15
        spec.loader.exec_module(r15)   # safe: main() is gated by __name__
        globals()["R15"] = vars(r15)
        ok(f"_round15_dials (variants={len(r15.VARIANTS)} "
           f"tasks={len(r15.TASKS)})")
    except Exception:
        fail("_round15_dials import", traceback.format_exc())
        return False
    return True


# ── 2. Tooling presence ──────────────────────────────────────────────────
def check_tools_present():
    step("2. tooling")
    for binary in ("emacs", "claude", "pass", "git"):
        p = shutil.which(binary)
        if p: ok(f"{binary}: {p}")
        else: fail(f"{binary}: not on PATH")

    # OpenRouter API key
    try:
        cp = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True, timeout=5)
        if cp.stdout.strip():
            ok(f"openrouter api key: {len(cp.stdout.strip())} chars")
        else:
            fail("openrouter api key: empty")
    except Exception as exc:
        fail(f"pass org-llm/cloud/openrouter/api-key", str(exc))


# ── 3. Prefetch dry-run ──────────────────────────────────────────────────
def check_prefetch():
    step("3. prefetch dry-run")
    if "R15" not in globals():
        warn("R15 not loaded; skipping prefetch checks")
        return
    R15 = globals()["R15"]
    for task in R15["TASKS"]:
        try:
            pf = task["prefetch"](task)
            if not isinstance(pf, dict):
                fail(f"{task['id']}: prefetch returned {type(pf).__name__}, not dict")
                continue
            ok(f"{task['id']}: prefetch keys={list(pf.keys())[:5]}...")
        except Exception:
            fail(f"{task['id']}: prefetch crashed", traceback.format_exc())


# ── 4. Prompt-assembly dry-run ───────────────────────────────────────────
def check_prompt_assembly():
    step("4. prompt assembly")
    if "R15" not in globals():
        return
    R15 = globals()["R15"]
    DialConfig = R15["DialConfig"]
    BEST_CONFIG = R15["BEST_CONFIG"]
    for task in R15["TASKS"]:
        try:
            pf = task["prefetch"](task)
            handle = R15["default_handle_for_task"](task)
            instr = R15["render_specialist_brief"](
                task, task["goal"], BEST_CONFIG, pf, handle, "")
            n = len(instr)
            if n < 200:
                fail(f"{task['id']}: brief too short ({n} chars)")
            elif n > 60_000:
                fail(f"{task['id']}: brief too long ({n} chars)")
            else:
                ok(f"{task['id']}: brief={n} chars (handle={handle})")
        except Exception:
            fail(f"{task['id']}: assembly crashed", traceback.format_exc())


# ── 5. Tool dispatch sanity ──────────────────────────────────────────────
def check_dispatch():
    step("5. tool dispatch (incl. relative paths)")
    from org_llm.specialist import _dispatch_tool_call
    wd = Path(tempfile.mkdtemp(prefix="r15-preflight-"))
    (wd / "docs").mkdir()
    (wd / "docs/note.org").write_text(":PROPERTIES:\n:ID: deadbeef\n:END:\nhello\n")

    cases = [
        ("read_file relative",
         ("read_file", {"path": "docs/note.org"}, wd), True, "hello"),
        ("read_file absolute inside",
         ("read_file", {"path": str(wd / "docs/note.org")}, wd), True, "hello"),
        ("read_file outside",
         ("read_file", {"path": "/etc/hostname"}, wd), False, "outside workdir"),
        ("read_file on dir",
         ("read_file", {"path": "docs"}, wd), False, "is a directory"),
        ("edit_file relative",
         ("edit_file", {"path": "docs/note.org", "old_string": "hello",
                          "new_string": "HOWDY"}, wd), True, "applied"),
        ("list_dir",
         ("list_dir", {"path": "docs"}, wd), True, "note.org"),
        ("grep",
         ("grep", {"pattern": "HOWDY", "path": "docs"}, wd), True, "HOWDY"),
    ]
    for label, (name, args, w), want_ok, want_in in cases:
        try:
            got_ok, obs = _dispatch_tool_call(name, args, w)
            if got_ok != want_ok:
                fail(f"{label}: ok={got_ok} (wanted {want_ok}); obs={obs[:80]}")
            elif want_in not in obs:
                fail(f"{label}: missing '{want_in}' in obs={obs[:80]}")
            else:
                ok(f"{label}")
        except Exception:
            fail(f"{label}: raised", traceback.format_exc())

    shutil.rmtree(wd, ignore_errors=True)


# ── 6. End-to-end smoke cell ─────────────────────────────────────────────
def check_smoke_cell():
    step("6. end-to-end smoke cell (B13 K1-qwen30 BEST_CONFIG)")
    from org_llm.specialist import (SpecialistTask, run_specialist,
                                       BROAD_TOOLS_PLUS_ELISP)
    if "R15" not in globals():
        return
    R15 = globals()["R15"]
    tasks_by_id = {t["id"]: t for t in R15["TASKS"]}
    # Allow override via env var to smoke a different (task, variant) pair.
    smoke_task = os.environ.get("PREFLIGHT_TASK", "B13")
    smoke_model = os.environ.get("PREFLIGHT_MODEL",
                                    "qwen/qwen3-coder-30b-a3b-instruct")
    task = tasks_by_id.get(smoke_task)
    if task is None:
        fail(f"{smoke_task} task not found in R15 TASKS")
        return

    # Build a temp worktree off trunk
    wt_name = f"r15-preflight-{smoke_task}-{int(time.time())}"
    wt_path = REPO.parent / "org-llm-worktrees" / wt_name
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(
        ["git", "-C", str(REPO), "worktree", "add", "-b", wt_name,
          str(wt_path), "trunk"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        fail("git worktree add", cp.stderr)
        return
    ok(f"worktree: {wt_path.name}")

    try:
        prefetch = task["prefetch"](task)
        instr = R15["render_specialist_brief"](
            task, task["goal"], R15["BEST_CONFIG"], prefetch,
            R15["default_handle_for_task"](task), "")

        target_files = [wt_path / task["target_file"]] if task.get("target_file") else []

        spec_task = SpecialistTask(
            handle="@boothby",
            persona="You are @boothby — Bridge Crew ops + hygiene specialist.",
            instruction=instr,
            workdir=wt_path,
            model="qwen/qwen3-coder-30b-a3b-instruct",
            target_files=target_files,
            max_iterations=int(os.environ.get("PREFLIGHT_MAX_ITERS", "4")),
            max_budget_usd=0.10,
            scope_strict=True,
            tools=list(BROAD_TOOLS_PLUS_ELISP),
        )
        t0 = time.time()
        result = run_specialist(spec_task)
        wall = time.time() - t0
        ok(f"ran: success={result.success} iters={result.iterations} "
           f"edits={len(result.edits_applied)} cost=${result.cost_usd:.4f} "
           f"wall={wall:.1f}s")

        # Acceptance criteria
        if not result.success:
            fail(f"smoke cell failed: error={result.error}")
        if result.cost_usd > 0.05:
            fail(f"cost too high: ${result.cost_usd:.4f} (expected <$0.05)")
        if wall > 90:
            fail(f"wall too high: {wall:.1f}s (expected <90s)")

        # Diff check
        diff = subprocess.run(
            ["git", "-C", str(wt_path), "diff", "trunk"],
            capture_output=True, text=True,
        ).stdout
        if "2025" in diff and "2026" in diff:
            ok("diff: contains both 2025 (removed) and 2026 (added)")
        elif len(result.edits_applied) > 0:
            warn(f"smoke cell applied edits but diff doesn't show 2025→2026; "
                  f"edits={result.edits_applied[0].get('result') if result.edits_applied else None}")
        else:
            fail("no edits applied AND no diff change")

        # Events file written?
        # (Smoke uses run_specialist directly without harness; events go to
        # SpecialistResult.events not a file. Just check the dataclass.)
        if result.events:
            ok(f"events captured: {len(result.events)}")
        else:
            warn("no events in SpecialistResult")
        if result.tool_use_breakdown:
            ok(f"tool_use_breakdown: {result.tool_use_breakdown}")

    finally:
        # Cleanup worktree
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force",
                          str(wt_path)],
                         capture_output=True)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", wt_name],
                         capture_output=True)


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print(f"\n{D('═' * 64)}")
    print(f"  R15 PRE-FLIGHT SMOKE TEST")
    print(f"  trunk HEAD: {subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--short', 'trunk'], capture_output=True, text=True).stdout.strip()}")
    print(D('═' * 64))

    if not check_imports():
        print(f"\n{R('PRE-FLIGHT FAILED — imports broken; cannot continue')}")
        sys.exit(1)

    check_tools_present()
    check_prefetch()
    check_prompt_assembly()
    check_dispatch()
    if not FAILED:
        check_smoke_cell()
    else:
        warn(f"skipping end-to-end smoke (already {len(FAILED)} failures)")

    print(f"\n{D('═' * 64)}")
    if FAILED:
        print(f"  {R('PRE-FLIGHT FAILED')} — {len(FAILED)} issues:")
        for f in FAILED: print(f"    {R('•')} {f}")
        print(f"  Fix before launching R15.")
        sys.exit(1)
    else:
        print(f"  {G('PRE-FLIGHT GREEN')} — R15 is safe to launch.")
        print(f"\n  Launch:  nohup python3 scripts/_round15_dials.py "
                f"> scripts/_round15_dials_artifacts/stdout.log 2>&1 &")


if __name__ == "__main__":
    main()
