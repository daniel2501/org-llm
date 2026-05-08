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

# AR3 — worktree storm cleanup (proactive, not reactive)
# Lowered threshold per R25 mid-run audit: cleanup at 20 instead of 50,
# and prune any r25-* worktree dir older than 5 min unconditionally.
ar3_worktree_storm() {
    local r25_count pruned
    r25_count=$(ls -d "$WORKTREE_DIR"/r25-* 2>/dev/null | wc -l)
    pruned=0
    # Always prune stale dirs older than 5 min
    if [ "$r25_count" -gt 0 ]; then
        pruned=$(find "$WORKTREE_DIR" -maxdepth 1 -name "r25-*" -type d -mmin +5 2>/dev/null | wc -l)
        if [ "$pruned" -gt 0 ]; then
            git -C "$REPO" worktree prune --expire=5.minutes.ago 2>&1 | head -3
            find "$WORKTREE_DIR" -maxdepth 1 -name "r25-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null
            log_line "AR3: pruned $pruned stale r25-* worktrees (was $r25_count)"
        fi
    fi
    if [ "$r25_count" -gt 20 ]; then
        log_doc "AR3: worktree count $r25_count > 20 — aggressive prune"
    fi
    return 0
}

# AR11 — variant fail-rate signal (NEW R25 mid-run upgrade)
# Detects when a variant has many wall_cap kills, silent_noops, or
# score=0 cells. Cannot stop the variant mid-run (no harness hook)
# but surfaces the signal prominently so post-round triage is fast.
ar11_variant_fail_rate() {
    local stdout="$ART/stdout.log"
    [ ! -f "$stdout" ] && return 1
    # WALL_CAP_KILLED concentration
    local wcaps_total wcaps_k2 wcaps_k1
    wcaps_total=$(grep -c "WALL_CAP_KILLED" "$stdout" 2>/dev/null)
    wcaps_k2=$(grep "WALL_CAP_KILLED" "$stdout" 2>/dev/null | grep -c "K2-")
    wcaps_k1=$(grep "WALL_CAP_KILLED" "$stdout" 2>/dev/null | grep -c "K1-")
    if [ "$wcaps_total" -ge 5 ]; then
        local pct_k2
        pct_k2=$(awk "BEGIN { printf \"%.0f\", ($wcaps_k2 * 100) / $wcaps_total }")
        log_line "AR11: $wcaps_total WALL_CAP kills, ${pct_k2}% on K2"
        if [ "$pct_k2" -ge 70 ]; then
            log_doc "AR11: K2 WALL_CAP kill rate ${pct_k2}% of $wcaps_total kills — broker swap candidate for R26 (try Modal route)"
        fi
    fi

    # K33/K34/K35 ceiling-probe abort signal
    local probe_zeros
    for variant in K33 K34 K35; do
        local cells_n
        cells_n=$(jq -r ".leaderboard[] | select(.variant | startswith(\"${variant}-\")) | .cells // 0" "$LIVE" 2>/dev/null | head -1)
        local score_n
        score_n=$(jq -r ".leaderboard[] | select(.variant | startswith(\"${variant}-\")) | .total_score // 0" "$LIVE" 2>/dev/null | head -1)
        if [ -n "$cells_n" ] && [ "$cells_n" -ge 3 ] && \
           awk "BEGIN { exit ($score_n / $cells_n < 1.0) ? 0 : 1 }"; then
            log_doc "AR11: $variant ceiling probe — $cells_n cells / score $score_n (mean<1) → likely uncompetitive; abort recommended for R26"
        fi
    done
    return 0
}

# AR12 — OpenRouter headroom proactive monitor
# G9 cost circuit-breaker fires at $cap × 1.5 = $150 (current).
# But OR headroom may be the actual binding constraint. If headroom
# drops below 1× projected remaining round cost, log warning.
ar12_or_headroom() {
    local or_key headroom remaining_cells avg_cost projected_remaining
    or_key=$(pass org-llm/cloud/openrouter/api-key 2>/dev/null | head -1)
    [ -z "$or_key" ] && return 1
    headroom=$(curl -sS --max-time 5 https://openrouter.ai/api/v1/credits \
        -H "Authorization: Bearer $or_key" 2>/dev/null \
        | jq -r '.data.total_credits - .data.total_usage' 2>/dev/null)
    [ -z "$headroom" ] && return 1
    local cells cost
    cells=$(ar_cells_done)
    cost=$(ar_cost)
    if [ "$cells" -gt 10 ]; then
        avg_cost=$(awk "BEGIN { printf \"%.5f\", $cost / $cells }")
        remaining_cells=$((R25_EXPECTED_CELLS - cells))
        [ "$remaining_cells" -lt 0 ] && remaining_cells=0
        projected_remaining=$(awk "BEGIN { printf \"%.2f\", $avg_cost * $remaining_cells }")
        log_line "AR12: OR headroom \$$headroom; projected remaining cost \$$projected_remaining"
        if awk "BEGIN { exit ($headroom < $projected_remaining) ? 0 : 1 }"; then
            log_doc "AR12: OR headroom \$$headroom < projected remaining \$$projected_remaining — top-up may be needed before round completes"
            # Pre-emptively (re)launch credit-watcher so it'll auto-resume on top-up
            if ! pgrep -f "_openrouter_credit_watcher.sh" >/dev/null 2>&1; then
                nohup bash "$REPO/scripts/_openrouter_credit_watcher.sh" \
                    > /tmp/credit-watcher.log 2>&1 &
                disown $! 2>/dev/null
                log_line "AR12: pre-emptively restarted credit-watcher"
            fi
        fi
    fi
    return 0
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
    ar11_variant_fail_rate || true
    ar12_or_headroom || true

    sleep "$POLL_INTERVAL"
done

log_line "R25 daemon EXIT"
log_doc "Daemon exit at $(date +%H:%M:%S)."
