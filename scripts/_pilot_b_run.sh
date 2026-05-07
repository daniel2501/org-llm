#!/usr/bin/env bash
# Pilot B — sibling pair on one board (@data + @spock + captain).
# Mirrors agor-test-fanout.sh setup; deviates by spawning two PERSONA-loaded
# children that coordinate via mode:"btw" peer query, on the SAME worktree
# (sibling-by-boardId).
set -euo pipefail

: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_PATH:=/home/daniel/repos/org-llm}"
: "${AGOR_REPO_ID:=019dfbd1-abe8-717b-b0b4-099526fe0b65}"
: "${MODEL:=sonnet}"
: "${MAX_BUDGET_USD:=4.50}"          # hard cap $5 minus startup tax buffer
: "${TIMEOUT:=1500}"                  # 25 min, leaves buffer under 30-min wall
export PATH="$HOME/.npm-global/bin:$HOME/.guix-profile/bin:$PATH"

EPOCH=$(date +%s)
WT_NAME="pilot-B-${EPOCH}"
ART_DIR="/home/daniel/repos/org-llm/tests/_artifacts/pilot-b-${EPOCH}"
mkdir -p "$ART_DIR"
MCP_CONFIG="${ART_DIR}/mcp.json"
CAPTAIN_OUT="${ART_DIR}/captain.jsonl"
START_TS=$EPOCH

log() { printf '[pilot-B] %s\n' "$*"; }
die() { printf '[pilot-B FAIL] %s\n' "$*" >&2; exit 1; }

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

log "step 1 — verifying repo + branch"
DEFAULT_BRANCH="$(api GET /repos | jq -r --arg id "$AGOR_REPO_ID" \
  '.data[] | select(.repo_id == $id) | .default_branch')"
[[ -n "$DEFAULT_BRANCH" ]] || die "couldn't resolve default branch for repo $AGOR_REPO_ID"
log "  repo_id=${AGOR_REPO_ID} branch=${DEFAULT_BRANCH}"

log "step 2 — creating worktree '${WT_NAME}'"
WT_BODY="$(jq -nc \
  --arg name "$WT_NAME" \
  --arg src  "$DEFAULT_BRANCH" \
  '{name:$name, ref:$name, createBranch:true, sourceBranch:$src, pullLatest:false, refType:"branch"}')"
WT_JSON="$(api POST "/repos/${AGOR_REPO_ID}/worktrees" "$WT_BODY")"
WT_ID="$(echo "$WT_JSON" | jq -r '.worktree_id // empty')"
WT_PATH="$(echo "$WT_JSON" | jq -r '.path // empty')"
[[ -n "$WT_ID" ]] || die "worktree create failed: $WT_JSON"
log "  worktree_id=${WT_ID}"
log "  worktree_path=${WT_PATH}"

log "step 3 — creating captain session (claude-code) on worktree"
SESS_BODY="$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')"
SESS_JSON="$(api POST /sessions "$SESS_BODY")"
CAPTAIN_SID="$(echo "$SESS_JSON" | jq -r '.session_id // empty')"
MCP_TOKEN="$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')"
[[ -n "$CAPTAIN_SID" ]] || die "captain session create failed: $SESS_JSON"
[[ -n "$MCP_TOKEN" ]]  || die "session response missing mcp_token"
log "  captain_session_id=${CAPTAIN_SID}"

log "step 4 — PATCH captain to bypassPermissions (children inherit, BUG #8)"
api PATCH "/sessions/${CAPTAIN_SID}" \
  '{"permission_config":{"mode":"bypassPermissions"}}' >/dev/null

log "step 5 — writing MCP config"
jq -nc --arg url "${AGOR_BASE_URL}/mcp" --arg auth "Bearer ${MCP_TOKEN}" \
  '{mcpServers:{agor:{type:"http", url:$url, headers:{Authorization:$auth}}}}' \
  > "$MCP_CONFIG"

DATA_PERSONA='You are @data — turn ideas into clean code/text artifacts. Today you are paired with a sibling reviewer @spock on the SAME git worktree.

TASK: Edit the file org_llm/notices.py — a 61-LoC audit-only sidecar that records bottom-up observations to a SQLite Notice table. Add Python type hints + one-line docstrings to every public function/class. Behavior must NOT change — preserve the swallow-on-failure contract.

PROCESS:
1. Read org_llm/notices.py.
2. Add: full type annotations (parameters + return), brief docstrings (1 line each) where missing. The module already has a docstring and notice() has one — improve them only if clearly weak.
3. Run: python -c "import ast; ast.parse(open(\"org_llm/notices.py\").read())" to confirm it parses.
4. git add org_llm/notices.py + git commit with message "chore(notices): add type hints + docstrings".
5. When done, print: TASK_DONE
6. Be available for sibling questions: your reviewer @spock will peer-query you via mode:"btw" before approving. Answer crisply when asked.'

SPOCK_PERSONA='You are @spock — a precise Vulcan reviewer. Your sibling @data is editing org_llm/notices.py to add type hints + docstrings. You are on the SAME worktree as @data.

YOUR JOB:
1. Wait until @data has committed changes. Use `git log --all --oneline -5` and `git diff HEAD~1 -- org_llm/notices.py` (or `git show HEAD -- org_llm/notices.py`) to inspect. If no commit yet, sleep 30s and retry up to 6 times.
2. Discover sibling sessions on this board: the board IS the registry. Use mcp__agor__agor_search_tools (query: "list sessions") to find the sessions-listing tool, then call it to enumerate sessions on your worktree. Identify @data by title (pilot-B-data) — capture its session_id.
3. Pick ONE specific function or design choice in @data is diff and ask a clarifying question via mcp__agor__agor_execute_tool calling agor_sessions_prompt with arguments {"mode":"btw","target":"<data_session_id>","prompt":"<your specific question>"}. Wait for the answer text in the tool response.
4. After @data answers, do a final review: type-correctness, docstring accuracy, no behavior change. If satisfied, print: APPROVED — followed by one short rationale line. If not, print: REJECTED with one specific fix request and let @data iterate once.
5. Stop after either APPROVED or one REJECTED + iteration.

Be a real Vulcan: precise, concise, no flattery.'

CAPTAIN_PROMPT=$(cat <<EOF
You are the CAPTAIN — control surface for a sibling pair. You have one MCP server "agor" with two tools:
  mcp__agor__agor_search_tools (browse tools by domain)
  mcp__agor__agor_execute_tool (call a discovered tool by name)

Your job: spawn TWO sibling child sessions on the SAME worktree (the worktree you are running in), then exit. The children coordinate among themselves via Agor — do NOT supervise them.

STEP 1: Browse. Call mcp__agor__agor_search_tools with query "spawn session" to discover the right tool name (likely "agor_sessions_spawn" or similar).

STEP 2: Spawn @data. Call mcp__agor__agor_execute_tool with toolName "agor_sessions_spawn" and arguments containing:
  - prompt: a SINGLE string that combines the @data persona below.
  - title: "pilot-B-data"

@data persona to use as the prompt verbatim:
<<<DATA_PERSONA
${DATA_PERSONA}
DATA_PERSONA

STEP 3: Spawn @spock as a sibling on the SAME board. Call mcp__agor__agor_execute_tool with toolName "agor_sessions_spawn" and arguments containing:
  - prompt: the @spock persona below verbatim.
  - title: "pilot-B-spock"

@spock persona to use as the prompt verbatim:
<<<SPOCK_PERSONA
${SPOCK_PERSONA}
SPOCK_PERSONA

STEP 4: After both spawn responses come back, print exactly these two lines (and nothing else after):
  DATA_SESSION_ID=<session_id from spawn 1 response>
  SPOCK_SESSION_ID=<session_id from spawn 2 response>

Do not poll. Do not narrate. Do not call other tools. Spawn → print IDs → stop.
EOF
)

log "step 6 — claude -p captain run (model=${MODEL}, budget=\$${MAX_BUDGET_USD}, timeout=${TIMEOUT}s)"
if ! timeout "${TIMEOUT}" claude -p \
       --model "$MODEL" \
       --output-format stream-json --verbose \
       --mcp-config "$MCP_CONFIG" --strict-mcp-config \
       --permission-mode bypassPermissions \
       --max-budget-usd "$MAX_BUDGET_USD" \
       "$CAPTAIN_PROMPT" >"$CAPTAIN_OUT" 2>&1; then
  rc=$?
  log "captain exited rc=$rc (output at $CAPTAIN_OUT) — proceeding to inspect anyway"
fi

CAPTAIN_DONE_TS=$(date +%s)

log "step 7 — parsing child IDs"
DATA_SID="$(grep -oE 'DATA_SESSION_ID=[A-Za-z0-9_-]+'  "$CAPTAIN_OUT" | head -1 | cut -d= -f2 || true)"
SPOCK_SID="$(grep -oE 'SPOCK_SESSION_ID=[A-Za-z0-9_-]+' "$CAPTAIN_OUT" | head -1 | cut -d= -f2 || true)"
log "  data_session_id=${DATA_SID:-<missing>}"
log "  spock_session_id=${SPOCK_SID:-<missing>}"

# Also fall back to /sessions?worktree_id genealogy listing if grep missed
if [[ -z "$DATA_SID" || -z "$SPOCK_SID" ]]; then
  log "  fallback: enumerating sessions on worktree to recover IDs"
  ALL="$(api GET "/sessions?\$limit=200")"
  if [[ -z "$DATA_SID" ]]; then
    DATA_SID="$(echo "$ALL" | jq -r --arg wt "$WT_ID" '.data[] | select(.worktree_id==$wt and (.title // "" | test("data"))) | .session_id' | head -1)"
  fi
  if [[ -z "$SPOCK_SID" ]]; then
    SPOCK_SID="$(echo "$ALL" | jq -r --arg wt "$WT_ID" '.data[] | select(.worktree_id==$wt and (.title // "" | test("spock"))) | .session_id' | head -1)"
  fi
  log "  recovered data=${DATA_SID:-<still missing>} spock=${SPOCK_SID:-<still missing>}"
fi

{
  echo "WT_NAME=${WT_NAME}"
  echo "WT_ID=${WT_ID}"
  echo "WT_PATH=${WT_PATH}"
  echo "CAPTAIN_SID=${CAPTAIN_SID}"
  echo "DATA_SID=${DATA_SID}"
  echo "SPOCK_SID=${SPOCK_SID}"
  echo "CAPTAIN_OUT=${CAPTAIN_OUT}"
  echo "ART_DIR=${ART_DIR}"
  echo "START_TS=${START_TS}"
  echo "CAPTAIN_DONE_TS=${CAPTAIN_DONE_TS}"
} > "${ART_DIR}/state.env"
ln -sf "${ART_DIR}/state.env" /home/daniel/repos/org-llm/tests/_artifacts/pilot-b-state.env

CAPTAIN_COST="$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd] | add // 0' < "$CAPTAIN_OUT" 2>/dev/null || echo 0)"
log "  captain_cost_usd=${CAPTAIN_COST}"
log "step 7 done — state at ${ART_DIR}/state.env"
