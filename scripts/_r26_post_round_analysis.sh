#!/bin/bash
# R26 post-round analysis handoff watcher.
# Mirrors R25's _r25_post_round_handoff.sh but for R26's 4-agent scour pattern.
#
# Polls for R26 harness PID exit. When R26 done:
#   1. Commits any uncommitted R26 artifacts
#   2. Captures final summary stats
#   3. Writes prominent "R26 READY FOR ANALYSIS" block to live-log
#   4. Drops a sentinel file at /tmp/R26_DONE_SPAWN_AGENTS so a Claude
#      session can detect + fire the 4-agent + synthesis + go-forward
#      update agents (each step carries the full agent prompt template).
#   5. Exits.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS=$REPO/docs/wiki/2026-05-08-r26-progress.org
SENTINEL=/tmp/R26_DONE_SPAWN_AGENTS
ART=$REPO/scripts/_round26_dials_artifacts

cd "$REPO"

log_line() { echo "[r26-handoff $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_block() { echo >> "$LOG"; printf '%s\n' "$@" >> "$LOG"; }

R26_PID=$(pgrep -f "python3 scripts/_round26_dials" | head -1)
if [ -z "$R26_PID" ]; then
    log_line "no R26 PID found at watcher start — exiting"
    exit 1
fi

log_line "watching R26 PID=$R26_PID; will fire handoff when round exits"

# Wait until R26 exits (check every 60s)
while ps -p "$R26_PID" >/dev/null 2>&1; do
    sleep 60
done

# Round done — capture stats + commit + sentinel
log_block "" "** [r26-handoff $(date +%H:%M:%S)] R26 EXITED — round complete"

# Capture final summary
SUMMARY=$(ls -t "$ART"/summary-*.json 2>/dev/null | head -1)
LIVE="$ART/R26_LIVE.json"

CELLS="?"; COST="?"; TOP="?"
if [ -n "$SUMMARY" ]; then
    CELLS=$(jq -r '.total_cells // empty' "$SUMMARY" 2>/dev/null)
    COST=$(jq -r '.total_cost_usd // empty' "$SUMMARY" 2>/dev/null)
fi
if [ -f "$LIVE" ]; then
    TOP=$(jq -r '.leaderboard[0] | "\(.variant) score=\(.total_score) cells=\(.cells) $\(.total_cost)"' "$LIVE" 2>/dev/null)
fi

log_line "R26 final: cells=$CELLS cost=\$$COST top=$TOP"

{
    echo
    echo "** R26 COMPLETE — $(date +%H:%M:%S)"
    echo
    echo "*Cells.* $CELLS"
    echo "*Spend.* \$$COST"
    echo "*Top variant.* $TOP"
    echo
    echo "Awaiting Claude-session analysis agent spawn (4 agents + synthesis"
    echo "+ go-forward config update). Sentinel at $SENTINEL."
} >> "$PROGRESS"

# Commit artifacts directory (untracked layer*/ + summary + R26_LIVE)
git add "$ART"/ docs/wiki/2026-05-08-r26-progress.org 2>/dev/null
git commit -m "bench(r26): final artifacts — $CELLS cells, \$$COST spent

R26 round exited. Top: $TOP

Auto-committed by post-round analysis watcher. Awaiting analysis
agent spawn (4-agent scour + synthesis + go-forward config update).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 | tail -2 >> "$LOG"

# Drop sentinel with the agent-spawn checklist + per-agent prompt templates
cat > "$SENTINEL" <<EOF
R26 DONE — $(date +%Y-%m-%dT%H:%M:%S)

Stats:
  cells: $CELLS
  spend: \$$COST
  top:   $TOP

Artifacts:
  $ART/
  $LIVE
  $SUMMARY

═══════════════════════════════════════════════════════════════════════
SPAWN 4 ANALYSIS AGENTS IN PARALLEL (Steps 1-4), then synthesis (5),
then go-forward update (6). Each block below is a copy-paste prompt.
═══════════════════════════════════════════════════════════════════════

──────────────────────────────────────────────────────────────────────
STEP 1 — R26 quality judge
        OUT: docs/notes/2026-05-08-r26-quality-judge.org
──────────────────────────────────────────────────────────────────────
Read $LIVE and $SUMMARY plus all
$ART/layer*/cell-*.json files.

For each task × variant, blind-judge output quality (1-5 score) on:
- correctness of result for the task
- format compliance with the prompt rubric
- absence of refusals / truncations / hallucinated tool calls

Compare per-variant aggregate quality vs R25 baseline (read
docs/notes/2026-05-08-r25-quality-judge.org). Flag any regressions
> 0.3 score points or > 10pp refusal-rate increase.

Output an org-mode file with: *Summary.* + *Expanded.* sections,
per-task score table, top-3 quality wins, top-3 quality regressions,
recommended pin/unpin decisions for go-forward routing.

──────────────────────────────────────────────────────────────────────
STEP 2 — R26 Pareto + cost-quality
        OUT: docs/notes/2026-05-08-r26-pareto.org
──────────────────────────────────────────────────────────────────────
Read $LIVE leaderboard. For every variant compute:
  cost_per_cell    = total_cost / cells
  score_per_dollar = total_score / total_cost
  Pareto rank on (cost ↓, score ↑)

Compare to R25 Pareto frontier (docs/notes/2026-05-08-r25-pareto.org).
Identify:
- new variants on the frontier
- variants that fell off (worse cost/quality after R26 changes)
- the cheapest variant within 5% of top score
- the highest-score variant under \$0.001/cell

Output org-mode with *Summary.* + *Expanded.*, frontier table, ASCII
scatter (cost x-axis, score y-axis), and recommended Sp1-Sp7 strategy
re-ranking based on R26-actual savings.

──────────────────────────────────────────────────────────────────────
STEP 3 — R26 walltime + parallelism
        OUT: docs/notes/2026-05-08-r26-walltime.org
──────────────────────────────────────────────────────────────────────
Read every $ART/layer*/cell-*.json and the
harness $ART/stdout.log.

Compute per-variant: median latency, p95 latency, parallelism
saturation (was R26_PARALLELISM=32 actually exploited?), provider
queue stalls, K20 vs cloud walltime split.

Compare to R25 (docs/notes/2026-05-08-r25-walltime.org). Flag any
variant where p95 > 2× R25 baseline. Recommend parallelism dial for
R27 based on observed saturation curve.

Output org-mode: *Summary.* + *Expanded.*, latency table, saturation
graph (ASCII), bottleneck call-out.

──────────────────────────────────────────────────────────────────────
STEP 4 — R26 provider forensics
        OUT: docs/notes/2026-05-08-r26-providers.org
──────────────────────────────────────────────────────────────────────
Read $ART/cell_replay.jsonl,
LIVE_DENY_LIST (if present), and PROVIDER_PINS state from
$LIVE.

For each provider/endpoint: cell count, success rate, retry rate,
deny-list trips, pin-hold rate, cost share, quality contribution.
Compare to R25 (docs/notes/2026-05-08-r25-providers.org).

Flag any provider that:
- crossed deny threshold during R26
- broke a previous R25 pin
- showed > 20% quality regression on its pinned tasks

Output org-mode: *Summary.* + *Expanded.*, provider table,
recommended pin/unpin/deny actions for R27 PROVIDER_PINS config.

──────────────────────────────────────────────────────────────────────
STEP 5 — R26 4-agent synthesis
        OUT: docs/wiki/2026-05-08-r26-synthesis.org
──────────────────────────────────────────────────────────────────────
After steps 1-4 complete, read all four output files:
  docs/notes/2026-05-08-r26-quality-judge.org
  docs/notes/2026-05-08-r26-pareto.org
  docs/notes/2026-05-08-r26-walltime.org
  docs/notes/2026-05-08-r26-providers.org

Cross-reference with R25 synthesis (docs/wiki/2026-05-08-r25-synthesis.org).

Produce an org-mode wiki entry with:
- *Summary.* (1-3 sentences) + *Expanded.* per Rule 2b
- "What R26 changed vs R25" — concrete deltas only
- "Open contradictions" — places where the 4 agents disagree
- "Locked-in findings" — claims supported by ≥3 of the 4 agents
- "R27 design hooks" — 3-5 dials worth turning next round
- Cost/quality/walltime/provider headline numbers in a single table

──────────────────────────────────────────────────────────────────────
STEP 6 — Update go-forward config doc
        FILE: docs/wiki/2026-05-08-org-llm-go-forward-config.org
──────────────────────────────────────────────────────────────────────
Read current go-forward doc + the R26 synthesis from step 5.
Update sections:
- Section 1 (Bridge Crew): incorporate R26 K8/K2/K20 routing changes
- Section 2 (specialization): update task winners with R26 leaders
- Section 3 (hardware): update PROVIDER_PINS pin-hold rate from R26
- Section 5 (strategy): re-rank Sp1-Sp7 by R26-actual savings
- Section 7 (cost): R26 spend folded into cumulative
- Open questions: mark answered ones; add any new ones surfaced by R26

Edit-in-place; preserve *Summary.*/*Expanded.* style; update only
sections with new R26 evidence.
EOF

log_line "sentinel written to $SENTINEL — handoff watcher exit"
