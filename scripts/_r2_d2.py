#!/usr/bin/env python3
"""
PILOT R2-D2 driver — vault-hygiene long-lived team (1-tick), round-2 retry.

Round-1 D2 (scripts/_pilot_d2.py) reached MCP discovery, then the captain
stalled after a `rate_limit_event` while attempting to spawn three
siblings in a single parent assistant turn. See finding F9 in
docs/wiki/2026-05-06-agor-pilot-round-1-findings.org.

This round-2 harness fixes that by:

  * Spawning the three siblings SEQUENTIALLY — three separate
    ``claude -p`` invocations of the captain pilot, one per sibling,
    with a small sleep between each. This bounds each subprocess'
    chance of a rate-limit stall and lets the harness retry one
    spawn cleanly without losing any successful prior spawns.

  * Catching ``rate_limit_event`` in the parent stream-json (and the
    "announced but no tool_use" failure mode); on detection, sleep 30s
    and retry up to 2 times.

  * Refreshing the captain pilot session ``mcp_token`` between
    spawns (GET /sessions/{id} regenerates it; see
    scripts/agor-token-refresh.sh) so each ``claude -p`` invocation
    gets a fresh MCP JWT.

  * PATCH bypassPermissions on each spawned sibling defensively
    (BUG-8), in case it's not inherited from the captain pilot.

The team itself + on-disk verification logic are unchanged from D2.
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
WT_NAME = f"pilot-R2D2-{EPOCH}"
MODEL = "sonnet"
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r2_d2_artifacts")
WORK_DIR.mkdir(exist_ok=True)
TODAY = "2026-05-06"
HARD_BUDGET_USD = 4.50
SPAWN_BUDGET_USD = "1.20"   # per spawn invocation
SPAWN_TIMEOUT_S = 360       # 6 min per spawn invocation
SPAWN_MAX_RETRIES = 2
SIBLING_POLL_DEADLINE_S = 1500
INTER_SPAWN_SLEEP_S = 8

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r2-d2 {time.strftime('%H:%M:%S')}] {msg}"
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


# Persona text — abbreviated soul + identity from org_llm/bridge_crew.py + agents/_builtins.
PERSONAS = {
    "boothby": {
        "soul": (
            "I am Boothby, the gardener. I tend the vault. I advise; I do not "
            "auto-write. Heavy work waits when the laptop is on battery saver. "
            "Slow growth beats fast rot."
        ),
        "identity": (
            "Role: captain of the vault-hygiene long-lived team. "
            "Tools: vault_profile, org_orphans, embed status, power_profile. "
            "Coordination: scheduled heartbeat (nightly sweep) + cross-tool "
            "escalation for heavy-context audits. Recommends; never auto-runs writes."
        ),
    },
    "geordi": {
        "soul": (
            "I am Lt. Cmdr. Geordi La Forge. I see across the spectrum — "
            "SUMMARIZE, EXTRACT, ANALYZE. I make the dashboards talk. "
            "I sketch in Sandpack before I commit a Superset card."
        ),
        "identity": (
            "Modes: SUMMARIZE / EXTRACT / ANALYZE. For this 1-tick: scan "
            "docs/wiki/ for drift candidates."
        ),
    },
    "atoz": {
        "soul": (
            "I am Mr. Atoz, the Sarpeidon librarian. Every page in its place; "
            "every link unbroken. I sweep for drift between wiki tables and "
            "source code, and for orphan terms that lost their home."
        ),
        "identity": (
            "Tools (current): grep, search_notes, get_node. Coordination: "
            "sibling-session by boardId for multi-agent wiki sweeps; captain's-log "
            "reconciliation for link-fix provenance."
        ),
    },
}


def sibling_prompt(handle: str) -> str:
    """Prompt that the spawned child Claude session will see."""
    p = PERSONAS[handle]
    if handle == "geordi":
        return f"""You are @{handle}, drift-detector role.

PERSONA:
{p['soul']}
{p['identity']}

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
   "# @geordi — drift candidates {TODAY}".
4. Print exactly the line GEORDI_DONE on its own and stop.

CONSTRAINTS:
- DO NOT run any agor MCP tools.
- DO NOT edit anything under docs/wiki/.
- Use grep/find/Read to scan; you are a normal Claude Code with bash.
- Probe sibling-discovery once: list other live sessions on your worktree
  (e.g. by checking .agor-assistants/ siblings or the agor REST if exposed)
  and note in the file what you saw. Do not block on it.
"""
    if handle == "atoz":
        return f"""You are @{handle}, link-fix specialist role.

PERSONA:
{p['soul']}
{p['identity']}

CONTEXT:
- You are a sibling session in the vault-hygiene team. Captain @boothby
  + drift-detector @geordi + you (link-fixer) all run on the same Agor
  worktree. Sibling-discovery is by shared boardId/worktree.
- READ-ONLY task — propose only; do not edit any wiki file.

TASK (single tick):
1. POLL for @geordi's drift list at .agor-assistants/geordi/memory/{TODAY}.md
   in your worktree. Wait up to 8 minutes (sleep 20s loop). If the file
   never appears, write a "no peer output observed" note instead and
   still proceed to step 4 with whatever you can do alone.
2. Once @geordi's list is available: pick ONE candidate (your choice,
   prefer broken org links if any).
3. Propose a fix: show the EXACT current line and the EXACT replacement
   line, plus a 1-sentence rationale. DO NOT modify the wiki file.
4. Save your proposal to .agor-assistants/atoz/memory/{TODAY}.md
   starting with "# @atoz — link-fix proposal {TODAY}".
5. Print exactly the line ATOZ_DONE on its own and stop.

CONSTRAINTS:
- DO NOT edit anything under docs/wiki/.
- DO NOT run any agor MCP tools.
- Use grep/find/Read/bash for the poll loop.
- Probe sibling-discovery: note in your file whether you could see your
  worktree-mates (geordi / boothby) without being told.
"""
    # boothby
    return f"""You are @{handle}, captain of the vault-hygiene long-lived team
for ONE TICK.

PERSONA:
{p['soul']}
{p['identity']}

CONTEXT:
- You are the captain. Your two siblings on this boardId are
  @geordi (drift detector) and @atoz (link-fixer). They are spawning
  alongside you on the same worktree.
- This is one heartbeat tick of a long-lived team — read-only on the
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
   "# @boothby — vault-hygiene tick decision {TODAY}". Include:
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


# ── steps ────────────────────────────────────────────────────────────────


def step_create_worktree():
    log(f"step 1 — creating worktree {WT_NAME}")
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
    log("step 2 — creating captain pilot session (claude-code)")
    j = api("POST", "/sessions",
            {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    sid = j["session_id"]
    tok = j["mcp_token"]
    log(f"  pilot_session_id={sid} mcp_token_prefix={tok[:24]}...")
    log("  PATCH bypassPermissions on pilot session (BUG-8)")
    api("PATCH", f"/sessions/{sid}",
        {"permission_config": {"mode": "bypassPermissions"}})
    return sid, tok


def refresh_mcp_token(sid: str) -> str | None:
    """GET /sessions/{id} regenerates mcp_token (after-get hook)."""
    try:
        got = api("GET", f"/sessions/{sid}")
        return got.get("mcp_token")
    except Exception as e:
        log(f"  refresh_mcp_token({sid}) failed: {e}")
        return None


def write_mcp_config(mcp_token: str, path: str) -> None:
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


def run_claude(prompt: str, mcp_config: str, out_path: str,
               budget: str = SPAWN_BUDGET_USD,
               timeout: int = SPAWN_TIMEOUT_S) -> int:
    env = os.environ.copy()
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


def parse_stream(text: str):
    """Pull rate_limit_events, cost, and a tool_use index out of stream-json.

    Returns dict {rate_limits, cost, has_result, tool_uses (list of (name,input))}.
    """
    rate_limits = 0
    cost = 0.0
    has_result = False
    tool_uses: list[tuple[str, dict]] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "rate_limit_event":
            rate_limits += 1
        if t == "result":
            has_result = True
            cost += float(obj.get("total_cost_usd") or 0)
        if t == "assistant":
            for c in obj.get("message", {}).get("content", []) or []:
                if c.get("type") == "tool_use":
                    tool_uses.append((c.get("name", ""), c.get("input") or {}))
    return {
        "rate_limits": rate_limits,
        "cost": cost,
        "has_result": has_result,
        "tool_uses": tool_uses,
    }


def spawn_one_sibling(handle: str, pilot_sid: str, mcp_config: str,
                      attempt_label: str) -> dict:
    """Drive ONE captain claude -p call that issues exactly one
    agor_sessions_spawn for ``handle``. Retries on rate_limit_event."""
    sib_prompt = sibling_prompt(handle)
    spawn_args = json.dumps({
        "prompt": sib_prompt,
        "title": f"vault-hygiene-{handle}",
    })
    captain_prompt = f"""You have one MCP server "agor" with tools
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Spawn ONE child session by issuing exactly ONE
mcp__agor__agor_execute_tool call with these arguments:

  tool_name: "agor_sessions_spawn"
  arguments: {spawn_args}

Note: the field name inside agor_execute_tool is `tool_name` (snake_case),
NOT `toolName`. Do NOT pass a `worktree_id` — the child must inherit your
worktree (sibling-by-boardId pattern).

Once the spawn response comes back, print exactly this single line and
then STOP (do not poll, do not call any other tool, do not narrate):

{handle.upper()}_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR / f"spawn-{handle}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(rf"{handle.upper()}_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[{handle}] rc={rc} rate_limits={info['rate_limits']} "
        f"tool_uses={len(info['tool_uses'])} cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(handle: str, pilot_sid: str,
                     mcp_config_path: str) -> dict:
    """Sequential spawn driver with rate-limit retry."""
    rl_total = 0
    cost_total = 0.0
    last = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        # Refresh the pilot's mcp_token before each attempt so the
        # subprocess'es MCP JWT is fresh (15-min TTL — F8).
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
        log(f"  spawn[{handle}] attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_sibling(handle, pilot_sid, mcp_config_path,
                                 f"a{attempt}")
        rl_total += last["rate_limits"]
        cost_total += last["cost"]
        if last["sid"]:
            log(f"  spawn[{handle}] OK on attempt {attempt}")
            last["rl_total"] = rl_total
            last["cost_total"] = cost_total
            return last
        # Failure: rate-limited or stalled.
        if attempt > SPAWN_MAX_RETRIES:
            break
        if last["rate_limits"] > 0:
            log(f"  spawn[{handle}] rate_limit_event seen — sleeping 30s before retry")
        else:
            log(f"  spawn[{handle}] no sid + no rate_limit — sleeping 30s anyway "
                f"(stall mode, may have hit invisible budget/quota)")
        time.sleep(30)
    last = last or {"sid": None, "out_path": "", "rate_limits": rl_total,
                    "cost": cost_total, "tool_uses": []}
    last["rl_total"] = rl_total
    last["cost_total"] = cost_total
    return last


def step_lift_permission(sid: str | None) -> None:
    if not sid:
        return
    try:
        api("PATCH", f"/sessions/{sid}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  PATCHed bypassPermissions on {sid}")
    except Exception as e:
        log(f"  PATCH {sid} failed: {e}")


def step_poll_siblings(sids, deadline_s=SIBLING_POLL_DEADLINE_S):
    log(f"step 6 — polling siblings to terminal status (≤ {deadline_s}s)")
    deadline = time.time() + deadline_s
    # F1: idle is terminal for spawned children.
    terminal = {"completed", "stopped", "archived", "failed", "errored", "idle"}
    states = {sid: "?" for sid in sids if sid}
    if not states:
        return states
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
    log("step 7 — verify memory files + wiki untouched")
    expected = {
        "geordi":  Path(wt_path) / ".agor-assistants" / "geordi"  / "memory" / f"{TODAY}.md",
        "atoz":    Path(wt_path) / ".agor-assistants" / "atoz"    / "memory" / f"{TODAY}.md",
        "boothby": Path(wt_path) / ".agor-assistants" / "boothby" / "memory" / f"{TODAY}.md",
    }
    landed = {}
    for h, p in expected.items():
        ex = p.exists()
        sz = p.stat().st_size if ex else 0
        first10 = ""
        if ex:
            try:
                with open(p) as f:
                    first10 = "".join([next(f, "") for _ in range(10)])
            except Exception:
                first10 = "<read-error>"
        landed[h] = {"path": str(p), "exists": ex, "size": sz, "first10": first10}
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

    # Sequential spawn order: geordi first (independent),
    # then atoz (will poll geordi's file), then boothby (polls both).
    spawn_order = ["geordi", "atoz", "boothby"]
    spawn_results: dict[str, dict] = {}
    rl_total = 0
    cost_total = 0.0

    for handle in spawn_order:
        log(f"step 4.{handle} — sequential spawn")
        # Pre-flight cost budget check.
        if cost_total > HARD_BUDGET_USD:
            log(f"  ABORT: cost ${cost_total:.4f} > hard cap ${HARD_BUDGET_USD}")
            break
        result = spawn_with_retry(handle, pilot_sid, mcp_config)
        spawn_results[handle] = result
        rl_total += result.get("rl_total", 0)
        cost_total += result.get("cost_total", 0.0)
        if result["sid"]:
            step_lift_permission(result["sid"])
        else:
            log(f"  WARNING: spawn[{handle}] never returned a session_id "
                f"after {SPAWN_MAX_RETRIES + 1} attempts — continuing anyway")
        time.sleep(INTER_SPAWN_SLEEP_S)

    sids = [spawn_results.get(h, {}).get("sid") for h in spawn_order]
    states = step_poll_siblings([s for s in sids if s])
    landed, wiki_unchanged, wiki_diff = step_verify_filesystem(wt_path)

    # Aggregate sibling cost via REST (likely null per F7 but try).
    sibling_cost_rest = 0.0
    for sid in sids:
        if not sid:
            continue
        try:
            got = api("GET", f"/sessions/{sid}")
            c = got.get("cost", {}) or {}
            sibling_cost_rest += float(c.get("total_usd")
                                       or got.get("total_cost_usd") or 0)
        except Exception:
            pass

    elapsed = int(time.time() - t0)
    sibling_states = {h: states.get(spawn_results.get(h, {}).get("sid"), "?")
                      for h in spawn_order}

    files_ok = all(landed[h]["exists"] for h in spawn_order)
    sessions_ok = all(states.get(s) in {"completed", "stopped", "archived",
                                        "idle"}
                      for s in sids if s) and all(
        spawn_results.get(h, {}).get("sid") for h in spawn_order)
    if files_ok and wiki_unchanged and sessions_ok:
        verdict = "PASS"
    elif files_ok or sessions_ok:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    siblings_disc = "unknown — check memory file contents"
    bp_first10 = landed.get("boothby", {}).get("first10", "")
    ap_first10 = landed.get("atoz",   {}).get("first10", "")
    if re.search(r"sibling-discovery|inferred|peer", bp_first10, re.I):
        siblings_disc = "see boothby's memory note (excerpt below)"

    atoz_read_geordi = "unknown"
    ap_full = ""
    if landed.get("atoz", {}).get("exists"):
        try:
            ap_full = Path(landed["atoz"]["path"]).read_text()
        except Exception:
            ap_full = ""
        if re.search(r"geordi", ap_full, re.I):
            atoz_read_geordi = "yes — atoz's memory references geordi"

    rl_summary = (f"{rl_total} rate_limit_event(s) observed across spawns; "
                  "harness slept 30s + retried each on hit "
                  f"(max {SPAWN_MAX_RETRIES} retries per spawn)")

    report = f"""
PILOT: R2-D2
TEAM_SHAPE: 3-member long-lived vault-hygiene (boothby + geordi + atoz, 1-tick) — sequential-spawn variant
HARNESS_SCRIPT: scripts/_r2_d2.py
AGOR_PRIMITIVES_USED: sibling-by-boardId (shared worktree), memory/{{date}}.md, MCP indirection (search→execute), permission_config bypass, sequential agor_sessions_spawn, mcp_token after-get refresh
TASK_OUTCOME: {verdict} — files_ok={files_ok} sessions_ok={sessions_ok} wiki_unchanged={wiki_unchanged}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_PILOT_SESSION_ID: {pilot_sid}
BOOTHBY_SESSION_ID: {spawn_results.get('boothby', {}).get('sid')}
GEORDI_SESSION_ID: {spawn_results.get('geordi', {}).get('sid')}
ATOZ_SESSION_ID: {spawn_results.get('atoz', {}).get('sid')}
SIBLING_STATES: {sibling_states}
RATE_LIMIT_EVENTS_OBSERVED: {rl_summary}
SIBLINGS_DISCOVERED_EACH_OTHER: {siblings_disc}
ATOZ_READ_GEORDI_OUTPUT: {atoz_read_geordi}
MEMORY_FILES_LANDED:
  | handle  | path                                          | exists | first 10 lines |
  | boothby | {landed['boothby']['path']} | {'y' if landed['boothby']['exists'] else 'n'} | {(landed['boothby']['first10'] or 'n/a').replace(chr(10),' ⏎ ')[:200]} |
  | geordi  | {landed['geordi']['path']}  | {'y' if landed['geordi']['exists']  else 'n'} | {(landed['geordi']['first10']  or 'n/a').replace(chr(10),' ⏎ ')[:200]} |
  | atoz    | {landed['atoz']['path']}    | {'y' if landed['atoz']['exists']    else 'n'} | {(landed['atoz']['first10']    or 'n/a').replace(chr(10),' ⏎ ')[:200]} |
WIKI_UNCHANGED: {'yes' if wiki_unchanged else 'no — ' + wiki_diff[:200]}
COST_USD: {cost_total:.4f} (captain stream-json sum; sibling REST cost reported {sibling_cost_rest:.4f} — likely null per F7)
DURATION_SECONDS: {elapsed}
"""
    print(report, flush=True)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    return 0


if __name__ == "__main__":
    sys.exit(main())
