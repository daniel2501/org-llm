#!/bin/bash
# Strategy-novelty check between rounds (PM6-G2 / P2-5).
#
# Per docs/notes/2026-05-08-bench-arc-post-mortems.org §PM6-G2:
# R20-R23 produced rounds whose "deltas" were mostly cosmetic
# round-ID renames — no real strategic change. R26+ rejects any
# chained round whose patch content (modulo round IDs) is
# byte-identical to the prior.
#
# Usage:
#   _strategy_novelty_check.sh <prev_round_dials.py> <next_round_dials.py>
#
# Returns:
#   exit 0  — real strategic delta (>=10 non-comment non-ws lines differ)
#   exit 1  — NOVELTY_FAIL: round is essentially a rerun of prior
#
# Threshold: 10 non-comment, non-whitespace differing lines.

set -uo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <prev_round_dials.py> <next_round_dials.py>" >&2
    exit 2
fi

PREV="$1"
NEXT="$2"

if [[ ! -f "$PREV" ]]; then
    echo "[novelty] ERR: prev file not found: $PREV" >&2
    exit 2
fi
if [[ ! -f "$NEXT" ]]; then
    echo "[novelty] ERR: next file not found: $NEXT" >&2
    exit 2
fi

# Threshold for "real" strategic delta.
THRESHOLD="${NOVELTY_THRESHOLD:-10}"

# Make tmp normalized copies; strip round IDs so cosmetic
# round-ID renames don't count as strategic deltas.
TMP_PREV=$(mktemp --suffix=.py)
TMP_NEXT=$(mktemp --suffix=.py)
trap 'rm -f "$TMP_PREV" "$TMP_NEXT"' EXIT

cp "$PREV" "$TMP_PREV"
cp "$NEXT" "$TMP_NEXT"

# Normalize round-id markers in both files.
# Order matters: longest patterns first to avoid partial-match hazards.
normalize() {
    local f="$1"
    sed -i \
        -e 's/_round[0-9]\+_dials/_round_NORM/g' \
        -e 's/_round[0-9]\+_l1_primed_5tasks/_round_NORM/g' \
        -e 's/Round-[0-9]\+/Round-N/g' \
        -e 's/R[0-9]\+_LIVE/R_LIVE/g' \
        -e 's/r[0-9]\+-/r-/g' \
        -e 's/R[0-9]\+ /R /g' \
        "$f"
}

normalize "$TMP_PREV"
normalize "$TMP_NEXT"

# Diff the normalized files.
DIFF_OUT=$(diff -u "$TMP_PREV" "$TMP_NEXT" 2>/dev/null || true)

# Count non-comment, non-whitespace differing lines (added or removed).
# Diff lines start with '+' or '-' (but not '+++ ' / '--- ' headers).
# A line is "real" if after stripping leading '+' or '-' and whitespace
# it is non-empty AND does not start with '#'.
REAL_DIFF_LINES=$(printf '%s\n' "$DIFF_OUT" \
    | awk '
        /^(\+\+\+|---) / { next }
        /^[+-]/ {
            line = substr($0, 2);
            # strip leading whitespace
            sub(/^[ \t]+/, "", line);
            if (line == "") next;
            if (substr(line, 1, 1) == "#") next;
            n++;
        }
        END { print n + 0 }
    ')

# Hunk summary: count of @@ hunks in the diff.
HUNK_COUNT=$(printf '%s\n' "$DIFF_OUT" | grep -c '^@@ ' || true)

echo "[novelty] prev=$(basename "$PREV") next=$(basename "$NEXT")"
echo "[novelty] real (non-comment non-ws) differing lines: $REAL_DIFF_LINES"
echo "[novelty] diff hunks: $HUNK_COUNT"
echo "[novelty] threshold: $THRESHOLD"

if [[ "$REAL_DIFF_LINES" -lt "$THRESHOLD" ]]; then
    echo "[novelty] FAIL: round is essentially a rerun of prior"
    echo "[novelty] (diff <$THRESHOLD real lines after stripping round-id renames)"
    exit 1
fi

echo "[novelty] PASS: real strategic delta detected"
exit 0
