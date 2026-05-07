#!/usr/bin/env python3
"""
PILOT D2 driver — vault-hygiene long-lived team (1-tick)

Spawns 3 siblings (@boothby + @geordi + @atoz) on a shared Agor worktree
(boardId equivalent), polls them to terminal, verifies memory files +
read-only-on-wiki invariant. See pilot brief for full rationale.

Underscore-prefixed: ad-hoc pilot driver, not a stable script. Intended
to be run once.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
SOURCE_BRANCH = "trunk"
EPOCH = int(time.time())
WT_NAME = f"pilot-D2-{EPOCH}"
MODEL = "sonnet"
TIMEOUT_PARENT = 1800
POLL_DEADLINE_S = 1500
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_pilot_d2_artifacts")
WORK_DIR.mkdir(exist_ok=True)

LOG: list[str] = []
def log(msg: str) -> None:
    line = f"[pilot-D2 {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def bearer() -> str:
    return json.load(open(TOKEN_FILE))["accessToken"]


def api(method: str, path: str, body=None, timeout: int = 30):
    url = BASE + path
    headers = {"Authorization": "Bearer " + bearer()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {body_txt}") from e


# Persona text from org_llm/bridge_crew.py
PERSONAS = {
    "boothby": {
        "soul": (
            "I am Boothby, the gardener. I tend the vault. I\n"
            "advise; I do not auto-write. Heavy work waits when\n"
            "the laptop is on battery saver. Slow growth beats\n"
            "fast rot.\n"
        ),
        "identity": (
            "# @boothby - Bridge Crew hygiene advisor\n\n"
            "Role: captain of the vault-hygiene long-lived team.\n"
            "Tools: vault_profile, org_orphans, embed status, power_profile.\n"
            "Coordination: scheduled heartbeat (nightly sweep) +\n"
            "  cross-tool escalation for heavy-context audits.\n"
            "Recommends; never auto-runs writes.\n"
        ),
    },
    "geordi": {
        "soul": (
            "I am Lt. Cmdr. Geordi La Forge. I see across the\n"
            "spectrum - SUMMARIZE, EXTRACT, ANALYZE. I make\n"
            "the dashboards talk. I sketch in Sandpack before\n"
            "I commit a Superset card.\n"
        ),
        "identity": (
            "# @geordi - Bridge Crew analyst (drift-detection role today)\n\n"
            "Modes: SUMMARIZE / EXTRACT / ANALYZE.\n"
            "For this 1-tick: scan docs/wiki/ for drift candidates.\n"
        ),
    },
    "atoz": {
        "soul": (
            "I am Mr. Atoz, the Sarpeidon librarian. Every page\n"
            "in its place; every link unbroken. I sweep for\n"
            "drift between wiki tables and source code, and\n"
            "for orphan terms that lost their home.\n"
        ),
        "identity": (
            "# @atoz - Bridge Crew wiki curator (link-fix role)\n\n"
            "Tools (current): grep, search_notes, get_node.\n"
            "Coordination: sibling-session by boardId for multi-\n"
            "  agent wiki sweeps; captain's-log reconciliation\n"
            "  for link-fix provenance.\n"
        ),
    },
}


TODAY = "2026-05-06"

GEORDI_PROMPT = f"""You are @geordi, drift-detector role.

PERSONA:
{PERSONAS['geordi']['soul']}
{PERSONAS['geordi']['identity']}

CONTEXT:
- You are running as a sibling session in a 3-member vault-hygiene team
  (captain @boothby, you @geordi, link-fixer @atoz). All three sessions
  live on the same Agor worktree (the boardId).
- This is a ONE-TICK READ-ONLY scan. DO NOT edit any files in docs/wiki/.

TASK (single tick):
1. Scan docs/wiki/ in your worktree for THREE drift candidates. Drift =
   broken org links, inconsistent terminology, outdated phase numbers,
   stale dates, conflicting facts between adjacent files, etc.
2. Write your output as a numbered list (1. 2. 3.) with for each:
   - file path
   - 1-line description of the drift
   - severity (low/med/high)
3. Save the list to .agor-assistants/geordi/memory/{TODAY}.md (create
   parent dirs as needed). The file should start with the heading
   "# @geordi - drift candidates {TODAY}".
4. Print exactly the line GEORDI_DONE on its own and stop.

CONSTRAINTS:
- DO NOT run any agor MCP tools.
- DO NOT edit anything under docs/wiki/.
- Use grep/find/Read to scan; you are a normal Claude Code with bash.
"""


ATOZ_PROMPT = f"""You are @atoz, link-fix specialist role.

PERSONA:
{PERSONAS['atoz']['soul']}
{PERSONAS['atoz']['identity']}

CONTEXT:
- You are a sibling session in the vault-hygiene team. Captain @boothby
  + drift-detector @geordi + you (link-fixer) all run on the same Agor
  worktree. Sibling-discovery is by shared boardId/worktree.
- READ-ONLY task - propose only; do not edit any wiki file.

TASK (single tick):
1. POLL for @geordi's drift list at .agor-assistants/geordi/memory/{TODAY}.md
   in your worktree. Wait up to 8 minutes (sleep 20s loop). If the file
   never appears, write a "no peer output observed" note instead.
2. Once @geordi's list is available: pick ONE candidate (your choice,
   prefer broken org links if any).
3. Propose a fix: show the EXACT current line and the EXACT replacement
   line, plus a 1-sentence rationale. DO NOT modify the wiki file.
4. Save your proposal to .agor-assistants/atoz/memory/{TODAY}.md
   starting with "# @atoz - link-fix proposal {TODAY}".
5. Print exactly the line ATOZ_DONE on its own and stop.

CONSTRAINTS:
- DO NOT edit anything under docs/wiki/.
- DO NOT run any agor MCP tools.
- Use grep/find/Read/bash for the poll loop.
"""


BOOTHBY_PROMPT = f"""You are @boothby, captain of the vault-hygiene long-lived team
for ONE TICK.

PERSONA:
{PERSONAS['boothby']['soul']}
{PERSONAS['boothby']['identity']}

CONTEXT:
- You are the captain. Your two siblings on this boardId are
  @geordi (drift detector) and @atoz (link-fixer). They are spawning
  alongside you on the same worktree.
- This is one heartbeat tick of a long-lived team - read-only on the
  wiki, coordination test only.

TASK (single tick):
1. POLL for both .agor-assistants/geordi/memory/{TODAY}.md and
   .agor-assistants/atoz/memory/{TODAY}.md in your worktree. Wait up
   to 12 minutes total (sleep 20s loop).
2. Once both are present: read them.
3. Decide whether to APPLY, REJECT, or DEFER @atoz's proposed fix.
   Briefly justify (2-3 lines).
4. Write a captain's-log-style entry to
   .agor-assistants/boothby/memory/{TODAY}.md starting with
   "# @boothby - vault-hygiene tick decision {TODAY}". Include:
   - timestamp
   - brief: drift count from @geordi, fix proposal from @atoz
   - verdict (apply/reject/defer)
   - sibling-discovery note: did you have to be told who your peers
     were, or could you have inferred it? (One sentence.)
5. DO NOT actually edit any docs/wiki/ file regardless of verdict.
6. Print exactly the line TICK_DONE on its own and stop.

CONSTRAINTS:
- DO NOT edit anything under docs/wiki/.
- DO NOT run any agor MCP tools.
"""


def step_create_worktree():
    log(f"step 1 - creating worktree {WT_NAME}")
    body = {
        "name": WT_NAME,
        "ref": WT_NAME,
        "createBranch": True,
        "sourceBranch": SOURCE_BRANCH,
        "pullLatest": False,
        "refType": "branch",
    }
    j = api("POST", f"/repos/{REPO_ID}/worktrees", body)
    log(f"  worktree_id={j['worktree_id']} path={j['path']}")
    return j


def step_create_pilot_session(wt_id):
    log("step 2 - creating captain pilot session (claude-code)")
    j = api("POST", "/sessions", {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    sid = j["session_id"]
    tok = j["mcp_token"]
    log(f"  pilot_session_id={sid} mcp_token_prefix={tok[:24]}...")
    log("  PATCH bypassPermissions on pilot session (BUG-8)")
    api("PATCH", f"/sessions/{sid}",
        {"permission_config": {"mode": "bypassPermissions"}})
    return sid, tok


def write_mcp_config(mcp_token, path):
    cfg = {
        "mcpServers": {
            "agor": {
                "type": "http",
                "url": BASE + "/mcp",
                "headers": {"Authorization": "Bearer " + mcp_token},
            }
        }
    }
    Path(path).write_text(json.dumps(cfg))


def run_claude(prompt, mcp_config, out_path, budget="2.00", timeout=900):
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.npm-global/bin')}:{env.get('PATH','')}"
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", mcp_config,
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", budget,
        prompt,
    ]
    with open(out_path, "w") as f:
        try:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                               timeout=timeout, env=env)
            return p.returncode
        except subprocess.TimeoutExpired:
            return 124


def step_discover_scheduler(mcp_config_path):
    log("step 3 - discover scheduler via agor_search_tools")
    prompt = (
        'You have one MCP server "agor". Call mcp__agor__agor_search_tools '
        'with query "schedule" and print the tool names returned, one per '
        'line. Then print SEARCH_DONE on its own line. Do not call other tools.'
    )
    out_path = str(WORK_DIR / f"discover-{EPOCH}.jsonl")
    rc = run_claude(prompt, mcp_config_path, out_path,
                    budget="0.50", timeout=180)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    sched_seen = bool(re.search(r"schedul", text, re.I))
    log(f"  discover rc={rc} scheduler_keyword_seen={sched_seen}")
    return sched_seen, out_path, text


def step_spawn_siblings_via_captain(mcp_config_path):
    log("step 4 - captain pilot spawns 3 siblings on same worktree (boardId)")

    g_args = json.dumps({"prompt": GEORDI_PROMPT, "title": "vault-hygiene-geordi"})
    a_args = json.dumps({"prompt": ATOZ_PROMPT,   "title": "vault-hygiene-atoz"})
    b_args = json.dumps({"prompt": BOOTHBY_PROMPT,"title": "vault-hygiene-boothby"})

    parent_prompt = f"""You have one MCP server "agor" with tools
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Your job: spawn THREE child sessions IN PARALLEL by issuing three
mcp__agor__agor_execute_tool calls in the SAME response (all three
tool_use blocks in one assistant turn, no waiting between them).

Each spawn call has toolName "agor_sessions_spawn" and the arguments
exactly as given below. Note `worktree_id` is OMITTED so each child
inherits the captain's worktree (sibling-by-boardId pattern).

Call 1 (geordi):
  toolName: "agor_sessions_spawn"
  arguments: {g_args}

Call 2 (atoz):
  toolName: "agor_sessions_spawn"
  arguments: {a_args}

Call 3 (boothby):
  toolName: "agor_sessions_spawn"
  arguments: {b_args}

After all three spawn responses come back, print exactly these three
lines and then STOP (do not poll, do not call other tools, do not narrate):

GEORDI_SESSION_ID=<session_id from call 1>
ATOZ_SESSION_ID=<session_id from call 2>
BOOTHBY_SESSION_ID=<session_id from call 3>
"""

    out_path = str(WORK_DIR / f"captain-{EPOCH}.jsonl")
    log(f"  invoking claude -p (budget=$3.00, timeout=900s)")
    rc = run_claude(parent_prompt, mcp_config_path, out_path,
                    budget="3.00", timeout=900)
    log(f"  captain rc={rc}")
    text = Path(out_path).read_text()

    def grab(label):
        m = re.search(rf"{label}=([A-Za-z0-9_-]+)", text)
        return m.group(1) if m else None

    g_sid = grab("GEORDI_SESSION_ID")
    a_sid = grab("ATOZ_SESSION_ID")
    b_sid = grab("BOOTHBY_SESSION_ID")
    log(f"  geordi_sid={g_sid} atoz_sid={a_sid} boothby_sid={b_sid}")

    parallel = 0
    cost = 0.0
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "assistant":
            content = obj.get("message", {}).get("content", []) or []
            n = sum(1 for c in content
                    if c.get("type") == "tool_use"
                    and c.get("name") == "mcp__agor__agor_execute_tool")
            parallel = max(parallel, n)
        if obj.get("type") == "result":
            cost += float(obj.get("total_cost_usd") or 0)
    log(f"  max parallel agor_execute_tool: {parallel}")
    log(f"  captain cost: ${cost:.4f}")

    return {
        "rc": rc, "out_path": out_path,
        "geordi_sid": g_sid, "atoz_sid": a_sid, "boothby_sid": b_sid,
        "parallel": parallel, "cost": cost, "raw_text": text,
    }


def step_lift_permission_each(sids):
    log("step 5 - PATCH bypassPermissions on each sibling (defensive)")
    for sid in sids:
        if sid:
            try:
                api("PATCH", f"/sessions/{sid}",
                    {"permission_config": {"mode": "bypassPermissions"}})
                log(f"  PATCHed {sid}")
            except Exception as e:
                log(f"  PATCH {sid} failed: {e}")


def step_poll_siblings(sids, deadline_s=POLL_DEADLINE_S):
    log(f"step 6 - polling 3 siblings to terminal status (<= {deadline_s}s)")
    deadline = time.time() + deadline_s
    terminal = {"completed", "stopped", "archived", "failed", "errored"}
    states = {sid: "?" for sid in sids if sid}
    while time.time() < deadline:
        all_done = True
        for sid in list(states.keys()):
            if states[sid] in terminal:
                continue
            try:
                got = api("GET", f"/sessions/{sid}")
                states[sid] = got.get("status", "?")
            except Exception as e:
                states[sid] = f"err({type(e).__name__})"
            if states[sid] not in terminal:
                all_done = False
        log("  states: " + " ".join(f"{s[:8]}={st}" for s, st in states.items()))
        if all_done:
            break
        time.sleep(15)
    return states


def step_verify_filesystem(wt_path):
    log("step 7 - verify memory files + wiki untouched")
    expected = {
        "geordi":  Path(wt_path) / ".agor-assistants" / "geordi"  / "memory" / f"{TODAY}.md",
        "atoz":    Path(wt_path) / ".agor-assistants" / "atoz"    / "memory" / f"{TODAY}.md",
        "boothby": Path(wt_path) / ".agor-assistants" / "boothby" / "memory" / f"{TODAY}.md",
    }
    landed = {}
    for h, p in expected.items():
        ex = p.exists()
        sz = p.stat().st_size if ex else 0
        landed[h] = (str(p), ex, sz)
        log(f"  {h:8s} {p} exists={ex} size={sz}")

    try:
        p = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--", "docs/wiki/"],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        wiki_diff = (p.stdout or "").strip()
        wiki_unchanged = (wiki_diff == "")
        log(f"  git diff docs/wiki/: {'<empty>' if wiki_unchanged else wiki_diff[:200]}")
    except Exception as e:
        wiki_diff = f"err: {e}"
        wiki_unchanged = False

    return landed, wiki_unchanged, wiki_diff


def main():
    t0 = time.time()
    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]

    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)

    sched_seen, _, _ = step_discover_scheduler(mcp_config)
    scheduler_path = ("discovered: 'schedul' keyword seen in MCP search "
                      "results - but not used (fired direct spawn instead "
                      "for predictable test shape)") if sched_seen else \
                     "fallback: direct spawn (scheduler keyword not seen)"

    spawn = step_spawn_siblings_via_captain(mcp_config)

    sids = [spawn["geordi_sid"], spawn["atoz_sid"], spawn["boothby_sid"]]
    step_lift_permission_each(sids)

    states = step_poll_siblings(sids)
    landed, wiki_unchanged, wiki_diff = step_verify_filesystem(wt_path)

    sibling_cost = 0.0
    for sid in sids:
        if not sid:
            continue
        try:
            got = api("GET", f"/sessions/{sid}")
            c = got.get("cost", {}) or {}
            sibling_cost += float(c.get("total_usd")
                                  or got.get("total_cost_usd") or 0)
        except Exception:
            pass
    total_cost = spawn["cost"] + sibling_cost

    elapsed = int(time.time() - t0)
    sibling_states = {h: states.get(s, "?") for h, s in
                      [("geordi", spawn["geordi_sid"]),
                       ("atoz",   spawn["atoz_sid"]),
                       ("boothby",spawn["boothby_sid"])]}

    files_ok = all(v[1] for v in landed.values())
    sessions_ok = all(states.get(s) in {"completed","stopped","archived"}
                      for s in sids if s)
    if files_ok and wiki_unchanged and sessions_ok:
        verdict = "PASS"
    elif files_ok or sessions_ok:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    siblings_disc = "unknown"
    bp = landed.get("boothby")
    if bp and bp[1]:
        bt = Path(bp[0]).read_text()
        if re.search(r"sibling-discovery|inferred|peer", bt, re.I):
            siblings_disc = "see boothby's memory note (excerpt below)"

    atoz_read_geordi = "unknown"
    ap = landed.get("atoz")
    if ap and ap[1]:
        at = Path(ap[0]).read_text()
        if re.search(r"geordi", at, re.I):
            atoz_read_geordi = "yes - atoz's memory references geordi"

    report = f"""
PILOT: D2
TEAM_SHAPE: 3-member long-lived (vault-hygiene: @boothby + @geordi + @atoz, 1-tick)
SCHEDULER_PATH: {scheduler_path}
AGOR_PRIMITIVES_USED: sibling-by-boardId (shared worktree), memory/{{date}}.md, MCP indirection (search->execute), permission_config bypass, parallel agor_sessions_spawn
TASK_OUTCOME: {verdict} - files_ok={files_ok} sessions_ok={sessions_ok} wiki_unchanged={wiki_unchanged}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_PILOT_SESSION_ID: {pilot_sid}
BOOTHBY_SESSION_ID: {spawn['boothby_sid']}
GEORDI_SESSION_ID: {spawn['geordi_sid']}
ATOZ_SESSION_ID: {spawn['atoz_sid']}
SIBLING_STATES: {sibling_states}
PARALLEL_TOOL_USE: {spawn['parallel']}
SIBLINGS_DISCOVERED_EACH_OTHER: {siblings_disc}
ATOZ_READ_GEORDI_OUTPUT: {atoz_read_geordi}
MEMORY_FILES_LANDED:
  geordi:  exists={landed['geordi'][1]} size={landed['geordi'][2]} path={landed['geordi'][0]}
  atoz:    exists={landed['atoz'][1]}   size={landed['atoz'][2]}   path={landed['atoz'][0]}
  boothby: exists={landed['boothby'][1]} size={landed['boothby'][2]} path={landed['boothby'][0]}
WIKI_UNCHANGED: {'yes' if wiki_unchanged else 'no - ' + wiki_diff[:200]}
COST_USD: {total_cost:.4f} (captain ${spawn['cost']:.4f} + siblings ${sibling_cost:.4f})
DURATION_SECONDS: {elapsed}
"""
    print(report)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    return 0


if __name__ == "__main__":
    sys.exit(main())
