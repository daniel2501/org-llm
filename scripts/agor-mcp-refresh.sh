#!/usr/bin/env bash
# agor-mcp-refresh.sh — refresh the bearer token in an Agor MCP-config
# JSON file before its JWT expires.
#
# Designed to be called from a cron / heartbeat loop alongside a
# long-lived Agor team session whose `mcp_token` would otherwise lapse
# mid-flight. Companion to scripts/agor-smoke.sh; same conventions
# (admin JWT at ~/.agor/cli-token, REST against $AGOR_BASE_URL).
#
# Strategy:
#   1. Decode the existing config's bearer JWT (no signature verify).
#   2. If `exp - now > ttl-buffer`, exit 0 (still fresh).
#   3. Else: ensure admin JWT is valid (re-login via `pass` if expired).
#   4. GET /sessions/:id, read fresh `mcp_token` off the response.
#   5. Atomically rewrite the config (write `<path>.tmp.<pid>` → mv).
#   6. If --pid given, kill -SIGHUP <pid> as a best-effort nudge.
#
# See docs/wiki/agor-smoke-recipe.org § Long-lived sessions for the
# cron / supervisor recipe.

set -euo pipefail

# ── Exit codes ────────────────────────────────────────────────────────────
EX_OK=0
EX_PREREQ=10
EX_AUTH=11
EX_SESSION_GONE=12
EX_CONFIG_BAD=13
EX_WRITE_FAIL=14

usage() {
  cat <<'USAGE'
agor-mcp-refresh.sh [--session-id ID | --pid PID] --config PATH
                    [--check-only] [--ttl-buffer SEC]
                    [--bootstrap-token PATH]

Refreshes the bearer token in an Agor MCP-config JSON file before
expiry. Designed to be called from a cron / heartbeat loop.

REQUIRED:
  --config PATH           Path to mcp-config JSON to refresh
                          (typically /tmp/agor-mcp-<sid>.json)

ONE OF:
  --session-id ID         Agor session UUID to fetch fresh token for
  --pid PID               PID of a running claude -p; refresh discovers
                          its session via the daemon

OPTIONAL:
  --check-only            Print "EXPIRES_IN <sec>" + exit; no rewrite
  --ttl-buffer SEC        Refresh if token has < SEC remaining (default 600)
  --bootstrap-token PATH  Path to admin JWT (default ~/.agor/cli-token)

Exit codes:
  0   refreshed (or skipped because still fresh)
  10  PREREQ              missing tools / config file
  11  AUTH                admin JWT expired or invalid
  12  SESSION_GONE        session_id 404 / session ended
  13  CONFIG_BAD          mcp-config file malformed
  14  WRITE_FAIL          atomic rewrite failed
USAGE
}

die() { local code="$1"; shift; printf '[refresh FAIL %s] %s\n' "$code" "$*" >&2; exit "$code"; }
log() { [[ "${REFRESH_QUIET:-0}" == "1" ]] || printf '[refresh] %s\n' "$*" >&2; }

# ── Arg parsing ───────────────────────────────────────────────────────────
SESSION_ID=""
PID=""
CONFIG=""
CHECK_ONLY=0
TTL_BUFFER=600
BOOTSTRAP_TOKEN="${AGOR_TOKEN_FILE:-${HOME}/.agor/cli-token}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-id)      SESSION_ID="${2:-}"; shift 2 ;;
    --pid)             PID="${2:-}";        shift 2 ;;
    --config)          CONFIG="${2:-}";     shift 2 ;;
    --check-only)      CHECK_ONLY=1;        shift ;;
    --ttl-buffer)      TTL_BUFFER="${2:-}"; shift 2 ;;
    --bootstrap-token) BOOTSTRAP_TOKEN="${2:-}"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *)                 usage >&2; die $EX_PREREQ "unknown arg: $1" ;;
  esac
done

[[ -n "$CONFIG" ]] || { usage >&2; die $EX_PREREQ "--config is required"; }
if [[ -z "$SESSION_ID" && -z "$PID" ]]; then
  usage >&2; die $EX_PREREQ "one of --session-id or --pid is required"
fi

# ── Cleanup trap ──────────────────────────────────────────────────────────
TMP_FILE=""
cleanup() {
  if [[ -n "$TMP_FILE" && -e "$TMP_FILE" ]]; then
    rm -f "$TMP_FILE" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ── Step 0: prerequisites ─────────────────────────────────────────────────
for bin in jq curl; do
  command -v "$bin" >/dev/null 2>&1 || die $EX_PREREQ "missing prerequisite: $bin"
done
[[ -f "$CONFIG" ]] || die $EX_PREREQ "config not found: $CONFIG"

: "${AGOR_BASE_URL:=http://localhost:3030}"

# ── Step 1: parse existing config ─────────────────────────────────────────
# Extract Authorization header and (optionally) any embedded session id.
EXISTING_AUTH="$(jq -r '.mcpServers.agor.headers.Authorization // empty' "$CONFIG" 2>/dev/null || true)"
[[ -n "$EXISTING_AUTH" ]] || die $EX_CONFIG_BAD "no .mcpServers.agor.headers.Authorization in $CONFIG"

EXISTING_TOKEN="${EXISTING_AUTH#Bearer }"
[[ "$EXISTING_TOKEN" != "$EXISTING_AUTH" ]] || die $EX_CONFIG_BAD "Authorization header is not 'Bearer <token>'"
[[ -n "$EXISTING_TOKEN" ]] || die $EX_CONFIG_BAD "empty bearer token in $CONFIG"

# ── Step 2: decode JWT and read exp ───────────────────────────────────────
# JWT payload is base64url; pad to multiple of 4, swap url-safe chars.
b64url_decode() {
  local s="$1"
  s="${s//-/+}"; s="${s//_/\/}"
  case $(( ${#s} % 4 )) in 2) s="${s}==" ;; 3) s="${s}=" ;; esac
  printf '%s' "$s" | base64 -d 2>/dev/null || return 1
}

JWT_PAYLOAD_B64="$(printf '%s' "$EXISTING_TOKEN" | cut -d. -f2)"
[[ -n "$JWT_PAYLOAD_B64" ]] || die $EX_CONFIG_BAD "token is not a JWT (no payload segment)"

JWT_PAYLOAD_JSON="$(b64url_decode "$JWT_PAYLOAD_B64" || true)"
[[ -n "$JWT_PAYLOAD_JSON" ]] || die $EX_CONFIG_BAD "JWT payload not base64url-decodable"
echo "$JWT_PAYLOAD_JSON" | jq -e . >/dev/null 2>&1 \
  || die $EX_CONFIG_BAD "JWT payload not valid JSON"

EXP="$(echo "$JWT_PAYLOAD_JSON" | jq -r '.exp // empty')"
[[ -n "$EXP" && "$EXP" =~ ^[0-9]+$ ]] || die $EX_CONFIG_BAD "JWT payload has no numeric .exp"

# JWT .sub is the session_id for per-session mcp_tokens; fall back if --pid
# resolution becomes a follow-up feature.
JWT_SUB="$(echo "$JWT_PAYLOAD_JSON" | jq -r '.sub // empty')"

NOW=$(date +%s)
EXPIRES_IN=$(( EXP - NOW ))

if [[ "$CHECK_ONLY" == "1" ]]; then
  printf 'EXPIRES_IN %d\n' "$EXPIRES_IN"
  exit $EX_OK
fi

if (( EXPIRES_IN > TTL_BUFFER )); then
  log "still fresh: ${EXPIRES_IN}s remaining > ${TTL_BUFFER}s buffer; skipping"
  exit $EX_OK
fi

log "stale: ${EXPIRES_IN}s remaining ≤ ${TTL_BUFFER}s buffer; refreshing"

# ── Step 3: resolve session id ────────────────────────────────────────────
if [[ -z "$SESSION_ID" ]]; then
  # --pid path. v0.1: derive session from JWT .sub (mcp_token's `sub` claim
  # is the session_id). The daemon doesn't expose pid→session lookup, so
  # the JWT itself is our only handle.
  if [[ -n "$JWT_SUB" ]]; then
    SESSION_ID="$JWT_SUB"
    log "resolved session_id=${SESSION_ID} from JWT .sub (pid=${PID})"
  else
    die $EX_PREREQ "cannot resolve session: --pid given but JWT has no .sub claim"
  fi
fi

# ── Step 4: ensure admin JWT is valid ─────────────────────────────────────
admin_jwt() {
  jq -r '.accessToken // empty' "$BOOTSTRAP_TOKEN" 2>/dev/null
}

admin_jwt_valid() {
  [[ -f "$BOOTSTRAP_TOKEN" ]] || return 1
  local exp_ms now_ms
  exp_ms="$(jq -r '.expiresAt // empty' "$BOOTSTRAP_TOKEN" 2>/dev/null || true)"
  [[ -n "$exp_ms" ]] || return 1
  now_ms=$(( $(date +%s) * 1000 ))
  [[ "$exp_ms" -gt "$now_ms" ]]
}

if ! admin_jwt_valid; then
  log "admin JWT missing/expired at $BOOTSTRAP_TOKEN; attempting re-login via pass"
  command -v agor >/dev/null 2>&1 || die $EX_AUTH "agor CLI missing; cannot re-login"
  command -v pass >/dev/null 2>&1 || die $EX_AUTH "pass missing; cannot fetch admin password"
  if ! AGOR_PWD="$(pass org-llm/agor/admin-password 2>/dev/null)"; then
    die $EX_AUTH "pass org-llm/agor/admin-password unavailable"
  fi
  if ! agor auth login -e admin@agor.live -p "$AGOR_PWD" >/dev/null 2>&1; then
    die $EX_AUTH "agor auth login failed (admin creds rejected)"
  fi
  unset AGOR_PWD
  admin_jwt_valid || die $EX_AUTH "admin JWT still invalid after re-login"
fi

ADMIN_TOK="$(admin_jwt)"
[[ -n "$ADMIN_TOK" ]] || die $EX_AUTH "admin JWT empty after login"

# ── Step 5: GET /sessions/:id ─────────────────────────────────────────────
HTTP_CODE=0
SESS_RESP="$(curl -sS -o /tmp/agor-refresh-resp.$$ -w '%{http_code}' \
  --max-time 30 \
  -H "Authorization: Bearer ${ADMIN_TOK}" \
  "${AGOR_BASE_URL}/sessions/${SESSION_ID}" 2>/dev/null || true)"
HTTP_CODE="$SESS_RESP"
SESS_BODY="$(cat /tmp/agor-refresh-resp.$$ 2>/dev/null || true)"
rm -f /tmp/agor-refresh-resp.$$ 2>/dev/null || true

case "$HTTP_CODE" in
  200) : ;;
  401|403) die $EX_AUTH "GET /sessions/${SESSION_ID} returned ${HTTP_CODE} (admin JWT rejected)" ;;
  404)     die $EX_SESSION_GONE "session ${SESSION_ID} not found (404)" ;;
  *)       die $EX_SESSION_GONE "GET /sessions/${SESSION_ID} returned ${HTTP_CODE}: ${SESS_BODY}" ;;
esac

NEW_TOKEN="$(echo "$SESS_BODY" | jq -r '.mcp_token // empty' 2>/dev/null || true)"
[[ -n "$NEW_TOKEN" ]] || die $EX_SESSION_GONE "session response missing mcp_token (session archived?)"

# Belt-and-braces: if the daemon hands back the same token (it might —
# regeneration is hook-driven), accept it; the new exp is what matters.
if [[ "$NEW_TOKEN" == "$EXISTING_TOKEN" ]]; then
  log "daemon returned the same token; reusing (exp will be re-checked next pass)"
fi

# ── Step 6: atomic rewrite ────────────────────────────────────────────────
TMP_FILE="${CONFIG}.tmp.$$"
if ! jq --arg auth "Bearer ${NEW_TOKEN}" \
       '.mcpServers.agor.headers.Authorization = $auth' \
       "$CONFIG" > "$TMP_FILE" 2>/dev/null; then
  die $EX_WRITE_FAIL "jq rewrite failed"
fi

# Validate the temp file before swapping in
jq -e '.mcpServers.agor.headers.Authorization' "$TMP_FILE" >/dev/null 2>&1 \
  || die $EX_WRITE_FAIL "rewritten file failed validation"

# Preserve mode of original where possible
if command -v stat >/dev/null 2>&1; then
  ORIG_MODE="$(stat -c '%a' "$CONFIG" 2>/dev/null || stat -f '%Lp' "$CONFIG" 2>/dev/null || true)"
  [[ -n "$ORIG_MODE" ]] && chmod "$ORIG_MODE" "$TMP_FILE" 2>/dev/null || true
fi

if ! mv -f "$TMP_FILE" "$CONFIG"; then
  die $EX_WRITE_FAIL "atomic mv failed"
fi
TMP_FILE=""  # consumed; cleanup trap should not unlink the live config

log "refreshed ${CONFIG} (session=${SESSION_ID})"

# ── Step 7: SIGHUP nudge (best effort) ────────────────────────────────────
# Claude Code's --mcp-config is read at startup; SIGHUP is documented as a
# *suggestion* — most clients ignore it. Left in for clients that respect
# it; the canonical refresh path remains "supervisor restarts the agent
# after this script flips the file."
if [[ -n "$PID" ]]; then
  if kill -0 "$PID" 2>/dev/null; then
    if kill -HUP "$PID" 2>/dev/null; then
      log "SIGHUP sent to pid=${PID} (best-effort; client may ignore)"
    else
      log "SIGHUP to pid=${PID} failed (likely permissions); supervisor must restart"
    fi
  else
    log "pid=${PID} not running; no signal sent"
  fi
fi

exit $EX_OK
