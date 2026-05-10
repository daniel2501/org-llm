#!/usr/bin/env python3
"""R29 design patch + verification — capability + correctness round.

R29 ROUND-FORK 2026-05-09 — verifies that =scripts/_round29_dials.py=
and the R29 fix-set are all in place. Run via:

    python3 scripts/_r29_design_patch.py

R29 verification covers:
  R28 carry-forwards (still verified):
    R28-1  WALL_CAP _wall_cap_fired sentinel
    R28-2  K11-qwen3coder retired
    R28-3  Groq literal in PROVIDER_PINS["ignore"]
    R28-4  K20 clamp + liveness re-poll (specialist.py)
    R28-5  _is_in_flight_or_stub helper
    R28-6  round-fork constants (ROUND="R29", R29_LIVE.json, etc)
    R28-7  4 procedural memory rules (still indexed)

  R29 new patches:
    R29-1  DISABLE_REASONING_MODELS canonical + warmup imports it
    R29-2  CAT 3 K20 resume budget = 600s (was 480s)
    R29-11 _heartbeat_cell helper for per-cell watchdog visibility
    R29-12 _r29_post_round_analysis.sh exists + executable
    R29-14 N_REPLICATES_L1["K1-qwen30"] = 100 (bumped from 50 to tighten zero-rate CI)
    R29-15 K20 NOT in TASK_FAMILY_VARIANTS["wiki_edit"] under K20_ENABLED=1
    R29-16 score_cell BK preservation gate (destructive_penalty + floor)
    R29-17 K5 solo grounding-hint in run_claude_solo_cell
    R29 memory rules: per-task slicing, verifier-preservation, stratify-before-generalize

Per [Round-fork path-pointer + variant-list audit] memory rule, this
script is the formal acceptance probe for the round-fork. R29 launch
gates on PASS."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
DST = REPO / "scripts/_round29_dials.py"
ARTIFACTS = REPO / "scripts/_round29_dials_artifacts"
MEMORY_DIR = Path.home() / ".claude/projects/-home-daniel-repos-org-llm/memory"


def verify_patches() -> tuple[int, int]:
    """Verify all R28 carry-forwards + R29 new patches. Returns (passed, failed)."""
    passed, failed = 0, 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}: {detail}")
            failed += 1

    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(REPO))
    try:
        import _round29_dials as m
    except Exception as e:
        print(f"FATAL: cannot import _round29_dials: {e}")
        return 0, 1

    # ── R28 carry-forwards ─────────────────────────────────────────────
    check("R28-1 WALL_CAP _wall_cap_fired sentinel helper",
          hasattr(m, "_wall_cap_fired"))
    check("R28-2 K11 retired from VARIANTS",
          not any(v[0] == "K11-qwen3coder" for v in m.VARIANTS),
          "K11 still in VARIANTS")
    check("R28-2 K11 retired from HEDGED_ESCALATIONS",
          "K11-qwen3coder" not in m.HEDGED_ESCALATIONS.values()
          and "K11-qwen3coder" not in m.HEDGED_ESCALATIONS)
    missing_groq = [k for k, v in m.PROVIDER_PINS.items()
                    if "Groq" not in v.get("ignore", [])]
    check("R28-3 Groq literal in every PROVIDER_PINS['ignore']",
          not missing_groq, f"missing in: {missing_groq}")
    try:
        import org_llm.specialist as sp
        check("R28-4 K20 max_tokens clamp",
              hasattr(sp, "_clamp_max_tokens_for_k20"))
        check("R28-4 K20 pod-liveness re-poll",
              hasattr(sp, "_k20_pod_liveness_check"))
    except Exception as e:
        check("R28-4 specialist.py imports", False, str(e))
    check("R28-5 in-flight stub detector",
          hasattr(m, "_is_in_flight_or_stub"))
    check("R28-6 ROUND constant = 'R29'",
          getattr(m, "ROUND", None) == "R29",
          f"ROUND={getattr(m, 'ROUND', None)!r}")
    check("R28-6 ARTIFACTS path = _round29_dials_artifacts",
          str(m.ARTIFACTS).endswith("_round29_dials_artifacts"))
    check("R28-6 LIVE = R29_LIVE.json",
          str(m.LIVE).endswith("R29_LIVE.json"))
    expected_r28_rules = [
        "feedback_failfast_stage_aware_watcher.md",
        "feedback_multi_surface_probe_pool.md",
        "feedback_preflight_slug_validation.md",
        "feedback_kill_kill_not_term_with_exit_trap.md",
    ]
    missing = [r for r in expected_r28_rules if not (MEMORY_DIR / r).exists()]
    check("R28-7 4 procedural memory rules present",
          not missing, f"missing: {missing}")

    # ── R29 new patches ────────────────────────────────────────────────
    check("R29-1 DISABLE_REASONING_MODELS canonical (specialist.py)",
          hasattr(sp, "DISABLE_REASONING_MODELS")
          and "moonshotai/kimi-k2-thinking" in sp.DISABLE_REASONING_MODELS)
    check("R29-1 _round29_dials._WARMUP_REASONING_DISABLE imported from specialist",
          m._WARMUP_REASONING_DISABLE is sp.DISABLE_REASONING_MODELS)

    watchdog = (REPO / "scripts/_r29_watchdog.sh").read_text()
    check("R29-2 CAT 3 budget = 600s",
          "[PREFLIGHT_CAT_3_K20_RESUME]=600" in watchdog,
          "still showing 480 or other")

    check("R29-11 _heartbeat_cell helper",
          hasattr(m, "_heartbeat_cell"))
    import inspect
    cell_src = inspect.getsource(m.execute_cell)
    check("R29-11 cell-start heartbeat in execute_cell",
          'CELL_START' in cell_src)
    check("R29-11 wall-cap heartbeat in execute_cell",
          'CELL_WALL_CAP' in cell_src)

    handoff = REPO / "scripts/_r29_post_round_analysis.sh"
    check("R29-12 _r29_post_round_analysis.sh exists + executable",
          handoff.exists() and os.access(handoff, os.X_OK))

    # R29-14 K1 N=100
    # N_REPLICATES_L1 is local to the main runner; check via re-find in source
    src_text = (REPO / "scripts/_round29_dials.py").read_text()
    check("R29-14 N_REPLICATES_L1['K1-qwen30'] = 100",
          '"K1-qwen30":        100' in src_text or
          '"K1-qwen30": 100' in src_text)

    # R29-15 K20 only in small_atomic when enabled (not wiki_edit)
    # Need to set K20_ENABLED=1 to test the conditional, but the source
    # check is sufficient: wiki_edit append removed.
    check("R29-15 K20 NOT appended to wiki_edit",
          'TASK_FAMILY_VARIANTS["wiki_edit"].append("K20-foss-distill")' not in src_text)
    check("R29-15 K20 still appended to small_atomic",
          'TASK_FAMILY_VARIANTS["small_atomic"].append("K20-foss-distill")' in src_text)

    score_src = inspect.getsource(m.score_cell)
    check("R29-16 BK preservation gate (destructive_penalty)",
          'destructive_penalty' in score_src and 'excess_deletions' in score_src)
    check("R29-16 floor_dropped on BK + extreme excess",
          '_R29_BK_FLOOR_THRESHOLD' in score_src)

    solo_src = inspect.getsource(m.run_claude_solo_cell)
    check("R29-17 K5 grounding-hint in run_claude_solo_cell",
          'NEVER fabricate IDs' in solo_src)

    expected_r29_rules = [
        "feedback_per_task_slicing_default.md",
        "feedback_verifier_preservation_gate.md",
        "feedback_stratify_before_generalize.md",
    ]
    missing_r29 = [r for r in expected_r29_rules if not (MEMORY_DIR / r).exists()]
    check("R29 3 procedural memory rules present (per-task / verifier / stratify)",
          not missing_r29, f"missing: {missing_r29}")

    return passed, failed


def main() -> int:
    print("=== R29 design patch + verification ===")
    if not DST.exists():
        print(f"FATAL: {DST} missing — fork _round28_dials.py first")
        return 1
    try:
        import ast
        ast.parse(DST.read_text())
        print(f"forked harness AST OK ({len(DST.read_text())} chars)")
    except SyntaxError as e:
        print(f"FATAL: AST error: {e}")
        return 1

    print("\n--- R29 patch verification (R28 carry-fwd + R29 new) ---")
    passed, failed = verify_patches()
    print(f"\n{passed}/{passed + failed} R29 checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
