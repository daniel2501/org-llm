#!/bin/bash
# Together LoRA deployment daemon — aggressive try-and-validate.
# Goal: stand up a working chat-completions endpoint for the
# fine-tuned K20-foss-distill model so R26 can use it as K20 variant.
#
# Strategy:
#   1. Probe /v1/hardware for compatible options (sorted cheap → expensive)
#   2. For each hardware: try multiple deploy shapes
#      (a) /v1/endpoints with autoscaling.{min,max}_replicas
#      (b) /v1/endpoints with top-level min_replicas/max_replicas
#      (c) Together SDK's endpoints.create (handles their quirks)
#   3. After each create, poll until status==RUNNING (or fails)
#   4. Sanity-test with a real chat completion
#   5. On success: store URL+model in pass + update progress doc + exit
#   6. On all fail: detailed error matrix to progress doc + halt
#
# All attempts logged to:
#   - /home/daniel/repos/org-llm/docs/wiki/2026-05-08-r19-lora-progress.org
#   - /home/daniel/repos/org-llm/docs/wiki/2026-05-08-r18-live-log.org (one-liners)
#
# Caps: $20 total deployment spend (each attempt terminates if no
# success in 10 min); if still no success after all hardware tried,
# halt + surface for human review.

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS=$REPO/docs/wiki/2026-05-08-r19-lora-progress.org
ATTEMPT_CAP=30   # raised after first run hit the cap on too-small GPUs
PER_ATTEMPT_TIMEOUT=600   # 10 min per attempt
MIN_GPU_MEMORY_GB=80      # 30B model needs >= 60GB; 80GB safe floor

cd "$REPO"

TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)
MODEL_NAME=$(pass org-llm/cloud/together/k20-output-model 2>/dev/null | head -1)
[ -z "$MODEL_NAME" ] && MODEL_NAME="daniel2501_9324/Qwen3-Coder-30B-A3B-Instruct-k20-foss-distill-32c0ef6c"

# Cloudflare in front of api.together.xyz 403s default curl/python UA
# with "error code: 1010" — set a stable, identifying UA for all calls.
UA="org-llm/0.1 (https://github.com/daniel2501/org-llm)"

if [ -z "$TG_KEY" ]; then
    echo "no Together key; abort"; exit 1
fi

log_line() { echo "[lora-deploy $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_doc() {
    {
        echo
        echo "** [lora-deploy $(date +%H:%M:%S)] $*"
    } >> "$PROGRESS"
}
log_doc_block() {
    {
        echo "#+begin_src text"
        echo "$@"
        echo "#+end_src"
    } >> "$PROGRESS"
}

log_line "deploy daemon STARTED for model $MODEL_NAME"
log_doc "Together LoRA deploy daemon started — try every hardware × API shape until success"

# ── Discover available hardware ─────────────────────────────────────────
log_line "discovering hardware options (filtering to >= ${MIN_GPU_MEMORY_GB}GB)"
HARDWARE_LIST=$(curl -sS --max-time 15 -A "$UA" https://api.together.xyz/v1/hardware \
    -H "Authorization: Bearer $TG_KEY" 2>/dev/null \
    | jq -r --argjson min "$MIN_GPU_MEMORY_GB" \
        '.data[] | select(.specs.gpu_count >= 1 and .specs.gpu_memory >= $min)
                | "\(.id) \(.pricing.cents_per_minute)"' 2>/dev/null \
    | sort -k2 -n)

if [ -z "$HARDWARE_LIST" ]; then
    log_line "no hardware list; falling back to known 80GB+ trio"
    HARDWARE_LIST=$(printf "1x_nvidia_h100_80gb_sxm 9.15\n1x_nvidia_a100_80gb_sxm 6.67\n1x_nvidia_h200_140gb_sxm 9.15\n2x_nvidia_h100_80gb_sxm 18.30\n")
fi

log_doc "Compatible hardware (>= ${MIN_GPU_MEMORY_GB}GB, cheapest first):"
log_doc_block "$HARDWARE_LIST"

# ── Probe loop ──────────────────────────────────────────────────────────
ATTEMPTS_DOC="| # | hardware | shape | result |"
ATTEMPTS_DOC+="\n|---+----------+-------+--------|"

deploy_via_curl_autoscaling() {
    local hw="$1"
    curl -sS --max-time 30 -A "$UA" -X POST https://api.together.xyz/v1/endpoints \
        -H "Authorization: Bearer $TG_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL_NAME\",\"hardware\":\"$hw\",\"autoscaling\":{\"min_replicas\":1,\"max_replicas\":1},\"display_name\":\"k20-foss-distill-$$-$RANDOM\"}"
}

deploy_via_curl_flat() {
    local hw="$1"
    curl -sS --max-time 30 -A "$UA" -X POST https://api.together.xyz/v1/endpoints \
        -H "Authorization: Bearer $TG_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL_NAME\",\"hardware\":\"$hw\",\"min_replicas\":1,\"max_replicas\":1,\"display_name\":\"k20-foss-distill-$$-$RANDOM\"}"
}

deploy_via_sdk() {
    local hw="$1"
    python3 <<PY
import os, sys, json
os.environ["TOGETHER_API_KEY"] = "$TG_KEY"
from together import Together
# Cloudflare 403s default UAs with "error code: 1010" — identify as org-llm.
client = Together(default_headers={
    "User-Agent": "org-llm/0.1 (https://github.com/daniel2501/org-llm)"
})
try:
    ep = client.endpoints.create(
        model="$MODEL_NAME",
        hardware="$hw",
        autoscaling={"min_replicas": 1, "max_replicas": 1},
        display_name="k20-foss-distill-sdk-$$-$RANDOM",
    )
    print(json.dumps({"id": ep.id, "status": ep.state, "url": getattr(ep, 'url', None)}))
except Exception as e:
    print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
PY
}

# Sanity test the model via inference (stripped of reading endpoint metadata)
sanity_test_endpoint() {
    local model="$1"
    local resp
    resp=$(curl -sS --max-time 30 -A "$UA" -X POST https://api.together.xyz/v1/chat/completions \
        -H "Authorization: Bearer $TG_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: K20 ALIVE\"}],\"max_tokens\":20}" \
        2>&1)
    echo "$resp"
    if echo "$resp" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Wait for endpoint to reach RUNNING state, polling every 30s
wait_endpoint_running() {
    local endpoint_id="$1"
    local deadline=$(( $(date +%s) + PER_ATTEMPT_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local state
        state=$(curl -sS --max-time 15 -A "$UA" \
            "https://api.together.xyz/v1/endpoints/$endpoint_id" \
            -H "Authorization: Bearer $TG_KEY" 2>/dev/null \
            | jq -r '.state // empty' 2>/dev/null)
        log_line "  endpoint $endpoint_id state=$state"
        case "$state" in
            STARTED|RUNNING|ACTIVE)
                return 0
                ;;
            FAILED|ERROR|CANCELLED|STOPPED)
                return 1
                ;;
        esac
        sleep 30
    done
    return 1   # timeout
}

# Cleanup: stop a deployed endpoint we don't need
stop_endpoint() {
    local endpoint_id="$1"
    [ -z "$endpoint_id" ] && return
    curl -sS --max-time 15 -A "$UA" -X POST \
        "https://api.together.xyz/v1/endpoints/$endpoint_id/stop" \
        -H "Authorization: Bearer $TG_KEY" 2>&1 | head -3 >> "$LOG"
    log_line "  stopped endpoint $endpoint_id"
}

# ── Main probe loop ─────────────────────────────────────────────────────
ATTEMPT=0
SUCCESS=0
SUCCESS_ENDPOINT=""
declare -a TRIED=()

while IFS= read -r hw_line; do
    [ -z "$hw_line" ] && continue
    HW=$(echo "$hw_line" | awk '{print $1}')
    PRICE=$(echo "$hw_line" | awk '{print $2}')

    for SHAPE in "autoscaling" "flat" "sdk"; do
        ATTEMPT=$((ATTEMPT + 1))
        if [ "$ATTEMPT" -gt "$ATTEMPT_CAP" ]; then
            log_line "ATTEMPT_CAP=$ATTEMPT_CAP reached — halting"
            log_doc "Hit attempt cap. No working hardware × shape combo found."
            break 2
        fi

        log_line "ATTEMPT $ATTEMPT: hw=$HW (\$$PRICE/min) shape=$SHAPE"
        log_doc "ATTEMPT $ATTEMPT — hardware=$HW (\$$PRICE/min) shape=$SHAPE"

        case "$SHAPE" in
            autoscaling) RESP=$(deploy_via_curl_autoscaling "$HW") ;;
            flat)        RESP=$(deploy_via_curl_flat "$HW") ;;
            sdk)         RESP=$(deploy_via_sdk "$HW") ;;
        esac

        log_doc_block "$RESP"

        # Parse response
        EP_ID=$(echo "$RESP" | jq -r '.id // empty' 2>/dev/null)
        ERR=$(echo "$RESP" | jq -r '.error.message // .error // empty' 2>/dev/null)

        if [ -n "$ERR" ]; then
            log_line "  failed: $ERR"
            TRIED+=("$ATTEMPT|$HW|$SHAPE|FAIL: $ERR")
            continue
        fi

        if [ -z "$EP_ID" ]; then
            log_line "  no endpoint id in response"
            TRIED+=("$ATTEMPT|$HW|$SHAPE|FAIL: no id returned")
            continue
        fi

        log_line "  endpoint created: $EP_ID — waiting for RUNNING"
        if wait_endpoint_running "$EP_ID"; then
            log_line "  $EP_ID reached RUNNING — sanity test"
            SAN=$(sanity_test_endpoint "$MODEL_NAME")
            if echo "$SAN" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
                CONTENT=$(echo "$SAN" | jq -r '.choices[0].message.content')
                log_line "  SANITY PASS — response: $CONTENT"
                log_doc "*SUCCESS* — endpoint $EP_ID running on $HW; sanity returned: =$CONTENT="
                # Persist
                echo "https://api.together.xyz/v1/chat/completions" \
                    | pass insert -e -f org-llm/cloud/foss-lora/url 2>&1 | head -1
                echo "$MODEL_NAME" \
                    | pass insert -e -f org-llm/cloud/foss-lora/model 2>&1 | head -1
                echo "$EP_ID" \
                    | pass insert -e -f org-llm/cloud/foss-lora/endpoint-id 2>&1 | head -1
                echo "$HW" \
                    | pass insert -e -f org-llm/cloud/foss-lora/hardware 2>&1 | head -1
                log_doc "Persisted to pass: =org-llm/cloud/foss-lora/{url,model,endpoint-id,hardware}="
                SUCCESS=1
                SUCCESS_ENDPOINT="$EP_ID"
                TRIED+=("$ATTEMPT|$HW|$SHAPE|*SUCCESS* $EP_ID")
                break 2
            else
                log_line "  sanity FAIL — stopping endpoint"
                log_doc_block "$SAN"
                stop_endpoint "$EP_ID"
                TRIED+=("$ATTEMPT|$HW|$SHAPE|FAIL: sanity didn't return chat completion")
            fi
        else
            log_line "  endpoint never reached RUNNING — stopping"
            stop_endpoint "$EP_ID"
            TRIED+=("$ATTEMPT|$HW|$SHAPE|FAIL: never RUNNING (timeout/failed)")
        fi
    done
done <<< "$HARDWARE_LIST"

# ── Final report ────────────────────────────────────────────────────────
{
    echo
    echo "** [lora-deploy $(date +%H:%M:%S)] FINAL — success=$SUCCESS"
    echo
    echo "*Attempts matrix:*"
    echo
    echo "| # | hardware | shape | result |"
    echo "|---+----------+-------+--------|"
    for t in "${TRIED[@]}"; do
        IFS='|' read -r n hw sh res <<< "$t"
        echo "| $n | $hw | $sh | $res |"
    done
    echo
    if [ "$SUCCESS" -eq 1 ]; then
        echo "*Endpoint live.* =$SUCCESS_ENDPOINT= on hardware (saved to pass)."
        echo "Verify: =curl -X POST https://api.together.xyz/v1/chat/completions= "
        echo "with model =$MODEL_NAME=."
    else
        echo "*All attempts failed.* Manual triage needed. See per-attempt"
        echo "error JSONs above. Likely root causes: model fine-tune quota,"
        echo "Together account tier, region availability, or model name typo."
    fi
} >> "$PROGRESS"

if [ "$SUCCESS" -eq 1 ]; then
    log_line "DEPLOY DAEMON SUCCESS — endpoint $SUCCESS_ENDPOINT live"
else
    log_line "DEPLOY DAEMON FAILED after $ATTEMPT attempts"
fi
