#!/bin/bash
# K20 endpoint resume helper — start the dedicated Together endpoint
# when R26 (or any caller) needs to use the K20 LoRA, then sanity-test.
#
# Usage:
#   bash scripts/_k20_endpoint_resume.sh
#
# Reads endpoint-id from pass. Polls every 30s for up to 8 min for
# replica to land. Sanity-tests with chat completion. On success:
# prints the model name to stdout; exit 0. On failure: prints error
# to stderr; exit 1.
#
# Pair with scripts/_k20_endpoint_pause.sh (after R26 finishes).
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org

TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)
EP_ID=$(pass org-llm/cloud/foss-lora/endpoint-id 2>/dev/null | head -1)
EP_NAME=$(pass org-llm/cloud/foss-lora/model 2>/dev/null | head -1)

[ -z "$TG_KEY" ] && { echo "ERR no Together key" >&2; exit 1; }
[ -z "$EP_ID" ]  && { echo "ERR no endpoint-id in pass" >&2; exit 1; }

log() { echo "[k20-resume $(date +%H:%M:%S)] $*" >> "$LOG"; }

log "starting $EP_ID"

curl -sS --max-time 15 -X PATCH "https://api.together.xyz/v1/endpoints/$EP_ID" \
  -H "Authorization: Bearer $TG_KEY" -H "Content-Type: application/json" \
  -d '{"state":"STARTED"}' > /dev/null

# Poll for ready
DEADLINE=$(($(date +%s) + 480))   # 8 min cap
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    READY=$(curl -sS --max-time 10 \
        "https://api.together.xyz/v1/endpoints/$EP_ID" \
        -H "Authorization: Bearer $TG_KEY" \
        | jq -r '.autoscaling.ready_replicas // 0' 2>/dev/null)
    log "  ready_replicas=$READY"
    [ "$READY" -ge 1 ] && break
    sleep 30
done

if [ "$READY" -lt 1 ]; then
    log "FAIL — never reached ready"
    echo "ERR endpoint never became ready" >&2
    exit 1
fi

# Sanity test
RESP=$(curl -sS --max-time 30 -X POST https://api.together.xyz/v1/chat/completions \
    -H "Authorization: Bearer $TG_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$EP_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply: K20 ALIVE\"}],\"max_tokens\":20}")

if echo "$RESP" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
    log "SANITY PASS — endpoint live"
    echo "$EP_NAME"   # stdout for callers
    exit 0
else
    log "SANITY FAIL: $(echo "$RESP" | jq -r '.error.message // .')"
    echo "ERR sanity test failed: $RESP" >&2
    exit 1
fi
