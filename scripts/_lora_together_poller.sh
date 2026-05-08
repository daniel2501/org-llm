#!/bin/bash
# Poll Together fine-tune job every 3 min; when done, store endpoint
# URL in pass + log to docs/wiki/2026-05-08-r19-lora-progress.org.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS_DOC=$REPO/docs/wiki/2026-05-08-r19-lora-progress.org

JOB_ID=$(pass org-llm/cloud/together/k20-job-id 2>/dev/null | head -1)
TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)

[ -z "$JOB_ID" ] && { echo "no job id in pass"; exit 1; }
[ -z "$TG_KEY" ] && { echo "no Together key"; exit 1; }

log_line() { echo "[lora-together $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_doc() {
    {
        echo
        echo "** [lora-together $(date +%H:%M:%S)] $*"
    } >> "$PROGRESS_DOC"
}

log_line "started polling Together job $JOB_ID"
log_doc "Together poller started for $JOB_ID"

while true; do
    STATUS_JSON=$(python3 -c "
import os, json
os.environ['TOGETHER_API_KEY'] = '$TG_KEY'
from together import Together
# Cloudflare in front of api.together.xyz 403s opaque/default UAs with
# 'error code: 1010'. Override the SDK's default UA to identify org-llm.
client = Together(default_headers={
    'User-Agent': 'org-llm/0.1 (https://github.com/daniel2501/org-llm)'
})
ft = client.fine_tuning.retrieve('$JOB_ID')
print(json.dumps({
    'status': ft.status,
    'output_name': getattr(ft, 'output_name', None),
    'trained_tokens': getattr(ft, 'trained_tokens', None),
    'epochs_completed': getattr(ft, 'epochs_completed', None),
}))" 2>&1)

    STATUS=$(echo "$STATUS_JSON" | python3 -c "import sys, json; print(json.loads(sys.stdin.read()).get('status', '?'))" 2>/dev/null)
    OUTPUT=$(echo "$STATUS_JSON" | python3 -c "import sys, json; print(json.loads(sys.stdin.read()).get('output_name', '') or '')" 2>/dev/null)

    log_line "status=$STATUS output=${OUTPUT:-pending}"

    case "$STATUS" in
        completed)
            log_line "TRAINING COMPLETE — output: $OUTPUT"
            log_doc "TRAINING COMPLETE on Together. Endpoint: $OUTPUT"
            # Store endpoint URL in pass for harness to use
            echo "https://api.together.xyz/v1/chat/completions" | pass insert -e -f org-llm/cloud/foss-lora/url 2>/dev/null
            echo "$OUTPUT" | pass insert -e -f org-llm/cloud/foss-lora/model 2>/dev/null
            log_line "endpoint URL + model stored in pass"
            break
            ;;
        failed|cancelled|error)
            log_line "TRAINING $STATUS — see Together dashboard"
            log_doc "TRAINING $STATUS — manual investigation needed."
            break
            ;;
        running|pending|queued)
            # keep going
            ;;
        *)
            log_line "unknown status: $STATUS — continuing"
            ;;
    esac
    sleep 180
done

log_line "poller exit"
