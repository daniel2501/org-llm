#!/usr/bin/env python3
"""Solo Claude baseline — no Agor, no team, single `claude -p` does it all.

The canonical A/B for the 'FOSS + orchestration beats solo Claude'
aspiration. Same task as the round-5 matrix, same bundle context,
but one Claude session does everything: audit + 5 cross-link edits +
commit. No @atoz, no @spock, no @picard, no Agor.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
TARGET_REL = "docs/wiki/literate-tools.org"
ARTIFACTS = REPO / "scripts/_round5_matrix_artifacts/Solo-Claude"
ARTIFACTS.mkdir(exist_ok=True)
BUNDLE = json.loads((REPO / "scripts/_round5_matrix_artifacts/context_bundle.json").read_text())

EPOCH = int(time.time())
WT_NAME = f"pilot-R5-soloClaude-{EPOCH}"
WT_PATH = REPO.parent / "org-llm-worktrees" / WT_NAME

print(f"[solo-claude] start :: target={TARGET_REL}")

# Create worktree (NOT via Agor — pure git)
WT_PATH.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-b", WT_NAME,
                str(WT_PATH), "trunk"], check=True, capture_output=True)
print(f"[solo-claude] worktree at {WT_PATH}")

# Build task brief — same as @atoz brief in matrix A1, but single-session
candidates_str = "\n".join(
    f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
    f"{c.get('matched_basename', c.get('matched_dec',''))} "
    f"-> id={c['candidate_ids'][0]}"
    for c in BUNDLE['candidate_concepts'][:12]
)

task = f"""You are an experienced wiki concept-graph specialist. Audit the org-mode wiki page `{TARGET_REL}` ({BUNDLE['target_lines']} lines) for outgoing-cross-link opportunities. The page is currently a concept-graph orphan with ZERO `[[id:UUID]]` outgoing links.

Your job: identify EXACTLY 5 places to add `[[id:UUID][label]]` cross-link wrappers in the page. Apply the edits directly. Do NOT touch any other file. Do NOT change prose meaning — only wrap existing mentions.

PRE-FETCHED CONTEXT (deterministic, from grep + file reads):
- {BUNDLE['known_ids_count']} known wiki IDs catalogued across docs/wiki/
- {len(BUNDLE['candidate_concepts'])} candidate insertion points already located by name-matching scan:
{candidates_str}

Full bundle: {ARTIFACTS.parent}/context_bundle.json

STYLE RULES:
1. PRESERVE inline-code formatting INSIDE link labels. If the original prose has `=foo=`, the link must keep it: `=foo=` → `[[id:UUID][=foo=]]` (not `[[id:UUID][foo]]`).
2. Prefer `[[id:UUID]]` form over `[[file:X.org]]` (id-links are portable).
3. Don't wrap mentions that are ALREADY inside link labels.

When done, run `git add docs/wiki/literate-tools.org && git commit -m "docs(wiki): add 5 cross-links to literate-tools.org (solo-Claude baseline)"` in this worktree. Then print exactly `SOLO_DONE` and stop.
"""

# Run claude -p, blocking
env = os.environ.copy()
env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
               f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))

stream_out = ARTIFACTS / f"stream-{EPOCH}.jsonl"
cmd = ["claude", "-p",
       "--model", "sonnet",
       "--output-format", "stream-json", "--verbose",
       "--permission-mode", "bypassPermissions",
       "--max-budget-usd", "0.50",
       task]

print(f"[solo-claude] running claude -p (cwd={WT_PATH})...")
t0 = time.time()
with stream_out.open("w") as f:
    rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                         cwd=str(WT_PATH), timeout=600).returncode
elapsed = time.time() - t0
print(f"[solo-claude] rc={rc} elapsed={elapsed:.1f}s")

# Aggregate cost
cost = 0.0
for line in stream_out.read_text().splitlines():
    line = line.strip()
    if not line: continue
    try: ev = json.loads(line)
    except Exception: continue
    if ev.get("type") == "result":
        cost += float(ev.get("total_cost_usd") or 0)

# Capture diff
diff_stat = subprocess.run(
    ["git", "-C", str(WT_PATH), "diff", "--stat", "trunk", "--", TARGET_REL],
    capture_output=True, text=True).stdout.strip()
diff_full = subprocess.run(
    ["git", "-C", str(WT_PATH), "diff", "trunk", "--", TARGET_REL],
    capture_output=True, text=True).stdout
log_oneline = subprocess.run(
    ["git", "-C", str(WT_PATH), "log", "--oneline", "-2"],
    capture_output=True, text=True).stdout.strip()
(ARTIFACTS / "diff.patch").write_text(diff_full)

print()
print("=" * 70)
print("SOLO-CLAUDE BASELINE REPORT")
print("=" * 70)
print(f"WT: {WT_PATH}")
print(f"DIFF: {diff_stat or '(none)'}")
print(f"COMMIT: {log_oneline}")
print(f"COST: ${cost:.4f}")
print(f"WALL: {elapsed:.0f}s")
print()
print("=" * 70)
print("DIFF for hand review:")
print("=" * 70)
print(diff_full or "(no diff)")
