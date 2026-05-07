#!/usr/bin/env bash
# agor-pilot-a.sh — Pilot A (solo @data specialist) Agor harness
#
# Drives Agor to spawn ONE crew session that adds type hints + docstrings
# to org_llm/notices.py inside its own worktree. We poll for completion,
# verify the diff, and report.
#
# Built from agor-smoke.sh / agor-test-fanout.sh patterns.

set -euo pipefail

: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_DATA_DIR:=$HOME/.agor}"
: "${AGOR_TOKEN_FILE:=${AGOR_DATA_DIR}/cli-token}"
: "${AGOR_REPO_ID:=019dfbd1-abe8-717b-b0b4-099526fe0b65}"
: "${WT_NAME:=pilot-A-$(date +%s)}"
: "${MODEL:=sonnet}"
: "${MAX_BUDGET_USD:=5.00}"
: "${TIMEOUT:=1500}"   # 25 min wall cap
: "${POLL_DEADLINE_SECONDS:=1200}"

export PATH="$HOME/.npm-global/bin:$HOME/.guix-profile/bin:$PATH"

START_TS=$(date +%s)
PARENT_OUT="/tmp/agor-pilot-a-parent-$$.jsonl"
MCP_CONFIG="/tmp/agor-pilot-a-mcp-$$.json"
STATE_DIR="/tmp/agor-pilot-a-state-$$"
mkdir -p "$STATE_DIR"

log() { printf '[pilot-A] %s\n' "$*" >&2; }
die() { printf '[pilot-A FAIL] %s\n' "$*" >&2; exit 1; }

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

# ── 2: session ────────────────────────────────────────────────────────────
log "step 2 — create session on worktree"
SESS_BODY=$(jq -nc --arg wt "$WT_ID" '{worktree_id:$wt, agentic_tool:"claude-code"}')
SESS_JSON=$(api POST /sessions "$SESS_BODY")
echo "$SESS_JSON" > "$STATE_DIR/session.json"
SID=$(echo "$SESS_JSON" | jq -r '.session_id // empty')
MCP_TOKEN=$(echo "$SESS_JSON" | jq -r '.mcp_token // empty')
[[ -n "$SID" ]] || die "session create failed: $SESS_JSON"
log "  session_id=$SID"

# Lift permission_config so the session bypasses prompts
api PATCH "/sessions/${SID}" '{"permission_config":{"mode":"bypassPermissions"}}' >/dev/null
log "  permission_config: bypassPermissions"

# ── 3: write task prompt to a file ────────────────────────────────────────
PROMPT_FILE="$STATE_DIR/prompt.txt"
cat > "$PROMPT_FILE" <<'EOF'
You are @data — the Bridge Crew scribe (Lt. Cmdr. Data persona) operating
as a code-quality specialist for this single task. You are precise,
careful, and you do not presume.

=== ROLE ===
I turn ideas into clean structured artifacts. I confirm before saving.
I prefer explicit signatures over deep cleverness. I do not change
behavior; I document it.

=== TASK ===
You are working inside an Agor-managed worktree of the org-llm repo
(branch already checked out). The file org_llm/notices.py (61 LoC)
needs Python type hints and one-line docstrings on every public
function/class. Existing behavior MUST NOT change.

Specifically:
1. Read org_llm/notices.py.
2. Add precise type hints to every public function/class signature
   (params + return type). The module-level `notice()` function is
   the main one; ensure all params and the `-> None` return type
   are typed (some are already; keep them). If there are private
   helpers, type them too.
3. Add a one-line docstring to any public function/class that lacks
   one. The existing `notice()` docstring is multi-line; leave it.
4. Run `python -c "import org_llm.notices"` from the worktree root
   to confirm the module imports clean. If it fails, fix and retry.
5. `git add org_llm/notices.py` and `git commit -m "style(notices):
   add type hints + docstrings (Pilot A)"` on the current branch.
   Do NOT push.
6. Print exactly the literal line on its own:
      TASK_DONE
   and then stop. Do not narrate further.

Constraints:
- Do not modify any other file.
- Do not change runtime behavior.
- If the file already has full hints + docstrings, still commit a
  trivial whitespace-stable touch is NOT acceptable — instead print
     TASK_DONE_NOOP
  on its own line and stop.
EOF

PROMPT="$(cat "$PROMPT_FILE")"

# Write the MCP config (parent talks back to Agor if it needs to,
# though for this Pilot it mostly just runs Claude on the worktree).
jq -nc --arg url "${AGOR_BASE_URL}/mcp" --arg auth "Bearer ${MCP_TOKEN}" \
  '{mcpServers:{agor:{type:"http", url:$url, headers:{Authorization:$auth}}}}' \
  > "$MCP_CONFIG"

# ── 4: post the prompt as a user message to drive the session ─────────────
# Approach: run claude -p locally on the worktree path. This is the same
# "outer Claude pilots Agor" pattern the smoke test uses, except the
# worktree IS our work surface.
log "step 4 — invoking claude -p on worktree (model=$MODEL, budget=\$$MAX_BUDGET_USD, timeout=${TIMEOUT}s)"
log "  worktree_path=$WT_PATH"

if [[ ! -d "$WT_PATH" ]]; then
  die "worktree path does not exist on disk: $WT_PATH (executor may not have populated it yet — wait + retry)"
fi

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

# ── 5: parse cost + outcome ───────────────────────────────────────────────
COST_USD=$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd] | add // 0' < "$PARENT_OUT" 2>/dev/null || echo 0)
DONE_TS=$(date +%s)
ELAPSED=$(( DONE_TS - START_TS ))

TASK_DONE_LINE=$(grep -oE 'TASK_DONE(_NOOP)?' "$PARENT_OUT" | head -1 || true)

# ── 6: verify the diff ────────────────────────────────────────────────────
log "step 6 — verifying diff in worktree"
GIT_LOG=$(git -C "$WT_PATH" log --oneline -3 2>&1 || true)
GIT_DIFF_STAT=$(git -C "$WT_PATH" diff --stat trunk..HEAD 2>&1 || true)
GIT_DIFF=$(git -C "$WT_PATH" diff trunk..HEAD -- org_llm/notices.py 2>&1 || true)
IMPORT_CHECK=$(cd "$WT_PATH" && python3 -c "import org_llm.notices; print('IMPORT_OK')" 2>&1 || true)

REPORT_FILE="$STATE_DIR/report.txt"
{
  echo "=== git log (last 3) ==="
  echo "$GIT_LOG"
  echo
  echo "=== git diff --stat trunk..HEAD ==="
  echo "$GIT_DIFF_STAT"
  echo
  echo "=== git diff trunk..HEAD -- org_llm/notices.py ==="
  echo "$GIT_DIFF"
  echo
  echo "=== import check ==="
  echo "$IMPORT_CHECK"
  echo
  echo "=== task_done_line ==="
  echo "$TASK_DONE_LINE"
} > "$REPORT_FILE"

cat <<EOF

── Pilot A report ───────────────────────────────────────────────────────
worktree_name      : $WT_NAME
worktree_id        : $WT_ID
worktree_path      : $WT_PATH
session_id         : $SID
task_done_line     : ${TASK_DONE_LINE:-<not seen>}
duration_seconds   : $ELAPSED
cost_usd           : $COST_USD
parent_stream      : $PARENT_OUT
state_dir          : $STATE_DIR
import_check       : $IMPORT_CHECK
─────────────────────────────────────────────────────────────────────────
EOF
echo "Detailed verification at $REPORT_FILE"
