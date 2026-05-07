#!/usr/bin/env python3
"""(b') R3-Memory tick-2 driver — top-level execution, no sub-agent.

Spawns @atoz on a fresh worktree where tick-1 memory file has been
pre-copied. Tick-2 must read the file, audit a SECOND wiki page,
APPEND findings under `## Tick 2`, explicitly cross-reference tick-1.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
WT_ID = Path("/tmp/_b_prime_wt_id").read_text().strip()
WT_NAME = Path("/tmp/_b_prime_wt_name").read_text().strip()
EPOCH = Path("/tmp/_b_prime_epoch").read_text().strip()
WT_PATH = Path.home() / ".agor" / "worktrees" / "local" / "org-llm" / WT_NAME
ARTIFACTS = Path("/home/daniel/repos/org-llm/scripts/_b_prime_tick2_artifacts")
ARTIFACTS.mkdir(exist_ok=True)


def tok() -> str:
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def req(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())


# ── Step 1: pick second target wiki file (must exist in worktree) ─────────
small_wikis = sorted(
    [p for p in (WT_PATH / "docs" / "wiki").glob("*.org") if 80 < p.stat().st_size < 6000],
    key=lambda p: p.stat().st_size,
)
if not small_wikis:
    print("FAIL: no small wiki files in worktree", file=sys.stderr)
    sys.exit(1)
target_wiki = small_wikis[len(small_wikis) // 3]  # pick a small-ish but not tiniest
print(f"target_wiki: {target_wiki.relative_to(WT_PATH)} ({target_wiki.stat().st_size} bytes)")

# ── Step 2: captain session ────────────────────────────────────────────────
print(f"creating captain on worktree {WT_ID[:18]}...")
captain = req("POST", "/sessions", {"worktree_id": WT_ID, "agentic_tool": "claude-code"})
CAPTAIN_ID = captain["session_id"]
MCP_TOKEN = captain["mcp_token"]
print(f"captain={CAPTAIN_ID[:18]}")

req("PATCH", f"/sessions/{CAPTAIN_ID}", {"permission_config": {"mode": "bypassPermissions"}})
print("PATCHed bypassPermissions")

# ── Step 3: refresh mcp_token (R2-D2 pattern) ──────────────────────────────
fresh = req("GET", f"/sessions/{CAPTAIN_ID}")
MCP_TOKEN = fresh.get("mcp_token") or MCP_TOKEN
mcp_cfg_path = ARTIFACTS / f"mcp-{EPOCH}.json"
mcp_cfg_path.write_text(
    json.dumps(
        {
            "mcpServers": {
                "agor": {
                    "type": "http",
                    "url": f"{BASE}/mcp",
                    "headers": {"Authorization": f"Bearer {MCP_TOKEN}"},
                }
            }
        }
    )
)

# ── Step 4: build captain spawn prompt ─────────────────────────────────────
atoz_persona = (
    "You are @atoz — Bridge Crew wiki-link / cross-reference specialist. "
    "Personality: meticulous, archive-grade. You write in concise, evidence-citing prose. "
    "You audit org-mode wiki cross-links and write findings to memory files."
)

target_rel = str(target_wiki.relative_to(WT_PATH))

atoz_task = f"""You are @atoz. You have a memory file at `.agor-assistants/atoz/memory/2026-05-06.md` from a PREVIOUS TICK on this worktree's predecessor. READ IT FIRST so you remember what tick 1 found.

Now do a tick-2 audit on a SECOND wiki page: `{target_rel}` (in your worktree). Check that all `[[id:...]]` links in this page resolve to existing org-roam :ID: entries elsewhere under `docs/wiki/`. Build a known-IDs set with: `grep -h "^:ID:" docs/wiki/*.org | awk '{{print $2}}' | sort -u`. Extract `[[id:UUID]]` from the target page. Flag broken links.

APPEND your findings to the SAME memory file (`.agor-assistants/atoz/memory/2026-05-06.md`) under a NEW heading `## Tick 2`. Do NOT overwrite tick-1 content. In your tick-2 findings, EXPLICITLY reference at least one tick-1 finding (e.g. "tick 1 found target file absent — tick 2 confirms / extends / contrasts ...").

When done, print exactly `TICK_2_DONE` on its own line and stop.
"""

captain_prompt = f"""You have one MCP server "agor" exposing two tools: mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Spawn ONE child session by issuing a single mcp__agor__agor_execute_tool call with snake_case tool_name:

  tool_name: "agor_sessions_spawn"
  arguments: {{"prompt": <PROMPT_BELOW>, "title": "r3memoryT2-atoz-tick2", "agenticTool": "claude-code"}}

PROMPT_BELOW (verbatim, including the persona + task):
---PROMPT_START---
{atoz_persona}

{atoz_task}
---PROMPT_END---

After the spawn response comes back, print exactly these two lines and then stop:
ATOZ_SESSION_ID=<session_id from spawn response>
SPAWN_DONE

Do not poll. Do not narrate. Issue the spawn, print the two lines, stop.
"""

# ── Step 5: run claude -p (one-spawn-per-claude-p per F9 amendment) ────────
print("running captain claude -p...")
parent_out = ARTIFACTS / f"captain-{EPOCH}.jsonl"
env = os.environ.copy()
env["PATH"] = f"{os.path.expanduser('~/.npm-global/bin')}:{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", "")

cmd = [
    "claude", "-p",
    "--model", "sonnet",
    "--output-format", "stream-json", "--verbose",
    "--mcp-config", str(mcp_cfg_path),
    "--strict-mcp-config",
    "--permission-mode", "bypassPermissions",
    "--max-budget-usd", "2.5",
    captain_prompt,
]
t0 = time.time()
with parent_out.open("w") as f:
    p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, timeout=600)
elapsed = time.time() - t0
print(f"captain rc={p.returncode} elapsed={elapsed:.1f}s")

# ── Step 6: parse atoz session id + cost ───────────────────────────────────
atoz_id = None
captain_cost = 0.0
for line in parent_out.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "result":
        captain_cost += float(ev.get("total_cost_usd") or 0)
        result = ev.get("result", "") or ""
        for ln in result.splitlines():
            if ln.startswith("ATOZ_SESSION_ID="):
                atoz_id = ln.split("=", 1)[1].strip()
print(f"atoz_session_id={atoz_id} captain_cost=${captain_cost:.4f}")

if not atoz_id:
    print("FAIL: no atoz session id from captain output", file=sys.stderr)
    sys.exit(2)

# ── Step 7: PATCH bypass on atoz child + poll to idle ──────────────────────
try:
    req("PATCH", f"/sessions/{atoz_id}", {"permission_config": {"mode": "bypassPermissions"}})
except Exception as e:
    print(f"warn: PATCH on atoz failed: {e}")

terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
deadline = time.time() + 900
while time.time() < deadline:
    s = req("GET", f"/sessions/{atoz_id}")
    st = s.get("status")
    print(f"  poll: status={st}")
    if st in terminal:
        print(f"  terminal: {st}")
        break
    time.sleep(10)

# ── Step 8: verify memory file post-tick-2 ─────────────────────────────────
mem_file = WT_PATH / ".agor-assistants" / "atoz" / "memory" / "2026-05-06.md"
if not mem_file.exists():
    print("FAIL: memory file vanished")
    sys.exit(3)
mem_content = mem_file.read_text()
mem_lines = mem_content.splitlines()
has_tick2 = "## Tick 2" in mem_content
has_tick1 = "## Tick 1" in mem_content
references_tick1 = any(
    "tick 1" in ln.lower() or "tick-1" in ln.lower()
    for ln in mem_lines
    if mem_lines.index(ln) > (mem_lines.index("## Tick 2") if has_tick2 else 0)
)

print()
print("=" * 60)
print("REPORT")
print("=" * 60)
print(f"PILOT: R3-Memory-T2 (b' workaround)")
print(f"WORKTREE_NAME: {WT_NAME}")
print(f"WORKTREE_ID: {WT_ID}")
print(f"CAPTAIN_SESSION_ID: {CAPTAIN_ID}")
print(f"ATOZ_SESSION_ID: {atoz_id}")
print(f"TARGET_WIKI: {target_rel}")
print(f"MEMORY_FILE: {mem_file}")
print(f"MEMORY_LINES: {len(mem_lines)}")
print(f"HAS_TICK_1_HEADING: {has_tick1}")
print(f"HAS_TICK_2_HEADING: {has_tick2}")
print(f"TICK_2_REFERENCES_TICK_1: {references_tick1}")
print(f"COST_USD: ${captain_cost:.4f}")
print(f"DURATION_SECONDS: {elapsed:.1f}")
print("=" * 60)

# Save report
report = ARTIFACTS / f"report-{EPOCH}.txt"
report.write_text(
    f"PILOT: R3-Memory-T2\nWT={WT_NAME}\nATOZ={atoz_id}\nCOST=${captain_cost:.4f}\n"
    f"HAS_T1={has_tick1} HAS_T2={has_tick2} REF_T1={references_tick1}\n"
    f"MEM_LINES={len(mem_lines)}\n"
)
print(f"\nreport written: {report}")
