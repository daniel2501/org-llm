#!/usr/bin/env bash
# agor-token-refresh.sh — sourceable + standalone primitive for refreshing
# the per-session `mcp_token` JWT issued by Agor.
#
# Background:
#   `POST /sessions` issues a 24h JWT in `mcp_token`. Long-lived teams
#   (vault-hygiene, daily-review, drift-detection) need that token to
#   survive across heartbeats. Verified live 2026-05-06 against
#   agor-live v0.17.3: `GET /sessions/:id` regenerates `mcp_token`
#   in the response (after-get hook), with a fresh `exp` claim.
#
# Public API (when sourced):
#   agor_decode_jwt_exp <jwt>                        → prints exp epoch
#   agor_token_seconds_until_expiry <jwt>            → prints seconds (negative = expired)
#   agor_token_near_expiry <jwt> [thresh_seconds=300] → exit 0 if near/past expiry
#   agor_refresh_session_token <session_id>          → prints fresh mcp_token
#   agor_refresh_if_near_expiry <session_id> <jwt> [thresh=300] → prints fresh OR current
#
# Standalone CLI (when invoked directly):
#   agor-token-refresh.sh decode <jwt>
#   agor-token-refresh.sh seconds-left <jwt>
#   agor-token-refresh.sh check <jwt> [threshold_seconds]
#   agor-token-refresh.sh refresh <session_id>
#   agor-token-refresh.sh refresh-if-needed <session_id> <jwt> [threshold_seconds]
#
# Env (overridable):
#   AGOR_BASE_URL    (default http://localhost:3030)
#   AGOR_TOKEN_FILE  (default ~/.agor/cli-token)
#   AGOR_REFRESH_THRESHOLD_SECONDS (default 300 — refresh if <5 min left)
#
# Exit codes (CLI + sourced functions):
#   0  ok / near-expiry-true
#   1  near-expiry-false (decode/check ok but token has time left)
#   10 PREREQ (missing binary, daemon unreachable)
#   20 AUTH (cli-token missing/expired/invalid)
#   30 NOTFOUND (session 404)
#   35 NOMCP (response missing mcp_token field)
#   40 USAGE (bad arg shape)

set -euo pipefail

: "${AGOR_BASE_URL:=http://localhost:3030}"
: "${AGOR_TOKEN_FILE:=$HOME/.agor/cli-token}"
: "${AGOR_REFRESH_THRESHOLD_SECONDS:=300}"

_atr_die() { local code="$1"; shift; printf 'agor-token-refresh: %s\n' "$*" >&2; exit "$code"; }

_atr_require() {
  local bin
  for bin in jq curl base64; do
    command -v "$bin" >/dev/null 2>&1 || _atr_die 10 "missing prerequisite: $bin"
  done
}

_atr_admin_bearer() {
  [[ -f "$AGOR_TOKEN_FILE" ]] || _atr_die 20 "token file missing at $AGOR_TOKEN_FILE — run: agor login"
  local tok
  tok="$(jq -r '.accessToken // empty' "$AGOR_TOKEN_FILE" 2>/dev/null || true)"
  [[ -n "$tok" ]] || _atr_die 20 "token file lacks .accessToken — re-run: agor login"
  local expires_at now_ms
  expires_at="$(jq -r '.expiresAt // empty' "$AGOR_TOKEN_FILE" 2>/dev/null || true)"
  now_ms=$(($(date +%s) * 1000))
  if [[ -n "$expires_at" ]] && [[ "$expires_at" -lt "$now_ms" ]]; then
    _atr_die 20 "stored cli-token expired — re-run: agor login"
  fi
  printf '%s' "$tok"
}

agor_decode_jwt_exp() {
  local jwt="${1:-}"; [[ -n "$jwt" ]] || _atr_die 40 "agor_decode_jwt_exp: jwt required"
  local payload pad
  payload="$(printf '%s' "$jwt" | cut -d. -f2)"
  pad=$(( (4 - ${#payload} % 4) % 4 ))
  payload="${payload}$(printf '=%.0s' $(seq 1 $pad))"
  printf '%s' "$payload" | tr '_-' '/+' | base64 -d 2>/dev/null | jq -r '.exp // empty'
}

agor_token_seconds_until_expiry() {
  local jwt="${1:-}"; [[ -n "$jwt" ]] || _atr_die 40 "agor_token_seconds_until_expiry: jwt required"
  local exp now
  exp="$(agor_decode_jwt_exp "$jwt")"
  [[ -n "$exp" ]] || _atr_die 40 "could not decode exp claim from jwt"
  now=$(date +%s)
  printf '%d\n' $(( exp - now ))
}

agor_token_near_expiry() {
  local jwt="${1:-}"; local thresh="${2:-$AGOR_REFRESH_THRESHOLD_SECONDS}"
  [[ -n "$jwt" ]] || _atr_die 40 "agor_token_near_expiry: jwt required"
  local left
  left="$(agor_token_seconds_until_expiry "$jwt")"
  [[ "$left" -le "$thresh" ]]
}

agor_refresh_session_token() {
  _atr_require
  local sid="${1:-}"; [[ -n "$sid" ]] || _atr_die 40 "agor_refresh_session_token: session_id required"
  local bearer resp http_code body new_token
  bearer="$(_atr_admin_bearer)"
  # Use -w to capture the http status separately so 404s/auth fails are clear.
  resp="$(curl -sS -m 10 -w $'\n__HTTP__%{http_code}' -X GET \
            "${AGOR_BASE_URL}/sessions/${sid}" \
            -H "Authorization: Bearer ${bearer}" \
            -H "Accept: application/json" 2>&1 || true)"
  http_code="$(printf '%s' "$resp" | awk -F'__HTTP__' 'END{print $NF}')"
  body="$(printf '%s' "$resp" | sed '$d')"
  case "$http_code" in
    200) ;;
    404) _atr_die 30 "session not found: $sid" ;;
    401|403) _atr_die 20 "auth rejected (HTTP $http_code) — admin role required for after-get hook" ;;
    500)
      # v0.17.3 quirk: daemon returns HTTP 500 with `Session not found: …`
      # instead of a proper 404 when the session id is unknown. Treat the
      # message-pattern as authoritative for NOTFOUND.
      if printf '%s' "$body" | jq -e '.message | test("[Ss]ession not found")' >/dev/null 2>&1; then
        _atr_die 30 "session not found: $sid (daemon returned 500 — v0.17.3 quirk)"
      fi
      _atr_die 10 "GET /sessions/${sid} returned HTTP 500: ${body}"
      ;;
    *)   _atr_die 10 "GET /sessions/${sid} returned HTTP ${http_code:-?}: ${body}" ;;
  esac
  new_token="$(printf '%s' "$body" | jq -r '.mcp_token // empty')"
  [[ -n "$new_token" ]] || _atr_die 35 "GET /sessions/${sid} response missing mcp_token (after-get hook may not have fired for this caller)"
  printf '%s' "$new_token"
}

agor_refresh_if_near_expiry() {
  local sid="${1:-}"; local cur="${2:-}"; local thresh="${3:-$AGOR_REFRESH_THRESHOLD_SECONDS}"
  [[ -n "$sid" ]] || _atr_die 40 "agor_refresh_if_near_expiry: session_id required"
  [[ -n "$cur" ]] || _atr_die 40 "agor_refresh_if_near_expiry: current jwt required"
  if agor_token_near_expiry "$cur" "$thresh"; then
    agor_refresh_session_token "$sid"
  else
    printf '%s' "$cur"
  fi
}

# ── Standalone CLI dispatch ───────────────────────────────────────────────
# Sourced? Stop here. Invoked? Continue.
(return 0 2>/dev/null) && return 0

cmd="${1:-}"; shift || true
case "$cmd" in
  decode)
    [[ $# -eq 1 ]] || _atr_die 40 "usage: $0 decode <jwt>"
    agor_decode_jwt_exp "$1"
    ;;
  seconds-left)
    [[ $# -eq 1 ]] || _atr_die 40 "usage: $0 seconds-left <jwt>"
    agor_token_seconds_until_expiry "$1"
    ;;
  check)
    [[ $# -ge 1 ]] || _atr_die 40 "usage: $0 check <jwt> [threshold_seconds]"
    if agor_token_near_expiry "$1" "${2:-$AGOR_REFRESH_THRESHOLD_SECONDS}"; then
      printf 'NEAR_EXPIRY\n'; exit 0
    else
      printf 'OK\n'; exit 1
    fi
    ;;
  refresh)
    [[ $# -eq 1 ]] || _atr_die 40 "usage: $0 refresh <session_id>"
    agor_refresh_session_token "$1"
    printf '\n'
    ;;
  refresh-if-needed)
    [[ $# -ge 2 ]] || _atr_die 40 "usage: $0 refresh-if-needed <session_id> <current_jwt> [threshold]"
    agor_refresh_if_near_expiry "$1" "$2" "${3:-$AGOR_REFRESH_THRESHOLD_SECONDS}"
    printf '\n'
    ;;
  ''|-h|--help|help)
    sed -n '2,/^set -euo pipefail/p' "$0" | sed 's/^#//' | head -50
    exit 0
    ;;
  *)
    _atr_die 40 "unknown subcommand: $cmd (try: decode | seconds-left | check | refresh | refresh-if-needed)"
    ;;
esac
