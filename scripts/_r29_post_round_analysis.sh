#!/usr/bin/env bash
# R29 post-round analysis handoff (R29-12 — fixes R28 launch warning
# "post-handoff exited non-zero (no script)").
#
# Runs after harness exits cleanly. Captures stats from R29_LIVE.json,
# emits a summary line + a sentinel file the analysis-agent-spawn can
# detect, and exits 0 so launch.sh proceeds to step 6 (K20 pause).
#
# Per [Productionalize harness into app] memory rule: this script seeds
# the pattern that the manager-agent design (Phase 2026-05.30) will
# productionalize as a real post-round event with structured outputs.
#
# Per [Per-task slicing is the default scoring lens] memory rule: the
# summary line emits per-variant + per-task tables, not just aggregate.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
ARTIFACTS=${ARTIFACTS:-$REPO/scripts/_round29_dials_artifacts}
LIVE=$ARTIFACTS/R29_LIVE.json
SENTINEL=$ARTIFACTS/POST_ROUND_HANDOFF_DONE
LIVE_LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org

ts() { date +%H:%M:%S; }
log() {
    echo "[r29-handoff $(ts)] $*"
    echo "[r29-handoff $(ts)] $*" >> "$LIVE_LOG" 2>/dev/null || true
}

if [ ! -f "$LIVE" ]; then
    log "WARN: $LIVE missing — round didn't write live state; sentinel skipped"
    exit 0
fi

log "post-round handoff starting"

# Per-variant + aggregate stats
python3 - <<'PYEOF' || log "WARN: python summary failed"
import json
from pathlib import Path
import os

ARTIFACTS = Path(os.environ.get("ARTIFACTS",
    "/home/daniel/repos/org-llm/scripts/_round29_dials_artifacts"))
LIVE = ARTIFACTS / "R29_LIVE.json"
d = json.loads(LIVE.read_text())
lb = d.get("leaderboard", [])
total_cells = d.get("cells_done", 0)
total_cost = sum(v.get("total_cost", 0) for v in lb)
total_score = sum(v.get("total_score", 0) for v in lb)

print("=" * 60)
print(f"R29 ROUND COMPLETE — {total_cells} cells, "
      f"score={total_score:.1f}, cost=${total_cost:.4f}")
print("=" * 60)
print()
print(f"  {'variant':>20}  {'cells':>5}  {'pri/cell':>10}  "
      f"{'$/u':>10}  {'fab':>4}  {'noops':>5}")
for v in lb:
    n = v.get("cells", 0) or 1
    pri = v.get("primary_total", 0) / n
    cpu = v.get("cost_per_unit") or 0
    print(f"  {v.get('variant','?'):>20}  {v.get('cells',0):>5}  "
          f"{pri:>10.2f}  ${cpu:>9.5f}  {v.get('fab_total',0):>4}  "
          f"{v.get('silent_noops',0):>5}")
print()
print(f"  TOTAL spend: ${total_cost:.4f}")
print()

# Per-variant per-task slicing — read cell archives to compute
# per-task means (per [Per-task slicing] memory rule, this is the
# default scoring lens).
import collections
by_vt = collections.defaultdict(list)  # (variant, task) → [primary]
for layer_dir in ARTIFACTS.glob("layer*/B*/*/cell_result.json"):
    try:
        c = json.loads(layer_dir.read_text())
        v = c.get("variant", "?")
        t = c.get("task_id", "?")
        an = c.get("analysis") or {}
        in_s = an.get("diff_in_scope", {})
        in_total = in_s.get("insertions", 0) + in_s.get("deletions", 0)
        if c.get("task_id") == "B1":
            cats = an.get("wrap_categories", {})
            primary = cats.get("on_candidate", 0) * 2 + cats.get("creative", 0)
        else:
            primary = min(in_total, 50)
        by_vt[(v, t)].append(primary)
    except Exception:
        pass

if by_vt:
    print("Per-variant × per-task primary mean (R29-default lens):")
    tasks = sorted({t for _, t in by_vt.keys()})
    variants = sorted({v for v, _ in by_vt.keys()})
    print(f"  {'variant':>20} | " + "  ".join(f"{t:>6}" for t in tasks))
    for v in variants:
        row = []
        for t in tasks:
            vals = by_vt.get((v, t), [])
            if not vals:
                row.append("    -")
            else:
                row.append(f"{sum(vals)/len(vals):>6.1f}")
        print(f"  {v:>20} | " + "  ".join(row))
    print()
PYEOF

# Sentinel: signals that handoff completed; analysis-agent-spawn
# (manager-agent in Phase 2026-05.30 design) can poll for this file
# to know when to fire scour agents.
date -Is > "$SENTINEL"
log "sentinel written: $SENTINEL"

# Stage artifacts (don't commit — operator decides when)
cd "$REPO"
git add "$ARTIFACTS"/*.json "$ARTIFACTS"/HEARTBEAT.jsonl 2>/dev/null || true
log "artifacts staged (operator commits explicitly)"

log "post-round handoff DONE"
exit 0
