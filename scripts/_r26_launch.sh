#!/usr/bin/env bash
# R26 single-entry-point launcher — P1-21a.
#
# Wraps the full R26 round flow in one bash:
#   1. _r26_design_patch.py       — fork dials + run all 5 P0 acceptance probes
#   2. _r26_preflight.sh          — 6 gate categories (funding / health / K20 /
#                                   patches / resources / config sanity)
#   3. _r26_self_healing_daemon.sh (background) — monitors + writes
#                                   COST_CIRCUIT_BREAKER + LIVE_DENY_LIST +
#                                   cell_replay.jsonl entries
#   4. python3 scripts/_round26_dials.py — main bench (R18_PARALLELISM=32)
#   5. _r26_post_round_analysis.sh — captures stats, commits artifacts, drops
#                                   sentinel for analysis-agent spawn (4-agent
#                                   scour + synthesis + go-forward update)
#   6. _k20_endpoint_pause.sh     — belt-and-braces (P1-10 already does atexit)
#
# Replaces R25's 4-snippet inline launch. Single command, single state file,
# single cost ceiling.
#
# Usage:
#   bash scripts/_r26_launch.sh                 # full round
#   bash scripts/_r26_launch.sh --dry-run       # design patch + preflight only
#   bash scripts/_r26_launch.sh --skip-preflight   # emergency override (warned)
#
# Env overrides:
#   R26_MAX_SPEND_USD=30        — cost ceiling (default $30); read R26_LIVE.json
#                                 leaderboard.total_cost; trip COST_CIRCUIT_BREAKER
#                                 + halt round on exceed.
#   R26_PARALLELISM=32          — passed as R18_PARALLELISM env to harness
#   R26_HEADROOM_FLOOR=40       — preflight OR headroom floor (passed through)
#   R26_RUNPOD_FLOOR=20         — preflight RunPod balance floor (passed through)
#   FAST_PREFLIGHT=1            — skip 410s WALL_CAP probe in preflight
#   ARTIFACTS=…                 — output dir override
#                                 (default scripts/_round26_dials_artifacts)
#
# Exit codes:
#   0   all steps succeeded (or --dry-run completed without preflight FAIL)
#   1   any step FAILED — failed step name printed to stderr; LAUNCH_STATE
#       file shows where we stopped
#
# State machine (LAUNCH_STATE file):
#   STARTED → DESIGN_PATCH → PREFLIGHT → DAEMON_LAUNCHED → HARNESS_RUNNING
#                                                       → POST_HANDOFF → DONE
# Any step FAIL writes step name + reason and exits 1.

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LIVE_LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
ARTIFACTS=${ARTIFACTS:-$REPO/scripts/_round26_dials_artifacts}
LAUNCH_STATE_FILE="$ARTIFACTS/LAUNCH_STATE"
COST_CB="$ARTIFACTS/COST_CIRCUIT_BREAKER"
LIVE_JSON="$ARTIFACTS/R26_LIVE.json"
STDOUT_LOG="$ARTIFACTS/stdout.log"

# Defaults
R26_MAX_SPEND_USD=${R26_MAX_SPEND_USD:-30}
R26_PARALLELISM=${R26_PARALLELISM:-32}

# CLI flags
DRY_RUN=0
SKIP_PREFLIGHT=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)        DRY_RUN=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "[r26-launch] unknown arg: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

mkdir -p "$ARTIFACTS"

# ── helpers ───────────────────────────────────────────────────────────────
ts() { date +%H:%M:%S; }
log_live() { echo "[r26-launch $(ts)] $*" >> "$LIVE_LOG"; }
say() {
    echo "[r26-launch $(ts)] $*"
    log_live "$*"
}
state() {
    echo "$1" > "$LAUNCH_STATE_FILE"
    say "STATE → $1"
}
fail_step() {
    local step="$1" reason="$2"
    say "[FAIL] $step — $reason"
    echo "FAIL=$step" >> "$LAUNCH_STATE_FILE"
    echo "REASON=$reason" >> "$LAUNCH_STATE_FILE"
    echo "[r26-launch] FAIL at $step: $reason" >&2
    print_summary
    exit 1
}

print_summary() {
    say "── R26 launch summary ──"
    if [ -f "$LAUNCH_STATE_FILE" ]; then
        say "  state file: $LAUNCH_STATE_FILE"
        while IFS= read -r line; do
            say "    $line"
        done < "$LAUNCH_STATE_FILE"
    fi
    if [ -f "$ARTIFACTS/PREFLIGHT_FAIL" ]; then
        say "  preflight failures:"
        while IFS= read -r line; do
            say "    $line"
        done < "$ARTIFACTS/PREFLIGHT_FAIL"
        say "  suggested action: see preflight failure JSONL above"
    fi
    if [ -f "$ARTIFACTS/PATCH_VERIFY_FAIL" ]; then
        say "  patch-verify failures:"
        while IFS= read -r line; do
            say "    $line"
        done < "$ARTIFACTS/PATCH_VERIFY_FAIL"
        say "  suggested action: fix probe target then re-run launcher"
    fi
}

# ── start ─────────────────────────────────────────────────────────────────
say "── R26 launcher starting (dry-run=$DRY_RUN, skip-preflight=$SKIP_PREFLIGHT) ──"
say "  artifacts:        $ARTIFACTS"
say "  cost ceiling:     \$${R26_MAX_SPEND_USD}"
say "  parallelism:      $R26_PARALLELISM"
state STARTED

# ── 1. Design patch + verification ─────────────────────────────────────────
state DESIGN_PATCH
say "step 1/6 — design patch + P0 probes"
DP_OUT=$(mktemp /tmp/r26-launch-dp.XXXXXX)
if ! python3 "$REPO/scripts/_r26_design_patch.py" >"$DP_OUT" 2>&1; then
    say "design patch output (last 20 lines):"
    tail -20 "$DP_OUT" | while IFS= read -r line; do say "    $line"; done
    rm -f "$DP_OUT"
    fail_step "design_patch" "P0 acceptance probes failed — see PATCH_VERIFY_FAIL"
fi
DP_PASS=$(grep -c "PASS" "$DP_OUT" || true)
say "  design patch OK ($DP_PASS PASS lines)"
rm -f "$DP_OUT"

# ── 2. Preflight ──────────────────────────────────────────────────────────
state PREFLIGHT
if [ "$SKIP_PREFLIGHT" = "1" ]; then
    say "step 2/6 — preflight SKIPPED (--skip-preflight emergency override)"
    say "  WARNING: cost ceiling, endpoint health, provider funding NOT verified"
else
    say "step 2/6 — preflight (6 gate categories)"
    PF_OUT=$(mktemp /tmp/r26-launch-pf.XXXXXX)
    if ! ARTIFACTS="$ARTIFACTS" bash "$REPO/scripts/_r26_preflight.sh" \
            >"$PF_OUT" 2>&1; then
        say "preflight output (last 30 lines):"
        tail -30 "$PF_OUT" | while IFS= read -r line; do say "    $line"; done
        rm -f "$PF_OUT"
        fail_step "preflight" "one or more gates failed — see PREFLIGHT_FAIL JSONL"
    fi
    PF_PASS=$(grep -c "\[PASS\]" "$PF_OUT" || true)
    PF_WARN=$(grep -c "\[WARN\]" "$PF_OUT" || true)
    say "  preflight OK ($PF_PASS PASS, $PF_WARN WARN)"
    rm -f "$PF_OUT"
fi

# ── --dry-run exit point ──────────────────────────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
    state DRY_RUN_DONE
    say "step 3/6 — daemon (skipped, --dry-run)"
    say "step 4/6 — harness (skipped, --dry-run)"
    say "step 5/6 — post-handoff (skipped, --dry-run)"
    say "step 6/6 — K20 pause (skipped, --dry-run)"
    say ""
    say "── DRY RUN COMPLETE — design_patch + preflight passed; harness NOT invoked ──"
    print_summary
    exit 0
fi

# ── 3. Self-healing daemon (background) ───────────────────────────────────
state DAEMON_LAUNCHED
say "step 3/6 — launching self-healing daemon (background)"
DAEMON_SCRIPT="$REPO/scripts/_r26_self_healing_daemon.sh"
DAEMON_LOG=/tmp/r26-self-healing-daemon.log
if [ ! -f "$DAEMON_SCRIPT" ]; then
    # Fallback to R25 daemon if R26 not yet shipped (P1-20 still pending).
    DAEMON_SCRIPT="$REPO/scripts/_r25_autorepair_daemon.sh"
    say "  WARNING: _r26_self_healing_daemon.sh missing — using R25 daemon as stand-in"
fi
if [ -f "$DAEMON_SCRIPT" ]; then
    nohup bash "$DAEMON_SCRIPT" >"$DAEMON_LOG" 2>&1 &
    DAEMON_PID=$!
    disown $DAEMON_PID 2>/dev/null || true
    say "  daemon PID=$DAEMON_PID, log=$DAEMON_LOG"
    echo "DAEMON_PID=$DAEMON_PID" >> "$LAUNCH_STATE_FILE"
else
    say "  WARNING: no daemon script found — proceeding without self-healing"
fi

# ── 4. Harness ────────────────────────────────────────────────────────────
state HARNESS_RUNNING
say "step 4/6 — launching harness scripts/_round26_dials.py"
say "  R18_PARALLELISM=$R26_PARALLELISM"
say "  stdout → $STDOUT_LOG"

# Cost-ceiling watchdog: backgrounded; reads R26_LIVE.json every 60s
# and writes COST_CIRCUIT_BREAKER if leaderboard total_cost > ceiling.
(
    while true; do
        sleep 60
        # Stop watchdog if harness already exited
        if ! pgrep -f "python3 .*scripts/_round26_dials" >/dev/null 2>&1; then
            exit 0
        fi
        if [ -f "$LIVE_JSON" ]; then
            spend=$(jq -r '[.leaderboard[].total_cost // 0] | add // 0' "$LIVE_JSON" 2>/dev/null)
            [ -z "$spend" ] && continue
            if awk "BEGIN { exit ($spend >= $R26_MAX_SPEND_USD) ? 0 : 1 }" 2>/dev/null; then
                if [ ! -f "$COST_CB" ]; then
                    {
                        echo "tripped=$(date -Is)"
                        echo "spend=$spend"
                        echo "ceiling=$R26_MAX_SPEND_USD"
                        echo "source=_r26_launch.sh cost-ceiling watchdog"
                    } > "$COST_CB"
                    echo "[r26-launch $(date +%H:%M:%S)] COST_CIRCUIT_BREAKER tripped: \$$spend >= \$$R26_MAX_SPEND_USD" >> "$LIVE_LOG"
                fi
                exit 0
            fi
        fi
    done
) &
WATCHDOG_PID=$!
disown $WATCHDOG_PID 2>/dev/null || true
echo "WATCHDOG_PID=$WATCHDOG_PID" >> "$LAUNCH_STATE_FILE"
say "  cost-ceiling watchdog PID=$WATCHDOG_PID (polls every 60s)"

# Run the harness, capturing stdout
HARNESS_RC=0
R18_PARALLELISM="$R26_PARALLELISM" \
    python3 "$REPO/scripts/_round26_dials.py" >"$STDOUT_LOG" 2>&1 || HARNESS_RC=$?

# Stop watchdog if still alive
kill "$WATCHDOG_PID" 2>/dev/null || true

if [ "$HARNESS_RC" -ne 0 ]; then
    say "  harness exited with rc=$HARNESS_RC (last 20 lines stdout:)"
    tail -20 "$STDOUT_LOG" 2>/dev/null | while IFS= read -r line; do say "    $line"; done
    fail_step "harness" "python3 _round26_dials.py exited rc=$HARNESS_RC"
fi
say "  harness exited cleanly (rc=0)"

# ── 5. Post-round handoff ─────────────────────────────────────────────────
state POST_HANDOFF
say "step 5/6 — post-round handoff (capture stats + commit + sentinel)"
HANDOFF_SCRIPT="$REPO/scripts/_r26_post_round_analysis.sh"
[ -f "$HANDOFF_SCRIPT" ] || HANDOFF_SCRIPT="$REPO/scripts/_r26_post_round_handoff.sh"
[ -f "$HANDOFF_SCRIPT" ] || HANDOFF_SCRIPT="$REPO/scripts/_r25_post_round_handoff.sh"
if [ -f "$HANDOFF_SCRIPT" ]; then
    HANDOFF_OUT=$(mktemp /tmp/r26-launch-hd.XXXXXX)
    if ! bash "$HANDOFF_SCRIPT" >"$HANDOFF_OUT" 2>&1; then
        say "  WARNING: post-handoff exited non-zero (see $HANDOFF_OUT)"
        tail -10 "$HANDOFF_OUT" | while IFS= read -r line; do say "    $line"; done
    else
        say "  handoff OK"
    fi
    rm -f "$HANDOFF_OUT"
else
    say "  WARNING: no post-handoff script found — manual analysis required"
fi

# ── 6. K20 endpoint pause (belt-and-braces) ───────────────────────────────
say "step 6/6 — K20 endpoint pause (belt-and-braces; harness atexit also runs)"
K20_PAUSE="$REPO/scripts/_k20_endpoint_pause.sh"
if [ -f "$K20_PAUSE" ]; then
    if bash "$K20_PAUSE" >/tmp/r26-k20-pause.$$ 2>&1; then
        say "  K20 pause OK"
    else
        say "  WARNING: K20 pause failed (may already be paused by atexit)"
    fi
    rm -f /tmp/r26-k20-pause.$$
fi

# ── done ──────────────────────────────────────────────────────────────────
state DONE
say ""
say "── R26 LAUNCH COMPLETE ──"
print_summary
exit 0
