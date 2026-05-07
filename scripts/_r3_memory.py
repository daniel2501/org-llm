#!/usr/bin/env python3
"""PILOT R3-Memory — cross-run persistent agent memory validation.

Two @atoz sessions on the SAME worktree, separated by ~30s, validate
that:

  (a) tick 1 writes ``.agor-assistants/atoz/memory/<date>.md`` and
      reaches ``idle``;
  (b) tick 2 (a fresh @atoz session, separate ``claude -p`` captain
      subprocess) is able to *read* that file before doing its own
      task and *append* its findings to the same file under a
      ``## Tick 2`` heading;
  (c) the tick-2 findings explicitly reference at least one tick-1
      finding (proving the file was actually consumed, not just
      coexisted on disk).

This closes the Layer-2a per-agent-journal slot from
``docs/wiki/multi-agent-org-llm.org`` § "Persistent agent memory
substrate" — the round-1 D pilot wrote a memory file but never
validated that a NEXT-run agent could read it.

Pattern is the proven F9-amendment shape: one spawn per ``claude -p``
captain subprocess, mcp_token refresh between ticks, PATCH
bypassPermissions on each spawn.
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
WT_NAME = f"pilot-R3Memory-{EPOCH}"
MODEL = "sonnet"
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r3_memory_artifacts")
WORK_DIR.mkdir(exist_ok=True)
TODAY = "2026-05-06"
HARD_BUDGET_USD = 5.00
SPAWN_BUDGET_USD = "1.50"
SPAWN_TIMEOUT_S = 360
SPAWN_MAX_RETRIES = 2
TICK_POLL_DEADLINE_S = 900
INTER_TICK_SLEEP_S = 30

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r3-memory {time.strftime('%H:%M:%S')}] {msg}"
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


# @atoz persona — soul + identity from org_llm/cli.py + _builtins.
ATOZ_SOUL = (
    "I am Mr. Atoz, the Sarpeidon librarian. Every page in its place; "
    "every link unbroken. I sweep for drift between wiki tables and "
    "source code, and for orphan terms that lost their home. Scope: "
    "docs/wiki/ ONLY."
)
ATOZ_IDENTITY = (
    "Tools: read_file, search_notes, shell (grep). Defaults: PROPOSE "
    "THEN APPLY for fixes; LINK FORM is [[id:<uuid>][title]] for "
    "cross-page and [[file:../../path][=path=]] for code files; "
    "BITE-SIZED — propose 1-3 edits at a time. For this pilot, "
    "audit-only — no edits to docs/wiki/."
)


def tick_prompt(tick: int) -> str:
    """Sibling prompt for the spawned @atoz child session."""
    if tick == 1:
        return f"""You are @atoz, wiki concept-graph specialist.

PERSONA:
{ATOZ_SOUL}
{ATOZ_IDENTITY}

CONTEXT:
- This is TICK 1 of a two-tick memory-persistence pilot.
- A SECOND @atoz session will run on this same worktree ~30s after
  you finish, and will need to read your findings.

TASK (tick 1 — single audit pass):
1. Read docs/wiki/captains-log-agor-bridge.org (it's ~70 lines).
2. Audit it for:
   - Broken or stale [[id:<uuid>][...]] links (verify each id refers
     to a page that actually exists somewhere in docs/wiki/ — use
     `grep -rn ":ID:.*<uuid>" docs/wiki/` to verify).
   - Internal claim consistency (does the page contradict itself?).
   - Reference quality (the cross-refs section at the bottom).
3. Write your findings to .agor-assistants/atoz/memory/{TODAY}.md
   in your worktree (create parent dirs as needed).
   The file MUST start with the heading:
     # @atoz — captains-log-agor-bridge audit {TODAY}
   Followed by a `## Tick 1` heading.
   Under tick 1, list each finding as a bullet with:
     - location (line number or section)
     - issue (one sentence)
     - severity (low/med/high)
     - if it's a link verification: include the target id you checked
       and whether it resolved
4. Print exactly the line TICK_1_DONE on its own and stop.

CONSTRAINTS:
- DO NOT edit docs/wiki/ — audit only.
- DO NOT run any agor MCP tools.
- Use grep / find / Read / Bash for the audit.
- If you find ZERO issues, still write the heading + a "no issues
  found" bullet + a one-line note explaining what you checked.
"""
    # tick 2
    return f"""You are @atoz, wiki concept-graph specialist.

PERSONA:
{ATOZ_SOUL}
{ATOZ_IDENTITY}

CONTEXT:
- This is TICK 2 of a two-tick memory-persistence pilot.
- A PREVIOUS @atoz session (tick 1, a different Claude session) ran
  on this same worktree ~30s ago and wrote findings to a memory file
  at .agor-assistants/atoz/memory/{TODAY}.md inside this worktree.
- The pilot is testing whether a fresh agent session can read that
  memory file and build on it cumulatively.

TASK (tick 2 — read prior memory + new audit):
1. FIRST, read .agor-assistants/atoz/memory/{TODAY}.md. This is your
   own prior tick's findings. Note what tick 1 looked at and what it
   found.
2. THEN audit a SECOND wiki page: docs/wiki/agor-smoke-recipe.org
   (it's ~343 lines — feel free to spot-check sections rather than
   line-by-line). Same audit shape as tick 1:
   - Broken or stale [[id:<uuid>][...]] links.
   - Internal claim consistency.
   - Reference quality.
3. APPEND your tick-2 findings to the SAME memory file
   (.agor-assistants/atoz/memory/{TODAY}.md). Do NOT overwrite the
   file. Use the Edit tool with append-style semantics, or shell
   redirect with `>>`. Add a new heading:
     ## Tick 2
   Under tick 2, list your new findings in the same bullet shape as
   tick 1.
4. CRITICAL: Your tick-2 section MUST include at least ONE explicit
   cross-reference to a tick-1 finding. Examples:
     - "tick 1 found page X had broken link Y; tick 2 confirms / re-checks / extends..."
     - "tick 1 reported N issues on captains-log-agor-bridge.org; tick 2's audit of agor-smoke-recipe.org found M issues..."
     - "the [[id:...]] link tick 1 flagged at line N is also referenced from this page at line M..."
   Make the cross-reference concrete — quote or paraphrase a tick-1
   finding by location/severity/text.
5. Print exactly the line TICK_2_DONE on its own and stop.

CONSTRAINTS:
- DO NOT edit docs/wiki/ — audit only.
- DO NOT run any agor MCP tools.
- DO NOT overwrite the memory file — APPEND only. Verify with
  `wc -l .agor-assistants/atoz/memory/{TODAY}.md` before and after
  if you want.
- If tick 1's file is missing or empty, note that loudly in your
  tick-2 section ("MEMORY-MISS: tick 1's file not found at <path>")
  and proceed with a tick-2-only audit anyway.
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
    """GET /sessions/{id} regenerates mcp_token (after-get hook, F8)."""
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


def spawn_one_tick(tick: int, pilot_sid: str, mcp_config: str,
                   attempt_label: str) -> dict:
    """Drive ONE captain claude -p call that issues exactly one
    agor_sessions_spawn for an @atoz tick. Retries on rate_limit."""
    sib_prompt = tick_prompt(tick)
    spawn_args = json.dumps({
        "prompt": sib_prompt,
        "title": f"r3memory-atoz-tick{tick}",
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

TICK_{tick}_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR / f"spawn-tick{tick}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(rf"TICK_{tick}_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[tick{tick}] rc={rc} rate_limits={info['rate_limits']} "
        f"tool_uses={len(info['tool_uses'])} cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(tick: int, pilot_sid: str,
                     mcp_config_path: str) -> dict:
    rl_total = 0
    cost_total = 0.0
    last = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
            log(f"  spawn[tick{tick}] refreshed mcp_token")
        log(f"  spawn[tick{tick}] attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_tick(tick, pilot_sid, mcp_config_path,
                              f"a{attempt}")
        rl_total += last["rate_limits"]
        cost_total += last["cost"]
        if last["sid"]:
            log(f"  spawn[tick{tick}] OK on attempt {attempt}")
            last["rl_total"] = rl_total
            last["cost_total"] = cost_total
            return last
        if attempt > SPAWN_MAX_RETRIES:
            break
        if last["rate_limits"] > 0:
            log(f"  spawn[tick{tick}] rate_limit_event seen — sleeping 30s")
        else:
            log(f"  spawn[tick{tick}] no sid — sleeping 30s anyway (stall mode)")
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


def step_poll_session(sid: str, deadline_s: int = TICK_POLL_DEADLINE_S) -> str:
    """Poll a single session to terminal state. F1: idle is terminal."""
    if not sid:
        return "no-sid"
    log(f"  polling {sid[:8]} to terminal (≤ {deadline_s}s)")
    deadline = time.time() + deadline_s
    terminal = {"completed", "stopped", "archived", "failed", "errored", "idle"}
    state = "?"
    while time.time() < deadline:
        try:
            got = api("GET", f"/sessions/{sid}")
            state = got.get("status", "?")
        except Exception as e:
            state = f"err({type(e).__name__})"
        log(f"    state={state}")
        if state in terminal:
            return state
        time.sleep(15)
    return state


def read_memory_file(wt_path: str) -> dict:
    """Read the @atoz memory file and return shape info."""
    p = Path(wt_path) / ".agor-assistants" / "atoz" / "memory" / f"{TODAY}.md"
    info = {"path": str(p), "exists": p.exists(), "size": 0,
            "first10": "", "first15": "", "lines": 0,
            "has_tick1": False, "has_tick2": False, "full": ""}
    if not p.exists():
        return info
    try:
        full = p.read_text()
    except Exception as e:
        info["full"] = f"<read-error: {e}>"
        return info
    info["full"] = full
    info["size"] = p.stat().st_size
    info["lines"] = full.count("\n")
    lines = full.splitlines()
    info["first10"] = "\n".join(lines[:10])
    info["first15"] = "\n".join(lines[:15])
    info["has_tick1"] = bool(re.search(r"^##\s*Tick\s*1\b", full, re.I | re.M))
    info["has_tick2"] = bool(re.search(r"^##\s*Tick\s*2\b", full, re.I | re.M))
    return info


def find_tick2_section(full: str) -> str:
    """Extract the tick-2 section (heading through next ## or EOF)."""
    m = re.search(r"^##\s*Tick\s*2\b.*?(?=^##\s|\Z)", full,
                  re.I | re.M | re.S)
    return m.group(0) if m else ""


def tick2_references_tick1(tick2_text: str) -> tuple[bool, str]:
    """Heuristic: does the tick-2 text explicitly reference tick 1?"""
    if not tick2_text:
        return False, ""
    patterns = [
        r"tick\s*1\s+(found|reported|flagged|noted|identified|checked|audit(ed)?)",
        r"in\s+tick\s*1",
        r"per\s+tick\s*1",
        r"tick\s*1[''`]s\s+(finding|note|audit|memo|file|list)",
        r"(confirms?|extends?|re-checks?|builds?\s+on)\s+tick\s*1",
        r"captains?-log-agor-bridge.*tick\s*1",
        r"tick\s*1.*captains?-log-agor-bridge",
    ]
    for pat in patterns:
        m = re.search(pat, tick2_text, re.I)
        if m:
            # Quote the surrounding sentence
            start = max(0, m.start() - 80)
            end = min(len(tick2_text), m.end() + 80)
            quote = tick2_text[start:end].replace("\n", " ").strip()
            return True, quote
    return False, ""


def main():
    t0 = time.time()
    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]

    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)

    # ── TICK 1 ───────────────────────────────────────────────────────
    log("step 3 — TICK 1: spawn @atoz to audit captains-log-agor-bridge.org")
    if 0 > HARD_BUDGET_USD:
        log("ABORT: budget exceeded pre-tick-1")
        return 1
    tick1 = spawn_with_retry(1, pilot_sid, mcp_config)
    if tick1["sid"]:
        step_lift_permission(tick1["sid"])
        tick1_state = step_poll_session(tick1["sid"])
    else:
        tick1_state = "no-sid"
    log(f"  TICK 1 final state: {tick1_state}")

    # Verification 1: read memory file after tick 1
    log("step 4 — VERIFICATION 1: read memory file post-tick-1")
    mem_after_tick1 = read_memory_file(wt_path)
    log(f"  memory_file exists={mem_after_tick1['exists']} "
        f"size={mem_after_tick1['size']} lines={mem_after_tick1['lines']} "
        f"has_tick1_heading={mem_after_tick1['has_tick1']}")
    if mem_after_tick1["exists"]:
        log(f"  first 10 lines:\n{mem_after_tick1['first10']}")

    # ── PAUSE ────────────────────────────────────────────────────────
    log(f"step 5 — sleeping {INTER_TICK_SLEEP_S}s between ticks "
        "(proves session-to-session, not turn-to-turn)")
    time.sleep(INTER_TICK_SLEEP_S)

    # ── TICK 2 ───────────────────────────────────────────────────────
    log("step 6 — TICK 2: spawn fresh @atoz to read prior memory + audit "
        "agor-smoke-recipe.org + APPEND")
    cost_so_far = tick1.get("cost_total", 0.0)
    if cost_so_far > HARD_BUDGET_USD:
        log(f"ABORT: cost ${cost_so_far:.4f} > hard cap ${HARD_BUDGET_USD}")
        tick2 = {"sid": None, "out_path": "", "rate_limits": 0,
                 "cost": 0.0, "tool_uses": [], "rl_total": 0,
                 "cost_total": 0.0}
        tick2_state = "skipped-budget"
    else:
        tick2 = spawn_with_retry(2, pilot_sid, mcp_config)
        if tick2["sid"]:
            step_lift_permission(tick2["sid"])
            tick2_state = step_poll_session(tick2["sid"])
        else:
            tick2_state = "no-sid"
    log(f"  TICK 2 final state: {tick2_state}")

    # Verification 2: re-read memory file after tick 2
    log("step 7 — VERIFICATION 2: re-read memory file post-tick-2")
    mem_after_tick2 = read_memory_file(wt_path)
    log(f"  memory_file exists={mem_after_tick2['exists']} "
        f"size={mem_after_tick2['size']} lines={mem_after_tick2['lines']} "
        f"has_tick1={mem_after_tick2['has_tick1']} "
        f"has_tick2={mem_after_tick2['has_tick2']}")

    appended_not_overwritten = (
        mem_after_tick2["exists"] and mem_after_tick1["exists"]
        and mem_after_tick2["size"] >= mem_after_tick1["size"]
        and mem_after_tick2["has_tick1"]
    )
    log(f"  appended_not_overwritten={appended_not_overwritten} "
        f"(tick1_size={mem_after_tick1['size']} → "
        f"tick2_size={mem_after_tick2['size']})")

    tick2_section = find_tick2_section(mem_after_tick2["full"])
    cross_ref, quote = tick2_references_tick1(tick2_section)
    log(f"  tick2_references_tick1={cross_ref}")
    if cross_ref:
        log(f"  quote: {quote[:200]}")

    # ── verdict ──────────────────────────────────────────────────────
    elapsed = int(time.time() - t0)
    cost_total = tick1.get("cost_total", 0.0) + tick2.get("cost_total", 0.0)

    layer_2a_pass = (
        mem_after_tick1["exists"]
        and mem_after_tick1["has_tick1"]
        and mem_after_tick2["exists"]
        and mem_after_tick2["has_tick1"]
        and mem_after_tick2["has_tick2"]
        and appended_not_overwritten
        and cross_ref
    )
    layer_2a_partial = (
        mem_after_tick1["exists"]
        and mem_after_tick2["exists"]
        and mem_after_tick2["has_tick2"]
    )

    if layer_2a_pass:
        verdict = "PASS"
        layer_2a_verdict = "yes"
    elif layer_2a_partial:
        verdict = "PARTIAL"
        layer_2a_verdict = "partial"
    else:
        verdict = "FAIL"
        layer_2a_verdict = "no"

    why = []
    if not mem_after_tick1["exists"]:
        why.append("tick1 didn't write memory file")
    if not mem_after_tick1["has_tick1"]:
        why.append("tick1 file missing '## Tick 1' heading")
    if not mem_after_tick2["exists"]:
        why.append("memory file vanished post-tick-2")
    if not mem_after_tick2["has_tick2"]:
        why.append("tick2 didn't add '## Tick 2' heading")
    if not appended_not_overwritten:
        why.append("tick2 may have overwritten tick1 (size shrank or tick1 heading lost)")
    if not cross_ref:
        why.append("tick2 didn't explicitly reference tick1")

    why_summary = "; ".join(why) if why else "all checks passed"

    report = f"""
PILOT: R3-Memory
TEAM_SHAPE: two-tick @atoz on shared worktree (cross-run memory persistence)
HARNESS_SCRIPT: scripts/_r3_memory.py
AGOR_PRIMITIVES_USED: sibling-by-boardId (cross-tick on shared worktree), persistent memory/{{date}}.md (cross-run append), MCP indirection (search→execute), permission_config bypass (BUG-8), agor_sessions_spawn (×2), mcp_token after-get refresh (F8), one-spawn-per-claude-p (F9 amendment)
TASK_OUTCOME: {verdict} — {why_summary}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
TICK_1_SESSION_ID: {tick1.get('sid')}
TICK_2_SESSION_ID: {tick2.get('sid')}
TICK_1_FINAL_STATE: {tick1_state}
TICK_2_FINAL_STATE: {tick2_state}
MEMORY_FILE_PATH: {mem_after_tick2['path']}
MEMORY_FILE_EXISTS: {mem_after_tick2['exists']}
MEMORY_FILE_SIZE_AFTER_TICK_1: {mem_after_tick1['size']}
MEMORY_FILE_SIZE_AFTER_TICK_2: {mem_after_tick2['size']}
MEMORY_FILE_LINES_AFTER_TICK_2: {mem_after_tick2['lines']}
TICK_1_FIRST_10_LINES: |
{chr(10).join('  ' + l for l in mem_after_tick1['first10'].splitlines())}
TICK_2_FIRST_15_LINES: |
{chr(10).join('  ' + l for l in mem_after_tick2['first15'].splitlines())}
TICK_2_SECTION_FULL: |
{chr(10).join('  ' + l for l in tick2_section.splitlines())}
TICK_2_REFERENCES_TICK_1: {'yes — ' + quote if cross_ref else 'no'}
MEMORY_FILE_APPENDED_NOT_OVERWRITTEN: {'yes' if appended_not_overwritten else 'no'}
COST_USD: {cost_total:.4f}
DURATION_SECONDS: {elapsed}
LAYER_2A_VERDICT: {layer_2a_verdict}
"""
    print(report, flush=True)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    if mem_after_tick2["exists"]:
        (WORK_DIR / f"memory-snapshot-{EPOCH}.md").write_text(
            mem_after_tick2["full"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
