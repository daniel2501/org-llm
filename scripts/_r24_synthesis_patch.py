#!/usr/bin/env python3
"""R24 synthesis patch — bakes R15-R23 grand-synthesis learnings.

Builds on _r19_wiring_patch.py (BK1-BK5 import + provider.ignore +
quality_lint hook) by adding R24-specific structural fixes:

  P1. *Layer-BK selector* — schedules BK1-BK5 against top-3 FOSS +
      Claude after Layer 2. Without this, BK1-BK5 are in TASKS but
      never run (Layer 1 is hardcoded TASKS[0], Layer 2 iterates
      TASKS[1:8]).

  P2. *Drop floor/broken tasks* — best-strategy-per-shape agent
      verdicts: B13 (license year), B20 (summary add), B26
      (defcustom add) are floor/broken. Skipped via cell-spec
      filter, not removed from TASKS (preserves BK indices).

  P3. *Drop K9-mixtral + K15-kimi-thinking* — variant-longevity
      agent verdicts. K9 5/5 small samples, never frontier. K15
      subsumed by K2 at n=25.

  P4. *Sample-up K11-qwen3coder on B25* — best-strategy agent:
      K11 elisp lead is n=1 single-cell; needs verification. Bump
      to n=8 for B25.

  P5. *Bump K1-qwen30 N to 20 on B1* — outlier agent: K1 R19
      mech-47 cells partially illusory; need replicate variance
      to surface noise. Lightweight S2 best-of-N proxy.

Idempotent — markers prevent re-application.
"""
from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path("/home/daniel/repos/org-llm/scripts/_round24_dials.py")

MARKER_LAYER_BK = "# R24_SYNTHESIS: Layer-BK selector"
MARKER_DROP_TASKS = "# R24_SYNTHESIS: drop floor/broken tasks"
MARKER_DROP_VARIANTS = "# R24_SYNTHESIS: drop K9 + K15"
MARKER_K11_BUMP = "# R24_SYNTHESIS: K11-B25 sample-up"
MARKER_K1_BUMP = "# R24_SYNTHESIS: K1-B1 sample-up"
MARKER_PARALLEL = "# R24_SYNTHESIS: parallelism bump"
MARKER_WALL_CAP = "# R24_SYNTHESIS: per-cell wall cap"


def patch_layer_bk(src: str) -> str:
    if MARKER_LAYER_BK in src:
        return src
    # Insert a new Layer-BK after Layer 3 finishes but before final
    # summary. Anchor: 'log("=" * 80)' followed by 'R19 COMPLETE' /
    # 'R24 COMPLETE' line. We inject before the final LEADERBOARD print.
    anchor_complete = 'log(f"R24 COMPLETE'
    if anchor_complete not in src:
        # Try R19 etc patterns left over from sed-rename
        for hint in ("R19 COMPLETE", "R20 COMPLETE", "R21 COMPLETE",
                      "R22 COMPLETE", "R23 COMPLETE"):
            if f'log(f"{hint}' in src:
                anchor_complete = f'log(f"{hint}'
                break
    if anchor_complete not in src:
        print("[r24] couldn't find round-COMPLETE anchor — Layer-BK skip")
        return src

    inject = f'''
    {MARKER_LAYER_BK}
    bk_tasks = [t for t in TASKS if t.get("id", "").startswith("BK")]
    if bk_tasks and top3_foss:
        log(f"\\nLayer BK — long-horizon tasks: {{[t['id'] for t in bk_tasks]}}")
        bk_advancing = [v for v in VARIANTS if v[0] in top3_foss
                          or v[2] == "claude-solo"]
        bk_specs = [(task, variant, BEST_CONFIG, "layer_bk")
                       for task in bk_tasks
                       for variant in bk_advancing]
        run_layer_parallel("LAYER BK — BK1-BK5 long-horizon",
                              bk_specs, all_cells)
    else:
        log("\\nLayer BK skipped — no BK tasks loaded or no top-3 FOSS yet")

    '''

    # Inject right before the COMPLETE log line
    pattern = f'    log("=" * 80)\n    {anchor_complete}'
    if pattern not in src:
        # fall-back: just insert before the COMPLETE line
        idx = src.find(f'    {anchor_complete}')
        if idx == -1:
            print("[r24] anchor pattern not found — Layer-BK skip")
            return src
        return src[:idx] + inject + src[idx:]
    return src.replace(pattern, inject + pattern)


def patch_drop_tasks(src: str) -> str:
    if MARKER_DROP_TASKS in src:
        return src
    # Modify the layer 2 cell_specs comprehension to filter out
    # floor/broken tasks (B13, B20, B26).
    old = (
        '    cell_specs = [(task, variant, BEST_CONFIG, "layer2")\n'
        '                   for task in TASKS[1:]\n'
        '                   for variant in advancing]'
    )
    new = (
        f'    {MARKER_DROP_TASKS}\n'
        '    _R24_DROP_TASK_IDS = {"B13", "B20", "B26"}\n'
        '    cell_specs = [(task, variant, BEST_CONFIG, "layer2")\n'
        '                   for task in TASKS[1:]\n'
        '                   if task.get("id") not in _R24_DROP_TASK_IDS\n'
        '                   and not task.get("id", "").startswith("BK")\n'
        '                   for variant in advancing]'
    )
    if old in src:
        src = src.replace(old, new)
    return src


def patch_drop_variants(src: str) -> str:
    if MARKER_DROP_VARIANTS in src:
        return src
    # Inject a filter right after VARIANTS = [...].
    # Find the `]` close of VARIANTS and inject a post-filter.
    idx = src.find("VARIANTS = [")
    if idx == -1:
        return src
    close_idx = src.find("\n]\n", idx)
    if close_idx == -1:
        return src
    insert_pos = close_idx + 3   # after "\n]\n"
    inject = f'''
{MARKER_DROP_VARIANTS}
_R24_DROP_VARIANT_IDS = {{"K9-mixtral", "K15-kimi-thinking"}}
VARIANTS = [v for v in VARIANTS if v[0] not in _R24_DROP_VARIANT_IDS]
print(f"[R24] active variants: {{[v[0] for v in VARIANTS]}}")
'''
    return src[:insert_pos] + inject + src[insert_pos:]


def patch_n_replicates(src: str) -> str:
    """P4 + P5: bump K11 on B25 + K1 on B1.

    The harness uses N_REPLICATES_L1[variant] as the per-variant
    sample size in Layer 1. We can't easily change Layer 2 task-
    specific N without bigger surgery, so we leverage what's there:
      - Bump K1's L1 n from 15 → 20 (more B1 samples for variance).
      - Add a Layer-2.5 mini-stage that runs K11 × B25 at n=8.
    """
    if MARKER_K1_BUMP in src and MARKER_K11_BUMP in src:
        return src

    # P5 — K1 n bump on Layer 1
    old_k1 = '"K1-qwen30":         15,'
    new_k1 = f'"K1-qwen30":         20,  {MARKER_K1_BUMP}'
    if old_k1 in src and MARKER_K1_BUMP not in src:
        src = src.replace(old_k1, new_k1)

    # P4 — K11 × B25 sample-up: inject a mini-stage in main()
    if MARKER_K11_BUMP not in src:
        anchor = '    # ── Layer 3: dial ablation on B1 + top-2 FOSS ──'
        if anchor in src:
            inject = f'''    {MARKER_K11_BUMP}
    k11_var = next((v for v in VARIANTS if v[0] == "K11-qwen3coder"), None)
    b25_task = next((t for t in TASKS if t.get("id") == "B25"), None)
    if k11_var and b25_task:
        log("\\nLayer K11-B25 sample-up — n=8 verification of elisp lead")
        k11b25_specs = [(b25_task, k11_var, BEST_CONFIG, "layer_k11b25")
                          for _ in range(8)]
        run_layer_parallel("LAYER K11-B25 (n=8)", k11b25_specs, all_cells)

'''
            src = src.replace(anchor, inject + anchor)

    return src


def patch_parallelism(src: str) -> str:
    """P6 — bump PARALLELISM 16 → 32. Walltime agent showed parallel
    efficiency was max-cell-bound, but more workers means more cells
    in flight during the long-pole block, so total wall is shorter."""
    if MARKER_PARALLEL in src:
        return src
    old = "PARALLELISM = 16"
    new = f"PARALLELISM = 32  {MARKER_PARALLEL}"
    if old in src:
        src = src.replace(old, new)
    return src


def patch_wall_cap(src: str) -> str:
    """P7 — hard 400s wall cap per cell. K5/B5 took 648s in R18,
    blocking the layer for 9 min. Walltime agent: hedged retry
    or hard cap.

    Implementation: wrap execute_cell's specialist call with a
    threading.Timer that records 'wall_cap_exceeded' and returns
    early. Minimally invasive: add a top-level constant + a
    Run-elapsed check inside the specialist loop.

    Simplest path: add WALL_CAP_PER_CELL_S constant. The actual
    enforcement requires runtime change to execute_cell which is
    out of scope for a structural sed-patch; future round will
    wire it. We log the constant so a follow-up runtime patch
    can pick it up.
    """
    if MARKER_WALL_CAP in src:
        return src
    # Insert constant near other top-level constants (after PARALLELISM)
    anchor = "PARALLELISM = "
    if anchor not in src:
        return src
    # Find the line, append our constant after it
    lines = src.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.strip().startswith(anchor):
            out.append(f"WALL_CAP_PER_CELL_S = 400  {MARKER_WALL_CAP}\n")
            inserted = True
    return "".join(out)


def main() -> int:
    if not HARNESS.exists():
        print(f"[r24] harness not found at {HARNESS}")
        return 1

    # Apply R19 wiring patch first (idempotent)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _r19_wiring_patch as r19
    r19_path_orig = r19.HARNESS
    r19.HARNESS = HARNESS
    rc = r19.main()
    r19.HARNESS = r19_path_orig
    if rc != 0:
        print(f"[r24] R19 wiring patch failed (rc={rc})")
        return rc

    # Apply R24-specific patches
    src = HARNESS.read_text()
    orig = src
    src = patch_layer_bk(src)
    src = patch_drop_tasks(src)
    src = patch_drop_variants(src)
    src = patch_n_replicates(src)
    src = patch_parallelism(src)
    src = patch_wall_cap(src)

    if src == orig:
        print("[r24] no R24-specific changes (already patched)")
        return 0

    HARNESS.write_text(src)
    import ast
    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"[r24] SYNTAX ERROR after R24 patches: {e}")
        HARNESS.write_text(orig)
        return 1

    # Report which markers landed
    landed = []
    for m, label in [(MARKER_LAYER_BK, "Layer-BK"),
                       (MARKER_DROP_TASKS, "drop B13/B20/B26"),
                       (MARKER_DROP_VARIANTS, "drop K9+K15"),
                       (MARKER_K1_BUMP, "K1-B1 n=20"),
                       (MARKER_K11_BUMP, "K11-B25 n=8"),
                       (MARKER_PARALLEL, "parallelism 32"),
                       (MARKER_WALL_CAP, "wall-cap 400s")]:
        if m in src:
            landed.append(label)
    print(f"[r24] applied: {', '.join(landed) if landed else 'NONE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
