#!/usr/bin/env bash
# agor-pilot-c.sh — Pilot C (captain + scout-fork mixed-tool) Agor harness
#
# Drives Agor to spawn ONE captain (@picard, claude-code/sonnet) which
# in-turn forks TWO specialist children via MCP agor_sessions_prompt
# with mode:"fork":
#
#   * @data    — code-author    (claude-code, sonnet)
#   * @geordi  — sanity-checker (per-spawn override: cheaper FOSS-friendly
#                 model — qwen2.5-72b via openrouter — to test the
#                 cross-tool/model override primitive)
#
# Task: identify a small uncovered helper in org_llm/metrics.py and add
# a unit test for it in tests/test_metrics_registry.py. Test must pass.
#
# References: scripts/agor-smoke.sh, scripts/agor-test-fanout.sh,
# scripts/agor-pilot-a.sh, docs/wiki/multi-agent-org-llm.org §
# "Scout-pattern dispatch" + "Cross-tool escalation".

set -euo pipefail

: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_ID:=019dfbd1-abe8-717b-b0b4-099526fe0b65}"
: "${WT_NAME:=pilot-C-$(date +%s)}"
: "${MODEL:=sonnet}"
: "${MAX_BUDGET_USD:=5.00}"
: "${TIMEOUT:=1500}"   # 25 min wall cap
: "${POLL_DEADLINE_SECONDS:=900}"

export PATH="$HOME/.npm-global/bin:$HOME/.guix-profile/bin:$PATH"

START_TS=$(date +%s)
PARENT_OUT="/tmp/agor-pilot-c-parent-$$.jsonl"
MCP_CONFIG="/tmp/agor-pilot-c-mcp-$$.json"
STATE_DIR="/tmp/agor-pilot-c-state-$$"
mkdir -p "$STATE_DIR"

log() { printf '[pilot-C] %s\n' "$*" >&2; }
die() { printf '[pilot-C FAIL] %s\n' "$*" >&2; exit 1; }

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

# ── 1: worktree ───────────────────────────────────────────────────────────
log "step 1 — create worktree '$WT_NAME'"
WT_BODY=$(jq -nc --arg name "$WT_NAME" \
  '{name:$name, ref:$name, createBranch:true, sourceBranch:"trunk", pullLatest:false, refType:"branch"}')
WT_JSON=$(api POST "/repos/${AGOR_REPO_ID}/worktrees" "$WT_BODY")
echo "$WT_JSON" > "$STATE_DIR/wt.json"
WT_ID=$(echo "$WT_JSON" | jq -r '.worktree_id // empty')
WT_PATH=$(echo "$WT_JSON" | jq -r '.path // empty')
[[ -n "$WT_ID" ]] || die "worktree create failed: $WT_JSON"
log "  worktree_id=$WT_ID path=$WT_PATH"

# ── 2: captain session ────────────────────────────────────────────────────
log "step 2 — create captain session on worktree"
SESS_BODY=$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')
SESS_JSON=$(api POST /sessions "$SESS_BODY")
echo "$SESS_JSON" > "$STATE_DIR/captain-session.json"
SID=$(echo "$SESS_JSON" | jq -r '.session_id // empty')
MCP_TOKEN=$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')
[[ -n "$SID" ]] || die "captain session create failed: $SESS_JSON"
log "  captain_session_id=$SID"

# Lift permission_config so MCP-spawned children inherit bypass mode (BUG #8)
api PATCH "/sessions/${SID}" '{"permission_config":{"mode":"bypassPermissions"}}' >/dev/null
log "  permission_config: bypassPermissions"

# ── 3: write captain prompt ───────────────────────────────────────────────
PROMPT_FILE="$STATE_DIR/prompt.txt"
cat > "$PROMPT_FILE" <<'EOF'
You are @picard — Captain Jean-Luc Picard. The Bridge Crew captain. You
do not write code yourself; you decide who writes it, you dispatch them
with clear orders, and you synthesise their reports. Calm, principled,
"engage" decisions are unambiguous.

=== TEAM SHAPE ===
For this mission you have access to the Agor MCP server (tools
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool — progressive
disclosure). Use it to FORK two specialist children:

  * @data   (Lt. Cmdr. Data, scribe) — precise, careful, will author the
            test. Default tool: claude-code, model: sonnet. (No override
            needed; default arrangement.)
  * @geordi (Lt. Cmdr. Geordi La Forge, analyst/diagnostics) — sanity-
            checks coverage delta. Use a CHEAPER model override on the
            spawn so we test the per-spawn cross-tool/model override
            primitive. Try, in priority order:
              1. agentic_tool:"opencode" (FOSS coding tool)
              2. or pass an explicit cheaper-model hint in the prompt
                 (e.g. "use qwen2.5-72b-instruct via openrouter") and
                 leave agentic_tool:"claude-code" but include any
                 per-spawn model_override / model / assistant arg the
                 spawn schema accepts.

=== MISSION ===
Inside this worktree (current directory == worktree root), the file
org_llm/metrics.py (442 LoC) has uncovered code paths. Your team must:

  1. Run the coverage probe to find a small uncovered function:
       .venv/bin/pytest --cov=org_llm.metrics tests/test_metrics_registry.py --cov-report=term-missing 2>&1 | tail -50
  2. Pick ONE small, easy-to-test pure helper (e.g. a private
     `_method` or a small public helper that's currently uncovered).
     Do NOT pick a method that requires complex DB / Superset state.
  3. Author a pytest unit test for it in
     tests/test_metrics_registry.py (or a new test file under tests/).
     The test MUST pass.
  4. Run the test to confirm it's green:
       .venv/bin/pytest tests/test_metrics_registry.py -x
  5. Commit the test on the current branch:
       git add tests/   && git commit -m "test(metrics): cover <fn> (Pilot C)"

=== HOW TO USE THE FORK PRIMITIVE ===
You are running INSIDE an Agor session that has the agor MCP attached.
First call: mcp__agor__agor_search_tools with a query like "session prompt
fork spawn" to discover the spawn/fork tool's name and arg schema. THEN
issue mcp__agor__agor_execute_tool with toolName "agor_sessions_prompt"
and arguments shaped like:

  { "mode": "fork",
    "prompt": "<the child's full briefing, persona text + task chunk>",
    "title": "<short-handle>",
    ... per-spawn overrides (if discovered): "agentic_tool", "model",
        "assistant" — pick whichever the schema actually accepts ... }

Issue ONE fork for @data (no override) carrying the full task. Wait for
its response (the spawn returns a session_id). THEN issue ONE fork for
@geordi WITH the cheaper-model / cross-tool override and a sanity-check
brief: "verify the test @data wrote actually exercises previously
uncovered lines; confirm it passes; report PASS/FAIL only".

If the discovered spawn schema does NOT have a per-spawn model/assistant
override arg AT ALL, that is itself an important test outcome — record
it in your final report under BUG_OBSERVED. Spawn @geordi anyway with
default tool/model so the fork primitive itself still gets tested.

=== POLL + REPORT ===
After both forks have been issued, you may stop. The harness will poll
the children to completion. Print exactly:

  CAPTAIN_DONE
  DATA_SESSION_ID=<uuid from data-fork response>
  GEORDI_SESSION_ID=<uuid from geordi-fork response>
  GEORDI_OVERRIDE_USED=<the override-arg name + value you sent, or "none-supported">

Then stop. Do not narrate further. Do not poll yourself.

=== CONSTRAINTS ===
- Work strictly inside this worktree.
- Don't push.
- Keep the commit small (one new test).
- If anything blocks you, print BLOCKED=<one-line-reason> instead of
  CAPTAIN_DONE and stop. Do not loop.
EOF

PROMPT="$(cat "$PROMPT_FILE")"

# Write the MCP config so the captain can reach Agor
jq -nc --arg url "${AGOR_BASE_URL}/mcp" --arg auth "Bearer ${MCP_TOKEN}" \
  '{mcpServers:{agor:{type:"http", url:$url, headers:{Authorization:$auth}}}}' \
  > "$MCP_CONFIG"

# ── 4: invoke claude -p as the captain ────────────────────────────────────
log "step 4 — invoking claude -p as @picard captain (model=$MODEL, budget=\$$MAX_BUDGET_USD, timeout=${TIMEOUT}s)"
log "  worktree_path=$WT_PATH"

# Wait for executor to populate the worktree on disk (async populate per smoke recipe).
WAIT_DEADLINE=$(( $(date +%s) + 60 ))
while [[ ! -d "$WT_PATH" ]] && [[ $(date +%s) -lt $WAIT_DEADLINE ]]; do
  sleep 1
done
if [[ ! -d "$WT_PATH" ]]; then
  die "worktree path did not materialize after 60s: $WT_PATH"
fi
log "  worktree dir ready"

cd "$WT_PATH"

if ! timeout "$TIMEOUT" claude -p \
       --model "$MODEL" \
       --output-format stream-json --verbose \
       --mcp-config "$MCP_CONFIG" --strict-mcp-config \
       --permission-mode bypassPermissions \
       --max-budget-usd "$MAX_BUDGET_USD" \
       "$PROMPT" >"$PARENT_OUT" 2>&1; then
  rc=$?
  log "  claude -p exited rc=$rc (output at $PARENT_OUT)"
fi

# ── 5: parse outcome ──────────────────────────────────────────────────────
COST_USD=$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd] | add // 0' < "$PARENT_OUT" 2>/dev/null || echo 0)
CAPTAIN_DONE_LINE=$(grep -oE 'CAPTAIN_DONE|BLOCKED=.*' "$PARENT_OUT" | head -1 || true)
DATA_SID=$(grep -oE 'DATA_SESSION_ID=[A-Za-z0-9_-]+' "$PARENT_OUT" | head -1 | cut -d= -f2 || true)
GEORDI_SID=$(grep -oE 'GEORDI_SESSION_ID=[A-Za-z0-9_-]+' "$PARENT_OUT" | head -1 | cut -d= -f2 || true)
GEORDI_OVERRIDE=$(grep -E 'GEORDI_OVERRIDE_USED=' "$PARENT_OUT" | head -1 | sed 's/.*GEORDI_OVERRIDE_USED=//' || true)

log "  captain_done=$CAPTAIN_DONE_LINE"
log "  data_session_id=$DATA_SID"
log "  geordi_session_id=$GEORDI_SID"
log "  geordi_override=$GEORDI_OVERRIDE"

# ── 6: poll children to completion ────────────────────────────────────────
poll_child() {
  local sid="$1"
  local deadline=$(( $(date +%s) + POLL_DEADLINE_SECONDS ))
  while [[ $(date +%s) -lt $deadline ]]; do
    local got; got="$(api GET "/sessions/${sid}")"
    local status; status="$(echo "$got" | jq -r '.status // ""')"
    case "$status" in
      completed|stopped|archived|failed|errored)
        echo "$got"
        return 0
        ;;
    esac
    sleep 10
  done
  echo "$got"
  return 1
}

DATA_GET=""
GEORDI_GET=""
DATA_MODEL=""
GEORDI_MODEL=""
DATA_TOOL=""
GEORDI_TOOL=""
DATA_PARENT_OK=0
GEORDI_PARENT_OK=0

if [[ -n "$DATA_SID" ]]; then
  log "step 6a — polling @data child $DATA_SID"
  DATA_GET=$(poll_child "$DATA_SID" || true)
  echo "$DATA_GET" > "$STATE_DIR/data-session.json"
  DATA_MODEL=$(echo "$DATA_GET" | jq -r '.model // .agent_state.model // .config.model // ""')
  DATA_TOOL=$(echo "$DATA_GET" | jq -r '.agentic_tool // .config.agentic_tool // ""')
  DPARENT=$(echo "$DATA_GET" | jq -r '.genealogy.parent_session_id // ""')
  [[ "$DPARENT" == "$SID" ]] && DATA_PARENT_OK=1
fi

if [[ -n "$GEORDI_SID" ]]; then
  log "step 6b — polling @geordi child $GEORDI_SID"
  GEORDI_GET=$(poll_child "$GEORDI_SID" || true)
  echo "$GEORDI_GET" > "$STATE_DIR/geordi-session.json"
  GEORDI_MODEL=$(echo "$GEORDI_GET" | jq -r '.model // .agent_state.model // .config.model // ""')
  GEORDI_TOOL=$(echo "$GEORDI_GET" | jq -r '.agentic_tool // .config.agentic_tool // ""')
  GPARENT=$(echo "$GEORDI_GET" | jq -r '.genealogy.parent_session_id // ""')
  [[ "$GPARENT" == "$SID" ]] && GEORDI_PARENT_OK=1
fi

# ── 7: verify the diff + run the test ─────────────────────────────────────
log "step 7 — verifying diff in worktree"
GIT_LOG=$(git -C "$WT_PATH" log --oneline -3 2>&1 || true)
GIT_DIFF_STAT=$(git -C "$WT_PATH" diff --stat trunk..HEAD 2>&1 || true)
GIT_DIFF=$(git -C "$WT_PATH" diff trunk..HEAD -- tests/ 2>&1 || true)
TEST_OUTPUT=$(cd "$WT_PATH" && .venv/bin/pytest tests/test_metrics_registry.py -x 2>&1 | tail -30 || true)

DONE_TS=$(date +%s)
ELAPSED=$(( DONE_TS - START_TS ))

REPORT_FILE="$STATE_DIR/report.txt"
{
  echo "=== git log (last 3) ==="
  echo "$GIT_LOG"
  echo
  echo "=== git diff --stat trunk..HEAD ==="
  echo "$GIT_DIFF_STAT"
  echo
  echo "=== git diff trunk..HEAD -- tests/ (first 200 lines) ==="
  echo "$GIT_DIFF" | head -200
  echo
  echo "=== pytest output (tail) ==="
  echo "$TEST_OUTPUT"
  echo
  echo "=== captain_done_line ==="
  echo "$CAPTAIN_DONE_LINE"
  echo
  echo "=== child @data ==="
  echo "  session_id=$DATA_SID"
  echo "  parent_ok=$DATA_PARENT_OK"
  echo "  model=$DATA_MODEL"
  echo "  agentic_tool=$DATA_TOOL"
  echo
  echo "=== child @geordi ==="
  echo "  session_id=$GEORDI_SID"
  echo "  parent_ok=$GEORDI_PARENT_OK"
  echo "  model=$GEORDI_MODEL"
  echo "  agentic_tool=$GEORDI_TOOL"
  echo "  override_used=$GEORDI_OVERRIDE"
} > "$REPORT_FILE"

cat <<EOF

── Pilot C report ───────────────────────────────────────────────────────
worktree_name        : $WT_NAME
worktree_id          : $WT_ID
worktree_path        : $WT_PATH
captain_session_id   : $SID
captain_done_line    : ${CAPTAIN_DONE_LINE:-<not seen>}
data_session_id      : ${DATA_SID:-<missing>}   parent_ok=${DATA_PARENT_OK} model=${DATA_MODEL} tool=${DATA_TOOL}
geordi_session_id    : ${GEORDI_SID:-<missing>} parent_ok=${GEORDI_PARENT_OK} model=${GEORDI_MODEL} tool=${GEORDI_TOOL}
geordi_override_used : ${GEORDI_OVERRIDE:-<none>}
duration_seconds     : $ELAPSED
cost_usd             : $COST_USD
parent_stream        : $PARENT_OUT
state_dir            : $STATE_DIR
─────────────────────────────────────────────────────────────────────────
EOF
echo "Detailed verification at $REPORT_FILE"
