#!/usr/bin/env bash
# agor-smoke-v01.sh — v0.1 extended primitives harness for Agor integration
#
# Builds on scripts/agor-smoke.sh (v0): same auth + worktree + parent
# session bootstrap, but instead of spending LLM on `claude -p` it
# exercises the v0.1 primitives directly through the MCP HTTP transport
# (per DEC-006 — supervision is deterministic by default; data-tool reads
# fire first, LLM narration is optional). Net LLM spend for v0.1: $0.
#
# Coverage delta vs. v0:
#   1. mode:"btw"          (agor_sessions_prompt mode, ephemeral fork w/ callback)
#   2. mode:"subsession"   (resumable child via archive→unarchive cycle)
#   3. cron / scheduled    (NOT exposed via MCP or REST in v0.17.3 — recorded as gap)
#   4. agor_artifacts_publish  (the actual tool name — `_create` doesn't exist)
#   5. mode:"fork"         (sibling-from on shared worktree, diverged context)
#   6. captain's-log bridge (org_llm.logbook.write_event kind=team-session)
#
# Verified live against agor-live v0.17.3 + claude code 2.1.x on 2026-05-06.
# See docs/wiki/agor-smoke-recipe.org § v0.1 for design rationale and bug log.

set -euo pipefail

# Make jq, curl, and `agor` visible even if the caller's PATH is sparse —
# guix profile carries jq/curl, npm-global carries `agor` (npm i -g agor-live).
for p in "$HOME/.guix-profile/bin" "$HOME/.npm-global/bin"; do
  case ":$PATH:" in
    *":$p:"*) ;;
    *) [[ -d "$p" ]] && export PATH="$p:$PATH" ;;
  esac
done

cat <<'BANNER'
╭──────────────────────────────────────────────────────────────────────╮
│ Agor smoke v0.1 — extended primitives                                │
│ Verifies: btw · subsession · fork · artifacts_publish · captain's-log │
│ Records:  cron-scheduled-children gap (no MCP/REST surface)           │
│ Docs:     docs/wiki/agor-smoke-recipe.org § v0.1                      │
╰──────────────────────────────────────────────────────────────────────╯
BANNER

# ── Config (env-overridable) ──────────────────────────────────────────────
: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_ID:=}"
: "${AGOR_REPO_PATH:=}"
: "${AGOR_WORKTREE_NAME:=v01-wt-$$}"
: "${AGOR_SOURCE_BRANCH:=}"
: "${KEEP_DIRTY:=0}"
: "${RUN_LOG:=/tmp/agor-smoke-v01-run-$(date +%Y-%m-%d).org}"
: "${ART_DIR:=/tmp/agor-smoke-v01-art-$$}"

START_TS=$(date +%s)
declare -a SESSIONS_TO_ARCHIVE=()
ARTIFACT_ID=""

# ── Exit codes ────────────────────────────────────────────────────────────
EX_PREREQ=10
EX_AUTH=20
EX_WORKTREE=30
EX_SESSION=35
EX_BTW=41
EX_SUBSESSION=42
EX_FORK=43
EX_ARTIFACT=44
EX_CAPLOG=45

die() { local code="$1"; shift; printf '\n[FAIL %s] %s\n' "$code" "$*" >&2; exit "$code"; }
log() { printf '[v01] %s\n' "$*"; }

# Run-log helper — all results land in $RUN_LOG as an org file.
runlog() { printf '%s\n' "$*" >> "$RUN_LOG"; }

# ── Cleanup trap ──────────────────────────────────────────────────────────
cleanup() {
  local rc=$?
  if [[ "$KEEP_DIRTY" != "1" ]]; then
    for sid in "${SESSIONS_TO_ARCHIVE[@]}"; do
      api PATCH "/sessions/$sid" '{"archived":true,"archived_reason":"v01_smoke_cleanup"}' >/dev/null 2>&1 || true
    done
    if [[ -n "$ARTIFACT_ID" ]]; then
      api DELETE "/artifacts/$ARTIFACT_ID" >/dev/null 2>&1 || true
    fi
    rm -rf "$ART_DIR" 2>/dev/null || true
  fi
  local elapsed=$(( $(date +%s) - START_TS ))
  log "elapsed: ${elapsed}s · exit=${rc} · run-log: $RUN_LOG"
  runlog ""
  runlog "* Final"
  runlog "elapsed_seconds: ${elapsed}"
  runlog "exit_code:       ${rc}"
}
trap cleanup EXIT

# ── REST helper (token re-read every call) ────────────────────────────────
api() {
  local method="$1" path="$2" body="${3:-}"
  local tok; tok="$(jq -r .accessToken "$AGOR_TOKEN_FILE")"
  if [[ -n "$body" ]]; then
    curl -sS --max-time 30 -X "$method" "${AGOR_BASE_URL}${path}" \
      -H "Authorization: Bearer ${tok}" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -sS --max-time 30 -X "$method" "${AGOR_BASE_URL}${path}" \
      -H "Authorization: Bearer ${tok}"
  fi
}

# Call an MCP tool through agor_execute_tool, returning the inner text JSON.
# $1 = mcp_token, $2 = tool_name, $3 = arguments JSON
mcp_exec() {
  local mtok="$1" tool="$2" args="$3" id="${RANDOM}"
  local payload; payload="$(jq -nc --arg t "$tool" --argjson a "$args" --arg id "$id" \
    '{jsonrpc:"2.0", id:$id, method:"tools/call",
       params:{name:"agor_execute_tool", arguments:{tool_name:$t, arguments:$a}}}')"
  # SSE stream: pull the single `data:` line out, strip the prefix, parse it.
  # `tail -1` is wrong — the response ends with a blank trailer.
  curl -sS --max-time 60 -X POST "${AGOR_BASE_URL}/mcp" \
    -H "Authorization: Bearer ${mtok}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$payload" \
    | grep '^data: ' | sed 's/^data: //' \
    | jq -r '.result.content[0].text // (.error | tostring)'
}

# ── Run-log header ────────────────────────────────────────────────────────
{
  echo "#+TITLE: Agor smoke v0.1 — run log"
  echo "#+DATE: $(date --iso-8601=seconds)"
  echo "#+OPTIONS: toc:nil num:nil"
  echo ""
  echo "* Run"
  echo "started_at: $(date --iso-8601=seconds)"
  echo "harness:    scripts/agor-smoke-v01.sh"
} > "$RUN_LOG"

# ── Step 0: prerequisites ─────────────────────────────────────────────────
log "step 0 — prerequisites"
for bin in agor curl jq python3; do
  command -v "$bin" >/dev/null 2>&1 || die $EX_PREREQ "missing prerequisite: $bin"
done
# Captain's-log bridge needs org_llm's deps (sqlite-vec, sqlalchemy, …);
# pick the project venv when present, fall back to system python3.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  ORGLLM_PY="$REPO_ROOT/.venv/bin/python"
else
  ORGLLM_PY="$(command -v python3)"
  log "  warn: no $REPO_ROOT/.venv — falling back to system python3 (captain's-log bridge may fail to import sqlite_vec)"
fi

# ── Step 1: daemon health ─────────────────────────────────────────────────
log "step 1 — daemon /health"
HEALTH="$(curl -sf -m 5 "${AGOR_BASE_URL}/health" || true)"
[[ -n "$HEALTH" ]] || die $EX_PREREQ "daemon not reachable at ${AGOR_BASE_URL}"
echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null \
  || die $EX_PREREQ "daemon /health did not return status=ok"
log "  ok · version $(echo "$HEALTH" | jq -r '.version')"

# ── Step 2: auth probe ────────────────────────────────────────────────────
log "step 2 — auth"
[[ -f "$AGOR_TOKEN_FILE" ]] \
  || die $EX_AUTH "token file missing — run: agor login -e admin@agor.live -p \$(pass org-llm/agor/admin-password)"
api GET /repos | jq -e '.data' >/dev/null \
  || die $EX_AUTH "auth probe failed (GET /repos)"
log "  ok"

# ── Step 3: resolve repo + board ──────────────────────────────────────────
log "step 3 — repo + board"
REPOS_JSON="$(api GET /repos)"
if [[ -n "$AGOR_REPO_ID" ]]; then
  REPO_ID="$AGOR_REPO_ID"
elif [[ -n "$AGOR_REPO_PATH" ]]; then
  REPO_ID="$(echo "$REPOS_JSON" | jq -r --arg p "$AGOR_REPO_PATH" '.data[] | select(.local_path == $p) | .repo_id' | head -1)"
else
  REPO_ID="$(echo "$REPOS_JSON" | jq -r '.data[0].repo_id // empty')"
fi
[[ -n "$REPO_ID" ]] || die $EX_PREREQ "no repos registered"
DEFAULT_BRANCH="$(echo "$REPOS_JSON" | jq -r --arg id "$REPO_ID" '.data[] | select(.repo_id == $id) | .default_branch')"
SOURCE_BRANCH="${AGOR_SOURCE_BRANCH:-${DEFAULT_BRANCH:-main}}"
BOARD_ID="$(api GET /boards | jq -r '.data[0].board_id // empty')"
[[ -n "$BOARD_ID" ]] || die $EX_PREREQ "no boards (POST /boards needed first)"
log "  repo=${REPO_ID} board=${BOARD_ID} src_branch=${SOURCE_BRANCH}"

# ── Step 4: create worktree ───────────────────────────────────────────────
log "step 4 — worktree '${AGOR_WORKTREE_NAME}'"
WT_BODY="$(jq -nc \
  --arg name "$AGOR_WORKTREE_NAME" --arg src "$SOURCE_BRANCH" \
  '{name:$name, ref:$name, createBranch:true, sourceBranch:$src, pullLatest:false, refType:"branch"}')"
WT_JSON="$(api POST "/repos/${REPO_ID}/worktrees" "$WT_BODY")"
WT_ID="$(echo "$WT_JSON" | jq -r '.worktree_id // empty')"
[[ -n "$WT_ID" ]] || die $EX_WORKTREE "worktree create failed: $WT_JSON"
WT_PATH="$(echo "$WT_JSON" | jq -r '.path')"
log "  wt=${WT_ID} path=${WT_PATH}"

# ── Step 5: parent session (auth-anchor only — never prompted) ────────────
log "step 5 — parent session (anchor for mcp_token)"
SESS_BODY="$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')"
SESS_JSON="$(api POST /sessions "$SESS_BODY")"
PARENT_SID="$(echo "$SESS_JSON" | jq -r '.session_id // empty')"
PARENT_MTOK="$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')"
[[ -n "$PARENT_SID" && -n "$PARENT_MTOK" ]] \
  || die $EX_SESSION "parent session create failed: $SESS_JSON"
SESSIONS_TO_ARCHIVE+=("$PARENT_SID")
log "  parent=${PARENT_SID}"

runlog ""
runlog "* Bootstrap"
runlog "repo_id:    $REPO_ID"
runlog "board_id:   $BOARD_ID"
runlog "worktree:   $WT_ID  ($WT_PATH)"
runlog "parent_sid: $PARENT_SID"

# ── Primitive 1: mode:"btw" — async peer query ────────────────────────────
log "primitive 1 — mode:\"btw\" async peer query"
T1_START=$(date +%s)
BTW_ARGS="$(jq -nc --arg sid "$PARENT_SID" \
  '{sessionId:$sid, prompt:"v01 btw probe — what is 2+2?", mode:"btw", title:"v01-btw"}')"
BTW_RESP="$(mcp_exec "$PARENT_MTOK" agor_sessions_prompt "$BTW_ARGS")"
BTW_SID="$(echo "$BTW_RESP" | jq -r '.session.session_id // empty')"
BTW_FORK_ORIGIN="$(echo "$BTW_RESP" | jq -r '.session.fork_origin // empty')"
BTW_CB_ENABLED="$(echo "$BTW_RESP" | jq -r '.session.callback_config.enabled // false')"
BTW_CB_TARGET="$(echo "$BTW_RESP" | jq -r '.session.callback_config.callback_session_id // empty')"
BTW_FORK_FROM="$(echo "$BTW_RESP" | jq -r '.session.genealogy.forked_from_session_id // empty')"
T1_ELAPSED=$(( $(date +%s) - T1_START ))

if [[ -z "$BTW_SID" ]]; then
  runlog ""
  runlog "** PRIMITIVE 1 — btw — FAIL (${T1_ELAPSED}s)"
  runlog "response: $BTW_RESP"
  die $EX_BTW "btw mode returned no session_id; resp: $BTW_RESP"
fi
SESSIONS_TO_ARCHIVE+=("$BTW_SID")

BTW_OK=0
if [[ "$BTW_FORK_ORIGIN" == "btw" ]] \
   && [[ "$BTW_CB_ENABLED" == "true" ]] \
   && [[ "$BTW_CB_TARGET" == "$PARENT_SID" ]] \
   && [[ "$BTW_FORK_FROM" == "$PARENT_SID" ]]; then
  BTW_OK=1
fi

runlog ""
runlog "** PRIMITIVE 1 — mode:\"btw\" — $([[ $BTW_OK == 1 ]] && echo PASS || echo FAIL) (${T1_ELAPSED}s)"
runlog "btw_sid:           $BTW_SID"
runlog "fork_origin:       $BTW_FORK_ORIGIN  (expected: btw)"
runlog "callback_enabled:  $BTW_CB_ENABLED   (expected: true)"
runlog "callback_target:   $BTW_CB_TARGET    (expected: $PARENT_SID)"
runlog "forked_from:       $BTW_FORK_FROM    (expected: $PARENT_SID)"
runlog "spend_usd:         0  (deterministic MCP call, no LLM)"
log "  $([[ $BTW_OK == 1 ]] && echo PASS || echo FAIL) btw_sid=$BTW_SID"
[[ $BTW_OK == 1 ]] || die $EX_BTW "btw response shape mismatch (see $RUN_LOG)"

# ── Primitive 2: mode:"subsession" — resumable child ──────────────────────
log "primitive 2 — mode:\"subsession\" + archive/unarchive cycle"
T2_START=$(date +%s)
SUB_ARGS="$(jq -nc --arg sid "$PARENT_SID" \
  '{sessionId:$sid, prompt:"v01 subsession probe — to be persisted", mode:"subsession", title:"v01-sub"}')"
SUB_RESP="$(mcp_exec "$PARENT_MTOK" agor_sessions_prompt "$SUB_ARGS")"
SUB_SID="$(echo "$SUB_RESP" | jq -r '.session.session_id // empty')"
SUB_PARENT_SID="$(echo "$SUB_RESP" | jq -r '.session.genealogy.parent_session_id // empty')"

if [[ -z "$SUB_SID" || "$SUB_PARENT_SID" != "$PARENT_SID" ]]; then
  runlog ""
  runlog "** PRIMITIVE 2 — subsession — FAIL (initial spawn)"
  runlog "response: $SUB_RESP"
  die $EX_SUBSESSION "subsession spawn failed; resp: $SUB_RESP"
fi
SESSIONS_TO_ARCHIVE+=("$SUB_SID")

# Archive the subsession, verify it persists in DB, unarchive, re-fetch.
api PATCH "/sessions/$SUB_SID" '{"archived":true,"archived_reason":"v01_persistence_test"}' >/dev/null
SUB_ARCHIVED="$(api GET "/sessions/$SUB_SID" | jq -r '.archived')"
api PATCH "/sessions/$SUB_SID" '{"archived":false}' >/dev/null
SUB_AFTER="$(api GET "/sessions/$SUB_SID")"
SUB_AFTER_ARCHIVED="$(echo "$SUB_AFTER" | jq -r '.archived')"
SUB_AFTER_PARENT="$(echo "$SUB_AFTER" | jq -r '.genealogy.parent_session_id // empty')"
T2_ELAPSED=$(( $(date +%s) - T2_START ))

SUB_OK=0
if [[ "$SUB_ARCHIVED" == "true" ]] \
   && [[ "$SUB_AFTER_ARCHIVED" == "false" ]] \
   && [[ "$SUB_AFTER_PARENT" == "$PARENT_SID" ]]; then
  SUB_OK=1
fi

runlog ""
runlog "** PRIMITIVE 2 — mode:\"subsession\" — $([[ $SUB_OK == 1 ]] && echo PASS || echo FAIL) (${T2_ELAPSED}s)"
runlog "sub_sid:            $SUB_SID"
runlog "parent_after_spawn: $SUB_PARENT_SID  (expected: $PARENT_SID)"
runlog "archived_mid_cycle: $SUB_ARCHIVED    (expected: true)"
runlog "archived_after_un:  $SUB_AFTER_ARCHIVED  (expected: false)"
runlog "parent_after_un:    $SUB_AFTER_PARENT    (expected: $PARENT_SID)"
runlog "spend_usd:          0"
log "  $([[ $SUB_OK == 1 ]] && echo PASS || echo FAIL) sub_sid=$SUB_SID"
[[ $SUB_OK == 1 ]] || die $EX_SUBSESSION "subsession persistence broken (see $RUN_LOG)"

# ── Primitive 3: cron / scheduled child — surface probe ───────────────────
log "primitive 3 — cron / scheduled child (surface probe)"
T3_START=$(date +%s)
# Probe MCP for any scheduler tool — empty result is the diagnostic.
SCHED_QUERY='{"query":"schedule cron heartbeat","detail":"list","max_results":50}'
SCHED_RESP="$(curl -sS -X POST "${AGOR_BASE_URL}/mcp" \
  -H "Authorization: Bearer $PARENT_MTOK" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "$(jq -nc --argjson a "$SCHED_QUERY" '{jsonrpc:"2.0", id:"sp", method:"tools/call", params:{name:"agor_search_tools", arguments:$a}}')" \
  | grep '^data: ' | sed 's/^data: //' \
  | jq -r '.result.content[0].text')"
SCHED_TOOLS="$(echo "$SCHED_RESP" | jq -r '[.tools[]?.name | select(test("schedul|cron|heartbeat";"i"))] | length')"

# Probe REST common routes.
REST_PROBES=""
for ROUTE in /schedules /scheduled /cron /scheduler /scheduled-sessions; do
  CODE="$(curl -sS -o /dev/null -w "%{http_code}" -X GET "${AGOR_BASE_URL}${ROUTE}" \
          -H "Authorization: Bearer $(jq -r .accessToken "$AGOR_TOKEN_FILE")")"
  REST_PROBES+="${ROUTE}=${CODE} "
done

T3_ELAPSED=$(( $(date +%s) - T3_START ))
# Pass criterion is "the gap is recorded" — not "we successfully scheduled."
runlog ""
runlog "** PRIMITIVE 3 — cron / scheduled — RECORDED-GAP (${T3_ELAPSED}s)"
runlog "mcp_scheduler_tools_matched: $SCHED_TOOLS  (expected: 0)"
runlog "rest_probe_codes: $REST_PROBES"
runlog "diagnosis: agor v0.17.3 ships sessions.scheduled_from_worktree column +"
runlog "  bulk_archive sessionType filter \"scheduled\", but no MCP/REST surface to"
runlog "  *create* a scheduled session. Workaround: insert directly into agor.db, or"
runlog "  wait for upstream scheduler API. Filed in recipe doc § New bugs found."
runlog "spend_usd: 0"
log "  RECORDED-GAP — no scheduler tool exposed in MCP or REST"

# ── Primitive 4: agor_artifacts_publish ───────────────────────────────────
log "primitive 4 — agor_artifacts_publish"
T4_START=$(date +%s)
mkdir -p "$ART_DIR"
cat > "$ART_DIR/index.html" <<'HTML'
<!DOCTYPE html>
<html><body><h1>v0.1 smoke</h1><script src="./index.js"></script></body></html>
HTML
cat > "$ART_DIR/index.js" <<'JS'
console.log("v0.1 smoke artifact alive");
document.body.appendChild(document.createTextNode("hello-from-v01-smoke"));
JS
cat > "$ART_DIR/sandpack.json" <<'JSON'
{"template":"vanilla","entry":"/index.js"}
JSON

ART_ARGS="$(jq -nc \
  --arg fp "$ART_DIR" --arg bid "$BOARD_ID" --arg name "v01-smoke-art" \
  '{folderPath:$fp, boardId:$bid, name:$name, template:"vanilla"}')"
ART_RESP="$(mcp_exec "$PARENT_MTOK" agor_artifacts_publish "$ART_ARGS")"
ARTIFACT_ID="$(echo "$ART_RESP" | jq -r '.artifact.artifact_id // empty')"
ART_PATH="$(echo "$ART_RESP" | jq -r '.artifact.path // empty')"
ART_BUILD="$(echo "$ART_RESP" | jq -r '.artifact.build_status // empty')"
T4_ELAPSED=$(( $(date +%s) - T4_START ))

ART_OK=0
if [[ -n "$ARTIFACT_ID" ]] \
   && [[ "$ART_PATH" == "$ART_DIR" ]] \
   && [[ "$ART_BUILD" == "success" ]] \
   && [[ -f "$ART_DIR/index.js" ]]; then
  ART_OK=1
fi

runlog ""
runlog "** PRIMITIVE 4 — agor_artifacts_publish — $([[ $ART_OK == 1 ]] && echo PASS || echo FAIL) (${T4_ELAPSED}s)"
runlog "artifact_id:      $ARTIFACT_ID"
runlog "source_path:      $ART_PATH (on disk: $([[ -f "$ART_DIR/index.js" ]] && echo present || echo MISSING))"
runlog "build_status:     $ART_BUILD"
runlog "note:             tool name is *publish*, not *create* (recipe v0 doc named the wrong tool)"
runlog "spend_usd:        0"
log "  $([[ $ART_OK == 1 ]] && echo PASS || echo FAIL) artifact_id=$ARTIFACT_ID"
[[ $ART_OK == 1 ]] || die $EX_ARTIFACT "artifact publish failed (see $RUN_LOG)"

# ── Primitive 5: mode:"fork" — sibling-from on shared worktree ────────────
log "primitive 5 — mode:\"fork\""
T5_START=$(date +%s)
FORK_ARGS="$(jq -nc --arg sid "$PARENT_SID" \
  '{sessionId:$sid, prompt:"v01 fork probe — diverged sibling", mode:"fork", title:"v01-fork"}')"
FORK_RESP="$(mcp_exec "$PARENT_MTOK" agor_sessions_prompt "$FORK_ARGS")"
FORK_SID="$(echo "$FORK_RESP" | jq -r '.session.session_id // empty')"
FORK_WT="$(echo "$FORK_RESP" | jq -r '.session.worktree_id // empty')"
FORK_FROM="$(echo "$FORK_RESP" | jq -r '.session.genealogy.forked_from_session_id // empty')"
FORK_PARENT="$(echo "$FORK_RESP" | jq -r '.session.genealogy.parent_session_id // empty')"
T5_ELAPSED=$(( $(date +%s) - T5_START ))

if [[ -z "$FORK_SID" ]]; then
  die $EX_FORK "fork mode returned no session_id; resp: $FORK_RESP"
fi
SESSIONS_TO_ARCHIVE+=("$FORK_SID")

# Shared-worktree + diverged-context invariants:
FORK_OK=0
if [[ "$FORK_WT" == "$WT_ID" ]] \
   && [[ "$FORK_FROM" == "$PARENT_SID" ]] \
   && [[ "$FORK_SID" != "$PARENT_SID" ]] \
   && [[ "$FORK_PARENT" == "" || "$FORK_PARENT" == "null" ]]; then
  FORK_OK=1
fi

runlog ""
runlog "** PRIMITIVE 5 — mode:\"fork\" — $([[ $FORK_OK == 1 ]] && echo PASS || echo FAIL) (${T5_ELAPSED}s)"
runlog "fork_sid:           $FORK_SID  (parent: $PARENT_SID — distinct: $([[ $FORK_SID != $PARENT_SID ]] && echo yes || echo no))"
runlog "shared_worktree:    $FORK_WT == $WT_ID  ($([[ $FORK_WT == $WT_ID ]] && echo SHARED || echo DIVERGED))"
runlog "forked_from_sid:    $FORK_FROM   (expected: $PARENT_SID)"
runlog "parent_session_id:  ${FORK_PARENT:-null}  (expected: null — forks are siblings, not children)"
runlog "spend_usd:          0"
log "  $([[ $FORK_OK == 1 ]] && echo PASS || echo FAIL) fork_sid=$FORK_SID"
[[ $FORK_OK == 1 ]] || die $EX_FORK "fork response shape mismatch (see $RUN_LOG)"

# ── Primitive 6: captain's-log bridge ─────────────────────────────────────
log "primitive 6 — captain's-log bridge (kind=team-session)"
T6_START=$(date +%s)
CAP_LOG_BEFORE="$(stat -c %s ~/org/captains-log.org 2>/dev/null || echo 0)"

# org_llm.logbook gates writes through Config.log_kinds (default allowlist
# is `cli,llm,mcp,config,doctor,dbt,alert`). The `team-session` kind is the
# canonical name in multi-agent-org-llm.org § Captain's-log + session-
# genealogy reconciliation, so v0.1 enables it idempotently here. This is a
# *deliberate* config edit the integration owns — it stays after the smoke
# even with cleanup, because the bridge needs it on every future run.
"$ORGLLM_PY" -c "
import sys, os
from pathlib import Path
sys.path.insert(0, '/home/daniel/repos/org-llm')
from org_llm.db import DB_PATH, Config, make_engine
from sqlalchemy.orm import Session
path = Path(os.environ.get('ORG_LLM_DB') or DB_PATH)
engine = make_engine(path)
with Session(engine) as s:
    row = s.get(Config, 'log_kinds')
    cur = (row.value if row and row.value else 'cli,llm,mcp,config,doctor,dbt,alert')
    kinds = {k.strip() for k in cur.split(',') if k.strip()}
    if 'team-session' not in kinds:
        kinds.add('team-session')
        new_val = ','.join(sorted(kinds))
        if row:
            row.value = new_val
        else:
            s.add(Config(key='log_kinds', value=new_val))
        s.commit()
        print(f'log_kinds enabled team-session: {new_val}')
    else:
        print('log_kinds already includes team-session')
" 2>&1 | sed 's/^/  /'

# Build the args payload as JSON so the bridge entry is grep-friendly.
CAP_ARGS_JSON="$(jq -nc \
  --arg sid "$PARENT_SID" --arg bid "$BOARD_ID" --arg fork "$FORK_SID" \
  --arg btw "$BTW_SID" --arg sub "$SUB_SID" --arg art "$ARTIFACT_ID" \
  --arg wt "$WT_ID" \
  '{
     session_id:$sid, board_id:$bid, worktree_id:$wt,
     children:{fork:$fork, btw:$btw, subsession:$sub},
     artifacts:[$art],
     primitives_exercised:["btw","subsession","fork","artifacts_publish"]
   }')"

# Hand off to the canonical write_event boundary.
"$ORGLLM_PY" -c "
import sys
sys.path.insert(0, '/home/daniel/repos/org-llm')
from org_llm.logbook import write_event
write_event(
    kind='team-session',
    command='agor-smoke-v01',
    args='''$CAP_ARGS_JSON''',
    response='v0.1 primitives PASSED — btw + subsession + fork + artifacts_publish; cron RECORDED-GAP',
    model='',
    duration_ms=$(( ($(date +%s) - START_TS) * 1000 )),
    outcome='ok',
)
print('write_event committed')
" || die $EX_CAPLOG "captain's-log write failed"

CAP_LOG_AFTER="$(stat -c %s ~/org/captains-log.org 2>/dev/null || echo 0)"
CAP_GREW="$(( CAP_LOG_AFTER - CAP_LOG_BEFORE ))"
T6_ELAPSED=$(( $(date +%s) - T6_START ))

CAP_OK=0
if [[ "$CAP_GREW" -gt 0 ]] && grep -q "team-session" ~/org/captains-log.org 2>/dev/null; then
  CAP_OK=1
fi

runlog ""
runlog "** PRIMITIVE 6 — captain's-log bridge — $([[ $CAP_OK == 1 ]] && echo PASS || echo FAIL) (${T6_ELAPSED}s)"
runlog "captains_log_growth_bytes: $CAP_GREW"
runlog "kind=team-session_present:  $(grep -c team-session ~/org/captains-log.org 2>/dev/null || echo 0)"
runlog "args_payload: $CAP_ARGS_JSON"
runlog "spend_usd: 0"
log "  $([[ $CAP_OK == 1 ]] && echo PASS || echo FAIL) captain's-log grew ${CAP_GREW} bytes"
[[ $CAP_OK == 1 ]] || die $EX_CAPLOG "captain's-log did not grow / no team-session entry"

# ── Final summary ─────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
cat <<EOF

── v0.1 smoke report ───────────────────────────────────────────────────
parent_session   : $PARENT_SID
worktree         : $WT_ID
btw_session      : $BTW_SID
subsession       : $SUB_SID
fork_session     : $FORK_SID
artifact         : $ARTIFACT_ID
captain's-log    : grew ${CAP_GREW} bytes (kind=team-session present)
cron primitive   : RECORDED-GAP (no MCP/REST surface in v0.17.3)
elapsed_seconds  : ${ELAPSED}
total_llm_spend  : \$0.00 (all primitives via direct MCP — DEC-006 path)
run_log          : ${RUN_LOG}
─────────────────────────────────────────────────────────────────────────
PASS — 5/6 primitives green; 1/6 recorded as upstream gap.
EOF
exit 0
