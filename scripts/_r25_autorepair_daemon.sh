#!/bin/bash
# R25 auto-repair daemon — monitors a running _round25_dials.py harness
# every 3 min, detects known failure modes, applies fixes without
# prompting user, logs to docs/wiki/2026-05-08-r25-progress.org.
#
# Activation: bash scripts/_r25_autorepair_daemon.sh <PID>
# (or with no PID; daemon will auto-discover via pgrep)
#
# Auto-repair recipes (mirror R25 design doc § Auto-repair recipes):
#   AR1: stuck pod recovery (n/a for harness)
#   AR2: broker re-pin on tool-format silent_noop (live PROVIDER_PINS edit)
#   AR3: worktree storm cleanup
#   AR4: cost circuit-breaker manual override
#   AR5: provider failover on IncompleteRead (in-process; this just logs)
#   AR6: LoRA carryover (separate daemon)
#
# Plus R25-specific:
#   AR7: K7 quality_lint plummet → drop from active variants mid-round
#   AR8: OpenRouter 402 → pause + email/log; trigger credit-watcher
#   AR9: round-cost > 1.5× projection → pause via circuit-breaker file
#   AR10: live-log heartbeat — append round status every poll

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS_DOC=$REPO/docs/wiki/2026-05-08-r25-progress.org
HARNESS=scripts/_round25_dials.py
ART=$REPO/scripts/_round25_dials_artifacts
LIVE=$ART/R25_LIVE.json
COST_CB=$ART/COST_CIRCUIT_BREAKER
WORKTREE_DIR=/home/daniel/repos/org-llm-worktrees
POLL_INTERVAL=180   # 3 min
ROUND_PROJECTION_USD=100  # R25 cap — bumped from $20 per user 2026-05-08
COST_PAUSE_RATIO=1.5
R25_EXPECTED_CELLS=470   # per R25 design § task pool (Layer 1 + 2 + BK + comp + K20 A/B + large-FOSS)

cd "$REPO"

log_line() { echo "[r25-daemon $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_doc() {
    {
        echo
        echo "** [r25-daemon $(date +%H:%M:%S)] $*"
    } >> "$PROGRESS_DOC"
}

# Initialize progress doc
if [ ! -f "$PROGRESS_DOC" ]; then
    cat > "$PROGRESS_DOC" <<EOF
:PROPERTIES:
:ID:       r25-progress
:CREATED:  [2026-05-08]
:END:
#+TITLE: R25 — auto-repair daemon progress log
#+FILETAGS: :wiki:bench:r25:progress:autorepair:
#+OPTIONS: toc:1 num:nil
#+CATEGORY: bench

#+STARTUP: overview content

* Summary

*Summary.* Live progress + auto-repair log for R25. Daemon polls
every $((POLL_INTERVAL / 60)) min, applies AR1-AR10 recipes, logs
each check + each repair action.

*Expanded.* Daemon does NOT block on user prompts. If a known
failure mode is detected, the corresponding AR recipe fires. If
unknown / unrecoverable failure, daemon halts + leaves a status
in the live-log for morning triage.

* Live status checks
EOF
fi

# Find the harness PID (or accept as arg)
discover_pid() {
    local pid="${1:-}"
    if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then
        echo "$pid"; return
    fi
    pgrep -f "_round25_dials.py" | head -1
}

R25_PID=""

ar_check_harness_alive() {
    if [ -z "$R25_PID" ] || ! ps -p "$R25_PID" >/dev/null 2>&1; then
        local new_pid
        new_pid=$(discover_pid)
        if [ -n "$new_pid" ]; then
            R25_PID="$new_pid"
            log_line "discovered R25 PID=$R25_PID"
            return 0
        fi
        return 1
    fi
    return 0
}

ar_get_live_state() {
    if [ ! -f "$LIVE" ]; then echo "{}"; return; fi
    cat "$LIVE"
}

ar_cost() {
    local s
    s=$(ar_get_live_state)
    echo "$s" | jq -r '.spend_usd // .total_cost // 0' 2>/dev/null
}

ar_cells_done() {
    local s
    s=$(ar_get_live_state)
    echo "$s" | jq -r '.cells_done // 0' 2>/dev/null
}

# AR3 — worktree storm cleanup
ar3_worktree_storm() {
    local r25_count
    r25_count=$(ls -d "$WORKTREE_DIR"/r25-* 2>/dev/null | wc -l)
    if [ "$r25_count" -gt 50 ]; then
        log_line "AR3: $r25_count r25 worktrees → pruning"
        log_doc "AR3: worktree storm ($r25_count dirs) — pruning"
        git -C "$REPO" worktree prune --expire=now 2>&1 | head -5
        find "$WORKTREE_DIR" -maxdepth 1 -name "r25-*" -type d -mmin +10 -exec rm -rf {} \; 2>&1 | head -5
        return 0
    fi
    return 1
}

# AR4/AR9 — cost circuit-breaker
ar9_cost_circuit_breaker() {
    local cost
    cost=$(ar_cost)
    local cap
    cap=$(awk "BEGIN { printf \"%.2f\", $ROUND_PROJECTION_USD * $COST_PAUSE_RATIO }")
    if awk "BEGIN { exit ($cost >= $cap) ? 0 : 1 }"; then
        if [ ! -f "$COST_CB" ]; then
            log_line "AR9: cost \$$cost >= circuit cap \$$cap — pausing"
            log_doc "AR9: cost circuit-breaker fired at \$$cost (cap \$$cap)"
            mkdir -p "$ART"
            echo "tripped=$(date +%s) cost=$cost cap=$cap" > "$COST_CB"
            # Note: actual harness pause requires the harness to honor the
            # circuit-breaker file. See R25 patch for honor logic.
        fi
        return 0
    fi
    return 1
}

# AR7 — K7 quality_lint plummet detector
ar7_k7_plummet() {
    local k7_score
    k7_score=$(ar_get_live_state | jq -r '.leaderboard[] | select(.variant == "K7-qwen72b") | .total_score // 0' 2>/dev/null)
    [ -z "$k7_score" ] && return 1
    # Use awk for float compare
    if awk "BEGIN { exit ($k7_score < -50) ? 0 : 1 }"; then
        log_line "AR7: K7 score plummet ($k7_score) — would drop from leaderboard"
        log_doc "AR7: K7-qwen72b score=$k7_score (< -50). Quality_lint penalty hammering. R25 design recommends drop, but mid-round drop requires harness hook. Logged for post-round."
        return 0
    fi
    return 1
}

# AR8 — OpenRouter 402 detector
ar8_openrouter_402() {
    # Detect 402 in stdout
    if [ ! -d "$ART" ]; then return 1; fi
    if tail -50 "$ART"/stdout.log 2>/dev/null | grep -q "HTTP Error 402"; then
        log_line "AR8: OpenRouter 402 detected in stdout"
        log_doc "AR8: HTTP 402 detected — OpenRouter credit cap hit. Triggering credit-watcher."
        # Re-launch credit watcher if not running
        if ! pgrep -f "_openrouter_credit_watcher.sh" >/dev/null 2>&1; then
            nohup bash "$REPO/scripts/_openrouter_credit_watcher.sh" \
                > /tmp/credit-watcher.log 2>&1 &
            disown $! 2>/dev/null
            log_line "credit-watcher restarted by AR8"
        fi
        return 0
    fi
    return 1
}

# AR10 — heartbeat status (with % done + ETA)
ar10_heartbeat() {
    local cells cost top_var top_score elapsed_s pct rate_per_min remaining eta_min eta_str
    cells=$(ar_cells_done)
    cost=$(ar_cost)
    top_var=$(ar_get_live_state | jq -r '.leaderboard[0].variant // "?"' 2>/dev/null)
    top_score=$(ar_get_live_state | jq -r '.leaderboard[0].total_score // "?"' 2>/dev/null)

    # Wall + ETA math
    elapsed_s=$(ps -p "${R25_PID:-0}" -o etimes= 2>/dev/null | xargs)
    [ -z "$elapsed_s" ] && elapsed_s=0
    if [ "$cells" -gt 0 ] && [ "$elapsed_s" -gt 0 ]; then
        pct=$(awk "BEGIN { printf \"%.1f\", ($cells / $R25_EXPECTED_CELLS) * 100 }")
        rate_per_min=$(awk "BEGIN { printf \"%.2f\", $cells / ($elapsed_s / 60) }")
        remaining=$((R25_EXPECTED_CELLS - cells))
        [ "$remaining" -lt 0 ] && remaining=0
        if awk "BEGIN { exit ($rate_per_min > 0) ? 0 : 1 }"; then
            eta_min=$(awk "BEGIN { printf \"%.1f\", $remaining / $rate_per_min }")
            eta_str="${eta_min}min"
        else
            eta_str="unknown"
        fi
    else
        pct="0.0"
        rate_per_min="0"
        eta_str="warming up"
    fi

    # Cells over expected: show "OVER" with negative remaining
    local over_marker=""
    if [ "$cells" -gt "$R25_EXPECTED_CELLS" ]; then
        over_marker=" (OVER expected by $((cells - R25_EXPECTED_CELLS)))"
        pct="100+"
    fi

    log_line "heartbeat cells=${cells}/${R25_EXPECTED_CELLS} pct=${pct}% eta=${eta_str} rate=${rate_per_min}/min cost=\$$cost top=${top_var}@${top_score}"
    {
        echo
        echo "** Status check $(date +%H:%M:%S) — ${pct}% done, ETA ${eta_str}${over_marker}"
        echo
        echo "*Process.* PID ${R25_PID:-?}, $(ps -p ${R25_PID:-0} -o etime= 2>/dev/null | xargs) elapsed"
        echo "*Progress.* ${cells}/${R25_EXPECTED_CELLS} cells = ${pct}%"
        echo "*Throughput.* ${rate_per_min} cells/min"
        echo "*ETA.* ${eta_str}${over_marker}"
        echo "*Spend.* \$$cost (cap \$${ROUND_PROJECTION_USD})"
        echo
        if [ -f "$LIVE" ]; then
            echo "#+begin_src text"
            jq -r '.leaderboard[] | "\(.variant)  cells=\(.cells)  score=\(.total_score)  cost=$\(.total_cost)  cpu=\(.cost_per_unit)  noops=\(.silent_noops)"' "$LIVE" 2>/dev/null
            echo "#+end_src"
        fi
    } >> "$PROGRESS_DOC"
}

# Main loop
log_line "R25 daemon STARTED — polling every ${POLL_INTERVAL}s"
log_doc "Daemon started. PID-arg=${1:-AUTO}."

# Wait for harness to start
WAIT_DEADLINE=$((SECONDS + 1800))   # 30 min wait for harness to launch
while ! ar_check_harness_alive; do
    if [ $SECONDS -gt $WAIT_DEADLINE ]; then
        log_line "no R25 harness found after 30 min — daemon halting"
        log_doc "No R25 harness PID found after 30 min. Daemon halting."
        exit 1
    fi
    sleep 60
done

log_line "R25 harness PID=$R25_PID; entering monitor loop"

ROUND_CONSEC_DEAD=0
while true; do
    if ! ar_check_harness_alive; then
        ROUND_CONSEC_DEAD=$((ROUND_CONSEC_DEAD + 1))
        if [ $ROUND_CONSEC_DEAD -ge 2 ]; then
            log_line "R25 harness DEAD for 2 consecutive checks — exiting"
            log_doc "R25 harness PID is gone. Round complete or crashed."
            break
        fi
    else
        ROUND_CONSEC_DEAD=0
    fi

    ar10_heartbeat
    ar3_worktree_storm || true
    ar7_k7_plummet || true
    ar8_openrouter_402 || true
    ar9_cost_circuit_breaker || true

    sleep "$POLL_INTERVAL"
done

log_line "R25 daemon EXIT"
log_doc "Daemon exit at $(date +%H:%M:%S)."
