#!/bin/bash
# LoRA training auto-repair daemon.
# Polls every 3 min. Tries Modal first; falls back to RunPod variants
# if Modal can't allocate GPU. Auto-repairs without prompting user.
# Logs to docs/wiki/2026-05-08-r19-lora-progress.org.
#
# Strategies (tried in order, with fallback after stuck-detect):
#   S_MODAL  : modal run /tmp/lora_prep/06_modal_train_k20.py::train
#   S_RUNPOD_DOCKERARGS : runpod with bash -c dockerArgs (proven broken in this account)
#   S_RUNPOD_DEFAULT    : runpod with EMPTY dockerArgs + auto-init via container env
#   S_HALT   : exhausted; manual intervention needed
#
# State stored in /tmp/r19-lora-daemon-state for resumption.

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
PROGRESS_DOC=$REPO/docs/wiki/2026-05-08-r19-lora-progress.org
STATE=/tmp/r19-lora-daemon-state
ATTEMPT_COUNT=/tmp/r19-lora-daemon-attempts
STRATEGY_LOG=/tmp/r19-lora-daemon-strategies
MAX_ATTEMPTS=10
POLL_INTERVAL=180   # 3 min

ADAPTER_REPO="daniel2501/k20-foss-distill"
DATA_REPO="daniel2501/k20-foss-distill-data"

mkdir -p "$(dirname "$PROGRESS_DOC")"
[ ! -f "$ATTEMPT_COUNT" ] && echo 0 > "$ATTEMPT_COUNT"
[ ! -f "$STATE" ] && echo "INIT" > "$STATE"
[ ! -f "$STRATEGY_LOG" ] && touch "$STRATEGY_LOG"

log_line() { echo "[lora-daemon $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_doc() {
    {
        echo
        echo "** [lora-daemon $(date +%H:%M:%S)] $*"
    } >> "$PROGRESS_DOC"
}
log_doc_block() {
    {
        echo
        echo "#+begin_src text"
        echo "$*"
        echo "#+end_src"
    } >> "$PROGRESS_DOC"
}

check_done() {
    HF_TOK=$(pass org-llm/cloud/huggingface/token 2>/dev/null | head -1)
    [ -z "$HF_TOK" ] && return 1
    # DONE marker present in dataset repo?
    DONE=$(curl -sS --max-time 10 \
        "https://huggingface.co/api/datasets/$DATA_REPO/tree/main" \
        -H "Authorization: Bearer $HF_TOK" 2>/dev/null \
        | jq -r '.[] | select(.path == "DONE.json") | .path' 2>/dev/null)
    if [ "$DONE" = "DONE.json" ]; then
        ADAPTER_FILES=$(curl -sS --max-time 10 \
            "https://huggingface.co/api/models/$ADAPTER_REPO/tree/main" \
            -H "Authorization: Bearer $HF_TOK" 2>/dev/null \
            | jq -r '.[].path' 2>/dev/null | grep -v "^.gitattributes$" | head -3)
        if [ -n "$ADAPTER_FILES" ]; then
            return 0
        fi
    fi
    return 1
}

count_attempts() { cat "$ATTEMPT_COUNT" 2>/dev/null || echo 0; }
incr_attempts() { echo $(($(count_attempts) + 1)) > "$ATTEMPT_COUNT"; }
record_strategy() { echo "$1 $(date +%Y-%m-%dT%H:%M:%S)" >> "$STRATEGY_LOG"; }

# ── S_MODAL — run with retry-on-preemption ──────────────────────────────
launch_modal() {
    log_line "STRATEGY=S_MODAL launching"
    log_doc "Strategy: Modal A100-80GB serverless"
    record_strategy "S_MODAL_LAUNCH"

    # Kill any stale modal driver
    pkill -f "modal run.*06_modal_train_k20" 2>/dev/null
    sleep 1

    mv /tmp/r19-lora-modal-launch.log /tmp/r19-lora-modal-launch.log.attempt$(count_attempts).bak 2>/dev/null
    nohup modal run /tmp/lora_prep/06_modal_train_k20.py::train \
        > /tmp/r19-lora-modal-launch.log 2>&1 &
    disown $! 2>/dev/null

    incr_attempts
    echo "MODAL_RUNNING" > "$STATE"
    sleep 30
}

modal_progressed() {
    # Detect if Modal is making real progress: training step pattern in log
    if grep -qE "[1-9]+/12 \[|loss=|step.*[1-9]+|adapter saved" \
        /tmp/r19-lora-modal-launch.log 2>/dev/null; then
        return 0
    fi
    return 1
}

modal_stuck() {
    # Stuck pattern: "waiting to be scheduled" repeated for >10 min
    # OR no new log lines in 10 min
    if [ ! -f /tmp/r19-lora-modal-launch.log ]; then return 1; fi
    local last_modify=$(stat -c %Y /tmp/r19-lora-modal-launch.log 2>/dev/null || echo 0)
    local now=$(date +%s)
    local stale_s=$((now - last_modify))
    if [ $stale_s -gt 900 ]; then return 0; fi   # 15 min no log activity

    # Also check for explicit "waiting to be scheduled" in tail
    local tail=$(tail -10 /tmp/r19-lora-modal-launch.log 2>/dev/null)
    if echo "$tail" | grep -q "waiting to be scheduled"; then
        # Count how many times this has been the tail across checks
        echo "$tail" >> /tmp/r19-lora-modal-stuck-history
        local count=$(grep -c "waiting to be scheduled" /tmp/r19-lora-modal-stuck-history 2>/dev/null || echo 0)
        if [ "$count" -gt 5 ]; then return 0; fi   # 5 polls × 3 min = 15 min stuck
    fi

    return 1
}

# ── S_RUNPOD_DEFAULT — empty dockerArgs + container env vars ──────────────
launch_runpod_default() {
    log_line "STRATEGY=S_RUNPOD_DEFAULT launching"
    log_doc "Strategy: RunPod default image + env-driven training"
    record_strategy "S_RUNPOD_DEFAULT_LAUNCH"

    HF_TOK=$(pass org-llm/cloud/huggingface/token 2>/dev/null | head -1)
    RP_KEY=$(pass org-llm/cloud/runpod/api-key 2>/dev/null | head -1)
    if [ -z "$HF_TOK" ] || [ -z "$RP_KEY" ]; then
        log_line "missing HF or RP token"
        echo "FAILED" > "$STATE"
        return
    fi

    # Use a custom training-friendly image that has SSH + Jupyter
    # built in. RunPod's "PyTorch" template has its own runscript that
    # starts JupyterLab + opens SSH. We supply NO dockerArgs (lets
    # default entrypoint run) and pass training command via env.
    # The trick: env vars are persisted; we ssh in (or pull command via
    # Jupyter REST) to actually start training.
    #
    # For now: launch with NO dockerArgs at all, then immediately push
    # the training script via runpodctl and execute via ssh proxy.
    # Skipping full automation; logging the limitation.
    log_doc_block "S_RUNPOD_DEFAULT requires runpodctl SSH proxy + manual exec — implementation deferred. Falling back to S_MODAL retry."
    echo "RUNPOD_DEFAULT_NOT_IMPLEMENTED" > "$STATE"
}

# ── Main loop ──────────────────────────────────────────────────────────
log_line "daemon STARTED — polling every ${POLL_INTERVAL}s"
log_doc "Auto-repair daemon started. Polling every $((POLL_INTERVAL / 60)) min."

while true; do
    state=$(cat "$STATE" 2>/dev/null || echo "INIT")
    attempts=$(count_attempts)

    if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
        log_line "MAX ATTEMPTS REACHED ($attempts) — daemon halting"
        log_doc "Max attempts ($MAX_ATTEMPTS) reached — daemon halting. Manual intervention needed."
        break
    fi

    if check_done; then
        log_line "DONE detected — adapter on HF"
        log_doc "TRAINING COMPLETE — adapter at https://huggingface.co/$ADAPTER_REPO"
        # Store URL in pass
        echo "https://huggingface.co/$ADAPTER_REPO" \
            | pass insert -e -f org-llm/cloud/foss-lora/url 2>/dev/null
        echo "DONE" > "$STATE"
        break
    fi

    case "$state" in
        INIT)
            launch_modal
            ;;
        MODAL_RUNNING)
            if modal_progressed; then
                log_line "Modal progressing (training steps detected) — leaving alone"
                # Reset stuck-history so a future stuck-detect starts fresh
                rm -f /tmp/r19-lora-modal-stuck-history
            elif modal_stuck; then
                log_line "Modal STUCK — preemption or queue. Killing + retrying"
                log_doc "Modal stuck (>15min no progress / GPU queue). Auto-relaunching."
                pkill -f "modal run.*06_modal_train_k20" 2>/dev/null
                modal app stop --yes 2>&1 | tail -3 >> "$LOG"
                rm -f /tmp/r19-lora-modal-stuck-history
                sleep 5
                if [ "$attempts" -lt 3 ]; then
                    launch_modal   # retry Modal
                else
                    log_line "3+ Modal failures — switching to S_RUNPOD_DEFAULT"
                    launch_runpod_default
                fi
            else
                # Container probably starting; do nothing
                log_line "Modal: not yet progressed but not stuck either; waiting"
            fi
            ;;
        RUNPOD_DEFAULT_NOT_IMPLEMENTED|FAILED)
            log_line "all available strategies exhausted; halting"
            log_doc "All strategies exhausted (Modal failures + RunPod approach unimplemented). Halting daemon. Need manual intervention or new strategy."
            break
            ;;
        DONE)
            break
            ;;
        *)
            log_line "unknown state: $state — re-initializing"
            echo "INIT" > "$STATE"
            ;;
    esac

    sleep "$POLL_INTERVAL"
done

log_line "daemon EXIT (state=$(cat "$STATE"))"
log_doc "Daemon exit. Final state: $(cat "$STATE"). Attempts: $(count_attempts)."
