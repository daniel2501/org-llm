#!/usr/bin/env bash
# agor-smoke.sh — headless Agor + Claude Code smoke test
#
# First-contact harness for the multi-agent-org-llm integration. Stands up
# (or attaches to) the Agor daemon, mints an MCP bearer token via the
# anonymous-localhost path, hands it to `claude -p` over a transient
# mcp-config, and asserts the parent can spawn a child session and observe
# the child's final message.
#
# UNVERIFIED — written but not yet exercised against a live Agor daemon
# (Agor not installed on this machine as of 2026-05-06 EDT).
#
# See docs/wiki/agor-smoke-recipe.org for design + bootstrap-token research.

set -euo pipefail

# ── Banner ────────────────────────────────────────────────────────────────
cat <<'BANNER'
╭──────────────────────────────────────────────────────────────────────╮
│ Agor headless smoke — Bridge Crew first contact                      │
│ Verifies: daemon up · session POST · MCP token · spawn + callback    │
│ Docs:    docs/wiki/agor-smoke-recipe.org                             │
╰──────────────────────────────────────────────────────────────────────╯
BANNER

# ── Config (env-overridable) ──────────────────────────────────────────────
: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${MODEL:=claude-3-5-sonnet-latest}"
: "${MAX_TURNS:=6}"
: "${TIMEOUT:=300}"
: "${AGOR_BOOTSTRAP_TOKEN:=}"   # user-supplied; empty = auto-discover
: "${AGOR_BOARD_ID:=smoke}"
: "${AGOR_WORKTREE:=smoke-wt}"

MCP_CONFIG="/tmp/agor-mcp-$$.json"
WE_STARTED_DAEMON=0
START_TS=$(date +%s)

# ── Exit codes ────────────────────────────────────────────────────────────
EX_PREREQ=10
EX_BOOTSTRAP=20
EX_SPAWN_FAIL=30
EX_CHILD_FAIL=40
EX_TIMEOUT=50

die() { local code="$1"; shift; printf '\n[FAIL %s] %s\n' "$code" "$*" >&2; exit "$code"; }
log() { printf '[smoke] %s\n' "$*"; }

# ── Cleanup trap ──────────────────────────────────────────────────────────
cleanup() {
  local rc=$?
  rm -f "$MCP_CONFIG" 2>/dev/null || true
  if [[ "$WE_STARTED_DAEMON" == "1" ]]; then
    log "stopping daemon (we started it)"
    agor daemon stop >/dev/null 2>&1 || true
  fi
  local elapsed=$(( $(date +%s) - START_TS ))
  log "elapsed: ${elapsed}s · exit=${rc}"
}
trap cleanup EXIT

# ── Step 0: prerequisites ─────────────────────────────────────────────────
log "step 0 — checking prerequisites"
for bin in agor claude curl jq; do
  command -v "$bin" >/dev/null 2>&1 || die $EX_PREREQ "missing prerequisite: $bin (see docs/wiki/agor-pilot-install.org)"
done
command -v sqlite3 >/dev/null 2>&1 || log "  note: sqlite3 absent — db-fallback token path disabled"

# ── Step 1: ensure daemon up ──────────────────────────────────────────────
log "step 1 — daemon health"
if curl -sf -m 3 "${AGOR_BASE_URL}/api/health" >/dev/null 2>&1 \
  || curl -sf -m 3 "${AGOR_BASE_URL}/health"     >/dev/null 2>&1; then
  log "  daemon already running at ${AGOR_BASE_URL}"
else
  log "  daemon not reachable — starting"
  agor daemon start >/dev/null 2>&1 || die $EX_PREREQ "agor daemon start failed"
  WE_STARTED_DAEMON=1
  for _ in $(seq 1 20); do
    sleep 1
    curl -sf -m 2 "${AGOR_BASE_URL}/api/health" >/dev/null 2>&1 && break
    curl -sf -m 2 "${AGOR_BASE_URL}/health"     >/dev/null 2>&1 && break
  done
  curl -sf -m 2 "${AGOR_BASE_URL}/api/health" >/dev/null 2>&1 \
    || curl -sf -m 2 "${AGOR_BASE_URL}/health" >/dev/null 2>&1 \
    || die $EX_PREREQ "daemon never came up at ${AGOR_BASE_URL}"
fi

# ── Step 2: discover bootstrap token ──────────────────────────────────────
# Strategy ranked by confidence (see wiki Bootstrap-token investigation):
#   a. $AGOR_BOOTSTRAP_TOKEN env var
#   b. `agor token` CLI verb (currently undocumented; probe with --help)
#   c. anon-localhost POST /api/sessions  ← *most-likely* path; auth=anonymous default
#   d. sqlite read of ~/.agor/agor.db sessions.data.mcp_token (JSON1)
#   e. clear failure with file-an-issue pointer
#
# We DO NOT try to read a "tokens" file from disk — Agor has no such file
# (confirmed via schema.sqlite.ts: tokens live only inside sessions.data).
log "step 2 — discovering bootstrap token"

PARENT_JSON=""
TOKEN=""
SID=""

# (a) env var
if [[ -n "$AGOR_BOOTSTRAP_TOKEN" ]]; then
  log "  using \$AGOR_BOOTSTRAP_TOKEN"
  TOKEN="$AGOR_BOOTSTRAP_TOKEN"
fi

# (b) `agor token` verb — UNVERIFIED; agor CLI may not implement this
if [[ -z "$TOKEN" ]] && agor token --help >/dev/null 2>&1; then
  log "  found 'agor token' verb — calling"
  TOKEN="$(agor token 2>/dev/null | tr -d '[:space:]' || true)"
fi

# (c) anon POST /api/sessions  — *primary path on default install*
if [[ -z "$TOKEN" ]]; then
  log "  attempting anon-localhost POST /api/sessions"
  PARENT_JSON="$(curl -sf -m 10 -X POST "${AGOR_BASE_URL}/api/sessions" \
    -H 'Content-Type: application/json' \
    -d "{\"boardId\":\"${AGOR_BOARD_ID}\",\"worktree\":\"${AGOR_WORKTREE}\",\"assistant\":\"claude-code\"}" \
    || true)"
  if [[ -n "$PARENT_JSON" ]] && echo "$PARENT_JSON" | jq -e '.mcpToken' >/dev/null 2>&1; then
    TOKEN="$(echo "$PARENT_JSON" | jq -r '.mcpToken')"
    SID="$(echo  "$PARENT_JSON" | jq -r '.sessionId // .id')"
    log "  anon POST succeeded · session=${SID}"
  else
    log "  anon POST refused (auth=local/jwt enabled?)"
  fi
fi

# (d) sqlite fallback — read most-recent session's mcp_token
if [[ -z "$TOKEN" ]] && command -v sqlite3 >/dev/null 2>&1 && [[ -f "${AGOR_DATA_DIR}/agor.db" ]]; then
  log "  trying sqlite read of ${AGOR_DATA_DIR}/agor.db"
  # UNVERIFIED — exact JSON path inside `data` column not confirmed live;
  # schema.sqlite.ts says sessions.data.mcp_token is the field
  TOKEN="$(sqlite3 "${AGOR_DATA_DIR}/agor.db" \
    "SELECT json_extract(data, '\$.mcp_token') FROM sessions WHERE json_extract(data, '\$.mcp_token') IS NOT NULL ORDER BY created_at DESC LIMIT 1;" \
    2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "$TOKEN" ]] && log "  sqlite read succeeded"
fi

# (e) hard fail with actionable message
if [[ -z "$TOKEN" ]]; then
  cat >&2 <<EOF

[FAIL BOOTSTRAP] Could not obtain an MCP bearer token via any path.

Tried (in order):
  1. \$AGOR_BOOTSTRAP_TOKEN env var      — unset
  2. \`agor token\` CLI verb              — absent or failed
  3. anon POST /api/sessions             — refused (non-anonymous auth?)
  4. sqlite read of ${AGOR_DATA_DIR}/agor.db — empty or missing

Manual recovery:
  • Open the canvas once: \`agor open\`, create a session, then re-run.
  • Or pass a token explicitly:  AGOR_BOOTSTRAP_TOKEN=<jwt> $0
  • Or file: https://github.com/preset-io/agor/issues — title:
    "Document external-client MCP bootstrap-token path"
EOF
  exit $EX_BOOTSTRAP
fi

# If we got the token via path (a/b/d) we still need a parent SID — mint one
if [[ -z "$SID" ]]; then
  log "  minting parent session with discovered token"
  PARENT_JSON="$(curl -sf -m 10 -X POST "${AGOR_BASE_URL}/api/sessions" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{\"boardId\":\"${AGOR_BOARD_ID}\",\"worktree\":\"${AGOR_WORKTREE}\",\"assistant\":\"claude-code\"}" \
    || true)"
  SID="$(echo "$PARENT_JSON" | jq -r '.sessionId // .id // empty' 2>/dev/null || true)"
  [[ -z "$SID" ]] && die $EX_BOOTSTRAP "parent session mint failed (response: $PARENT_JSON)"
  TOKEN="$(echo "$PARENT_JSON" | jq -r '.mcpToken // empty')"
fi

log "  parent sessionId=${SID}"

# ── Step 3: write transient mcp-config ────────────────────────────────────
log "step 3 — writing ${MCP_CONFIG}"
cat > "$MCP_CONFIG" <<EOF
{"mcpServers":{"agor":{"type":"http","url":"${AGOR_BASE_URL}/mcp","headers":{"Authorization":"Bearer ${TOKEN}"}}}}
EOF

# ── Step 4: invoke claude -p (parent run) ─────────────────────────────────
# UNVERIFIED — exercises mcp__agor__agor_sessions_spawn shape from feasibility doc
log "step 4 — claude -p parent run (model=${MODEL}, max-turns=${MAX_TURNS}, timeout=${TIMEOUT}s)"
PROMPT='Use the MCP tool mcp__agor__agor_sessions_spawn to spawn a child session
whose entire job is to print the literal string HELLO_FROM_CHILD as its final
message. Then poll the child until it reaches a terminal status and print:
  CHILD_SESSION_ID=<id>
  CHILD_STATUS=<status>
  CHILD_FINAL=<final-message verbatim>
Exit when those three lines have been printed.'

OUT="/tmp/agor-smoke-parent-$$.jsonl"
if ! timeout "${TIMEOUT}" claude -p \
       --model "$MODEL" \
       --output-format stream-json \
       --mcp-config "$MCP_CONFIG" --strict-mcp-config \
       --permission-mode bypassPermissions \
       --max-turns "$MAX_TURNS" \
       "$PROMPT" >"$OUT" 2>&1; then
  rc=$?
  [[ $rc -eq 124 ]] && die $EX_TIMEOUT "claude -p exceeded ${TIMEOUT}s (output at $OUT)"
  die $EX_SPAWN_FAIL "claude -p exited rc=$rc (output at $OUT)"
fi

# ── Step 5: assertions + report ───────────────────────────────────────────
log "step 5 — asserting child outcome"
CHILD_SID="$(grep -oE 'CHILD_SESSION_ID=[A-Za-z0-9_-]+' "$OUT" | head -1 | cut -d= -f2 || true)"
CHILD_STATUS="$(grep -oE 'CHILD_STATUS=[A-Za-z_]+' "$OUT" | head -1 | cut -d= -f2 || true)"
CHILD_FINAL_HIT="$(grep -c 'HELLO_FROM_CHILD' "$OUT" || true)"

ELAPSED=$(( $(date +%s) - START_TS ))
TOK_IN="$(jq -rs  '[.[] | select(.usage?) | .usage.input_tokens]  | add // 0' < "$OUT" 2>/dev/null || echo "?")"
TOK_OUT="$(jq -rs '[.[] | select(.usage?) | .usage.output_tokens] | add // 0' < "$OUT" 2>/dev/null || echo "?")"

cat <<EOF

── Smoke report ─────────────────────────────────────────────────────────
parent_session_id : ${SID}
child_session_id  : ${CHILD_SID:-<not parsed>}
child_status      : ${CHILD_STATUS:-<not parsed>}
hello_from_child  : ${CHILD_FINAL_HIT} occurrence(s) in stream
elapsed_seconds   : ${ELAPSED}
tokens_in/out     : ${TOK_IN}/${TOK_OUT}
parent_stream     : ${OUT}
─────────────────────────────────────────────────────────────────────────
EOF

if [[ -z "$CHILD_SID" ]]; then
  die $EX_SPAWN_FAIL "no CHILD_SESSION_ID in parent output — spawn likely failed"
fi
if [[ "$CHILD_FINAL_HIT" == "0" ]]; then
  die $EX_CHILD_FAIL "child never echoed HELLO_FROM_CHILD"
fi

log "PASS"
exit 0
