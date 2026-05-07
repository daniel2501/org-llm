#!/usr/bin/env bash
# release.sh — DEC-019 — Hybrid SemVer + acpt suffix release helper.
#
# DEC-019 versioning scheme:
#   MAJOR.MINOR.PATCH                — prod releases (main / trunk-promoted)
#   MAJOR.MINOR.PATCH-acptN          — acpt-branch candidates
#   MAJOR.MINOR.PATCH+devSHA         — dev / trunk builds (PEP 440 local-version)
#
# Tag prefix is `v` (e.g. v0.1.0, v0.1.1-acpt2). PEP 440 form drops the
# leading `v` and rewrites the suffix to alphaN: v0.1.1-acpt1 → 0.1.1a1.
#
# Usage:
#   release.sh CMD [ARGS...] [--yes] [--push] [--dry-run]
#
# Commands:
#   next-acpt           Compute next acpt candidate tag from latest prod tag.
#   cut-acpt VERSION    Tag current HEAD as <VERSION>-acpt1 (or next acptN).
#   promote VERSION     Promote latest <VERSION>-acptN tag → <VERSION> prod tag.
#   pep440 VERSION      Translate vX.Y.Z-acptN → X.Y.ZaN (PyPI publish).
#   current             Print working version string for current branch.
#   help                Show this help.
#
# Flags:
#   --yes               Skip interactive confirmation for tagging operations.
#   --push              Push tags to origin (default: tag locally only).
#   --dry-run           Print the actions that would run but don't execute.
#
# Exit codes (named):
#   0  E_OK              success
#   2  E_USAGE           bad usage / unknown command / missing arg
#   3  E_VERSION_FORMAT  malformed version string
#   4  E_BRANCH_MISMATCH command requires a different branch
#   5  E_DIRTY_TREE      working tree has uncommitted changes (and no --yes)
#   6  E_NO_TAG_FOUND    needed an existing tag and didn't find one
#   7  E_TAG_EXISTS      target tag already exists
#   8  E_GIT_FAILED      git plumbing call failed
#   9  E_VALIDATION      VERSION not greater than latest prod
#
# Read-only by default. Tagging operations require --yes (interactive
# y/N prompt otherwise). Pushing requires --push (never automatic).
#
# DEC-019 — Hybrid SemVer + acpt suffix.

set -euo pipefail

# ── Named exit codes ────────────────────────────────────────────────────
readonly E_OK=0
readonly E_USAGE=2
readonly E_VERSION_FORMAT=3
readonly E_BRANCH_MISMATCH=4
readonly E_DIRTY_TREE=5
readonly E_NO_TAG_FOUND=6
readonly E_TAG_EXISTS=7
readonly E_GIT_FAILED=8
readonly E_VALIDATION=9

# ── State + cleanup ─────────────────────────────────────────────────────
TMP_FILES=()
cleanup() {
  local rc=$?
  for f in "${TMP_FILES[@]:-}"; do
    [[ -n "${f:-}" && -e "$f" ]] && rm -f "$f"
  done
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── Helpers ─────────────────────────────────────────────────────────────
err()  { printf 'release.sh: %s\n' "$*" >&2; }
die()  { err "$2"; exit "$1"; }
info() { printf '%s\n' "$*"; }

# Validate vX.Y.Z (no suffix). Echoes nothing; returns 0/E_VERSION_FORMAT.
validate_prod_version() {
  local v="$1"
  if [[ ! "$v" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    return "$E_VERSION_FORMAT"
  fi
  return 0
}

# Validate vX.Y.Z-acptN.
validate_acpt_version() {
  local v="$1"
  if [[ ! "$v" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-acpt[0-9]+$ ]]; then
    return "$E_VERSION_FORMAT"
  fi
  return 0
}

# Compare two semver strings vA.B.C ; echo 1 if $1 > $2, 0 if equal, -1 if <.
semver_cmp() {
  local a="${1#v}" b="${2#v}"
  local IFS=.
  read -ra A <<<"$a"
  read -ra B <<<"$b"
  for i in 0 1 2; do
    if (( ${A[$i]:-0} > ${B[$i]:-0} )); then echo 1; return; fi
    if (( ${A[$i]:-0} < ${B[$i]:-0} )); then echo -1; return; fi
  done
  echo 0
}

# Latest prod tag (vX.Y.Z, no suffix). Echoes "" if none.
latest_prod_tag() {
  git tag --list 'v[0-9]*' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V \
    | tail -n 1
}

# Highest acptN for a given vX.Y.Z. Echoes 0 if none.
latest_acpt_n_for() {
  local base="$1"
  local n
  n=$(git tag --list "${base}-acpt[0-9]*" \
       | sed -E "s/^${base}-acpt//" \
       | grep -E '^[0-9]+$' \
       | sort -n \
       | tail -n 1)
  echo "${n:-0}"
}

# Bump patch on vX.Y.Z → vX.Y.(Z+1).
bump_patch() {
  local v="${1#v}"
  local IFS=.
  read -ra P <<<"$v"
  echo "v${P[0]}.${P[1]}.$((P[2] + 1))"
}

# Translate vX.Y.Z-acptN → X.Y.ZaN (PEP 440 acceptable).
to_pep440() {
  local v="$1"
  if validate_acpt_version "$v"; then
    # vX.Y.Z-acptN
    local stripped="${v#v}"
    echo "${stripped/-acpt/a}"
  elif validate_prod_version "$v"; then
    echo "${v#v}"
  else
    return "$E_VERSION_FORMAT"
  fi
}

current_branch() {
  git rev-parse --abbrev-ref HEAD
}

require_branch() {
  local want="$1"
  local got
  got="$(current_branch)"
  if [[ "$got" != "$want" ]]; then
    die "$E_BRANCH_MISMATCH" "current branch is '$got' but this command requires '$want'"
  fi
}

is_dirty() {
  [[ -n "$(git status --porcelain)" ]]
}

# Confirm a destructive op. Honors --yes (auto-confirm) + --dry-run (print + skip).
confirm() {
  local prompt="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "[dry-run] would: $prompt"
    return 1   # caller treats as "do not execute"
  fi
  if [[ "$YES" -eq 1 ]]; then
    return 0
  fi
  printf '%s [y/N] ' "$prompt"
  read -r reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

warn_if_dirty() {
  if is_dirty; then
    err "WARNING: working tree is dirty (uncommitted changes present)"
    if [[ "$YES" -ne 1 && "$DRY_RUN" -ne 1 ]]; then
      die "$E_DIRTY_TREE" "refuse to tag a dirty tree without --yes"
    fi
  fi
}

# ── Commands ────────────────────────────────────────────────────────────
cmd_next_acpt() {
  local prod
  prod="$(latest_prod_tag)"
  if [[ -z "$prod" ]]; then
    # No prod tag yet — bootstrap candidate is v0.1.0-acpt1.
    info "v0.1.0-acpt1"
    return 0
  fi
  local next_base
  next_base="$(bump_patch "$prod")"
  local n
  n=$(latest_acpt_n_for "$next_base")
  info "${next_base}-acpt$((n + 1))"
}

cmd_cut_acpt() {
  local version="${1:-}"
  [[ -n "$version" ]] || die "$E_USAGE" "cut-acpt requires VERSION (vX.Y.Z, no suffix)"
  validate_prod_version "$version" || die "$E_VERSION_FORMAT" \
    "VERSION must look like vX.Y.Z (no suffix); got: $version"

  require_branch "acpt"

  # Validate VERSION > latest prod tag
  local prod
  prod="$(latest_prod_tag)"
  if [[ -n "$prod" ]] && [[ "$(semver_cmp "$version" "$prod")" -le 0 ]]; then
    die "$E_VALIDATION" "VERSION $version is not greater than latest prod $prod"
  fi

  warn_if_dirty

  local n
  n=$(latest_acpt_n_for "$version")
  local tag="${version}-acpt$((n + 1))"

  if git rev-parse "$tag" >/dev/null 2>&1; then
    die "$E_TAG_EXISTS" "tag $tag already exists"
  fi

  if confirm "tag HEAD as $tag?"; then
    git tag -a "$tag" -m "$tag — acpt candidate (DEC-019)"
    info "tagged: $tag"
    if [[ "$PUSH" -eq 1 ]]; then
      git push origin "$tag"
      info "pushed: $tag → origin"
    else
      info "(not pushed; pass --push to publish)"
    fi
  fi
}

cmd_promote() {
  local version="${1:-}"
  [[ -n "$version" ]] || die "$E_USAGE" "promote requires VERSION (vX.Y.Z, no suffix)"
  validate_prod_version "$version" || die "$E_VERSION_FORMAT" \
    "VERSION must look like vX.Y.Z (no suffix); got: $version"

  # Find the latest acpt tag for this version.
  local n
  n=$(latest_acpt_n_for "$version")
  if [[ "$n" -eq 0 ]]; then
    die "$E_NO_TAG_FOUND" "no ${version}-acptN tag found to promote"
  fi
  local src_tag="${version}-acpt${n}"
  local src_sha
  src_sha="$(git rev-parse "$src_tag^{commit}")"

  if git rev-parse "$version" >/dev/null 2>&1; then
    die "$E_TAG_EXISTS" "prod tag $version already exists"
  fi

  # NOTE: agent-runnable check verification is delegated to a separate
  # gate (CI / `org-llm doctor --walkthrough`). v0.1 of release.sh just
  # surfaces the SHA + lets the operator confirm. v0.2 follow-up: shell
  # out to the validation harness automatically.
  info "promote: ${src_tag} (${src_sha:0:12}) → ${version}"
  info "  reminder: verify all :test:agent-runnable: checks pass on ${src_sha:0:12}"

  warn_if_dirty

  if confirm "tag $src_sha as $version (prod)?"; then
    git tag -a "$version" -m "$version — promoted from $src_tag (DEC-019)" "$src_sha"
    info "tagged: $version"
    if [[ "$PUSH" -eq 1 ]]; then
      git push origin "$version"
      info "pushed: $version → origin"
    else
      info "(not pushed; pass --push to publish)"
    fi
  fi
}

cmd_pep440() {
  local version="${1:-}"
  [[ -n "$version" ]] || die "$E_USAGE" "pep440 requires VERSION"
  if ! to_pep440 "$version"; then
    die "$E_VERSION_FORMAT" "VERSION must be vX.Y.Z or vX.Y.Z-acptN; got: $version"
  fi
}

cmd_current() {
  local branch
  branch="$(current_branch)"
  case "$branch" in
    main|master)
      local prod
      prod="$(latest_prod_tag)"
      info "${prod:-v0.0.0}"
      ;;
    acpt)
      # Latest acpt tag overall.
      local tag
      tag=$(git tag --list 'v[0-9]*-acpt[0-9]*' | sort -V | tail -n 1)
      info "${tag:-v0.0.0-acpt0}"
      ;;
    *)
      # Dev / trunk / feature branch — emit <latest-prod>+dev<sha>.
      local prod sha
      prod="$(latest_prod_tag)"
      sha="$(git rev-parse --short HEAD)"
      info "${prod:-v0.0.0}+dev${sha}"
      ;;
  esac
}

cmd_help() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

# ── Argument parsing ────────────────────────────────────────────────────
YES=0
PUSH=0
DRY_RUN=0
POSITIONAL=()
while (( $# > 0 )); do
  case "$1" in
    --yes)     YES=1; shift ;;
    --push)    PUSH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) cmd_help; exit "$E_OK" ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) die "$E_USAGE" "unknown flag: $1" ;;
    *)  POSITIONAL+=("$1"); shift ;;
  esac
done

if (( ${#POSITIONAL[@]} == 0 )); then
  cmd_help
  exit "$E_USAGE"
fi

CMD="${POSITIONAL[0]}"
ARGS=("${POSITIONAL[@]:1}")

# Sanity: most commands need a git repo.
if [[ "$CMD" != "help" && "$CMD" != "pep440" ]]; then
  git rev-parse --git-dir >/dev/null 2>&1 || \
    die "$E_GIT_FAILED" "not inside a git work tree"
fi

case "$CMD" in
  next-acpt) cmd_next_acpt "${ARGS[@]:-}" ;;
  cut-acpt)  cmd_cut_acpt  "${ARGS[@]:-}" ;;
  promote)   cmd_promote   "${ARGS[@]:-}" ;;
  pep440)    cmd_pep440    "${ARGS[@]:-}" ;;
  current)   cmd_current   "${ARGS[@]:-}" ;;
  help)      cmd_help ;;
  *) die "$E_USAGE" "unknown command: $CMD (try: release.sh help)" ;;
esac
