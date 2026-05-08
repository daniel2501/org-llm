#!/bin/bash
# RunPod LoRA progress logger.
# - Polls RunPod GraphQL every 10 min for pod status + uptime
# - Polls HF for DONE.json marker (signal pod uploaded adapter)
# - On completion: stores adapter URL in pass + appends to live-log
# - On pod failure: logs the error + exits
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS_DOC=$REPO/docs/wiki/2026-05-08-r19-lora-progress.org
POD_ID="${1:-v66zx1wdcrtzc0}"
ADAPTER_REPO="daniel2501/k20-foss-distill"
DATA_REPO="daniel2501/k20-foss-distill-data"

cd "$REPO"
log_line() { echo "[lora-runpod $(date +%H:%M:%S)] $*" >> "$LOG"; }

RP_KEY=$(pass org-llm/cloud/runpod/api-key 2>/dev/null | head -1)
HF_TOK=$(pass org-llm/cloud/huggingface/token 2>/dev/null | head -1)

if [ -z "$RP_KEY" ] || [ -z "$HF_TOK" ]; then
    log_line "ABORT — missing RP_KEY or HF_TOK"
    exit 1
fi

{
    echo
    echo "** [lora-runpod $(date +%H:%M:%S)] tracking pod $POD_ID"
    echo "Pod monitoring + HF DONE.json poll started."
} >> "$PROGRESS_DOC"

log_line "watching pod $POD_ID + HF $ADAPTER_REPO"

while true; do
    # Pod status via GraphQL
    POD_JSON=$(curl -sS --max-time 15 -X POST \
        "https://api.runpod.io/graphql?api_key=$RP_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"query\":\"query { pod(input: {podId: \\\"$POD_ID\\\"}) { id desiredStatus lastStatusChange runtime { uptimeInSeconds gpus { id gpuUtilPercent memoryUtilPercent } container { cpuPercent memoryPercent } } costPerHr machineId } }\"}" \
        2>/dev/null)

    STATUS=$(echo "$POD_JSON" | jq -r '.data.pod.desiredStatus // "UNKNOWN"' 2>/dev/null)
    UPTIME=$(echo "$POD_JSON" | jq -r '.data.pod.runtime.uptimeInSeconds // 0' 2>/dev/null)
    COST=$(echo "$POD_JSON" | jq -r '.data.pod.costPerHr // 0' 2>/dev/null)
    GPU_UTIL=$(echo "$POD_JSON" | jq -r '.data.pod.runtime.gpus[0].gpuUtilPercent // null' 2>/dev/null)

    UPTIME_MIN=$(awk "BEGIN { printf \"%.1f\", $UPTIME / 60 }")
    SPEND_USD=$(awk "BEGIN { printf \"%.2f\", $UPTIME * $COST / 3600 }")

    {
        echo
        echo "** Status check $(date +%H:%M:%S)"
        echo
        echo "*Pod $POD_ID.* status=$STATUS uptime=${UPTIME_MIN}m spend=\$${SPEND_USD} gpu_util=${GPU_UTIL}%"
        echo
    } >> "$PROGRESS_DOC"

    log_line "pod=$STATUS uptime=${UPTIME_MIN}m spend=\$${SPEND_USD} gpu=${GPU_UTIL}%"

    # Check for DONE marker on HF
    DONE_CHECK=$(curl -sS --max-time 10 \
        "https://huggingface.co/api/datasets/$DATA_REPO/tree/main" \
        -H "Authorization: Bearer $HF_TOK" 2>/dev/null \
        | jq -r '.[] | select(.path == "DONE.json") | .path' 2>/dev/null)

    if [ "$DONE_CHECK" = "DONE.json" ]; then
        log_line "DONE.json found on HF — training complete"

        # Verify adapter exists in the model repo
        ADAPTER_FILES=$(curl -sS --max-time 10 \
            "https://huggingface.co/api/models/$ADAPTER_REPO/tree/main" \
            -H "Authorization: Bearer $HF_TOK" 2>/dev/null \
            | jq -r '.[].path' 2>/dev/null)
        log_line "adapter repo files: $(echo "$ADAPTER_FILES" | tr '\n' ' ')"

        # Store adapter URL in pass
        ADAPTER_URL="https://huggingface.co/$ADAPTER_REPO"
        echo "$ADAPTER_URL" | pass insert -e -f org-llm/cloud/foss-lora/url 2>&1 \
            | head -2 >> "$LOG"
        log_line "adapter URL stored in pass: org-llm/cloud/foss-lora/url"

        {
            echo
            echo "** [lora-runpod $(date +%H:%M:%S)] TRAINING COMPLETE"
            echo
            echo "*Adapter*: $ADAPTER_URL"
            echo
            echo "*Files in adapter repo*:"
            echo "#+begin_src text"
            echo "$ADAPTER_FILES"
            echo "#+end_src"
            echo
            echo "*Total spend*: \$${SPEND_USD}"
        } >> "$PROGRESS_DOC"

        # Stop the pod (RunPod auto-stops on entrypoint exit but be explicit)
        curl -sS --max-time 10 -X POST \
            "https://api.runpod.io/graphql?api_key=$RP_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"query\":\"mutation { podTerminate(input: {podId: \\\"$POD_ID\\\"}) }\"}" \
            2>&1 | head -3 >> "$LOG"
        log_line "pod terminate sent"
        break
    fi

    # Detect pod failure: status EXITED or TERMINATED without DONE marker
    if [ "$STATUS" = "EXITED" ] || [ "$STATUS" = "TERMINATED" ]; then
        log_line "POD $STATUS without DONE.json — training failed"
        {
            echo
            echo "** [lora-runpod $(date +%H:%M:%S)] POD $STATUS — TRAINING FAILED"
            echo
            echo "Inspect pod logs via RunPod dashboard or:"
            echo "  curl -s 'https://api.runpod.io/graphql?api_key=\$RP_KEY' \\\\"
            echo "    -d '{\"query\":\"query { pod(input: {podId: \\\"$POD_ID\\\"}) { lastStatusChange machineId } }\"}'"
        } >> "$PROGRESS_DOC"
        break
    fi

    sleep 600   # 10 min
done

log_line "logger exiting"
