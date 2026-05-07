#!/usr/bin/env bash
# agor-test-fanout.sh — Agor primitive test: parent spawns 2 children in parallel
#
# Validates fan-out genealogy + parallelism: parent session uses MCP to call
# agor_sessions_spawn twice (children A + B with distinct echo strings), then
# we verify both children land in the DB as siblings of the same parent and
# their stdout / final messages contain the expected echo strings.
#
# Reference: scripts/agor-smoke.sh (single-spawn pattern this builds on).
# Test 4 of docs/wiki/2026-05-06-agor-validation.org.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────
: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_PATH:=/home/daniel/repos/org-llm}"
: "${AGOR_WORKTREE_NAME:=fanout-wt-$$}"
: "${MODEL:=sonnet}"
: "${MAX_BUDGET_USD:=1.00}"          # task ceiling per user
: "${TIMEOUT:=900}"                  # 15 min, leaves buffer under 20-min wall
: "${KEEP_DIRTY:=1}"                 # leave sessions for inspection per user

export PATH="$HOME/.npm-global/bin:$HOME/.guix-profile/bin:$PATH"

MCP_CONFIG="/tmp/agor-fanout-mcp-$$.json"
PARENT_OUT="/tmp/agor-fanout-parent-$$.jsonl"
START_TS=$(date +%s)

EX_PREREQ=10; EX_AUTH=20; EX_WORKTREE=30; EX_SESSION=35
EX_SPAWN_FAIL=40; EX_CHILD_FAIL=45; EX_TIMEOUT=50

die() { local code="$1"; shift; printf '\n[FAIL %s] %s\n' "$code" "$*" >&2; exit "$code"; }
log() { printf '[fanout] %s\n' "$*"; }

cleanup() {
  local rc=$?
  rm -f "$MCP_CONFIG" 2>/dev/null || true
  local elapsed=$(( $(date +%s) - START_TS ))
  log "elapsed: ${elapsed}s · exit=${rc} · KEEP_DIRTY=${KEEP_DIRTY} (sessions retained for inspection)"
}
trap cleanup EXIT

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

# ── Step 0: prerequisites ─────────────────────────────────────────────────
log "step 0 — prerequisites"
for bin in agor curl jq python3 claude; do
  command -v "$bin" >/dev/null 2>&1 || die $EX_PREREQ "missing prerequisite: $bin"
done

# ── Step 1: daemon health ─────────────────────────────────────────────────
log "step 1 — daemon health"
HEALTH="$(curl -sf -m 5 "${AGOR_BASE_URL}/health" || true)"
[[ -n "$HEALTH" ]] || die $EX_PREREQ "daemon not reachable"
echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null \
  || die $EX_PREREQ "daemon /health not ok: $HEALTH"
log "  ok · v$(echo "$HEALTH" | jq -r '.version')"

# ── Step 2: auth ──────────────────────────────────────────────────────────
log "step 2 — auth probe"
[[ -f "$AGOR_TOKEN_FILE" ]] || die $EX_AUTH "token file missing at $AGOR_TOKEN_FILE"
EXPIRES_AT="$(jq -r '.expiresAt // empty' "$AGOR_TOKEN_FILE" 2>/dev/null || true)"
NOW_MS=$(($(date +%s) * 1000))
if [[ -n "$EXPIRES_AT" ]] && [[ "$EXPIRES_AT" -lt "$NOW_MS" ]]; then
  die $EX_AUTH "token expired"
fi
api GET /repos | jq -e '.data' >/dev/null 2>&1 || die $EX_AUTH "auth probe failed"
log "  ok"

# ── Step 3: pick repo ─────────────────────────────────────────────────────
log "step 3 — resolving repo"
REPOS_JSON="$(api GET /repos)"
REPO_ID="$(echo "$REPOS_JSON" | jq -r --arg p "$AGOR_REPO_PATH" \
  '.data[] | select(.local_path == $p) | .repo_id' | head -1)"
if [[ -z "$REPO_ID" ]]; then
  REPO_ID="$(echo "$REPOS_JSON" | jq -r '.data[0].repo_id // empty')"
fi
[[ -n "$REPO_ID" ]] || die $EX_PREREQ "no repos registered"
DEFAULT_BRANCH="$(echo "$REPOS_JSON" | jq -r --arg id "$REPO_ID" \
  '.data[] | select(.repo_id == $id) | .default_branch')"
log "  repo_id=${REPO_ID} · branch=${DEFAULT_BRANCH}"

# ── Step 4: create worktree ───────────────────────────────────────────────
log "step 4 — creating worktree '${AGOR_WORKTREE_NAME}'"
WT_BODY="$(jq -nc \
  --arg name "$AGOR_WORKTREE_NAME" \
  --arg src  "$DEFAULT_BRANCH" \
  '{name:$name, ref:$name, createBranch:true, sourceBranch:$src, pullLatest:false, refType:"branch"}')"
WT_JSON="$(api POST "/repos/${REPO_ID}/worktrees" "$WT_BODY")"
WT_ID="$(echo "$WT_JSON" | jq -r '.worktree_id // empty')"
[[ -n "$WT_ID" ]] || die $EX_WORKTREE "worktree create failed: $WT_JSON"
log "  worktree_id=${WT_ID}"

# ── Step 5: parent session ────────────────────────────────────────────────
log "step 5 — creating parent session"
SESS_BODY="$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')"
SESS_JSON="$(api POST /sessions "$SESS_BODY")"
PARENT_SID="$(echo "$SESS_JSON" | jq -r '.session_id // empty')"
MCP_TOKEN="$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')"
[[ -n "$PARENT_SID" ]] || die $EX_SESSION "parent session create failed: $SESS_JSON"
[[ -n "$MCP_TOKEN" ]]  || die $EX_SESSION "session response missing mcp_token"
log "  parent_session_id=${PARENT_SID}"

# Lift parent permission_config so MCP-spawned children inherit bypass mode
# (per BUG #8 in the validation canon — children inherit parent.permission_config,
# CLI --permission-mode flag scopes only the parent's local process).
api PATCH "/sessions/${PARENT_SID}" \
  '{"permission_config":{"mode":"bypassPermissions"}}' >/dev/null

# ── Step 6: mcp config ────────────────────────────────────────────────────
log "step 6 — writing ${MCP_CONFIG}"
jq -nc --arg url "${AGOR_BASE_URL}/mcp" --arg auth "Bearer ${MCP_TOKEN}" \
  '{mcpServers:{agor:{type:"http", url:$url, headers:{Authorization:$auth}}}}' \
  > "$MCP_CONFIG"

# ── Step 7: parent run — fan-out spawn ────────────────────────────────────
log "step 7 — claude -p parent run (model=${MODEL}, budget=\$${MAX_BUDGET_USD})"
PROMPT='You have one MCP server "agor" exposing two tools:
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Your job: spawn TWO child sessions IN PARALLEL by issuing two
mcp__agor__agor_execute_tool calls in the SAME response (one tool_use
block immediately after the other, no waiting between them).

Call 1:
  toolName: "agor_sessions_spawn"
  arguments: {"prompt":"Print exactly the literal text ECHO_A=alpha on its own line, then stop. Do not use any tools.","title":"fanout-child-A"}

Call 2:
  toolName: "agor_sessions_spawn"
  arguments: {"prompt":"Print exactly the literal text ECHO_B=beta on its own line, then stop. Do not use any tools.","title":"fanout-child-B"}

After both spawn responses come back, print exactly these two lines and then stop:
  CHILD_A_SESSION_ID=<session_id from call 1 response>
  CHILD_B_SESSION_ID=<session_id from call 2 response>

Do not poll, do not call other tools, do not narrate. Issue both spawns
in parallel, then print the two ID lines.'

if ! timeout "${TIMEOUT}" claude -p \
       --model "$MODEL" \
       --output-format stream-json --verbose \
       --mcp-config "$MCP_CONFIG" --strict-mcp-config \
       --permission-mode bypassPermissions \
       --max-budget-usd "$MAX_BUDGET_USD" \
       "$PROMPT" >"$PARENT_OUT" 2>&1; then
  rc=$?
  [[ $rc -eq 124 ]] && die $EX_TIMEOUT "claude -p exceeded ${TIMEOUT}s (output: $PARENT_OUT)"
  die $EX_SPAWN_FAIL "claude -p exited rc=$rc (output: $PARENT_OUT)"
fi

PARENT_DONE_TS=$(date +%s)

# ── Step 8: parse child IDs ───────────────────────────────────────────────
log "step 8 — parsing child IDs from parent output"
CHILD_A_SID="$(grep -oE 'CHILD_A_SESSION_ID=[A-Za-z0-9_-]+' "$PARENT_OUT" | head -1 | cut -d= -f2 || true)"
CHILD_B_SID="$(grep -oE 'CHILD_B_SESSION_ID=[A-Za-z0-9_-]+' "$PARENT_OUT" | head -1 | cut -d= -f2 || true)"
log "  child_a=${CHILD_A_SID:-<missing>}"
log "  child_b=${CHILD_B_SID:-<missing>}"

# Compute parallelism evidence: parent issued both tool_use calls in the
# same assistant-message turn (Anthropic's parallel-tool-use shape). Count
# how many tool_use blocks land in a single assistant message.
PARALLEL_COUNT="$(jq -rs '
  [ .[]
    | select(.type == "assistant")
    | .message.content
    | map(select(.type == "tool_use" and .name == "mcp__agor__agor_execute_tool"))
    | length
  ] | max // 0
' < "$PARENT_OUT" 2>/dev/null || echo 0)"
log "  max parallel agor_execute_tool calls in one assistant message: ${PARALLEL_COUNT}"

# ── Step 9: poll children to completion + verify ──────────────────────────
log "step 9 — polling children for completion"
poll_child() {
  local sid="$1"
  local deadline=$(( $(date +%s) + 240 ))   # 4-min poll budget per child
  while [[ $(date +%s) -lt $deadline ]]; do
    local got; got="$(api GET "/sessions/${sid}")"
    local status; status="$(echo "$got" | jq -r '.status // ""')"
    case "$status" in
      completed|stopped|archived|failed|errored)
        echo "$got"
        return 0
        ;;
    esac
    sleep 5
  done
  echo "$got"
  return 1
}

verify_child() {
  local sid="$1" expected="$2" label="$3"
  if [[ -z "$sid" ]]; then
    echo "${label}_FOUND=0"
    echo "${label}_GENEALOGY_OK=0"
    echo "${label}_ECHO_OK=0"
    return
  fi
  local got
  got="$(poll_child "$sid")" || true
  local status; status="$(echo "$got" | jq -r '.status // ""')"
  local parent_field; parent_field="$(echo "$got" | jq -r '.genealogy.parent_session_id // ""')"
  local genealogy_ok=0
  [[ "$parent_field" == "$PARENT_SID" ]] && genealogy_ok=1

  # Pull child final assistant text via /sessions/:id/messages
  local msgs; msgs="$(api GET "/sessions/${sid}/messages?\$limit=200")"
  local echo_ok=0
  if echo "$msgs" | grep -qF "$expected"; then
    echo_ok=1
  fi
  echo "${label}_FOUND=1"
  echo "${label}_STATUS=${status}"
  echo "${label}_GENEALOGY_OK=${genealogy_ok}"
  echo "${label}_PARENT_ON_RECORD=${parent_field}"
  echo "${label}_ECHO_OK=${echo_ok}"
}

A_RESULT="$(verify_child "$CHILD_A_SID" "ECHO_A=alpha" "A")"
B_RESULT="$(verify_child "$CHILD_B_SID" "ECHO_B=beta"  "B")"
echo "$A_RESULT"
echo "$B_RESULT"

ALL_DONE_TS=$(date +%s)
ELAPSED=$(( ALL_DONE_TS - START_TS ))
PARENT_ELAPSED=$(( PARENT_DONE_TS - START_TS ))

# Aggregate parent-run cost (per-result objects from stream-json)
COST_USD="$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd] | add // 0' < "$PARENT_OUT" 2>/dev/null || echo 0)"

# ── Step 10: report ───────────────────────────────────────────────────────
A_GOK="$(echo "$A_RESULT" | grep '^A_GENEALOGY_OK=' | cut -d= -f2)"
B_GOK="$(echo "$B_RESULT" | grep '^B_GENEALOGY_OK=' | cut -d= -f2)"
A_EOK="$(echo "$A_RESULT" | grep '^A_ECHO_OK='      | cut -d= -f2)"
B_EOK="$(echo "$B_RESULT" | grep '^B_ECHO_OK='      | cut -d= -f2)"

VERDICT="FAIL"
if [[ "$A_GOK" == "1" ]] && [[ "$B_GOK" == "1" ]] && [[ "$A_EOK" == "1" ]] && [[ "$B_EOK" == "1" ]]; then
  if [[ "$PARALLEL_COUNT" -ge 2 ]]; then
    VERDICT="PASS"
  else
    VERDICT="PARTIAL"   # genealogy + echoes ok but children were sequential
  fi
fi

cat <<EOF

── Fan-out test report ──────────────────────────────────────────────────
repo_id              : ${REPO_ID}
worktree_id          : ${WT_ID}
parent_session_id    : ${PARENT_SID}
child_a_session_id   : ${CHILD_A_SID:-<missing>}
child_b_session_id   : ${CHILD_B_SID:-<missing>}
A_genealogy_ok       : ${A_GOK}
B_genealogy_ok       : ${B_GOK}
A_echo_present       : ${A_EOK}   (ECHO_A=alpha)
B_echo_present       : ${B_EOK}   (ECHO_B=beta)
parallel_tool_calls  : ${PARALLEL_COUNT}
parent_wall_seconds  : ${PARENT_ELAPSED}
total_wall_seconds   : ${ELAPSED}
cost_usd             : ${COST_USD}
parent_stream        : ${PARENT_OUT}
verdict              : ${VERDICT}
─────────────────────────────────────────────────────────────────────────
EOF

if [[ "$VERDICT" == "FAIL" ]]; then
  exit $EX_CHILD_FAIL
fi
exit 0
