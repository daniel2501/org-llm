#!/usr/bin/env python3
"""R28 design patch + verification — P0-6/P0-7 implementation.

R28 ROUND-FORK 2026-05-09 — harness stability sprint. Verifies that
=scripts/_round28_dials.py= exists, has all 7 R28 P0 patches landed,
and parses cleanly. Run via:

    python3 scripts/_r28_design_patch.py

R28 P0 verification:
  R28-1  WALL_CAP _fut.cancel() + WALL_CAP_FIRED sentinel
  R28-2  K11-qwen3coder dropped from VARIANTS
  R28-3  Groq literal in every PROVIDER_PINS["ignore"] list
  R28-4  K20 max_tokens clamp + pod-liveness re-poll (in specialist.py)
  R28-5  in-flight + escalator guard helpers (_is_in_flight_or_stub)
  R28-6  round-fork constants (ARTIFACTS, LIVE, ROUND)
  R28-7  4 procedural memory rules indexed in MEMORY.md

Per [Re-read before edits] memory rule, the actual fork happened
manually via `cp` + targeted Edits this session; this script verifies
the result rather than re-forking from R27 (which would lose patches).

Set R28_FORCE_REFORK=1 to wipe + re-fork _round28_dials.py from R27
(only if you know what you're doing; loses all R28 patches)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
SRC = REPO / "scripts/_round27_dials.py"
DST = REPO / "scripts/_round28_dials.py"
ARTIFACTS = REPO / "scripts/_round28_dials_artifacts"
MEMORY_DIR = Path.home() / ".claude/projects/-home-daniel-repos-org-llm/memory"


def force_refork() -> bool:
    """Re-fork _round28_dials.py from R27. Destructive — loses any R28
    patches. Gated on R28_FORCE_REFORK=1."""
    if not SRC.exists():
        print(f"[r28] ERR: source harness not found: {SRC}")
        return False
    src_text = SRC.read_text()
    # Only path/identifier renames. Round-fork patches (P0-1..P0-7) get
    # re-applied separately if this re-fork path runs.
    renamed = (src_text
                 .replace("_round27_dials_artifacts", "_round28_dials_artifacts")
                 .replace("R27_LIVE", "R28_LIVE"))
    DST.write_text(renamed)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    print(f"[r28] FORCE-REFORKED {SRC.name} → {DST.name} ({len(renamed)} chars)")
    print(f"[r28] WARN: P0-1..P0-7 patches need re-applying!")
    return True


def verify_p0_patches() -> tuple[int, int]:
    """Verify all 7 R28 P0 patches landed. Returns (passed, failed)."""
    passed, failed = 0, 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}: {detail}")
            failed += 1

    # Import the forked harness
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import _round28_dials as m
    except Exception as e:
        print(f"[r28] FATAL: cannot import _round28_dials: {e}")
        return 0, 7

    # R28-1 WALL_CAP cancel — sentinel helper exists
    check("R28-1 WALL_CAP _wall_cap_fired sentinel helper",
          hasattr(m, "_wall_cap_fired"))

    # R28-2 K11 retire
    check("R28-2 K11 retired from VARIANTS",
          not any(v[0] == "K11-qwen3coder" for v in m.VARIANTS),
          "K11 still in VARIANTS")
    check("R28-2 K11 retired from HEDGED_ESCALATIONS",
          "K11-qwen3coder" not in m.HEDGED_ESCALATIONS.values()
          and "K11-qwen3coder" not in m.HEDGED_ESCALATIONS,
          f"cascade={m.HEDGED_ESCALATIONS}")

    # R28-3 Groq deny-list
    missing = [k for k, v in m.PROVIDER_PINS.items()
               if "Groq" not in v.get("ignore", [])]
    check("R28-3 Groq literal in every PROVIDER_PINS['ignore']",
          not missing, f"missing in: {missing}")

    # R28-4 K20 clamp + liveness in specialist.py
    sys.path.insert(0, str(REPO))
    try:
        import org_llm.specialist as sp
        check("R28-4 K20 max_tokens clamp",
              hasattr(sp, "_clamp_max_tokens_for_k20"))
        check("R28-4 K20 pod-liveness re-poll",
              hasattr(sp, "_k20_pod_liveness_check"))
    except Exception as e:
        check("R28-4 specialist.py imports", False, str(e))

    # R28-5 in-flight helpers
    check("R28-5 in-flight stub detector",
          hasattr(m, "_is_in_flight_or_stub"))

    # R28-6 round-fork constants
    check("R28-6 ROUND constant = 'R28'",
          getattr(m, "ROUND", None) == "R28",
          f"ROUND={getattr(m,'ROUND',None)!r}")
    check("R28-6 ARTIFACTS path = _round28_dials_artifacts",
          str(m.ARTIFACTS).endswith("_round28_dials_artifacts"),
          f"ARTIFACTS={m.ARTIFACTS}")
    check("R28-6 LIVE = R28_LIVE.json",
          str(m.LIVE).endswith("R28_LIVE.json"),
          f"LIVE={m.LIVE}")

    # R28-7 four procedural memory rules
    expected = [
        "feedback_failfast_stage_aware_watcher.md",
        "feedback_multi_surface_probe_pool.md",
        "feedback_preflight_slug_validation.md",
        "feedback_kill_kill_not_term_with_exit_trap.md",
    ]
    missing_rules = [r for r in expected if not (MEMORY_DIR / r).exists()]
    check("R28-7 4 procedural memory rules present",
          not missing_rules, f"missing: {missing_rules}")

    return passed, failed


def main() -> int:
    print("=== R28 design patch + verification ===")
    if os.environ.get("R28_FORCE_REFORK") == "1":
        print("[r28] R28_FORCE_REFORK=1 — destructive re-fork")
        if not force_refork():
            return 1
        print("[r28] re-applying P0 patches is your job; bailing now")
        return 1

    if not DST.exists():
        print(f"[r28] DST {DST} missing — running force-refork (set"
              f" R28_FORCE_REFORK=1 explicitly to acknowledge)")
        return 1

    try:
        import ast
        ast.parse(DST.read_text())
        print(f"[r28] forked harness AST OK ({len(DST.read_text())} chars)")
    except SyntaxError as e:
        print(f"[r28] FATAL: AST error: {e}")
        return 1

    print("\n--- R28 P0 patch verification ---")
    passed, failed = verify_p0_patches()
    print(f"\n{passed}/{passed+failed} R28 P0 checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
