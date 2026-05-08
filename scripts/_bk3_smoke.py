"""Smoke test for R26 P1-15: BK3 apply_starting_state hook.

Spins up a fresh `git worktree` (matching what the R26 harness does
via safe_worktree_add), invokes apply_bk3_starting_state(workdir),
and asserts:

  (a) the failing test file is created;
  (b) both _check_append_only return inversions are applied exactly
      once in workdir/org_llm/specialist.py;
  (c) re-running the hook is idempotent (no double-apply, byte-equal
      output, returns ok=True).

Run: python scripts/_bk3_smoke.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts._round19_long_horizon_tasks import (  # noqa: E402
    BK3_BUG_PATCHES,
    BK3_TARGET,
    BK3_TEST_PATH,
    apply_bk3_starting_state,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bk3-smoke-"))
    wt = tmp / "wt"
    wt_branch = f"tmp-bk3-smoke-{os.getpid()}"

    subprocess.run(
        ["git", "-C", str(REPO), "worktree", "add", "-b", wt_branch,
         str(wt), "trunk"],
        check=True, capture_output=True)

    try:
        spec_before = (wt / BK3_TARGET).read_text()

        # Pre-state sanity: correct (un-inverted) returns present.
        for old, new in BK3_BUG_PATCHES:
            assert old in spec_before, (
                "expected un-inverted pattern missing pre-state: "
                f"{old[:60]!r}")
            assert spec_before.count(old) == 1
            assert new not in spec_before, (
                f"pre-state already inverted: {new[:60]!r}")
        assert not (wt / BK3_TEST_PATH).exists(), (
            "failing test file should be absent pre-state")
        print("[pre]  source has correct returns; "
              "failing test absent: OK")

        # ── First apply ──────────────────────────────────────────
        ok1, msg1 = apply_bk3_starting_state(wt)
        assert ok1, msg1
        print(f"[run1] ok={ok1} msg={msg1}")

        # (a) failing test file created
        test_p = wt / BK3_TEST_PATH
        assert test_p.exists(), "BK3 failing test file not created"
        assert test_p.stat().st_size > 100, (
            f"BK3 test file too small ({test_p.stat().st_size} bytes)")
        print(f"[run1] failing test file present "
              f"({test_p.stat().st_size} bytes): OK")

        # (b) both inversions applied exactly once
        spec_after = (wt / BK3_TARGET).read_text()
        for old, new in BK3_BUG_PATCHES:
            assert new in spec_after, (
                f"inverted pattern missing post-apply: {new[:60]!r}")
            assert spec_after.count(new) == 1, (
                f"inverted pattern multi-applied: {new[:60]!r}")
            assert old not in spec_after, (
                f"old pattern still present after apply: {old[:60]!r}")
        print("[run1] both _check_append_only inversions present "
              "in specialist.py: OK")

        # ── Second apply (idempotency) ───────────────────────────
        ok2, msg2 = apply_bk3_starting_state(wt)
        assert ok2, msg2
        print(f"[run2] ok={ok2} msg={msg2} (idempotent re-apply)")

        spec_after2 = (wt / BK3_TARGET).read_text()
        assert spec_after2 == spec_after, (
            "second apply mutated source!")
        for old, new in BK3_BUG_PATCHES:
            assert spec_after2.count(new) == 1, (
                "idempotency broken — pattern multi-applied")
        print("[run2] source byte-equal to run1 — idempotent OK")

        print("SMOKE PASS — P1-15 BK3 hook fires + is idempotent")
        return 0
    finally:
        subprocess.run(
            ["git", "-C", str(REPO), "worktree", "remove",
             "--force", str(wt)],
            capture_output=True)
        subprocess.run(
            ["git", "-C", str(REPO), "branch", "-D", wt_branch],
            capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
