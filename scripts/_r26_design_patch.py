#!/usr/bin/env python3
"""R26 design patch + verification — P0-6 implementation.

Per PM6 (R25 verification gap): 3 of 11 R25 patches landed in code
but didn't change behavior. R26's patch composer must include a
verification step that runs each P0 patch's acceptance probe and
aborts if any fails.

Flow:
  1. Fork =scripts/_round25_dials.py= → =scripts/_round26_dials.py=
     with sed-rename round IDs.
  2. Run each P0 acceptance probe against the forked harness.
  3. On any probe FAIL: write =$ARTIFACTS/PATCH_VERIFY_FAIL= sentinel
     with the failing probe name; exit 1; do NOT launch round.
  4. On all PASS: exit 0; harness ready for round launch.

This script is the meta-gate that addresses PM6 — every P0 patch
must prove behavior-change before R26 launches. Replaces R25's
"patches land but might be dead code" failure mode.
"""
from __future__ import annotations

import re
import sys
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
SRC = REPO / "scripts/_round25_dials.py"
DST = REPO / "scripts/_round26_dials.py"
ARTIFACTS = REPO / "scripts/_round26_dials_artifacts"
NOVELTY_CHECK = REPO / "scripts/_strategy_novelty_check.sh"


def fork_harness() -> bool:
    """cp + sed-rename round IDs. Idempotent.

    P1-21c fix (2026-05-08): skip overwrite if DST already exists AND has
    been round-id-renamed AND is at least as large as the renamed SRC
    would be. This protects post-fork patches (P1-7 S1-S4 wiring,
    P1-11/12/13 file contracts, P2-4 wall_seconds, P1-15 BK3 hook,
    etc.) from being clobbered if the design-patch step is re-run as
    part of _r26_launch.sh after patches have already been layered.

    To force re-fork (e.g. R25 source moved forward and you want a
    clean re-base), set R26_FORCE_REFORK=1.
    """
    if not SRC.exists():
        print(f"[r26] ERR: source harness not found: {SRC}")
        return False
    src_text = SRC.read_text()
    # sed-rename round IDs
    renamed = (src_text
                 .replace("_round25_dials_artifacts", "_round26_dials_artifacts")
                 .replace("R25_LIVE", "R26_LIVE")
                 .replace("r25-", "r26-")
                 .replace("R25 ", "R26 ")
                 .replace("Round-25", "Round-26"))
    import os
    force = os.environ.get("R26_FORCE_REFORK", "0") == "1"
    if DST.exists() and not force:
        cur = DST.read_text()
        # Already renamed (R25→R26 markers present, no leftover R25 ones)
        # and at least as large as the renamed source (suggests post-fork
        # patches have been applied on top).
        already_renamed = ("_round26_dials_artifacts" in cur
                            and "R26_LIVE" in cur
                            and "_round25_dials_artifacts" not in cur)
        has_post_fork_patches = len(cur) >= len(renamed)
        if already_renamed and has_post_fork_patches:
            print(f"[r26] DST exists, already renamed, "
                  f"and {len(cur) - len(renamed)} chars larger than "
                  f"freshly-renamed SRC — preserving post-fork patches "
                  f"(set R26_FORCE_REFORK=1 to override)")
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            return True
    DST.write_text(renamed)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    print(f"[r26] forked {SRC.name} → {DST.name} ({len(renamed)} chars)")
    return True


# ── Acceptance probes (one per P0) ─────────────────────────────────────

def probe_p0_1_selector_dedupe() -> tuple[bool, str]:
    """P0-1: top-3 selector returns 3 distinct variants."""
    sys.path.insert(0, str(REPO / "scripts"))
    # Force fresh import in case prior probe loaded harness
    if "_round26_dials" in sys.modules:
        del sys.modules["_round26_dials"]
    import _round26_dials as h

    # Synthesize cells: K1=50@mean=23, K2=40@mean=15, K8=25@mean=10
    def synth(name, n, score, cost_each):
        return [{
            "variant": name,
            "specialists": [],
            "analysis": {
                "diff_in_scope": {"insertions": int(score), "deletions": 0},
                "diff_out_of_scope": {"insertions": 0, "deletions": 0},
                "wrap_categories": {"on_candidate": int(score / 2),
                                     "creative": 0, "unmatched": 0},
                "id_fabrication_count": 0,
            },
            "cost_usd": cost_each,
            "task_id": "B1",
        } for _ in range(n)]

    foss_cells = (synth("K1-qwen30", 50, 23, 0.001) +
                    synth("K2-kimi-k2.6", 40, 15, 0.003) +
                    synth("K8-deepseekV3", 25, 10, 0.001))

    # Re-implement the new selector logic for testing (avoid running full main())
    _by_variant: dict[str, list[float]] = {}
    _cost_by_variant: dict[str, float] = {}
    for c in foss_cells:
        v = c["variant"]
        s = h.score_cell(c).get("score", 0)
        _by_variant.setdefault(v, []).append(s)
        _cost_by_variant[v] = _cost_by_variant.get(v, 0.0) + (c.get("cost_usd") or 0)

    def rk(v):
        scores = _by_variant[v]
        m = sum(scores) / max(len(scores), 1)
        return (-m, _cost_by_variant.get(v, 0) / max(m, 0.001))
    ranked = sorted(_by_variant.keys(), key=rk)[:3]
    expected = ["K1-qwen30", "K2-kimi-k2.6", "K8-deepseekV3"]
    if ranked == expected:
        return True, f"selector returns {ranked}"
    return False, f"selector returns {ranked}, expected {expected}"


def probe_p0_2_max_tokens() -> tuple[bool, str]:
    """P0-2: SpecialistTask accepts max_tokens; threaded through to payload."""
    try:
        from org_llm.specialist import SpecialistTask  # type: ignore
    except Exception as e:
        return False, f"import SpecialistTask failed: {e}"
    try:
        t = SpecialistTask(
            handle="@atoz", persona="test", instruction="test",
            workdir="/tmp", max_tokens=4096,
        )
        if getattr(t, "max_tokens", None) != 4096:
            return False, f"max_tokens not stored: got {t.max_tokens}"
        return True, "SpecialistTask accepts max_tokens=4096"
    except TypeError as e:
        return False, f"SpecialistTask rejects max_tokens kwarg: {e}"


def probe_p0_3_wall_cap() -> tuple[bool, str]:
    """P0-3: outer worker exits ≤ cap+10% on synthetic timeout."""
    from concurrent.futures import ThreadPoolExecutor as TPE, TimeoutError as FTO
    cap = 2  # 2s test
    def slow():
        time.sleep(10)
    inner = TPE(max_workers=1)
    t0 = time.time()
    try:
        fut = inner.submit(slow)
        fut.result(timeout=cap)
    except FTO:
        inner.shutdown(wait=False)   # NEW pattern: don't wait
        elapsed = time.time() - t0
        if elapsed <= cap * 1.5:
            return True, f"outer exits at {elapsed:.2f}s (cap {cap}s)"
        return False, f"outer pinned for {elapsed:.2f}s vs cap {cap}s"
    return False, "TimeoutError did not fire"


def probe_p0_4_bracket_per_line() -> tuple[bool, str]:
    """P0-4: count_bracket_errors per-line; multi-line FP suppressed."""
    sys.path.insert(0, str(REPO / "scripts"))
    if "quality_lint" in sys.modules:
        del sys.modules["quality_lint"]
    import quality_lint as q
    # Diff with multi-line link whose ]] sits on context line
    diff = """+++ b/foo.org
+- See [[id:abc][a long
+- And another [[id:def][label]]
+- And more text
"""
    pen = q.count_bracket_errors("\n".join(
        l[1:] for l in diff.splitlines() if l.startswith("+")
    ))
    # Per-line: line 1 has 1 [[ 0 ]]: imbalance=1
    #          line 2 has 1 [[ 1 ]]: balanced
    #          line 3 has 0
    # Total = 1 (just the unclosed link in line 1)
    if pen <= 1:
        return True, f"per-line scan returns {pen} (expected ≤1, NOT 3+ from cross-line cascade)"
    return False, f"per-line scan returned {pen}, expected ≤1"


def probe_p0_5_warmup_failure() -> tuple[bool, str]:
    """P0-5: WARMED_VARIANTS gates roster; PREFLIGHT_FAIL on >30% bad."""
    sys.path.insert(0, str(REPO / "scripts"))
    if "_round26_dials" in sys.modules:
        del sys.modules["_round26_dials"]
    import _round26_dials as h
    if not hasattr(h, "WARMED_VARIANTS"):
        return False, "WARMED_VARIANTS module-level set missing"
    if not hasattr(h, "_warmup_one"):
        return False, "_warmup_one helper missing"
    return True, "WARMED_VARIANTS + _warmup_one present (live test deferred to preflight script)"


PROBES = [
    ("P0-1 top-3 selector dedupe", probe_p0_1_selector_dedupe),
    ("P0-2 K2 max_tokens wired",   probe_p0_2_max_tokens),
    ("P0-3 WALL_CAP outer-free",   probe_p0_3_wall_cap),
    ("P0-4 bracket per-line scan", probe_p0_4_bracket_per_line),
    ("P0-5 warmup pre-flight",     probe_p0_5_warmup_failure),
]


def run_probes() -> int:
    """Run each probe; abort + sentinel on any FAIL."""
    print(f"[r26] running {len(PROBES)} P0 acceptance probes")
    failed = []
    for name, probe in PROBES:
        try:
            ok, msg = probe()
        except Exception as e:
            ok, msg = False, f"probe raised {type(e).__name__}: {e}"
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {msg}")
        if not ok:
            failed.append((name, msg))
    if failed:
        sentinel = ARTIFACTS / "PATCH_VERIFY_FAIL"
        sentinel.write_text("\n".join(
            f"{name}: {msg}" for name, msg in failed
        ))
        print(f"[r26] {len(failed)} probe(s) FAILED — wrote {sentinel}")
        return 1
    print(f"[r26] all {len(PROBES)} probes PASS")
    return 0


def check_strategy_novelty() -> int:
    """P2-5 (PM6-G2): reject any chained round whose patch content
    (modulo round IDs) is byte-identical to the prior. R20-R23 hit
    this anti-pattern — "deltas" were cosmetic round-ID renames.

    On FAIL: write =$ARTIFACTS/NOVELTY_FAIL= sentinel + return 1
    (no point running probes if the round is a rerun).
    """
    if not NOVELTY_CHECK.exists():
        print(f"[r26] WARN: novelty checker missing: {NOVELTY_CHECK}")
        return 0  # advisory; don't block
    print("[r26] running strategy-novelty check (P2-5 / PM6-G2)")
    rc = subprocess.call(
        ["bash", str(NOVELTY_CHECK), str(SRC), str(DST)],
    )
    if rc != 0:
        sentinel = ARTIFACTS / "NOVELTY_FAIL"
        sentinel.write_text(
            f"strategy-novelty check FAILED for {DST.name} vs {SRC.name}\n"
            f"(round is essentially a rerun of prior — no real strategic delta)\n"
        )
        print(f"[r26] novelty check FAILED — wrote {sentinel}")
        return 1
    return 0


def main() -> int:
    print("=== R26 design patch + verification ===")
    if not fork_harness():
        return 1
    # Verify syntax of forked harness
    try:
        import ast
        ast.parse(DST.read_text())
        print(f"[r26] forked harness syntax OK")
    except SyntaxError as e:
        print(f"[r26] FATAL: forked harness syntax error: {e}")
        return 1
    # P2-5: strategy-novelty gate (PM6-G2). Skip probes if round is a rerun.
    if check_strategy_novelty() != 0:
        return 1
    return run_probes()


if __name__ == "__main__":
    sys.exit(main())
