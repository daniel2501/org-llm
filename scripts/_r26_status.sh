#!/usr/bin/env bash
# _r26_status.sh — one-shot dashboard of R26 + bench infra.
#
# Replaces 5+ separate curl/ps/jq invocations (OpenRouter credits, RunPod
# balance, Together /v1/endpoints, pgrep R26, R26_LIVE.json leaderboard,
# sentinel scan, daemon roll-call, recent commits) with a single
# <30s-bounded run. Every API call uses --max-time 5; no long polls.
#
# Sections:
#   1. Provider balances    (OpenRouter / RunPod / Together / Modal /
#                            Anthropic / HF / Qdrant)
#   2. K20 endpoint state   (state, replicas, hardware, $/hr, est. spend)
#   3. R26 process          (PID + elapsed, or "not running")
#   4. R26 leaderboard      (top-3 by total_score, throughput, ETA)
#   5. R26 sentinels        (PREFLIGHT_*, COST_CIRCUIT_BREAKER, …)
#   6. Background daemons   (autorepair, self-healing, pollers, watchers)
#   7. Recent commits       (last 5 in scripts/, plus overall HEAD)
#
# Usage:
#   bash scripts/_r26_status.sh
#   NO_COLOR=1 bash scripts/_r26_status.sh   # disable ANSI escapes
#
# Exit code is always 0 (status display, not a gate).
set -uo pipefail

REPO=/home/daniel/repos/org-llm
ARTIFACTS_R26=$REPO/scripts/_round26_dials_artifacts
LIVE_JSON=$ARTIFACTS_R26/R26_LIVE.json

# ── colors ────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD="\033[1m"
    C_DIM="\033[2m"
    C_RED="\033[31m"
    C_YEL="\033[33m"
    C_GRN="\033[32m"
    C_CYN="\033[36m"
    C_RST="\033[0m"
else
    C_BOLD="" C_DIM="" C_RED="" C_YEL="" C_GRN="" C_CYN="" C_RST=""
fi

hdr() {
    printf "\n${C_BOLD}=== %s ===${C_RST}\n" "$1"
}

ok()    { printf "  ${C_GRN}%s${C_RST}\n" "$*"; }
warn()  { printf "  ${C_YEL}%s${C_RST}\n" "$*"; }
fail()  { printf "  ${C_RED}%s${C_RST}\n" "$*"; }
dim()   { printf "  ${C_DIM}%s${C_RST}\n" "$*"; }
plain() { printf "  %s\n" "$*"; }

# Pretty fixed-width row: label (16) | value
row() {
    printf "  %-18s %s\n" "$1" "$2"
}
# Coloured value variant
row_c() {
    local color=$1
    printf "  %-18s ${color}%s${C_RST}\n" "$2" "$3"
}

# Format epoch seconds → "HH:MM:SS"
fmt_elapsed() {
    local secs=$1
    if [ "$secs" -lt 60 ]; then
        printf "%ds" "$secs"
    elif [ "$secs" -lt 3600 ]; then
        printf "%dm%02ds" $((secs / 60)) $((secs % 60))
    else
        printf "%dh%02dm" $((secs / 3600)) $(((secs % 3600) / 60))
    fi
}

# ── header ────────────────────────────────────────────────────────────────
printf "${C_BOLD}R26 status — %s${C_RST}\n" "$(date '+%Y-%m-%d %H:%M:%S %Z')"

# ─────────────────────────────────────────────────────────────────────────
# 1. Provider balances
# ─────────────────────────────────────────────────────────────────────────
hdr "1. Provider balances"

# OpenRouter
OR_KEY=$(pass org-llm/cloud/openrouter/api-key 2>/dev/null | head -1)
if [ -z "$OR_KEY" ]; then
    fail "OpenRouter        no key in pass"
else
    OR_JSON=$(curl -sS --max-time 5 https://openrouter.ai/api/v1/credits \
        -H "Authorization: Bearer $OR_KEY" 2>/dev/null)
    OR_TOTAL=$(echo "$OR_JSON" | jq -r '.data.total_credits // empty' 2>/dev/null)
    OR_USED=$(echo "$OR_JSON"  | jq -r '.data.total_usage   // empty' 2>/dev/null)
    if [ -z "$OR_TOTAL" ]; then
        fail "OpenRouter        query failed (timeout or auth)"
    else
        OR_HEAD=$(awk "BEGIN { printf \"%.2f\", $OR_TOTAL - $OR_USED }")
        OR_LINE=$(printf "credits=\$%.2f used=\$%.2f headroom=\$%s" \
            "$OR_TOTAL" "$OR_USED" "$OR_HEAD")
        if awk "BEGIN { exit ($OR_HEAD >= 40) ? 0 : 1 }"; then
            row_c "$C_GRN" "OpenRouter" "$OR_LINE"
        elif awk "BEGIN { exit ($OR_HEAD >= 5) ? 0 : 1 }"; then
            row_c "$C_YEL" "OpenRouter" "$OR_LINE (low)"
        else
            row_c "$C_RED" "OpenRouter" "$OR_LINE (depleted)"
        fi
    fi
fi

# RunPod
RP_KEY=$(pass org-llm/cloud/runpod/api-key 2>/dev/null | head -1)
if [ -z "$RP_KEY" ]; then
    warn "RunPod            no key in pass"
else
    RP_JSON=$(curl -sS --max-time 5 -X POST https://api.runpod.io/graphql \
        -H "Authorization: Bearer $RP_KEY" \
        -H "Content-Type: application/json" \
        -d '{"query":"{ myself { clientBalance currentSpendPerHr } }"}' \
        2>/dev/null)
    RP_BAL=$(echo "$RP_JSON" | jq -r '.data.myself.clientBalance // empty' 2>/dev/null)
    RP_HR=$(echo "$RP_JSON"  | jq -r '.data.myself.currentSpendPerHr // empty' 2>/dev/null)
    if [ -z "$RP_BAL" ] || [ "$RP_BAL" = "null" ]; then
        warn "RunPod            balance API returned no value"
    else
        RP_LINE=$(printf "balance=\$%s spend=\$%s/hr" \
            "$RP_BAL" "${RP_HR:-0}")
        if awk "BEGIN { exit ($RP_BAL >= 20) ? 0 : 1 }"; then
            row_c "$C_GRN" "RunPod" "$RP_LINE"
        else
            row_c "$C_YEL" "RunPod" "$RP_LINE (low)"
        fi
    fi
fi

# Together — best-effort balance lookup
TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)
if [ -z "$TG_KEY" ]; then
    warn "Together          no key in pass"
else
    TG_HTTP=$(curl -sS --max-time 5 -o /tmp/r26-status-tg.$$ -w "%{http_code}" \
        https://api.together.xyz/v1/finance \
        -H "Authorization: Bearer $TG_KEY" \
        -A "org-llm/0.1" 2>/dev/null)
    if [ "$TG_HTTP" = "200" ]; then
        TG_BAL=$(jq -r '.balance // .data.balance // empty' /tmp/r26-status-tg.$$ 2>/dev/null)
        if [ -n "$TG_BAL" ]; then
            row_c "$C_GRN" "Together" "balance=\$$TG_BAL"
        else
            row "Together" "200 OK but no balance field"
        fi
    else
        warn "Together          HTTP $TG_HTTP — balance unknown (best-effort)"
    fi
    rm -f /tmp/r26-status-tg.$$
fi

# Modal
row "Modal" "billing-cap unless reset"

# Anthropic
row "Anthropic" "not API-queryable; check console.anthropic.com"

# HF — auth probe only
HF_TOKEN=$(pass org-llm/cloud/huggingface/token 2>/dev/null | head -1)
if [ -z "$HF_TOKEN" ]; then
    warn "HuggingFace       no token in pass"
else
    HF_HTTP=$(curl -sS --max-time 5 -o /dev/null -w "%{http_code}" \
        https://huggingface.co/api/whoami-v2 \
        -H "Authorization: Bearer $HF_TOKEN" 2>/dev/null)
    if [ "$HF_HTTP" = "200" ]; then
        row_c "$C_GRN" "HuggingFace" "auth ok (free tier)"
    else
        warn "HuggingFace       HTTP $HF_HTTP"
    fi
fi

# Qdrant — reachability probe
QD_URL=$(pass org-llm/cloud/qdrant/url 2>/dev/null | head -1)
QD_KEY=$(pass org-llm/cloud/qdrant/api-key 2>/dev/null | head -1)
if [ -z "$QD_URL" ] || [ -z "$QD_KEY" ]; then
    warn "Qdrant            no url/key in pass"
else
    QD_HTTP=$(curl -sS --max-time 5 -o /dev/null -w "%{http_code}" \
        "$QD_URL/" -H "api-key: $QD_KEY" 2>/dev/null)
    if [ "$QD_HTTP" = "200" ]; then
        row_c "$C_GRN" "Qdrant" "reachable (free tier)"
    else
        warn "Qdrant            HTTP $QD_HTTP"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 2. K20 endpoint state
# ─────────────────────────────────────────────────────────────────────────
hdr "2. K20 endpoint state"

EP_ID=$(pass org-llm/cloud/foss-lora/endpoint-id 2>/dev/null | head -1)
if [ -z "$EP_ID" ]; then
    warn "no endpoint-id in pass — K20 not deployed"
elif [ -z "$TG_KEY" ]; then
    warn "no Together key — cannot query endpoint state"
else
    EP_JSON=$(curl -sS --max-time 5 \
        "https://api.together.xyz/v1/endpoints/$EP_ID" \
        -H "Authorization: Bearer $TG_KEY" \
        -A "org-llm/0.1" 2>/dev/null)
    EP_STATE=$(echo "$EP_JSON" | jq -r '.state // empty' 2>/dev/null)
    EP_READY=$(echo "$EP_JSON" | jq -r '.autoscaling.ready_replicas // 0' 2>/dev/null)
    EP_HW=$(echo "$EP_JSON"    | jq -r '.hardware // "?"' 2>/dev/null)
    # Together pricing field varies; try several names.
    EP_PRICE=$(echo "$EP_JSON" | jq -r '
        .hourly_price // .price_per_hour // .cost_per_hour //
        (.hourly_price_milli_cents | if . then ./100000 else empty end) //
        empty' 2>/dev/null)
    EP_STARTED=$(echo "$EP_JSON" | jq -r '.started_at // .created_at // empty' 2>/dev/null)

    row "endpoint-id" "$EP_ID"
    case "$EP_STATE" in
        STARTED)  row_c "$C_GRN" "state" "$EP_STATE (live)" ;;
        STARTING) row_c "$C_YEL" "state" "$EP_STATE (warming up)" ;;
        STOPPED)  row_c "$C_DIM" "state" "$EP_STATE" ;;
        STOPPING) row_c "$C_YEL" "state" "$EP_STATE" ;;
        ERROR)    row_c "$C_RED" "state" "$EP_STATE" ;;
        "")       row_c "$C_RED" "state" "(query failed)" ;;
        *)        row "state" "$EP_STATE" ;;
    esac
    row "ready_replicas" "$EP_READY"
    row "hardware" "$EP_HW"
    if [ -n "$EP_PRICE" ]; then
        row "costPerHr" "\$$EP_PRICE"
    else
        row "costPerHr" "(not exposed by API)"
    fi

    # Spend estimation: only meaningful when STARTED/STARTING with started_at.
    if [ "$EP_STATE" = "STARTED" ] || [ "$EP_STATE" = "STARTING" ]; then
        if [ -n "$EP_STARTED" ] && [ -n "$EP_PRICE" ]; then
            START_EPOCH=$(date -d "$EP_STARTED" +%s 2>/dev/null)
            NOW_EPOCH=$(date +%s)
            if [ -n "$START_EPOCH" ] && [ "$START_EPOCH" -gt 0 ]; then
                UP_SECS=$((NOW_EPOCH - START_EPOCH))
                SPEND=$(awk "BEGIN { printf \"%.4f\", ($UP_SECS / 3600.0) * $EP_PRICE }")
                row "uptime" "$(fmt_elapsed "$UP_SECS")"
                row "spend (est.)" "\$$SPEND"
            fi
        fi
    elif [ "$EP_STATE" = "STOPPED" ]; then
        # Estimate wake-up cost: ~5min STARTING burn, plus first-hour rounding.
        if [ -n "$EP_PRICE" ]; then
            WAKE=$(awk "BEGIN { printf \"%.2f\", $EP_PRICE * (5/60.0) }")
            row "wake-up cost" "~\$$WAKE (5min STARTING burn)"
        else
            dim "wake-up cost: ~5min STARTING burn at hardware rate"
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 3. R26 process
# ─────────────────────────────────────────────────────────────────────────
hdr "3. R26 process"

R26_PIDS=$(pgrep -f "_round26_dials\.py" 2>/dev/null || true)
if [ -z "$R26_PIDS" ]; then
    dim "not running"
else
    for pid in $R26_PIDS; do
        ETIME=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
        ESEC=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ')
        CMD=$(ps -p "$pid" -o args= 2>/dev/null)
        row_c "$C_GRN" "PID $pid" "running ${ETIME} ($(fmt_elapsed "${ESEC:-0}"))"
        dim "  $(echo "$CMD" | head -c 100)"
    done
fi

# ─────────────────────────────────────────────────────────────────────────
# 4. R26 leaderboard
# ─────────────────────────────────────────────────────────────────────────
hdr "4. R26 leaderboard"

if [ ! -f "$LIVE_JSON" ]; then
    dim "no R26_LIVE.json yet (round not started or not far enough in)"
else
    LB_JSON=$(cat "$LIVE_JSON" 2>/dev/null)
    CELLS_DONE=$(echo "$LB_JSON" | jq -r '.cells_done // 0' 2>/dev/null)
    EPOCH=$(echo "$LB_JSON"      | jq -r '.epoch // 0' 2>/dev/null)
    NOW=$(date +%s)
    ELAPSED=$((NOW - EPOCH))
    if [ "$EPOCH" -gt 0 ] && [ "$CELLS_DONE" -gt 0 ] && [ "$ELAPSED" -gt 0 ]; then
        RATE=$(awk "BEGIN { printf \"%.2f\", $CELLS_DONE / ($ELAPSED / 60.0) }")
    else
        RATE="?"
    fi
    row "cells_done" "$CELLS_DONE"
    row "elapsed" "$(fmt_elapsed "$ELAPSED")"
    row "throughput" "$RATE cells/min"

    TOTAL_SPEND=$(echo "$LB_JSON" | jq -r '[.leaderboard[]?.total_cost // 0] | add // 0' 2>/dev/null)
    row "total spend" "\$$(printf '%.4f' "$TOTAL_SPEND")"

    printf "\n  ${C_BOLD}%-22s %8s %8s %8s %12s${C_RST}\n" \
        "variant" "cells" "score" "cost" "$/unit"
    echo "$LB_JSON" | jq -r '
        .leaderboard
        | sort_by(-.total_score)
        | .[:3][]
        | [.variant, (.cells // 0), (.total_score // 0),
           (.total_cost // 0), (.cost_per_unit // 0)]
        | @tsv
    ' 2>/dev/null | while IFS=$'\t' read -r v c s co cu; do
        printf "  %-22s %8s %8.1f %8.4f %12.6f\n" \
            "$v" "$c" "$s" "$co" "$cu"
    done

    # Naive ETA: if leaderboard target ~ #variants × N cells (e.g. 9 variants
    # × 65 cells ≈ 585), estimate from rate. We don't know the target without
    # parsing dials, so just project at current pace until idle.
    if [ "$RATE" != "?" ] && awk "BEGIN { exit ($RATE > 0) ? 0 : 1 }"; then
        # Show "would hit 600 cells in" as a generic projection.
        REMAIN_600=$(awk "BEGIN { printf \"%.0f\", (600 - $CELLS_DONE) / $RATE }")
        if [ "$REMAIN_600" -gt 0 ]; then
            dim "  ETA to 600 cells: $(fmt_elapsed "$((REMAIN_600 * 60))") (at current rate)"
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 5. R26 sentinels
# ─────────────────────────────────────────────────────────────────────────
hdr "5. R26 sentinels"

SENTINELS=(
    PREFLIGHT_PASS PREFLIGHT_FAIL
    COST_CIRCUIT_BREAKER LIVE_DENY_LIST
    RESUME_PENDING NOVELTY_FAIL
    R26_DONE_SPAWN_AGENTS PATCH_VERIFY_FAIL
)
FOUND_ANY=0
for s in "${SENTINELS[@]}"; do
    f="$ARTIFACTS_R26/$s"
    [ -e "$f" ] || continue
    FOUND_ANY=1
    case "$s" in
        PREFLIGHT_PASS|R26_DONE_SPAWN_AGENTS) COL=$C_GRN ;;
        PREFLIGHT_FAIL|COST_CIRCUIT_BREAKER|NOVELTY_FAIL|PATCH_VERIFY_FAIL) COL=$C_RED ;;
        *) COL=$C_YEL ;;
    esac
    printf "  ${COL}%s${C_RST}  %s\n" "$s" "$f"
    # First few lines, indented.
    head -3 "$f" 2>/dev/null | sed 's/^/      /' | head -3
done
[ "$FOUND_ANY" -eq 0 ] && dim "no sentinels present (R26 round not started or in flight clean)"

# ─────────────────────────────────────────────────────────────────────────
# 6. Background daemons
# ─────────────────────────────────────────────────────────────────────────
hdr "6. Background daemons"

DAEMONS=(
    _r25_autorepair_daemon
    _r26_self_healing_daemon
    _lora_together_poller
    _lora_together_deploy_daemon
    _openrouter_credit_watcher
    _r26_post_round_analysis
)
ANY_DAEMON=0
for d in "${DAEMONS[@]}"; do
    PIDS=$(pgrep -f "$d" 2>/dev/null || true)
    if [ -z "$PIDS" ]; then
        printf "  ${C_DIM}%-32s not running${C_RST}\n" "$d"
    else
        ANY_DAEMON=1
        for pid in $PIDS; do
            ETIME=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
            ESEC=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ')
            printf "  ${C_GRN}%-32s${C_RST} PID=%s elapsed=%s\n" \
                "$d" "$pid" "$(fmt_elapsed "${ESEC:-0}")"
        done
    fi
done
[ "$ANY_DAEMON" -eq 0 ] && dim "(no R25/R26 daemons running)"

# ─────────────────────────────────────────────────────────────────────────
# 7. Recent commits
# ─────────────────────────────────────────────────────────────────────────
hdr "7. Recent commits"

plain "${C_BOLD}last 5 in scripts/${C_RST}"
git -C "$REPO" log --oneline -5 -- scripts/ 2>/dev/null \
    | sed 's/^/    /' \
    || dim "(git log failed)"

plain ""
plain "${C_BOLD}HEAD (any path)${C_RST}"
git -C "$REPO" log --oneline -1 2>/dev/null \
    | sed 's/^/    /' \
    || dim "(git log failed)"

printf "\n${C_DIM}— end of dashboard —${C_RST}\n"
exit 0
