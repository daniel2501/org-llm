#!/usr/bin/env python3
"""PROD-A-Wiki — FIRST PRODUCTION USE of FOSS-only Bridge Crew team.

Task: @atoz proposes 3-5 outgoing [[id:UUID]] cross-links for
`docs/wiki/agent-time-awareness.org` (current orphan: 213 lines, 0
outgoing links). @spock reviews via mode:"btw" with redirecting
questions ("is this the canonical target? would readers benefit?").
@atoz applies the agreed edits + commits on the worktree branch.

Per the "Agor crew uses no Claude" rule:
  - Both @atoz and @spock run on FOSS:
        agentic_tool="opencode"
        modelConfig={provider:"openrouter",
                     model:"qwen/qwen3-coder-30b-a3b-instruct"}
  - Top-level Claude is the human pilot (this script).
  - No claude-code agentic_tool anywhere on the crew side.

Pattern: top-level driver (no sub-agent) per R16 lesson; one-spawn-per-
claude-p per F9 amendment; mcp_token refresh between spawns per R2-D2.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
TARGET_REL = "docs/wiki/agent-time-awareness.org"
EPOCH = int(time.time())
WT_NAME = f"pilot-prod-A-{EPOCH}"
ARTIFACTS = Path("/home/daniel/repos/org-llm/scripts/_prod_a_wiki_team_artifacts")
ARTIFACTS.mkdir(exist_ok=True)
LOG = ARTIFACTS / f"log-{EPOCH}.txt"

FOSS_MODEL_CONFIG = {
    "provider": "openrouter",
    "model": "qwen/qwen3-coder-30b-a3b-instruct",
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    LOG.open("a").write(line + "\n")


def tok() -> str:
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def ensure_opencode_serve() -> subprocess.Popen | None:
    """Start opencode serve on :4096 if not already running."""
    try:
        urllib.request.urlopen("http://localhost:4096/", timeout=2)
        log("opencode serve already up on :4096")
        return None
    except Exception:
        pass
    log("starting opencode serve on :4096")
    env = os.environ.copy()
    env["PATH"] = (
        f"{os.path.expanduser('~/.npm-global/bin')}:"
        f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", "")
    )
    # Inject OpenRouter key from pass
    try:
        or_key = subprocess.run(
            ["pass", "org-llm/cloud/openrouter/api-key"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        env["OPENROUTER_API_KEY"] = or_key
        log(f"  OPENROUTER_API_KEY injected (len={len(or_key)})")
    except Exception as e:
        log(f"  WARN: could not retrieve OpenRouter key: {e}")
    serve_log = ARTIFACTS / f"opencode-serve-{EPOCH}.log"
    proc = subprocess.Popen(
        ["opencode", "serve", "--port", "4096"],
        stdout=serve_log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    time.sleep(3)
    return proc


def run_captain_spawn(
    captain_id: str,
    spawn_label: str,
    title: str,
    persona_text: str,
    task_text: str,
    *,
    budget_usd: float = 1.50,
) -> tuple[str | None, float]:
    """One short claude -p captain subprocess that issues ONE FOSS spawn."""
    # Refresh mcp_token via after-get
    fresh = req("GET", f"/sessions/{captain_id}")
    mcp_token = fresh.get("mcp_token")
    mcp_cfg = ARTIFACTS / f"mcp-{spawn_label}-{EPOCH}.json"
    mcp_cfg.write_text(json.dumps({
        "mcpServers": {
            "agor": {
                "type": "http",
                "url": f"{BASE}/mcp",
                "headers": {"Authorization": f"Bearer {mcp_token}"},
            }
        }
    }))

    spawn_args = {
        "prompt": persona_text + "\n\n" + task_text,
        "title": title,
        "agenticTool": "opencode",
        "modelConfig": FOSS_MODEL_CONFIG,
    }
    spawn_args_json = json.dumps(spawn_args)

    captain_prompt = f"""You have one MCP server "agor" exposing two tools:
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Spawn ONE child by issuing a single mcp__agor__agor_execute_tool call
with snake_case tool_name:

  tool_name: "agor_sessions_spawn"
  arguments: {spawn_args_json}

After the spawn response comes back, print exactly:
SPAWN_SESSION_ID=<session_id from spawn response>
SPAWN_DONE

Do not poll, do not call other tools, do not narrate.
"""

    parent_out = ARTIFACTS / f"captain-{spawn_label}-{EPOCH}.jsonl"
    env = os.environ.copy()
    env["PATH"] = (
        f"{os.path.expanduser('~/.npm-global/bin')}:"
        f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", "")
    )

    cmd = [
        "claude", "-p",
        "--model", "sonnet",
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(mcp_cfg),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", str(budget_usd),
        captain_prompt,
    ]
    log(f"  running captain claude -p for {spawn_label}...")
    t0 = time.time()
    with parent_out.open("w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, timeout=600)
    elapsed = time.time() - t0

    sid = None
    cost = 0.0
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "result":
            cost += float(ev.get("total_cost_usd") or 0)
            for ln in (ev.get("result", "") or "").splitlines():
                if ln.startswith("SPAWN_SESSION_ID="):
                    sid = ln.split("=", 1)[1].strip()
    log(f"  {spawn_label} sid={sid} cost=${cost:.4f} elapsed={elapsed:.1f}s")
    return sid, cost


def poll_terminal(sid: str, deadline_s: int = 1200) -> str:
    """Poll until idle or other terminal status."""
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            s = req("GET", f"/sessions/{sid}")
            st = s.get("status")
            if st in terminal:
                return st
        except Exception as e:
            log(f"  poll error (will retry): {e}")
        time.sleep(15)
    return "timeout"


# ── Main ───────────────────────────────────────────────────────────────────
log(f"PROD-A-Wiki start :: target={TARGET_REL} model={FOSS_MODEL_CONFIG['model']}")

opencode_proc = ensure_opencode_serve()

try:
    # 1. Worktree create
    log(f"creating worktree {WT_NAME}")
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": WT_NAME,
        "ref": WT_NAME,
        "createBranch": True,
        "sourceBranch": "trunk",
        "pullLatest": False,
        "refType": "branch",
    })
    WT_ID = wt["worktree_id"]
    WT_PATH = Path.home() / ".agor" / "worktrees" / "local" / "org-llm" / WT_NAME
    log(f"  wt_id={WT_ID}")
    time.sleep(3)  # filesystem-population race per round-3 R2-A NB1

    # 2. Captain pilot session
    captain = req("POST", "/sessions", {"worktree_id": WT_ID, "agentic_tool": "claude-code"})
    CAPTAIN_ID = captain["session_id"]
    log(f"captain={CAPTAIN_ID}")
    req("PATCH", f"/sessions/{CAPTAIN_ID}", {"permission_config": {"mode": "bypassPermissions"}})

    # 3. Spawn @atoz on FOSS
    atoz_persona = (
        "You are @atoz — Bridge Crew wiki concept-graph specialist. "
        "You are meticulous, archive-grade, and write in concise prose. "
        "Your specialty is keeping the wiki's [[id:UUID]] cross-link mesh well-knit."
    )
    atoz_task = f"""Audit the wiki page `{TARGET_REL}` (213 lines) for outgoing-cross-link opportunities. The page currently has ZERO `[[id:UUID]]` outgoing links — it is a concept-graph orphan despite being referenced 3 times from `multi-agent-org-llm.org`.

Your job:
1. Read the page in full.
2. Build the known-IDs set: `grep -h "^:ID:" docs/wiki/*.org | awk '{{print $2}}' | sort -u`.
3. Identify 3-5 specific places in the page where a cross-link would help readers (e.g. mentions of "DEC-006", "standing-orders", "captain's log", "scheduler", etc. that have canonical owning wiki pages with :ID: entries).
4. For each: propose the EXACT line + the EXACT replacement prose with `[[id:UUID][label]]`. Use precise line numbers.
5. Write your proposal to `.agor-assistants/atoz/memory/2026-05-07.md` under heading `## Atoz proposal`.
6. WAIT for @spock to send a btw question (your sibling reviewer; their session_id will be visible in the worktree's session list). Respond to @spock's questions; revise proposal if their points are valid.
7. Once @spock approves, APPLY the agreed edits to `{TARGET_REL}` directly. Do NOT touch any other file.
8. Run `git -C ~/.agor/worktrees/local/org-llm/{WT_NAME} add docs/wiki/agent-time-awareness.org && git -C ~/.agor/worktrees/local/org-llm/{WT_NAME} commit -m "docs(wiki): add 3-5 cross-links to agent-time-awareness.org (FOSS team)"`.
9. Print exactly `ATOZ_DONE` on its own line and stop.

Rules: do NOT edit files outside the target. No big refactors — only insert `[[id:UUID][label]]` cross-link wrappers around existing prose mentions. The page's prose stays the same; we add link mesh."""

    atoz_sid, atoz_captain_cost = run_captain_spawn(
        CAPTAIN_ID, "atoz", "prod-a-atoz",
        atoz_persona, atoz_task,
    )
    if not atoz_sid:
        log("FAIL: no @atoz session id")
        sys.exit(2)
    req("PATCH", f"/sessions/{atoz_sid}", {"permission_config": {"mode": "bypassPermissions"}})

    # 4. Spawn @spock on FOSS as sibling on same worktree
    spock_persona = (
        "You are @spock — Bridge Crew logic + canonical-source reviewer. "
        "You ask sharp redirecting questions; you do not rubber-stamp. "
        "Your specialty is catching missed-context errors others wouldn't."
    )
    spock_task = f"""You are the reviewer for a wiki cross-link audit. Your sibling @atoz is auditing `{TARGET_REL}` and proposing 3-5 outgoing cross-link insertions. @atoz's session_id is `{atoz_sid}` (visible in this worktree's session list).

Your job:
1. Wait until @atoz writes their proposal to `.agor-assistants/atoz/memory/2026-05-07.md`. Poll the file every ~20s; give up after ~5 min.
2. Read @atoz's proposal carefully.
3. For each proposed link, ask ONE redirecting question via `mode:"btw"` to @atoz (use mcp__agor__agor_execute_tool → agor_sessions_prompt with mode="btw" and target=@atoz's session_id). Examples: "is THIS the canonical owner of that concept, or is there a more-specific page?", "does the link wrapper change the prose meaning?", "would readers benefit more from a link to X or Y?". Send no more than 3 btw questions total — pick the 3 most-redirecting.
4. Wait for @atoz's responses (poll @atoz's `genealogy.children` for the btw ephemeral child sessions; per round-2 R2 the btw lands on TARGET's genealogy).
5. Write your verdict to `.agor-assistants/spock/memory/2026-05-07.md` under heading `## Spock review` — list each proposal + your verdict (APPROVE / REVISE / DROP) with one-line reasoning.
6. Print exactly `SPOCK_APPROVED` or `SPOCK_REVISE` on its own line and stop.

Rules: do not edit `{TARGET_REL}` yourself — only @atoz applies edits. Your role is review."""

    spock_sid, spock_captain_cost = run_captain_spawn(
        CAPTAIN_ID, "spock", "prod-a-spock",
        spock_persona, spock_task,
    )
    if not spock_sid:
        log("FAIL: no @spock session id")
        sys.exit(3)
    req("PATCH", f"/sessions/{spock_sid}", {"permission_config": {"mode": "bypassPermissions"}})

    # 5. Poll both to terminal
    log(f"polling @atoz {atoz_sid[:18]} + @spock {spock_sid[:18]}...")
    atoz_status = poll_terminal(atoz_sid)
    log(f"  @atoz terminal: {atoz_status}")
    spock_status = poll_terminal(spock_sid)
    log(f"  @spock terminal: {spock_status}")

    # 6. Verify: did the diff land?
    log("verifying worktree state...")
    diff_stat = subprocess.run(
        ["git", "-C", str(WT_PATH), "diff", "--stat", "trunk", f"--", TARGET_REL],
        capture_output=True, text=True,
    ).stdout.strip()
    log(f"  diff vs trunk on target: {diff_stat or '(no changes)'}")
    log_oneline = subprocess.run(
        ["git", "-C", str(WT_PATH), "log", "--oneline", "-3"],
        capture_output=True, text=True,
    ).stdout.strip()

    # 7. btw children probe (per R2 canon: on TARGET = @atoz's genealogy)
    atoz_geneology = req("GET", f"/sessions/{atoz_sid}").get("genealogy", {})
    btw_child_ids = atoz_geneology.get("children", [])
    btw_count = 0
    for child_id in btw_child_ids:
        try:
            child = req("GET", f"/sessions/{child_id}")
            if child.get("fork_origin") == "btw":
                btw_count += 1
        except Exception:
            pass

    # 8. Memory files
    atoz_mem = WT_PATH / ".agor-assistants" / "atoz" / "memory" / "2026-05-07.md"
    spock_mem = WT_PATH / ".agor-assistants" / "spock" / "memory" / "2026-05-07.md"

    total_cost = atoz_captain_cost + spock_captain_cost
    print()
    print("=" * 70)
    print("REPORT — PROD-A-Wiki (FIRST PRODUCTION USE: FOSS-only Bridge Crew)")
    print("=" * 70)
    print(f"TARGET: {TARGET_REL}")
    print(f"FOSS_MODEL: {FOSS_MODEL_CONFIG}")
    print(f"WORKTREE: {WT_NAME}")
    print(f"WT_ID: {WT_ID}")
    print(f"CAPTAIN: {CAPTAIN_ID}")
    print(f"ATOZ_SID: {atoz_sid} status={atoz_status}")
    print(f"SPOCK_SID: {spock_sid} status={spock_status}")
    print(f"BTW_CHILDREN_OBSERVED (on @atoz's genealogy): {btw_count}")
    print(f"DIFF_VS_TRUNK_ON_TARGET: {diff_stat or '(none)'}")
    print(f"WORKTREE_LOG (last 3):\n  {log_oneline}")
    print(f"ATOZ_MEMORY_EXISTS: {atoz_mem.exists()}")
    print(f"SPOCK_MEMORY_EXISTS: {spock_mem.exists()}")
    print(f"COMBINED_CAPTAIN_COST_USD: ${total_cost:.4f}")
    print("=" * 70)

    report = ARTIFACTS / f"report-{EPOCH}.txt"
    report.write_text(
        f"PROD-A-Wiki target={TARGET_REL}\n"
        f"WT={WT_NAME} CAPTAIN={CAPTAIN_ID}\n"
        f"ATOZ={atoz_sid} status={atoz_status}\n"
        f"SPOCK={spock_sid} status={spock_status}\n"
        f"BTW={btw_count}\n"
        f"COST=${total_cost:.4f}\n"
        f"DIFF: {diff_stat}\n"
        f"LOG:\n{log_oneline}\n"
    )
    log(f"report: {report}")

finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
