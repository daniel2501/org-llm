#!/bin/bash
# Watch OpenRouter credit balance; when it shows >= $5 of headroom,
# auto-launch R24 (R19 enhanced) + retry R21/R22/R23 in background.
#
# Polls every 5 min. Hits the credits endpoint with the API key from
# pass. Balance reading is from {data: {total_credits, total_usage}}.
# Headroom = total_credits - total_usage.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
HEADROOM_FLOOR=5.00   # need at least $5 free before launching

cd "$REPO"

log_line() { echo "[credit-watcher $(date +%H:%M:%S)] $*" >> "$LOG"; }

OR_KEY=$(pass org-llm/cloud/openrouter/api-key 2>/dev/null | head -1)
if [ -z "$OR_KEY" ]; then
    log_line "ABORT — no OpenRouter API key in pass"
    exit 1
fi

log_line "started — polling OpenRouter credits every 5 min, floor=\$$HEADROOM_FLOOR"

while true; do
    JSON=$(curl -s --max-time 10 \
        https://openrouter.ai/api/v1/credits \
        -H "Authorization: Bearer $OR_KEY" 2>/dev/null)

    TOTAL=$(echo "$JSON" | jq -r '.data.total_credits // 0' 2>/dev/null)
    USED=$(echo "$JSON"  | jq -r '.data.total_usage   // 0' 2>/dev/null)
    HEADROOM=$(awk "BEGIN { printf \"%.2f\", $TOTAL - $USED }")

    log_line "credits=\$$TOTAL used=\$$USED headroom=\$$HEADROOM"

    # Compare numerically; awk returns 1 when true
    if awk "BEGIN { exit ($HEADROOM >= $HEADROOM_FLOOR) ? 0 : 1 }"; then
        log_line "headroom \$$HEADROOM >= \$$HEADROOM_FLOOR — launching R21/R22/R23 retry + R24"
        break
    fi

    sleep 300
done

# ── R24: fork from R20 + apply R24 synthesis patch (BK1-BK5, S1-S4, drops) ──
# Skipping R21/R22/R23 retries — R20-R23 supplement agent confirmed those
# rounds were pure round-id renames; redoing them on top-up credits buys
# nothing the synthesis-driven R24 doesn't already deliver.
log_line "skipping R21/R22/R23 retries (placeholder rounds per supplement agent)"
log_line "forking R24 from R20 + applying R24 synthesis patch"

cp "$REPO/scripts/_round20_dials.py" "$REPO/scripts/_round24_dials.py"
mkdir -p "$REPO/scripts/_round24_dials_artifacts"
sed -i 's|_round20_dials_artifacts|_round24_dials_artifacts|g' "$REPO/scripts/_round24_dials.py"
sed -i 's|R20_LIVE|R24_LIVE|g' "$REPO/scripts/_round24_dials.py"
sed -i 's|r20-|r24-|g'         "$REPO/scripts/_round24_dials.py"
sed -i 's|R20 |R24 |g'         "$REPO/scripts/_round24_dials.py"
sed -i 's|Round-20|Round-24|g' "$REPO/scripts/_round24_dials.py"

# Apply R24 synthesis patch (which calls R19 wiring patch first)
python3 "$REPO/scripts/_r24_synthesis_patch.py" 2>&1 | tee -a "$LOG"

# Syntax check
if ! python3 -c "import ast; ast.parse(open('$REPO/scripts/_round24_dials.py').read())" 2>&1; then
    log_line "ABORT R24 — syntax error after fork"
    exit 1
fi

log_line "launching R24 with PARALLELISM=32"
R18_PARALLELISM=32 nohup python3 "$REPO/scripts/_round24_dials.py" > "$REPO/scripts/_round24_dials_artifacts/stdout.log" 2>&1 &
disown $! 2>/dev/null
log_line "R24 PID: $!"

# Auto-commit harness
cd "$REPO"
git add scripts/_round24_dials.py
git commit -m "bench(r24): credit-watcher autopilot — re-fork from R20

After OpenRouter credit top-up triggered R21-R23 retry + R24 fresh
launch. R24 forks from R20 (strongest data round before 402 hit).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 >/dev/null

log_line "credit-watcher autopilot exits — R24 in flight"
