#!/usr/bin/env bash
# verify_cell_emacs.sh -- R26 P2-2 headless-Emacs cell verifier
#
# Usage: verify_cell_emacs.sh <worktree-path> <target-file>
#
#   <worktree-path>  Absolute path to the cell worktree.
#   <target-file>    Path of the file under test, relative to worktree
#                    (or absolute; we normalize either way).
#
# Spawns `emacs --batch -Q -l elisp/verify_cell.el -f verify-cell-main`,
# captures stdout (JSON diagnostics), writes it to
# `<worktree>/.verify_cell.json`, and exits:
#
#   0  -> {"errors": [], "warnings": []*}   (warnings tolerated)
#   1  -> any errors, or emacs/parse failure
#
# The elisp side is the gold-standard semantic gate; this wrapper exists
# so the bench harness can shell out and check $? without parsing JSON.

set -u

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <worktree-path> <target-file>" >&2
    exit 2
fi

worktree=$1
target=$2

if [[ ! -d $worktree ]]; then
    echo "verify_cell_emacs: worktree not a directory: $worktree" >&2
    exit 2
fi

# Normalize target to absolute.
if [[ $target = /* ]]; then
    abs_target=$target
else
    abs_target=$worktree/$target
fi

if [[ ! -f $abs_target ]]; then
    echo "verify_cell_emacs: target file not found: $abs_target" >&2
    exit 2
fi

# Locate the elisp helper relative to this script (works whether invoked
# from the repo root or from a sibling worktree).
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
elisp_file=$repo_root/elisp/verify_cell.el

if [[ ! -f $elisp_file ]]; then
    echo "verify_cell_emacs: elisp helper missing: $elisp_file" >&2
    exit 2
fi

emacs_bin=${EMACS:-emacs}

json_out=$(VERIFY_FILE="$abs_target" \
    "$emacs_bin" --batch -Q -l "$elisp_file" -f verify-cell-main 2>/dev/null)
emacs_rc=$?

if [[ $emacs_rc -ne 0 || -z $json_out ]]; then
    # Last-resort fallback so consumers always have a JSON file.
    json_out=$(printf '{"file":"%s","kind":"unknown","errors":["emacs --batch failed (rc=%d)"],"warnings":[]}' \
        "$abs_target" "$emacs_rc")
fi

# Keep only the last line of stdout in case Emacs leaked stray prints.
last_line=$(printf '%s' "$json_out" | awk 'NF{line=$0} END{print line}')
if [[ -n $last_line ]]; then
    json_out=$last_line
fi

out_path=$worktree/.verify_cell.json
printf '%s\n' "$json_out" > "$out_path"

# Decide gate: any element in `errors` -> fail.
# Cheap parse: look for non-empty errors array. We try python3 first for
# correctness, then fall back to a regex.
if command -v python3 >/dev/null 2>&1; then
    if python3 -c '
import json, sys
data = json.loads(sys.argv[1])
sys.exit(1 if data.get("errors") else 0)
' "$json_out" 2>/dev/null; then
    rc=0
else
    rc=1
fi
else
    if grep -Eq '"errors"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]' <<<"$json_out"; then
        rc=0
    else
        rc=1
    fi
fi

exit $rc
