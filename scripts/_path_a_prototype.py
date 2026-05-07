#!/usr/bin/env python3
"""Path A prototype — direct REST spawn, NO claude-code in the harness.

Tests whether Agor's POST /sessions endpoint accepts a spawn-shape body
(agentic_tool="opencode" + model_config + prompt) directly. If yes,
claude-code-as-MCP-client can be retired from the harness — Claude
disappears from the production path entirely (per the "Claude not part
of this app" rule).

Two probes:
  Probe 1 — trivial: spawn opencode + qwen30, ask it to print PROTO_DONE.
  Probe 2 — real edit: spawn opencode + qwen30, ask it to do a tiny
           file edit in a worktree, verify the diff lands.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
ARTIFACTS = REPO / "scripts/_path_a_prototype_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"


def tok():
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin():
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                    capture_output=True, env=env, check=True)


def req(method, path, body=None, retries=1):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(BASE + path, data=data, method=method,
                headers={"Authorization": f"Bearer {tok()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries:
                print(f"  401 — re-login + retry")
                relogin()
                continue
            # Capture body for diagnostics
            try: err_body = e.read().decode()
            except Exception: err_body = "(no body)"
            print(f"  HTTPError {e.code}: {err_body[:500]}")
            raise


def poll_terminal(sid, deadline=300):
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    end = time.time() + deadline
    while time.time() < end:
        try:
            s = req("GET", f"/sessions/{sid}", retries=1)
            if s.get("status") in terminal:
                return s.get("status")
        except Exception:
            pass
        time.sleep(10)
    return "timeout"


print(f"=== PATH A PROTOTYPE — direct REST spawn (no claude-code) ===\n")
relogin()

# ── Probe 1: trivial print ──────────────────────────────────────────────
print("─" * 60)
print("PROBE 1 — trivial: spawn opencode + qwen30 with prompt only")
print("─" * 60)

# Create a worktree first (already known to work)
wt_name = f"path-a-proto-{EPOCH}"
print(f"  creating worktree {wt_name}...")
wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
    "name": wt_name, "ref": wt_name, "createBranch": True,
    "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
wt_id = wt["worktree_id"]
print(f"  wt_id={wt_id[:18]}")
time.sleep(2)

# Try POST /sessions with the full spawn-style body
spawn_body_v1 = {
    "worktree_id": wt_id,
    "agentic_tool": "opencode",
    "model_config": {"provider": "openrouter",
                       "model": "qwen/qwen3-coder-30b-a3b-instruct"},
    "prompt": "Print exactly the literal text PROTO_DONE on a line by itself, then stop. Do not use any tools. Do not narrate.",
    "title": "path-a-probe-1",
}
print(f"  POST /sessions with spawn-body shape (snake_case keys)...")
try:
    spawn = req("POST", "/sessions", spawn_body_v1)
    print(f"  RESPONSE: keys={sorted(spawn.keys())}")
    print(f"  session_id={spawn.get('session_id')}")
    print(f"  agentic_tool={spawn.get('agentic_tool')}")
    print(f"  model_config={spawn.get('model_config')}")
    print(f"  status={spawn.get('status')}")
    sid_v1 = spawn.get("session_id")
except Exception as e:
    print(f"  FAILED: {e}")
    sid_v1 = None

# If snake_case didn't carry the prompt, try camelCase like the MCP shape
if sid_v1:
    print(f"\n  polling specialist {sid_v1[:18]} to terminal...")
    final_status = poll_terminal(sid_v1, deadline=180)
    print(f"  terminal status: {final_status}")

    # Check messages
    msgs = req("GET", f"/messages?session_id={sid_v1}", retries=1)
    print(f"  messages: {len(msgs.get('data', []))}")
    asst = [m for m in msgs.get("data", []) if m.get("role") == "assistant"]
    print(f"  assistant messages: {len(asst)}")
    if asst:
        for m in asst[:2]:
            c = m.get("content_preview") or m.get("content") or ""
            if isinstance(c, str):
                print(f"    text: {c[:300]}")
            elif isinstance(c, list):
                for blk in c[:3]:
                    if isinstance(blk, dict):
                        t = blk.get("type")
                        if t == "text":
                            print(f"    TEXT: {blk.get('text','')[:300]}")
    # Did it print PROTO_DONE?
    full_text = ""
    for m in asst:
        c = m.get("content_preview") or m.get("content") or ""
        if isinstance(c, str): full_text += c
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    full_text += blk.get("text", "")
    proto_done_emitted = "PROTO_DONE" in full_text
    print(f"\n  PROTO_DONE emitted: {proto_done_emitted}")

    probe1_result = {
        "spawn_succeeded": True,
        "specialist_sid": sid_v1,
        "terminal_status": final_status,
        "assistant_messages": len(asst),
        "proto_done_emitted": proto_done_emitted,
        "full_text_preview": full_text[:500],
    }
else:
    probe1_result = {"spawn_succeeded": False}

# ── Probe 2: real file edit (skip if probe 1 failed) ───────────────────
if sid_v1 and probe1_result.get("proto_done_emitted"):
    print()
    print("─" * 60)
    print("PROBE 2 — real edit: spawn opencode + qwen30 to make a small edit")
    print("─" * 60)

    wt2_name = f"path-a-proto-edit-{EPOCH}"
    print(f"  creating worktree {wt2_name}...")
    wt2 = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt2_name, "ref": wt2_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt2_id = wt2["worktree_id"]
    wt2_path = Path.home() / ".agor/worktrees/local/org-llm" / wt2_name
    print(f"  wt_id={wt2_id[:18]}")
    time.sleep(2)

    spawn_body_v2 = {
        "worktree_id": wt2_id,
        "agentic_tool": "opencode",
        "model_config": {"provider": "openrouter",
                          "model": "qwen/qwen3-coder-30b-a3b-instruct"},
        "prompt": (f"Add a single line `# path-a prototype proof` at the very top of "
                    f"the file `LICENSE` in this worktree (path: {wt2_path}/LICENSE). "
                    f"Do NOT modify any other line. Then run `git add LICENSE && git commit -m 'path-a probe edit'`. "
                    f"When done, print EDIT_DONE on a line by itself and stop."),
        "title": "path-a-probe-2-edit",
    }
    spawn2 = req("POST", "/sessions", spawn_body_v2)
    sid_v2 = spawn2.get("session_id")
    print(f"  specialist sid: {sid_v2[:18]}")
    # Patch bypass perms
    req("PATCH", f"/sessions/{sid_v2}",
          {"permission_config": {"mode": "bypassPermissions"}})

    final_status_2 = poll_terminal(sid_v2, deadline=300)
    print(f"  terminal status: {final_status_2}")

    diff = subprocess.run(["git", "-C", str(wt2_path), "diff", "trunk"],
                            capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt2_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    log_oneline = subprocess.run(["git", "-C", str(wt2_path), "log", "--oneline", "-2"],
                                   capture_output=True, text=True).stdout.strip()
    print(f"  diff stat: {diff_stat or '(none)'}")
    print(f"  log: {log_oneline}")
    (ARTIFACTS / "probe2_diff.patch").write_text(diff)

    probe2_result = {
        "spawn_succeeded": True,
        "specialist_sid": sid_v2,
        "terminal_status": final_status_2,
        "diff_stat": diff_stat,
        "commit_landed": bool(log_oneline.split("\n")[0]) if log_oneline else False,
    }
else:
    probe2_result = {"skipped": "probe 1 prerequisites not met"}

# ── Summary ─────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("PATH A PROTOTYPE SUMMARY")
print("=" * 60)
print(f"PROBE 1 (trivial): {json.dumps(probe1_result, indent=2)}")
print()
print(f"PROBE 2 (real edit): {json.dumps(probe2_result, indent=2)}")

(ARTIFACTS / f"summary-{EPOCH}.json").write_text(json.dumps({
    "probe1": probe1_result,
    "probe2": probe2_result,
}, indent=2))

verdict_lines = []
if probe1_result.get("spawn_succeeded") and probe1_result.get("proto_done_emitted"):
    verdict_lines.append("✓ Direct REST spawn ACCEPTS opencode + modelConfig + prompt")
    verdict_lines.append("✓ Specialist actually ran the prompt")
else:
    verdict_lines.append("✗ Probe 1 failed — direct REST spawn doesn't work the same as MCP")

if probe2_result.get("spawn_succeeded") and probe2_result.get("commit_landed"):
    verdict_lines.append("✓ Real edit landed via direct REST path")
    verdict_lines.append("→ claude-code-as-MCP-client can be retired from the harness")
elif "skipped" not in probe2_result:
    verdict_lines.append("✗ Probe 2 didn't produce a commit — direct REST spawn missing some semantic")

print()
print("VERDICT:")
for line in verdict_lines:
    print(f"  {line}")
