#!/bin/bash
# Overnight autopilot — monitors the 3 R19-prep agents + commits/logs
# their output as it lands. Runs unattended until all 3 done.
set -uo pipefail

REPO=/home/daniel/repos/org-llm
LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
cd "$REPO"

log_line() { echo "[overnight $(date +%H:%M:%S)] $*" >> "$LOG"; }
log_block() { echo >> "$LOG"; printf '%s\n' "$@" >> "$LOG"; }

log_block "" "** [overnight] watching 3 R19-prep agent outputs"

# Files we're waiting on (per spawn prompts)
TARGETS=(
    "/tmp/r19-foss-lora-training-report.org:lora-foss-distill"
    "/tmp/r19-vault-index-report.org:qdrant-vault-rag"
    "/tmp/r19-long-horizon-design.org:long-horizon-tasks"
)

# Per-target seen flag
declare -A SEEN
for t in "${TARGETS[@]}"; do
    name=${t##*:}
    SEEN["$name"]=0
done

DONE_COUNT=0
TOTAL=${#TARGETS[@]}

while [ $DONE_COUNT -lt $TOTAL ]; do
    for entry in "${TARGETS[@]}"; do
        path=${entry%%:*}
        name=${entry##*:}
        if [ "${SEEN[$name]}" = "1" ]; then continue; fi
        if [ -f "$path" ]; then
            SEEN[$name]=1
            DONE_COUNT=$((DONE_COUNT + 1))
            target_doc="docs/notes/2026-05-08-r19-prep-${name}.org"
            cp "$path" "$target_doc" 2>/dev/null
            git add "$target_doc" 2>/dev/null
            git commit -m "docs(notes): R19 prep — ${name} agent report

Auto-committed by overnight autopilot when /tmp/$(basename $path) landed.
Full report at $target_doc.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 >/dev/null

            log_block "" "** [overnight $(date +%H:%M:%S)] $name agent COMPLETED"
            log_line "report saved to $target_doc + committed"
            log_line "first 5 lines of report:"
            log_block "#+begin_src text" "$(head -5 $path)" "#+end_src"

            # Special handling per agent
            case "$name" in
                lora-foss-distill)
                    if pass org-llm/cloud/modal-foss-lora/url 2>/dev/null | head -c 1 >/dev/null; then
                        URL=$(pass org-llm/cloud/modal-foss-lora/url 2>/dev/null | head -1)
                        log_line "FOSS LoRA endpoint live: $URL"
                    else
                        log_line "FOSS LoRA endpoint URL not in pass yet — agent may still be training"
                    fi
                    ;;
                qdrant-vault-rag)
                    if [ -f "$REPO/org_llm/vault_rag.py" ]; then
                        log_line "vault_rag.py module wired"
                        # Commit any new module + specialist.py changes
                        git add org_llm/vault_rag.py org_llm/specialist.py scripts/r19_qdrant_index.py 2>/dev/null
                        git commit -m "feat(r19): Qdrant vault RAG — vault_search tool wired

Auto-committed by overnight autopilot.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 >/dev/null
                    fi
                    ;;
                long-horizon-tasks)
                    if [ -f "$REPO/scripts/_round19_long_horizon_tasks.py" ]; then
                        log_line "long-horizon tasks module ready"
                        git add scripts/_round19_long_horizon_tasks.py 2>/dev/null
                        git commit -m "feat(r19): long-horizon task fixtures BK1-BK5

Auto-committed by overnight autopilot.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" 2>&1 >/dev/null
                    fi
                    ;;
            esac
        fi
    done
    [ $DONE_COUNT -lt $TOTAL ] && sleep 300   # check every 5 min
done

log_block "" "** [overnight $(date +%H:%M:%S)] ALL 3 R19-PREP AGENTS COMPLETE"
log_line "ready inputs for R19 design pass when user wakes"
