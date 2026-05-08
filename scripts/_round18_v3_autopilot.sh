#!/bin/bash
# R18 v3 autopilot — fires after v2 finishes; applies broker fixes; relaunches.
# Runs unattended overnight.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
HARNESS=$REPO/scripts/_round18_dials.py
ART=$REPO/scripts/_round18_dials_artifacts
V2_PID="${V2_PID:-1038443}"

cd "$REPO"

log_line() { echo "[autopilot $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_block() { echo >> "$LOG"; printf '%s\n' "$@" >> "$LOG"; }

log_block "" "** [autopilot] watching v2 PID $V2_PID for exit"

# Wait for v2 to exit
while ps -p "$V2_PID" >/dev/null 2>&1; do
    sleep 60
done

log_block "" "** [autopilot $(date +%H:%M:%S)] v2 EXITED — preparing v3"

# Capture v2 summary stats
V2_SUMMARY=$(ls -t "$ART"/summary-*.json 2>/dev/null | head -1)
if [ -n "$V2_SUMMARY" ]; then
    V2_CELLS=$(jq -r '.total_cells // (.cells | length) // empty' "$V2_SUMMARY" 2>/dev/null || echo "?")
    V2_COST=$(jq -r '.total_cost_usd // empty' "$V2_SUMMARY" 2>/dev/null || echo "?")
    log_line "v2 final: cells=$V2_CELLS cost=\$$V2_COST"
else
    log_line "v2 no summary file found — process may have errored"
fi

# Snapshot v2 artifacts to a v2/ subdir (preserves data; v3 starts fresh)
mkdir -p "$ART/v2"
mv "$ART"/{layer1,layer2,layer3_*,log-*.txt,stdout.log,R18_LIVE.json,summary-*.json} "$ART/v2/" 2>/dev/null || true

log_line "v2 artifacts moved to $ART/v2/"

# Apply v3 fixes to harness (PROVIDER_PINS)
# Fix 1: K2-kimi-k2.6 — DeepInfra doesn't tool-translate; use Moonshot
# Fix 2: K17-glm46 — SiliconFlow may have similar tool-format issues; try Z-AI native
# Fix 3: K15-kimi-thinking — Novita may also have tool issues; use Moonshot

python3 <<'PYFIX'
import re, sys
p = "/home/daniel/repos/org-llm/scripts/_round18_dials.py"
src = open(p).read()
# Re-pin K2 from DeepInfra to Moonshot (fixes R18 v2 tool-format failure)
src = src.replace(
    '"moonshotai/kimi-k2.6":               {"order": ["DeepInfra"]}',
    '"moonshotai/kimi-k2.6":               {"order": ["Moonshot", "Parasail"]}',
)
# Re-pin K15 to Moonshot first (Novita unreliable for tool format)
src = src.replace(
    '"moonshotai/kimi-k2-thinking":        {"order": ["Novita"]}',
    '"moonshotai/kimi-k2-thinking":        {"order": ["Moonshot", "Novita"]}',
)
# Re-pin K17 (glm-4.6) — try Z-AI native first, SiliconFlow fallback
src = src.replace(
    '"z-ai/glm-4.6":                       {"order": ["SiliconFlow"]}',
    '"z-ai/glm-4.6":                       {"order": ["Z-AI", "SiliconFlow"]}',
)
open(p, "w").write(src)
print("v3 broker fixes applied")
PYFIX
log_line "harness PROVIDER_PINS updated for K2/K15/K17"

# Verify syntax
python3 -c "import ast; ast.parse(open('$HARNESS').read())" 2>&1 | tee -a "$LOG"

# Launch v3
log_block "" "** [autopilot $(date +%H:%M:%S)] launching R18 v3"
nohup python3 "$HARNESS" > "$ART/stdout.log" 2>&1 &
V3_PID=$!
log_line "v3 PID: $V3_PID"

# Restart watcher pointing at v3
(
  while ps -p "$V3_PID" >/dev/null 2>&1; do
    sleep 600
    if ! ps -p "$V3_PID" >/dev/null 2>&1; then break; fi
    {
      echo
      echo "** Status check $(date +%H:%M:%S) (R18 v3)"
      echo
      echo "*Process.* PID $V3_PID, $(ps -p $V3_PID -o etime= 2>/dev/null | xargs) elapsed"
      LIVE=$ART/R18_LIVE.json
      if [ -f "$LIVE" ]; then
        DONE=$(jq -r '.cells_done' "$LIVE" 2>/dev/null)
        echo "*Cells done.* $DONE"
        echo
        echo "#+begin_src text"
        jq -r '.leaderboard[] | "\(.variant)  cells=\(.cells)  score=\(.total_score)  cost=$\(.total_cost)  cpu=\(.cost_per_unit)  noops=\(.silent_noops)"' "$LIVE" 2>/dev/null
        echo "#+end_src"
      fi
    } >> "$LOG"
  done
  {
    echo
    echo "** R18 v3 COMPLETE — $(date +%H:%M:%S)"
    echo
    echo "#+begin_src text"
    tail -30 "$ART/stdout.log" 2>/dev/null
    echo "#+end_src"
  } >> "$LOG"
) &
WATCHER_PID=$!
log_line "v3 watcher PID: $WATCHER_PID"

log_block "" "** [autopilot] handed off to v3 + watcher; autopilot exits"
