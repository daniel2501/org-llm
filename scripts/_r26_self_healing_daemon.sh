#!/bin/bash
# R26 self-healing daemon — extends the R25 auto-repair model into a
# control plane. Per docs/wiki/2026-05-08-r26-boost-plan.org §12.
#
# State machine: MONITORING → DETECTING → FIXING → RELAUNCHING.
# 60s in-round cadence, 5min between-round cadence.
#
# File contracts (with harness, P1-11/P1-12/P1-13):
#   $ARTIFACTS/COST_CIRCUIT_BREAKER  — daemon writes; harness aborts
#                                       layer when present (P1-11)
#   $ARTIFACTS/LIVE_DENY_LIST        — daemon writes JSONL; harness
#                                       filters (variant, broker) at
#                                       cell-spec construction (P1-12)
#   $ARTIFACTS/cell_replay.jsonl     — harness writes per-cell line;
#                                       daemon reads on round-crash to
#                                       identify pending cells (P1-13)
#
# Catches:
#   - Cell-level: per-(variant, broker) consecutive failures ≥3 →
#     LIVE_DENY_LIST entry
#   - Round-level: cumulative spend > 1.5× projection →
#     COST_CIRCUIT_BREAKER
#   - Crash recovery: harness PID dies mid-round + cells incomplete →
#     read cell_replay.jsonl + spawn resumption mini-round of just the
#     missing cells
#
# Auto-action cap: $5/round. After cap, daemon flips to advisory-only
# (logs to progress doc instead of writing sentinels).
#
# Activation:
#   bash scripts/_r26_self_healing_daemon.sh [PID]
#   (no PID → auto-discover via pgrep)

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS_DOC=$REPO/docs/wiki/2026-05-08-r26-progress.org
HARNESS=scripts/_round26_dials.py
ART=$REPO/scripts/_round26_dials_artifacts
LIVE=$ART/R26_LIVE.json
CELL_REPLAY=$ART/cell_replay.jsonl
COST_CB=$ART/COST_CIRCUIT_BREAKER
LIVE_DENY=$ART/LIVE_DENY_LIST
SM_STATE=/tmp/r26-self-healing-state
COST_LEDGER=$ART/r26_self_healing_cost_ledger.jsonl

POLL_INTERVAL_INROUND=60       # 60s in-round
POLL_INTERVAL_BETWEEN=300      # 5min between rounds
ROUND_PROJECTION_USD=20        # R26 envelope target (boost-plan §11)
COST_PAUSE_RATIO=1.5           # > 1.5× projection trips circuit-breaker
AUTO_ACTION_CAP_USD=5.00       # $5/round of remediation spend
RELAUNCH_CAP=3                 # max 3 relaunches per round
CONSEC_FAIL_THRESHOLD=3        # 3+ consecutive failures → deny entry
R26_EXPECTED_CELLS=470         # carried from R25 daemon — adjust for R26

mkdir -p "$ART"

# ── Helpers ─────────────────────────────────────────────────────────────
log_line() {
    local msg="[r26-daemon $(date +%H:%M:%S)] $*"
    echo "$msg" >> "$LOG" 2>/dev/null || true
    echo "$msg"
}

log_doc() {
    {
        echo
        echo "** [r26-daemon $(date +%H:%M:%S)] $*"
    } >> "$PROGRESS_DOC" 2>/dev/null || true
}

# Initialize progress doc
if [ ! -f "$PROGRESS_DOC" ]; then
    cat > "$PROGRESS_DOC" <<EOF
:PROPERTIES:
:ID:       r26-progress
:CREATED:  [2026-05-08]
:END:
#+TITLE: R26 — self-healing daemon progress log
#+FILETAGS: :wiki:bench:r26:progress:autorepair:
#+OPTIONS: toc:1 num:nil
#+CATEGORY: bench

#+STARTUP: overview content

* Summary

*Summary.* Live state-machine log for R26's self-healing daemon.
States: MONITORING → DETECTING → FIXING → RELAUNCHING. 60s in-round
cadence, 5min between-round. Per docs/wiki/2026-05-08-r26-boost-plan.org §12.

*Expanded.* The R25 daemon was a passive monitor — wrote advice the
harness ignored. R26 inverts the relationship. Daemon writes COST_
CIRCUIT_BREAKER and LIVE_DENY_LIST; harness reads them per cell.
Crash recovery uses cell_replay.jsonl: if PID dies mid-round, daemon
identifies completed cells, builds a "pending" cell-list, spawns a
resumption mini-round.

* State transitions
EOF
fi

# ── State machine ───────────────────────────────────────────────────────
sm_get_state() {
    if [ -f "$SM_STATE" ]; then cat "$SM_STATE"; else echo "MONITORING"; fi
}

sm_set_state() {
    local new_state=$1
    local old_state
    old_state=$(sm_get_state)
    echo "$new_state" > "$SM_STATE"
    if [ "$old_state" != "$new_state" ]; then
        log_line "STATE $old_state → $new_state"
        log_doc "STATE transition $old_state → $new_state"
    fi
}

# ── Cost ledger ─────────────────────────────────────────────────────────
ledger_total_spent() {
    if [ ! -f "$COST_LEDGER" ]; then echo "0"; return; fi
    jq -s 'map(.cost_usd // 0) | add // 0' "$COST_LEDGER" 2>/dev/null || echo "0"
}

ledger_record() {
    local action=$1 cost_usd=$2 detail=$3
    local entry
    entry=$(jq -nc --arg ts "$(date -Iseconds)" \
                  --arg action "$action" \
                  --argjson cost "$cost_usd" \
                  --arg detail "$detail" \
                  '{ts: $ts, action: $action, cost_usd: $cost, detail: $detail}')
    echo "$entry" >> "$COST_LEDGER"
}

ledger_can_act() {
    local would_cost=$1
    local spent
    spent=$(ledger_total_spent)
    awk "BEGIN { exit ($spent + $would_cost > $AUTO_ACTION_CAP_USD) ? 1 : 0 }"
}

# ── Harness PID discovery ───────────────────────────────────────────────
discover_pid() {
    local pid="${1:-}"
    if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then
        echo "$pid"; return
    fi
    pgrep -f "_round26_dials.py" | head -1
}

R26_PID=""

is_harness_alive() {
    if [ -z "$R26_PID" ] || ! ps -p "$R26_PID" >/dev/null 2>&1; then
        local new_pid
        new_pid=$(discover_pid)
        if [ -n "$new_pid" ]; then
            R26_PID="$new_pid"
            return 0
        fi
        return 1
    fi
    return 0
}

# ── Live state read ─────────────────────────────────────────────────────
live_cost() {
    if [ ! -f "$LIVE" ]; then echo "0"; return; fi
    jq -r '.spend_usd // .total_cost // (.cells_done * 0.05) // 0' "$LIVE" 2>/dev/null || echo "0"
}

live_cells_done() {
    if [ ! -f "$LIVE" ]; then echo "0"; return; fi
    jq -r '.cells_done // 0' "$LIVE" 2>/dev/null || echo "0"
}

# ── DETECTING ───────────────────────────────────────────────────────────
# Detect 1: per-(variant, broker) consecutive-failure streaks ≥ N.
# Reads cell_replay.jsonl, scans tail per (variant, broker), if last 3
# entries are non-OK, return the (variant, broker) pair.
detect_failing_combos() {
    [ ! -f "$CELL_REPLAY" ] && return 0
    # For each unique (variant, broker), check if last $CONSEC_FAIL_THRESHOLD
    # entries are all non-OK. Print one "variant<TAB>broker<TAB>reason" line
    # per failing combo.
    local combos
    combos=$(jq -r '.variant_name + "\t" + (.broker // "openrouter")' \
        "$CELL_REPLAY" 2>/dev/null | sort -u)
    while IFS=$'\t' read -r variant broker; do
        [ -z "$variant" ] && continue
        local last_n_statuses
        last_n_statuses=$(jq -r --arg v "$variant" --arg b "$broker" \
            'select(.variant_name == $v and (.broker // "openrouter") == $b) | .status' \
            "$CELL_REPLAY" 2>/dev/null | tail -"$CONSEC_FAIL_THRESHOLD")
        local n
        n=$(echo "$last_n_statuses" | grep -cv '^$' || true)
        if [ "$n" -ge "$CONSEC_FAIL_THRESHOLD" ]; then
            local n_ok
            n_ok=$(echo "$last_n_statuses" | grep -c '^ok$' || true)
            if [ "$n_ok" -eq 0 ]; then
                local reason
                reason=$(echo "$last_n_statuses" | sort | uniq -c | sort -rn | head -1 \
                    | awk '{print "consecutive_" $2}')
                printf '%s\t%s\t%s\n' "$variant" "$broker" "$reason"
            fi
        fi
    done <<< "$combos"
    return 0
}

# Detect 2: round-level cost overage.
detect_cost_overage() {
    local cost cap
    cost=$(live_cost)
    cap=$(awk "BEGIN { printf \"%.2f\", $ROUND_PROJECTION_USD * $COST_PAUSE_RATIO }")
    if awk "BEGIN { exit ($cost >= $cap) ? 0 : 1 }"; then
        echo "cost=$cost cap=$cap"
        return 0
    fi
    return 1
}

# Detect 3: crash. Harness PID is gone but cells_done < expected × 0.95.
detect_crash() {
    is_harness_alive && return 1
    local cells expected_floor
    cells=$(live_cells_done)
    expected_floor=$(awk "BEGIN { printf \"%d\", $R26_EXPECTED_CELLS * 0.95 }")
    if [ "$cells" -lt "$expected_floor" ]; then
        echo "cells_done=$cells expected_floor=$expected_floor"
        return 0
    fi
    return 1
}

# ── FIXING ──────────────────────────────────────────────────────────────
# Fix 1: write LIVE_DENY_LIST entries for failing combos.
fix_deny_failing_combos() {
    local failing
    failing=$(detect_failing_combos)
    [ -z "$failing" ] && return 1
    if ! ledger_can_act 0.0; then
        log_doc "fix_deny_failing_combos: cap reached, advisory-only"
        log_line "advisory: would deny [$failing]"
        return 1
    fi
    local count=0
    while IFS=$'\t' read -r variant broker reason; do
        [ -z "$variant" ] && continue
        # Skip if already denied (idempotent)
        if [ -f "$LIVE_DENY" ] && \
           jq -e --arg v "$variant" --arg b "$broker" \
              'select(.variant == $v and .broker == $b)' \
              "$LIVE_DENY" >/dev/null 2>&1; then
            continue
        fi
        local entry
        entry=$(jq -nc --arg v "$variant" --arg b "$broker" --arg r "$reason" \
            --arg ts "$(date -Iseconds)" \
            '{variant: $v, broker: $b, reason: $r, added_at: $ts}')
        echo "$entry" >> "$LIVE_DENY"
        log_line "DENY $variant/$broker ($reason)"
        log_doc "FIX deny added: $variant/$broker — $reason"
        ledger_record "deny_combo" 0 "$variant/$broker"
        count=$((count + 1))
    done <<< "$failing"
    [ "$count" -gt 0 ] && return 0
    return 1
}

# Fix 2: write COST_CIRCUIT_BREAKER if round cost > 1.5× projection.
fix_cost_circuit_breaker() {
    local detail
    detail=$(detect_cost_overage) || return 1
    if [ -f "$COST_CB" ]; then
        # Already tripped
        return 1
    fi
    if ! ledger_can_act 0.0; then
        log_doc "fix_cost_circuit_breaker: cap reached, advisory-only ($detail)"
        return 1
    fi
    {
        echo "tripped_at=$(date -Iseconds)"
        echo "$detail"
        echo "ratio=$COST_PAUSE_RATIO"
    } > "$COST_CB"
    log_line "COST_CIRCUIT_BREAKER tripped — $detail"
    log_doc "FIX cost-circuit-breaker tripped — $detail"
    ledger_record "cost_circuit_breaker" 0 "$detail"
    return 0
}

# ── RELAUNCHING ─────────────────────────────────────────────────────────
# Crash recovery: read cell_replay.jsonl, identify pending cells from
# the harness's planned roster, spawn a resumption mini-round.
#
# This implementation surfaces the pending-cells set and emits a
# resumption marker file ($ART/RESUME_PENDING.json). The actual
# resumption mini-round is launched by `_r26_launch.sh` reading the
# marker on next start. (Full state-machine RELAUNCHING does the
# spawn directly when the user opts in via R26_AUTO_RELAUNCH=1.)
relaunch_from_replay() {
    local relaunches_done
    relaunches_done=$(grep -c '"action":"relaunch"' "$COST_LEDGER" 2>/dev/null || echo 0)
    if [ "$relaunches_done" -ge "$RELAUNCH_CAP" ]; then
        log_doc "relaunch: cap reached ($RELAUNCH_CAP) — escalating to sentinel"
        echo "exhausted_at=$(date -Iseconds)" > "$ART/RELAUNCH_EXHAUSTED"
        return 1
    fi
    if [ ! -f "$CELL_REPLAY" ]; then
        log_line "relaunch: no cell_replay.jsonl — cannot resume"
        return 1
    fi
    local n_ok n_total
    n_ok=$(jq -r 'select(.status == "ok") | .task_id' "$CELL_REPLAY" 2>/dev/null | wc -l)
    n_total=$(wc -l < "$CELL_REPLAY")
    log_line "relaunch: $n_ok/$n_total cells OK in replay; building resume marker"
    # Emit pending-cells marker. The harness consumes this via
    # _completed_cells_from_replay() helper.
    local marker="$ART/RESUME_PENDING.json"
    jq -s --arg ts "$(date -Iseconds)" \
        '{generated_at: $ts,
          completed: [.[] | select(.status == "ok")
                      | {task_id, variant_name, dial_label, layer}],
          all_seen: [.[] | {task_id, variant_name, dial_label, layer, status}]}' \
        "$CELL_REPLAY" > "$marker" 2>/dev/null
    log_line "relaunch: wrote $marker ($n_ok completed entries)"
    log_doc "RELAUNCH marker written — $n_ok cells completed; resumption mini-round
ready for _r26_launch.sh to consume"
    ledger_record "relaunch" 0 "ok=$n_ok total=$n_total"
    # Optional: auto-spawn if R26_AUTO_RELAUNCH=1
    if [ "${R26_AUTO_RELAUNCH:-0}" = "1" ]; then
        log_line "R26_AUTO_RELAUNCH=1 — spawning resumption harness"
        nohup python3 "$REPO/$HARNESS" \
            > "$ART/resumption-stdout.log" 2>&1 &
        disown $! 2>/dev/null
    fi
    return 0
}

# ── Heartbeat ────────────────────────────────────────────────────────────
heartbeat() {
    local cells cost top_var top_score state
    cells=$(live_cells_done)
    cost=$(live_cost)
    state=$(sm_get_state)
    top_var=$(jq -r '.leaderboard[0].variant // "?"' "$LIVE" 2>/dev/null)
    top_score=$(jq -r '.leaderboard[0].total_score // "?"' "$LIVE" 2>/dev/null)
    log_line "[$state] cells=$cells cost=\$$cost top=${top_var}@${top_score}"
}

# ── Main loop ───────────────────────────────────────────────────────────
log_line "R26 self-healing daemon STARTED — in-round poll ${POLL_INTERVAL_INROUND}s"
log_doc "Daemon started. PID-arg=${1:-AUTO}. Cap \$$AUTO_ACTION_CAP_USD/round."

# Wait for harness to start (up to 30 min)
WAIT_DEADLINE=$((SECONDS + 1800))
while ! is_harness_alive; do
    if [ $SECONDS -gt $WAIT_DEADLINE ]; then
        log_line "no R26 harness found after 30 min — switching to between-round cadence"
        sm_set_state "MONITORING"
        break
    fi
    sleep 60
done

if is_harness_alive; then
    log_line "R26 harness PID=$R26_PID; entering monitor loop"
fi

DEAD_COUNT=0
while true; do
    state=$(sm_get_state)

    # Always check for crash even in MONITORING
    if ! is_harness_alive; then
        DEAD_COUNT=$((DEAD_COUNT + 1))
        if [ $DEAD_COUNT -ge 2 ]; then
            sm_set_state "DETECTING"
            if detect_crash >/dev/null 2>&1; then
                sm_set_state "RELAUNCHING"
                relaunch_from_replay || true
                sm_set_state "MONITORING"
            else
                # Round complete normally
                log_line "R26 harness gone, expected cell count met — daemon exit"
                log_doc "Daemon exit: round complete."
                break
            fi
            sleep "$POLL_INTERVAL_BETWEEN"
            continue
        fi
    else
        DEAD_COUNT=0
    fi

    case "$state" in
        MONITORING)
            heartbeat
            sm_set_state "DETECTING"
            ;;
        DETECTING)
            # Check signals; transition to FIXING if any trip
            if detect_cost_overage >/dev/null 2>&1; then
                sm_set_state "FIXING"
            elif [ -n "$(detect_failing_combos)" ]; then
                sm_set_state "FIXING"
            else
                sm_set_state "MONITORING"
            fi
            ;;
        FIXING)
            fix_cost_circuit_breaker || true
            fix_deny_failing_combos || true
            sm_set_state "MONITORING"
            ;;
        RELAUNCHING)
            # Set externally by crash detect
            relaunch_from_replay || true
            sm_set_state "MONITORING"
            ;;
        *)
            sm_set_state "MONITORING"
            ;;
    esac

    sleep "$POLL_INTERVAL_INROUND"
done

log_line "R26 self-healing daemon EXIT"
log_doc "Daemon exit at $(date +%H:%M:%S)."
