#!/bin/bash
# K20 endpoint pause helper — stop the dedicated Together endpoint
# to halt $7.98/hr billing. Symmetric with _k20_endpoint_resume.sh.
set -uo pipefail
REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)
EP_ID=$(pass org-llm/cloud/foss-lora/endpoint-id 2>/dev/null | head -1)
[ -z "$TG_KEY" ] || [ -z "$EP_ID" ] && { echo "ERR creds missing"; exit 1; }
echo "[k20-pause $(date +%H:%M:%S)] stopping $EP_ID" >> "$LOG"
curl -sS --max-time 15 -A "org-llm/0.1 (https://github.com/daniel2501/org-llm)" \
    -X PATCH "https://api.together.xyz/v1/endpoints/$EP_ID" \
    -H "Authorization: Bearer $TG_KEY" -H "Content-Type: application/json" \
    -d '{"state":"STOPPED"}' | jq -r '.state' 2>&1
