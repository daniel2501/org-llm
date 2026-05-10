#!/bin/bash
# R28 stall watchdog — sidecar that polls HEARTBEAT.jsonl every 15s,
# computes age (now - last-heartbeat-ts), and alerts when current stage
# exceeds its budget.
#
# Per [Fail-fast stage-aware watcher] memory rule: every long-running
# script gets per-stage deadlines + distinct fail-fast signals.
#
# Usage (typically backgrounded by _r29_launch.sh):
#     bash scripts/_r29_watchdog.sh &
#     WATCHDOG_PID=$!
#     trap "kill -KILL $WATCHDOG_PID 2>/dev/null" EXIT
#
# Per [`kill -KILL` not `-TERM` with EXIT trap] memory rule: launcher
# uses kill -KILL to terminate watchdog without firing watchdog's own
# trap (watchdog has no resource teardown — clean exit is cheap).
#
# Alert mechanism (when stall detected):
#     1. tput bel        — audible beep in terminal
#     2. STALL_DETECTED  — JSONL artifact written for postmortem
#     3. LIVE_LOG line   — [STALL] tagged so operator (and Claude
#                          monitoring via emacsclient/peek) sees it
#     4. stderr message  — visible in launch.sh stdout aggregation
#
# Watchdog does NOT kill the parent — it ALERTS only. Operator decides
# whether to abort. Multiple stalls within one stage are logged but
# only the first alerts (avoid bell spam).
#
# Env:
#     ARTIFACTS              — artifact dir (required)
#     R28_WATCHDOG_POLL_S    — poll interval (default 15s)
#     R28_WATCHDOG_LIVE_LOG  — live-log path (default repo wiki)
set -uo pipefail

REPO=/home/daniel/repos/org-llm
ARTIFACTS=${ARTIFACTS:-$REPO/scripts/_round29_dials_artifacts}
LIVE_LOG=${R29_WATCHDOG_LIVE_LOG:-$REPO/docs/wiki/2026-05-08-r18-live-log.org}
HEARTBEAT="$ARTIFACTS/HEARTBEAT.jsonl"
STALL_FILE="$ARTIFACTS/STALL_DETECTED.jsonl"
POLL_S=${R29_WATCHDOG_POLL_S:-15}

# Per-stage budgets (seconds). Default 60s for un-listed stages.
# R29-2 (per r28-walltime §7): CAT 3 K20 budget bumped 480 → 600s.
# R28 observed 486s (97.2% of 480 cap, saved by 15s poll cadence not
# margin). 600s = R28-observed + 25% margin per [Fail-fast stage-aware
# watcher] memory rule's P95 + margin guidance.
declare -A STAGE_BUDGET=(
    [STARTED]=10
    [DESIGN_PATCH]=30
    [PREFLIGHT]=720
    [PREFLIGHT_CAT_1_FUNDING]=30
    [PREFLIGHT_CAT_2_HEALTH]=120
    [PREFLIGHT_CAT_3_K20_RESUME]=600
    [PREFLIGHT_CAT_4_PATCHES]=30
    [PREFLIGHT_CAT_5_RESOURCES]=15
    [PREFLIGHT_CAT_6_CONFIG]=15
    [PREFLIGHT_CAT_7_CONTRACTS]=120
    [DAEMON_LAUNCHED]=15
    [HARNESS_RUNNING]=4500
    [POST_HANDOFF]=120
    [K20_PAUSE]=60
    [DRY_RUN_DONE]=10
    [DONE]=10
)
DEFAULT_BUDGET=60

mkdir -p "$ARTIFACTS"
: > "$STALL_FILE"  # truncate per-launch

ts() { date +%H:%M:%S; }
log_live() {
    echo "[r29-watchdog $(ts)] $*" >> "$LIVE_LOG" 2>/dev/null || true
}

# Track which stage we last alerted on, so we beep once per stage entry
LAST_ALERT_STAGE=""

while true; do
    if [ ! -f "$HEARTBEAT" ]; then
        sleep "$POLL_S"; continue
    fi
    last=$(tail -1 "$HEARTBEAT" 2>/dev/null)
    [ -z "$last" ] && { sleep "$POLL_S"; continue; }

    last_ts=$(echo "$last" | jq -r '.ts // 0' 2>/dev/null)
    last_stage=$(echo "$last" | jq -r '.stage // "UNKNOWN"' 2>/dev/null)
    [ -z "$last_ts" ] || [ "$last_ts" = "0" ] && { sleep "$POLL_S"; continue; }

    now=$(date +%s)
    age=$((now - last_ts))
    budget=${STAGE_BUDGET[$last_stage]:-$DEFAULT_BUDGET}

    if [ "$age" -gt "$budget" ]; then
        if [ "$LAST_ALERT_STAGE" != "$last_stage" ]; then
            # First alert for this stage — beep, log, file
            tput bel 2>/dev/null || printf "\a"
            echo "[r29-watchdog STALL] stage=$last_stage age=${age}s budget=${budget}s" >&2
            log_live "[STALL] stage=$last_stage age=${age}s > budget=${budget}s — investigate"
            jq -n --arg ts "$(date -Is)" \
                  --arg stage "$last_stage" \
                  --argjson age "$age" \
                  --argjson budget "$budget" \
                  '{ts:$ts, stage:$stage, age_s:$age, budget_s:$budget}' \
                  >> "$STALL_FILE" 2>/dev/null || true
            LAST_ALERT_STAGE="$last_stage"
        fi
    else
        # Reset alert flag when we transition stages OR get back under budget
        if [ "$last_stage" != "$LAST_ALERT_STAGE" ]; then
            LAST_ALERT_STAGE=""
        fi
    fi
    sleep "$POLL_S"
done
