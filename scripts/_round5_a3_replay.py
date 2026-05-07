#!/usr/bin/env python3
"""Replay A3-FOSS (full captain, qwen3-coder-30b) with explicit STYLE rules.

Same setup as A3-FOSS in the original matrix, but @atoz's brief now
includes explicit style guidance learned from hand-review of A3's first
output. Tests whether the style regressions (lost =...= formatting,
table column alignment break) were model limitations or brief gaps.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
TARGET_REL = "docs/wiki/literate-tools.org"
ARTIFACTS = Path("/home/daniel/repos/org-llm/scripts/_round5_matrix_artifacts")
BUNDLE_FILE = ARTIFACTS / "context_bundle.json"
CELL_NAME = "A3-FOSS-replay"
CELL_DIR = ARTIFACTS / CELL_NAME
LOG = CELL_DIR / f"log-{int(time.time())}.txt"

PER_SPAWN_TIMEOUT = 300
POLL_DEADLINE = 600

FOSS_MODEL_CONFIG = {"provider": "openrouter",
                      "model": "qwen/qwen3-coder-30b-a3b-instruct"}

ATOZ_PERSONA = (
    "You are @atoz — Bridge Crew wiki concept-graph specialist. "
    "Meticulous, archive-grade. You keep the wiki's [[id:UUID]] cross-link "
    "mesh well-knit by adding canonical link wrappers around prose mentions."
)
SPOCK_PERSONA = (
    "You are @spock — Bridge Crew logic + canonical-source reviewer."
)
PICARD_PERSONA = (
    "You are @picard — Bridge Crew captain. You coordinate specialists, "
    "NOT do their work. Always do deterministic data work first. FAST: "
    "pre-work in <60s. Delegate; never do specialist work yourself."
)

# Explicit style rules learned from round-5 hand review
STYLE_RULES = """
STYLE RULES (load-bearing — applied uniformly to every link insertion):

1. PRESERVE inline-code formatting INSIDE link labels.
   If the original prose has `=foo=` (org-mode verbatim/monospace),
   the link must wrap it AS-IS:
     BAD:  =foo= → [[file:bar.org][foo]]            (lost =...= formatting)
     GOOD: =foo= → [[file:bar.org][=foo=]]          (preserved)
   Same rule for `~code~` and `*bold*` and `/italic/` — keep the wrap
   markers INSIDE the link label.

2. TABLE-CELL CARE. If wrapping a value inside an org-mode table cell,
   note that link syntax adds ~20 chars and may break visual column
   alignment. Either:
     - skip table-cell wrapping in favor of an equivalent prose mention,
     - or accept the alignment break and trust org-mode to re-align on
       next edit (`org-table-align` will fix it).
   If you do wrap a table cell, prefer the shortest possible link
   target (e.g. id-form is shorter than file-form).

3. PROSE MEANING. The wrapper changes nothing semantic. The label
   text inside `[[...][LABEL]]` must equal the original phrase
   character-for-character (modulo the inline-code markers per rule 1).
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(line + "\n")


def tok() -> str:
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin() -> None:
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                        capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                   capture_output=True, env=env, check=True)


def req(method: str, path: str, body: dict | None = None, retries: int = 1) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(
                BASE + path, data=data, method=method,
                headers={"Authorization": f"Bearer {tok()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries:
                log("  401 — re-login + retry")
                relogin()
                continue
            raise


def run_captain_spawn(captain_id: str, label: str, title: str, persona: str,
                       task: str, model_config: dict, budget_usd: float = 0.30):
    fresh = req("GET", f"/sessions/{captain_id}", retries=1)
    mcp_token = fresh.get("mcp_token")
    mcp_cfg = CELL_DIR / f"mcp-{label}.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))
    spawn_args = {"prompt": persona + "\n\n" + task,
                  "title": title,
                  "agenticTool": "opencode",
                  "modelConfig": model_config}
    captain_prompt = f"""You have one MCP server "agor" exposing two tools.

Spawn ONE child by issuing a single mcp__agor__agor_execute_tool call:
  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(spawn_args)}

After spawn response, print exactly:
SPAWN_SESSION_ID=<session_id>
SPAWN_DONE
Then stop."""
    parent_out = CELL_DIR / f"captain-{label}.jsonl"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", "sonnet",
           "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
           "--permission-mode", "bypassPermissions",
           "--max-budget-usd", str(budget_usd), captain_prompt]
    log(f"  captain claude -p {label} (budget=${budget_usd})")
    t0 = time.time()
    with parent_out.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                           timeout=PER_SPAWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  TIMEOUT")
    elapsed = time.time() - t0
    sid, cost = None, 0.0
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            cost += float(ev.get("total_cost_usd") or 0)
            for ln in (ev.get("result", "") or "").splitlines():
                if ln.startswith("SPAWN_SESSION_ID="):
                    sid = ln.split("=", 1)[1].strip()
    log(f"  {label} sid={sid} cost=${cost:.4f} elapsed={elapsed:.1f}s")
    return sid, cost


def poll_terminal(sid: str, deadline_s: int = POLL_DEADLINE) -> str:
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            s = req("GET", f"/sessions/{sid}", retries=1)
            if s.get("status") in terminal:
                return s.get("status")
        except Exception as e:
            log(f"  poll err: {e}")
        time.sleep(15)
    return "timeout"


# ── Main ───────────────────────────────────────────────────────────────────
CELL_DIR.mkdir(exist_ok=True)
bundle = json.loads(BUNDLE_FILE.read_text())
log(f"A3-FOSS REPLAY (with style rules) :: target={TARGET_REL}")
relogin()

cell_start = time.time()
cell_cost = 0.0

wt_name = f"pilot-R5-A3-FOSS-replay-{int(time.time())}"
wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
    "name": wt_name, "ref": wt_name, "createBranch": True,
    "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
wt_id = wt["worktree_id"]
wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
log(f"wt_id={wt_id[:18]} path={wt_path}")
time.sleep(2)

cap = req("POST", "/sessions",
          {"worktree_id": wt_id, "agentic_tool": "claude-code"})
cap_id = cap["session_id"]
req("PATCH", f"/sessions/{cap_id}",
    {"permission_config": {"mode": "bypassPermissions"}})
log(f"captain_pilot={cap_id[:18]}")

# @picard decompose with style awareness
log("@picard decompose pass...")
decompose_task = (
    f"You are @picard. Specialists @atoz + @spock will audit "
    f"`{bundle['target_path']}` ({bundle['target_lines']} lines) for "
    f"5 cross-link insertions.\n\nPRE-FETCHED: "
    f"{bundle['known_ids_count']} known wiki IDs; "
    f"{len(bundle['candidate_concepts'])} candidate insertion points "
    f"already located by name-matching scan.\n\n"
    f"FULL UUIDs (use these literally — do not truncate):\n"
    + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                f"{c.get('matched_basename', c.get('matched_dec',''))} "
                f"-> id={c['candidate_ids'][0]}"
                for c in bundle['candidate_concepts'][:15])
    + f"\n\n{STYLE_RULES}\n\n"
    "Write ONE-PARAGRAPH role briefs for @atoz and @spock. The atoz brief "
    "MUST emphasize the style rules above (especially preserving =...= "
    "verbatim formatting inside link labels). Output exactly:\n"
    "ATOZ_BRIEF: <para>\nSPOCK_BRIEF: <para>\nPICARD_DONE")
picard_sid, picard_cost = run_captain_spawn(
    cap_id, "picard", "r5-replay-picard", PICARD_PERSONA,
    decompose_task, FOSS_MODEL_CONFIG, budget_usd=0.20)
cell_cost += picard_cost
poll_terminal(picard_sid, deadline_s=180) if picard_sid else None

# Read @picard's briefs
picard_brief = {"atoz": "", "spock": ""}
if picard_sid:
    msgs = req("GET", f"/messages?session_id={picard_sid}&$limit=50")
    asst = "\n".join(str(m.get("content_preview") or m.get("content") or "")
                      for m in msgs.get("data", []) if m.get("role") == "assistant")
    am = re.search(r"ATOZ_BRIEF:\s*(.+?)(?=SPOCK_BRIEF:|$)", asst, re.DOTALL)
    sm = re.search(r"SPOCK_BRIEF:\s*(.+?)(?=PICARD_DONE|$)", asst, re.DOTALL)
    picard_brief["atoz"] = am.group(1).strip() if am else ""
    picard_brief["spock"] = sm.group(1).strip() if sm else ""
log(f"@picard atoz_brief={'YES' if picard_brief['atoz'] else 'NONE'} "
    f"spock_brief={'YES' if picard_brief['spock'] else 'NONE'}")

# @atoz with explicit style rules + full UUIDs from bundle
atoz_task = (
    f"Audit `{bundle['target_path']}` ({bundle['target_lines']} lines) for "
    f"outgoing-cross-link opportunities. Page is concept-graph orphan.\n\n"
    f"Identify EXACTLY 5 places to add `[[file:X.org][label]]` cross-link "
    f"wrappers. Apply directly. Do NOT touch any other file.\n\n"
    f"PRE-FETCHED CONTEXT (FULL UUIDs — use these literally):\n"
    + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                f"{c.get('matched_basename', c.get('matched_dec',''))} "
                f"-> id={c['candidate_ids'][0]}"
                for c in bundle['candidate_concepts'][:12])
    + f"\n\n{STYLE_RULES}\n\n"
    f"@picard's brief:\n{picard_brief['atoz']}\n\n"
    f"When done, run `git add` + `git commit -m \"docs(wiki): add 5 "
    f"cross-links to literate-tools.org (round-5 A3 replay with style)\"` "
    f"in your worktree, then print `ATOZ_DONE` and stop.")

atoz_sid, atoz_cost = run_captain_spawn(
    cap_id, "atoz", "r5-replay-atoz", ATOZ_PERSONA, atoz_task,
    FOSS_MODEL_CONFIG, budget_usd=0.30)
cell_cost += atoz_cost

if atoz_sid:
    req("PATCH", f"/sessions/{atoz_sid}",
        {"permission_config": {"mode": "bypassPermissions"}})

# @spock review (full captain mode keeps it simple — no btw probe required for replay)
spock_task = (
    f"Review @atoz's diff on `{TARGET_REL}` (atoz session_id={atoz_sid}).\n\n"
    f"Wait for @atoz's commit to land in the worktree (poll git log every "
    f"~30s; give up at 5 min). Read the diff carefully. Verify the {STYLE_RULES} "
    f"are followed for each of the 5 insertions. Send up to 3 redirecting "
    f"`mode:'btw'` questions to @atoz only if you spot violations. Print "
    f"verdict (APPROVE / REQUEST-REVISE) + SPOCK_DONE.\n\n"
    f"@picard's brief:\n{picard_brief['spock']}")
spock_sid, spock_cost = run_captain_spawn(
    cap_id, "spock", "r5-replay-spock", SPOCK_PERSONA, spock_task,
    FOSS_MODEL_CONFIG, budget_usd=0.20)
cell_cost += spock_cost
if spock_sid:
    req("PATCH", f"/sessions/{spock_sid}",
        {"permission_config": {"mode": "bypassPermissions"}})

atoz_status = poll_terminal(atoz_sid) if atoz_sid else "n/a"
spock_status = poll_terminal(spock_sid) if spock_sid else "n/a"

# Capture diff
diff_stat = subprocess.run(
    ["git", "-C", str(wt_path), "diff", "--stat", "trunk", "--", TARGET_REL],
    capture_output=True, text=True).stdout.strip()
diff_full = subprocess.run(
    ["git", "-C", str(wt_path), "diff", "trunk", "--", TARGET_REL],
    capture_output=True, text=True).stdout
log_oneline = subprocess.run(
    ["git", "-C", str(wt_path), "log", "--oneline", "-2"],
    capture_output=True, text=True).stdout.strip()
(CELL_DIR / "diff.patch").write_text(diff_full)

cell_wall = time.time() - cell_start
print()
print("=" * 70)
print(f"REPORT — {CELL_NAME}")
print("=" * 70)
print(f"WT={wt_name}")
print(f"WT_ID={wt_id}")
print(f"CAPTAIN_PILOT={cap_id}")
print(f"PICARD={picard_sid} brief_present={bool(picard_brief['atoz'])}")
print(f"ATOZ={atoz_sid} status={atoz_status}")
print(f"SPOCK={spock_sid} status={spock_status}")
print(f"DIFF: {diff_stat or '(none)'}")
print(f"WORKTREE_LOG: {log_oneline}")
print(f"COST: ${cell_cost:.4f}")
print(f"WALL: {cell_wall:.0f}s")
print()
print("=" * 70)
print("DIFF for hand review:")
print("=" * 70)
print(diff_full or "(no diff)")
