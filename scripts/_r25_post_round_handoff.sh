#!/bin/bash
# R25 post-round handoff watcher.
# Polls for R25 harness PID exit. When R25 done:
#   1. Commits any uncommitted R25 artifacts
#   2. Captures final summary stats
#   3. Writes prominent "R25 READY FOR ANALYSIS" block to live-log
#   4. Drops a sentinel file at /tmp/R25_DONE_SPAWN_AGENTS so a Claude
#      session can detect + fire the 4-agent + synthesis + go-forward
#      update agents.
#   5. Exits.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS=$REPO/docs/wiki/2026-05-08-r25-progress.org
SENTINEL=/tmp/R25_DONE_SPAWN_AGENTS
ART=$REPO/scripts/_round25_dials_artifacts

cd "$REPO"

log_line() { echo "[r25-handoff $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_block() { echo >> "$LOG"; printf '%s\n' "$@" >> "$LOG"; }

R25_PID=$(pgrep -f "python3 scripts/_round25_dials" | head -1)
if [ -z "$R25_PID" ]; then
    log_line "no R25 PID found at watcher start — exiting"
    exit 1
fi

log_line "watching R25 PID=$R25_PID; will fire handoff when round exits"

# Wait until R25 exits (check every 60s)
while ps -p "$R25_PID" >/dev/null 2>&1; do
    sleep 60
done

# Round done — capture stats + commit + sentinel
log_block "" "** [r25-handoff $(date +%H:%M:%S)] R25 EXITED — round complete"

# Capture final summary
SUMMARY=$(ls -t "$ART"/summary-*.json 2>/dev/null | head -1)
LIVE="$ART/R25_LIVE.json"

CELLS="?"; COST="?"; TOP="?"
if [ -n "$SUMMARY" ]; then
    CELLS=$(jq -r '.total_cells // empty' "$SUMMARY" 2>/dev/null)
    COST=$(jq -r '.total_cost_usd // empty' "$SUMMARY" 2>/dev/null)
fi
if [ -f "$LIVE" ]; then
    TOP=$(jq -r '.leaderboard[0] | "\(.variant) score=\(.total_score) cells=\(.cells) $\(.total_cost)"' "$LIVE" 2>/dev/null)
fi

log_line "R25 final: cells=$CELLS cost=\$$COST top=$TOP"

{
    echo
    echo "** R25 COMPLETE — $(date +%H:%M:%S)"
    echo
    echo "*Cells.* $CELLS"
    echo "*Spend.* \$$COST"
    echo "*Top variant.* $TOP"
    echo
    echo "Awaiting Claude-session analysis agent spawn (4 agents + synthesis"
    echo "+ go-forward config update). Sentinel at $SENTINEL."
} >> "$PROGRESS"

# Commit artifacts directory (untracked layer*/ + summary + R25_LIVE)
git add "$ART"/ docs/wiki/2026-05-08-r25-progress.org 2>/dev/null
git commit -m "bench(r25): final artifacts — $CELLS cells, \$$COST spent

R25 round exited. Top: $TOP

Auto-committed by post-round handoff watcher. Awaiting analysis
agent spawn (4-agent + synthesis + go-forward config update).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 | tail -2 >> "$LOG"

# Drop sentinel with the agent-spawn checklist
cat > "$SENTINEL" <<EOF
R25 DONE — $(date +%Y-%m-%dT%H:%M:%S)

Stats:
  cells: $CELLS
  spend: \$$COST
  top:   $TOP

Spawn these agents (in parallel, all writing to docs/notes/):
  1. R25 quality judge        → docs/notes/2026-05-08-r25-quality-judge.org
  2. R25 Pareto + cost-quality → docs/notes/2026-05-08-r25-pareto.org
  3. R25 walltime + parallelism → docs/notes/2026-05-08-r25-walltime.org
  4. R25 provider forensics    → docs/notes/2026-05-08-r25-providers.org

Then spawn synthesis agent:
  5. R25 4-agent synthesis    → docs/wiki/2026-05-08-r25-synthesis.org

Then spawn go-forward doc updater:
  6. Update docs/wiki/2026-05-08-org-llm-go-forward-config.org
     with R25 findings:
     - Section 1 (Bridge Crew): update if K8 generalized or K2 raw-q changed
     - Section 2 (specialization): update with R25 task winners
     - Section 3 (hardware): update with PROVIDER_PINS pin-hold rate
     - Section 5 (strategy): re-rank Sp1-Sp7 by R25-actual savings
     - Section 7 (cost): R25 spend folded into cumulative
     - Open questions: mark Q1/Q3/Q4 answered with R25 data
EOF

log_line "sentinel written to $SENTINEL — handoff watcher exit"
