#!/usr/bin/env bash
# agor-smoke.sh — headless Agor + Claude Code smoke test
#
# First-contact harness for the multi-agent-org-llm integration. Authenticates
# against a running Agor daemon as admin (bearer JWT via `agor login`), creates
# a worktree on a registered repo, mints a parent session (which carries an
# `mcp_token` in its create-response), wires the token into a transient
# `--mcp-config` JSON, and exercises `claude -p` so the parent spawns a child
# session via the Agor MCP surface.
#
# Verified live against agor-live v0.17.3 + claude code 2.1.x on 2026-05-06
# EDT. See docs/wiki/agor-smoke-recipe.org for the full design + bug log
# uncovered during the test run.

set -euo pipefail

# ── Banner ────────────────────────────────────────────────────────────────
cat <<'BANNER'
╭──────────────────────────────────────────────────────────────────────╮
│ Agor headless smoke — Bridge Crew first contact                      │
│ Verifies: daemon · worktree create · session create · MCP spawn      │
│ Docs:    docs/wiki/agor-smoke-recipe.org                             │
╰──────────────────────────────────────────────────────────────────────╯
BANNER

# ── Config (env-overridable) ──────────────────────────────────────────────
: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_ID:=}"           # optional — auto-discovered from /repos[0]
: "${AGOR_REPO_PATH:=}"         # optional — used to auto-pick repo by local_path
: "${AGOR_WORKTREE_NAME:=smoke-wt-$$}"
: "${AGOR_SOURCE_BRANCH:=}"     # default: repo.default_branch
: "${MODEL:=sonnet}"            # claude --model alias or full ID
: "${MAX_BUDGET_USD:=2.00}"     # claude --max-budget-usd cap (was --max-turns)
: "${TIMEOUT:=300}"
: "${SKIP_CLAUDE:=0}"           # 1 = stop after REST primitives (no LLM spend)
: "${KEEP_DIRTY:=0}"            # 1 = leave worktree+sessions in DB for inspection

MCP_CONFIG="/tmp/agor-mcp-$$.json"
PARENT_OUT="/tmp/agor-smoke-parent-$$.jsonl"
START_TS=$(date +%s)

# ── Exit codes ────────────────────────────────────────────────────────────
EX_PREREQ=10
EX_AUTH=20
EX_WORKTREE=30
EX_SESSION=35
EX_SPAWN_FAIL=40
EX_CHILD_FAIL=45
EX_TIMEOUT=50

die() { local code="$1"; shift; printf '\n[FAIL %s] %s\n' "$code" "$*" >&2; exit "$code"; }
log() { printf '[smoke] %s\n' "$*"; }

# ── Cleanup trap ──────────────────────────────────────────────────────────
cleanup() {
  local rc=$?
  rm -f "$MCP_CONFIG" 2>/dev/null || true
  if [[ "$KEEP_DIRTY" != "1" ]] && [[ -n "${PARENT_SID:-}" ]]; then
    log "cleanup: archiving parent session $PARENT_SID (set KEEP_DIRTY=1 to keep)"
    api PATCH "/sessions/${PARENT_SID}" '{"archived":true,"archived_reason":"smoke_test_cleanup"}' >/dev/null 2>&1 || true
  fi
  local elapsed=$(( $(date +%s) - START_TS ))
  log "elapsed: ${elapsed}s · exit=${rc}"
}
trap cleanup EXIT

# ── Step 0: prerequisites ─────────────────────────────────────────────────
log "step 0 — checking prerequisites"
for bin in agor curl jq python3; do
  command -v "$bin" >/dev/null 2>&1 || die $EX_PREREQ "missing prerequisite: $bin"
done
[[ "$SKIP_CLAUDE" == "1" ]] || command -v claude >/dev/null 2>&1 \
  || die $EX_PREREQ "missing prerequisite: claude (set SKIP_CLAUDE=1 to skip the LLM leg)"

# Tiny REST helper. Reads bearer from $AGOR_TOKEN_FILE on each call so a
# `agor login` from outside the script picks up automatically.
api() {
  local method="$1" path="$2" body="${3:-}"
  local tok
  tok="$(jq -r .accessToken "$AGOR_TOKEN_FILE")"
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

# ── Step 1: daemon health ─────────────────────────────────────────────────
log "step 1 — daemon health"
HEALTH="$(curl -sf -m 5 "${AGOR_BASE_URL}/health" || true)"
[[ -n "$HEALTH" ]] || die $EX_PREREQ "daemon not reachable at ${AGOR_BASE_URL} — start with: agor daemon start"
echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null \
  || die $EX_PREREQ "daemon /health did not return status=ok: $HEALTH"
REQUIRE_AUTH="$(echo "$HEALTH" | jq -r '.auth.requireAuth // false')"
log "  daemon ok · version $(echo "$HEALTH" | jq -r '.version') · requireAuth=${REQUIRE_AUTH}"

# ── Step 2: auth ──────────────────────────────────────────────────────────
log "step 2 — auth (admin JWT via ~/.agor/cli-token)"
[[ -f "$AGOR_TOKEN_FILE" ]] \
  || die $EX_AUTH "token file missing at $AGOR_TOKEN_FILE — run: agor login -e admin@agor.live -p \$(pass org-llm/agor/admin-password)"
EXPIRES_AT="$(jq -r '.expiresAt // empty' "$AGOR_TOKEN_FILE" 2>/dev/null || true)"
NOW_MS=$(($(date +%s) * 1000))
if [[ -n "$EXPIRES_AT" ]] && [[ "$EXPIRES_AT" -lt "$NOW_MS" ]]; then
  die $EX_AUTH "stored token expired — re-run: agor login -e admin@agor.live -p \$(pass org-llm/agor/admin-password)"
fi
ME="$(api GET /repos)"
echo "$ME" | jq -e '.data' >/dev/null 2>&1 \
  || die $EX_AUTH "auth probe failed (GET /repos): $ME"
log "  auth ok"

# ── Step 3: pick repo ─────────────────────────────────────────────────────
log "step 3 — resolving repo"
REPOS_JSON="$(api GET /repos)"
if [[ -n "$AGOR_REPO_ID" ]]; then
  REPO_ID="$AGOR_REPO_ID"
elif [[ -n "$AGOR_REPO_PATH" ]]; then
  REPO_ID="$(echo "$REPOS_JSON" | jq -r --arg p "$AGOR_REPO_PATH" '.data[] | select(.local_path == $p) | .repo_id' | head -1)"
else
  REPO_ID="$(echo "$REPOS_JSON" | jq -r '.data[0].repo_id // empty')"
fi
[[ -n "$REPO_ID" ]] || die $EX_PREREQ "no repos registered (run: agor repo add <path>) — try AGOR_REPO_ID=… instead"
DEFAULT_BRANCH="$(echo "$REPOS_JSON" | jq -r --arg id "$REPO_ID" '.data[] | select(.repo_id == $id) | .default_branch')"
SOURCE_BRANCH="${AGOR_SOURCE_BRANCH:-${DEFAULT_BRANCH:-main}}"
log "  repo_id=${REPO_ID} · source_branch=${SOURCE_BRANCH}"

# ── Step 4: create worktree ───────────────────────────────────────────────
# v0.17.3 BUG: `agor worktree add` CLI errors with `client.service(...).createWorktree
# is not a function`. Bypass with REST POST /repos/:id/worktrees, which is what the
# CLI was *trying* to call (Feathers-mounted custom method on ReposService).
log "step 4 — creating worktree '${AGOR_WORKTREE_NAME}'"
WT_BODY="$(jq -nc \
  --arg name "$AGOR_WORKTREE_NAME" \
  --arg src  "$SOURCE_BRANCH" \
  '{name:$name, ref:$name, createBranch:true, sourceBranch:$src, pullLatest:false, refType:"branch"}')"
WT_JSON="$(api POST "/repos/${REPO_ID}/worktrees" "$WT_BODY")"
WT_ID="$(echo "$WT_JSON" | jq -r '.worktree_id // empty')"
[[ -n "$WT_ID" ]] || die $EX_WORKTREE "worktree create failed: $WT_JSON"
WT_PATH="$(echo "$WT_JSON" | jq -r '.path')"
WT_UNIQUE="$(echo "$WT_JSON" | jq -r '.worktree_unique_id')"
log "  worktree_id=${WT_ID} · unique_id=${WT_UNIQUE} · path=${WT_PATH}"
log "  (filesystem populated async by executor; sessions may race the first second)"

# ── Step 5: create parent session ─────────────────────────────────────────
log "step 5 — creating parent session (claude-code)"
SESS_BODY="$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')"
SESS_JSON="$(api POST /sessions "$SESS_BODY")"
PARENT_SID="$(echo "$SESS_JSON" | jq -r '.session_id // empty')"
MCP_TOKEN="$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')"
[[ -n "$PARENT_SID" ]] || die $EX_SESSION "parent session create failed: $SESS_JSON"
[[ -n "$MCP_TOKEN" ]]  || die $EX_SESSION "session response missing mcp_token (admin role lost?): $SESS_JSON"
log "  parent_session_id=${PARENT_SID}"
log "  mcp_token=${MCP_TOKEN:0:24}…(JWT, default 24h expiry)"

if [[ "$SKIP_CLAUDE" == "1" ]]; then
  cat <<EOF

── Smoke report (REST primitives only) ─────────────────────────────────
repo_id           : ${REPO_ID}
worktree_id       : ${WT_ID}
worktree_path     : ${WT_PATH}
parent_session_id : ${PARENT_SID}
mcp_token         : ${MCP_TOKEN:0:24}…
PASS (LLM leg skipped via SKIP_CLAUDE=1)
─────────────────────────────────────────────────────────────────────────
EOF
  exit 0
fi

# ── Step 6: write transient mcp-config ────────────────────────────────────
log "step 6 — writing ${MCP_CONFIG}"
jq -nc --arg url "${AGOR_BASE_URL}/mcp" --arg auth "Bearer ${MCP_TOKEN}" \
  '{mcpServers:{agor:{type:"http", url:$url, headers:{Authorization:$auth}}}}' \
  > "$MCP_CONFIG"

# ── Step 7: invoke claude -p (parent run) ─────────────────────────────────
# claude code 2.x dropped --max-turns; cost is bounded via --max-budget-usd.
# Tool path: agor's MCP exposes only `agor_search_tools` + `agor_execute_tool`
# at the surface (progressive disclosure); the parent must search → execute.
log "step 7 — claude -p parent run (model=${MODEL}, budget=\$${MAX_BUDGET_USD}, timeout=${TIMEOUT}s)"
PROMPT='You have access to one MCP server named "agor" via two tools:
mcp__agor__agor_search_tools (browse tools by domain) and
mcp__agor__agor_execute_tool (call a discovered tool).
Use agor_execute_tool to call "agor_sessions_spawn" with arguments
{"prompt":"Print the literal string HELLO_FROM_CHILD then exit.","title":"smoke-child"}
to spawn a child session, then immediately print exactly:
  CHILD_SESSION_ID=<the session_id from the spawn response>
on its own line. Do not poll, do not call any other tools, just print that line and stop.'

if ! timeout "${TIMEOUT}" claude -p \
       --model "$MODEL" \
       --output-format stream-json --verbose \
       --mcp-config "$MCP_CONFIG" --strict-mcp-config \
       --permission-mode bypassPermissions \
       --max-budget-usd "$MAX_BUDGET_USD" \
       "$PROMPT" >"$PARENT_OUT" 2>&1; then
  rc=$?
  [[ $rc -eq 124 ]] && die $EX_TIMEOUT "claude -p exceeded ${TIMEOUT}s (output at $PARENT_OUT)"
  die $EX_SPAWN_FAIL "claude -p exited rc=$rc (output at $PARENT_OUT)"
fi

# ── Step 8: assertions + report ───────────────────────────────────────────
log "step 8 — asserting child outcome"
CHILD_SID="$(grep -oE 'CHILD_SESSION_ID=[A-Za-z0-9_-]+' "$PARENT_OUT" | head -1 | cut -d= -f2 || true)"
ELAPSED=$(( $(date +%s) - START_TS ))
COST_USD="$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd] | add // 0' < "$PARENT_OUT" 2>/dev/null || echo "?")"

# Verify the child landed in the DB and has correct genealogy
GENEALOGY_OK=0
if [[ -n "$CHILD_SID" ]]; then
  CHILD_GET="$(api GET "/sessions/${CHILD_SID}")"
  if [[ "$(echo "$CHILD_GET" | jq -r '.genealogy.parent_session_id // empty')" == "$PARENT_SID" ]]; then
    GENEALOGY_OK=1
  fi
fi

cat <<EOF

── Smoke report ─────────────────────────────────────────────────────────
repo_id           : ${REPO_ID}
worktree_id       : ${WT_ID}
parent_session_id : ${PARENT_SID}
child_session_id  : ${CHILD_SID:-<not parsed>}
genealogy_ok      : ${GENEALOGY_OK}
elapsed_seconds   : ${ELAPSED}
cost_usd          : ${COST_USD}
parent_stream     : ${PARENT_OUT}
─────────────────────────────────────────────────────────────────────────
EOF

[[ -n "$CHILD_SID" ]]      || die $EX_SPAWN_FAIL "no CHILD_SESSION_ID in parent output (see $PARENT_OUT)"
[[ "$GENEALOGY_OK" == "1" ]] || die $EX_CHILD_FAIL "child exists but genealogy.parent_session_id != ${PARENT_SID}"
log "PASS"
exit 0
