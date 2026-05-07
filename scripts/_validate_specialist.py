#!/usr/bin/env python3
"""Validation probe for org_llm/specialist.py.

Tiny test: spawn @atoz on a small synthetic page, ask it to wrap one
mention. Verifies the multi-turn tool-use loop, file-edit dispatch,
and budget capping all work end-to-end.
"""
from __future__ import annotations
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/home/daniel/repos/org-llm")
from org_llm.specialist import (
    SpecialistTask,
    SpecialistResult,
    run_specialist,
)


def main():
    workdir = Path(tempfile.mkdtemp(prefix="specialist-validate-"))
    page = workdir / "tiny.org"
    page.write_text("""\
:PROPERTIES:
:ID:       0123abcd-0000-0000-0000-000000000001
:CREATED:  [2026-05-07]
:END:
#+TITLE: Tiny test

* Summary

This page mentions agents.org as a related concept.

* See also

- agents.org — the agent surface
""")
    print(f"workdir: {workdir}")
    print(f"target: {page}")
    print()

    task = SpecialistTask(
        handle="@atoz",
        persona=("You are @atoz — Bridge Crew wiki concept-graph specialist. "
                 "Use the edit_file tool to apply edits. When done, write a "
                 "one-line summary."),
        instruction=(
            f"In `{page}` there's a mention of `agents.org` on the body line "
            "(under '* Summary'). Wrap that ONE body-prose mention in an "
            "org-mode id-link: replace `agents.org` with "
            "`[[id:a8f4c2e1-9b3d-4e5f-87a6-c1d2e3f4b5a6][agents.org]]`. "
            "Make exactly ONE edit (the body mention, not the See-also "
            "bullet). Use the edit_file tool."
        ),
        workdir=workdir,
        target_files=[page],
        model="qwen/qwen3-coder-30b-a3b-instruct",
        max_iterations=4,
        max_budget_usd=0.10,
    )

    print(f"running specialist {task.handle} (model={task.model})...")
    t0 = time.time()
    result = run_specialist(task)
    elapsed = time.time() - t0
    print(f"done in {elapsed:.1f}s")
    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"  success:        {result.success}")
    print(f"  iterations:     {result.iterations}")
    print(f"  edits applied:  {len(result.edits_applied)}")
    for e in result.edits_applied:
        print(f"    - iter={e['iteration']} tool={e['tool']} → {e['result']}")
    print(f"  text_output:    {result.text_output[:200]}")
    print(f"  cost_usd:       ${result.cost_usd:.6f}")
    print(f"  duration:       {result.duration_seconds}s")
    if result.error:
        print(f"  ERROR:          {result.error}")
    print()
    print(f"--- final file content ---")
    print(page.read_text())
    print()
    # Verify
    final = page.read_text()
    expected_link = "[[id:a8f4c2e1-9b3d-4e5f-87a6-c1d2e3f4b5a6][agents.org]]"
    if expected_link in final:
        print(f"✓ Link wrapper present in file")
    else:
        print(f"✗ Link wrapper MISSING — specialist didn't apply the edit")


if __name__ == "__main__":
    main()
