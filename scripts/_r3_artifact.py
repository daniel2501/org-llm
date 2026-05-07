#!/usr/bin/env python3
"""
PILOT R3-Artifact — live artifact preview primitive validation.

Spawn @geordi (Bridge Crew analyst) on a worktree. @geordi:

  1. Reads ``org_llm/perf.py`` (real module on trunk, ~393 LoC, perf
     helpers + DB queries).
  2. Drafts a small React/JSX or HTML/CSS/SVG chart visualizing
     something concrete from that module (geordi's choice — function
     names by complexity, imports grouped by source, etc.).
  3. Calls the Agor MCP artifact-publish tool (canon name
     ``agor_artifacts_publish`` per multi-agent-org-llm.org § Live
     artifact preview, but harness verifies via ``agor_search_tools``
     first).
  4. Prints ``ARTIFACT_ID=<id>`` on a line by itself, then
     ``GEORDI_DONE``, then stops.

Pilot harness then:

  * GET /artifacts/{id} via REST to confirm fetchability.
  * Lists /artifacts (Feathers list-with-filter shape) as a sanity
     check that the new ID landed in DB.

PASS criterion: artifact ID returned + REST-fetchable.
NOT-required: visual confirmation of Sandpack rendering (browser-only,
out of scope for a CLI pilot — user can inspect later).

Following round-1+2 lessons:

  * F8 (JWT 15-min TTL) — refresh ``mcp_token`` before spawn.
  * F4 (``tool_name`` snake_case in ``agor_execute_tool`` arguments).
  * BUG-8 — PATCH ``bypassPermissions`` on captain pilot session.
  * F1 — ``idle`` IS terminal for spawned children.
  * F9 amendment — ONE spawn per ``claude -p`` invocation. This pilot
     has exactly one spawn, so single subprocess.
  * R4 — REST endpoint for messages is ``/messages?session_id={id}``.
  * R6 — captain transcript will leak child mcp_token; do not share.
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
WT_NAME = f"pilot-R3Artifact-{EPOCH}"
MODEL = "sonnet"
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r3_artifact_artifacts")
WORK_DIR.mkdir(exist_ok=True)
TODAY = "2026-05-06"
HARD_BUDGET_USD = 5.00
SPAWN_BUDGET_USD = "2.00"     # generous — chart + tool call
SPAWN_TIMEOUT_S = 600         # 10 min for the chart-drafting subprocess
SPAWN_MAX_RETRIES = 1
SIBLING_POLL_DEADLINE_S = 900

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r3-artifact {time.strftime('%H:%M:%S')}] {msg}"
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


# Persona text — verbatim from org_llm/bridge_crew.py @geordi.
GEORDI_SOUL = (
    "I am Lt. Cmdr. Geordi La Forge. I see across the\n"
    "spectrum — SUMMARIZE, EXTRACT, ANALYZE. I make\n"
    "the dashboards talk. I sketch in Sandpack before\n"
    "I commit a Superset card.\n"
)
GEORDI_IDENTITY = (
    "# @geordi — Bridge Crew analyst\n"
    "\n"
    "Modes: SUMMARIZE / EXTRACT / ANALYZE.\n"
    "Tools: dbt models, Superset cards, sandpack preview.\n"
    "Coordination: live artifact preview + async peer\n"
    "  query for clarifiers from @spock or @atoz.\n"
)


def geordi_prompt(wt_path: str) -> str:
    return f"""You are @geordi, Bridge Crew analyst.

PERSONA:
{GEORDI_SOUL}
{GEORDI_IDENTITY}

CONTEXT:
- You are running as a single sibling session on Agor worktree
  {WT_NAME} at path {wt_path}.
- You have one MCP server "agor" with at least these tools:
  mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.
- This is a one-tick artifact-publish probe. The canon-named tool is
  agor_artifacts_publish (per docs/wiki/multi-agent-org-llm.org §
  "Live artifact preview"). VERIFY the exact tool name first via
  agor_search_tools before calling agor_execute_tool.

TASK (single tick — do exactly these steps in order):

STEP 1. Discover the artifact-publish tool.
  - Call mcp__agor__agor_search_tools with query "artifact" to list
    artifact-related tools available in this session.
  - In your final transcript, print a section labeled
    "ARTIFACT_TOOL_DISCOVERY:" followed by the JSON of what you got back.
  - Identify the exact tool name (likely agor_artifacts_publish OR
    agor_artifacts_create) and its argument shape.

STEP 2. Read the source you're visualizing.
  - Read the file org_llm/perf.py (relative to your worktree
    {wt_path}). It is a real ~393-LoC performance module with helpers
    + DB queries. Use the Read tool.
  - Pick ONE concrete thing to visualize (you choose):
      a) function names + approximate complexity (LoC per function)
      b) imports grouped by source module
      c) something else simple and meaningful

STEP 3. Draft a small chart artifact (~50-150 lines).
  - Write a single React/JSX component (preferred — Sandpack renders
    React) that visualizes the data you picked. Plain HTML+SVG is OK
    if simpler. NO external dependencies beyond React itself if
    possible (use inline SVG, no chart libraries).
  - Hardcode the data you extracted from perf.py directly into the
    component (no fetches, no imports beyond React).

STEP 4. Publish the artifact via MCP.
  - Call mcp__agor__agor_execute_tool with:
      tool_name: <the exact name discovered in STEP 1>
      arguments: <the arg shape the tool expects, populated with your
                  artifact source>
  - Note: the field name inside agor_execute_tool is `tool_name`
    (snake_case), NOT `toolName`. (Round-1 finding F4.)
  - If the tool needs a folder of files vs a single source string,
    the search-tools schema will tell you. Adapt accordingly.

STEP 5. Report.
  - Print exactly the line:
      ARTIFACT_ID=<id-returned-by-the-publish-call>
    on its own line. No quotes, no commentary on that line.
  - Then print:
      ARTIFACT_KIND=<short-tag like "react+jsx" or "html+svg">
  - Then print the line:
      ARTIFACT_LOC=<approx number of lines in your chart source>
  - Then print exactly the line:
      GEORDI_DONE
    and STOP.

CONSTRAINTS:
- DO NOT modify any tracked file under {wt_path}.
- If a step fails, print FAILED_AT_STEP=<n> and a short reason, then
  GEORDI_DONE and stop. Do not loop.
- Keep your assistant turn count low. Avoid narrating between tool
  uses unless useful for the report.
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
    """Pull rate_limit_events, cost, and a tool_use index out of stream-json."""
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


def spawn_geordi(pilot_sid: str, mcp_config_path: str,
                 wt_path: str) -> dict:
    """ONE captain claude -p call that spawns @geordi via
    agor_sessions_spawn with the chart-drafting prompt."""
    geordi_p = geordi_prompt(wt_path)
    spawn_args = json.dumps({
        "prompt": geordi_p,
        "title": "live-artifact-preview-geordi",
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

GEORDI_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR / f"captain-spawn-{EPOCH}.jsonl")
    rc = run_claude(captain_prompt, mcp_config_path, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(r"GEORDI_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  captain rc={rc} rate_limits={info['rate_limits']} "
        f"tool_uses={len(info['tool_uses'])} cost=${info['cost']:.4f} "
        f"geordi_sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def step_lift_permission(sid: str | None) -> None:
    if not sid:
        return
    try:
        api("PATCH", f"/sessions/{sid}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  PATCHed bypassPermissions on {sid}")
    except Exception as e:
        log(f"  PATCH {sid} failed: {e}")


def step_poll_geordi(sid: str, deadline_s: int = SIBLING_POLL_DEADLINE_S):
    log(f"step 5 — polling @geordi to terminal status (≤ {deadline_s}s)")
    deadline = time.time() + deadline_s
    terminal = {"completed", "stopped", "archived", "failed", "errored", "idle"}
    last = "?"
    while time.time() < deadline:
        try:
            got = api("GET", f"/sessions/{sid}")
            last = got.get("status", "?")
        except Exception as e:
            last = f"err({type(e).__name__})"
        log(f"  @geordi state={last}")
        if last in terminal:
            break
        time.sleep(20)
    return last


def step_fetch_geordi_messages(sid: str):
    """R4 endpoint: /messages?session_id={id}&$limit=N."""
    log("step 6 — fetching @geordi messages via /messages?session_id=…")
    try:
        j = api("GET", f"/messages?session_id={sid}&%24limit=500")
        msgs = j.get("data", []) if isinstance(j, dict) else []
        log(f"  got {len(msgs)} messages")
        # Dump for inspection.
        out = WORK_DIR / f"geordi-messages-{EPOCH}.json"
        out.write_text(json.dumps(j, indent=2)[:500_000])
        return msgs
    except Exception as e:
        log(f"  /messages fetch failed: {e}")
        return []


_ARTIFACT_ID_RE = re.compile(r"ARTIFACT_ID=([A-Za-z0-9_-]+)")
_ARTIFACT_KIND_RE = re.compile(r"ARTIFACT_KIND=([^\s\n]+)")
_ARTIFACT_LOC_RE = re.compile(r"ARTIFACT_LOC=([0-9]+)")
_TOOL_DISCOVERY_RE = re.compile(r"ARTIFACT_TOOL_DISCOVERY:(.+?)(?=ARTIFACT_ID=|GEORDI_DONE|FAILED_AT_STEP=)",
                                re.DOTALL)


def _extract_text_from_msg(m: dict) -> str:
    """Pull all text content out of a Feathers message row."""
    # Messages may have shape {role, content: str|list, ...} or nested
    # under raw_payload / etc. Be defensive.
    bits: list[str] = []
    for k in ("content", "text", "body", "raw_text"):
        v = m.get(k)
        if isinstance(v, str):
            bits.append(v)
        elif isinstance(v, list):
            for c in v:
                if isinstance(c, dict):
                    if isinstance(c.get("text"), str):
                        bits.append(c["text"])
                    elif isinstance(c.get("content"), str):
                        bits.append(c["content"])
                elif isinstance(c, str):
                    bits.append(c)
        elif isinstance(v, dict):
            bits.append(json.dumps(v))
    raw = m.get("raw_payload")
    if isinstance(raw, (dict, list)):
        bits.append(json.dumps(raw))
    elif isinstance(raw, str):
        bits.append(raw)
    return "\n".join(bits)


def step_extract_artifact(messages: list[dict]) -> dict:
    log("step 7 — extracting ARTIFACT_ID from @geordi messages")
    full = "\n".join(_extract_text_from_msg(m) for m in messages)
    # Also stash the full text for forensics.
    (WORK_DIR / f"geordi-fulltext-{EPOCH}.txt").write_text(full[:1_000_000])
    aid = None
    kind = None
    loc = None
    discovery = None
    m = _ARTIFACT_ID_RE.search(full)
    if m:
        aid = m.group(1)
    m = _ARTIFACT_KIND_RE.search(full)
    if m:
        kind = m.group(1)
    m = _ARTIFACT_LOC_RE.search(full)
    if m:
        loc = int(m.group(1))
    m = _TOOL_DISCOVERY_RE.search(full)
    if m:
        discovery = m.group(1).strip()[:4000]
    log(f"  artifact_id={aid} kind={kind} loc={loc} discovery_len="
        f"{len(discovery) if discovery else 0}")
    return {"id": aid, "kind": kind, "loc": loc, "discovery": discovery}


def step_fetch_artifact(aid: str | None) -> dict:
    log("step 8 — verifying artifact via REST GET")
    if not aid:
        return {"fetchable": False, "endpoint": "n/a", "status": None,
                "body_preview": "no artifact id to fetch"}
    # Try /artifacts/{id} first.
    for path in (f"/artifacts/{aid}",):
        try:
            j = api("GET", path)
            preview = json.dumps(j)[:1500] if isinstance(j, (dict, list)) else str(j)[:1500]
            log(f"  GET {path} -> ok, body_preview_len={len(preview)}")
            return {"fetchable": True, "endpoint": path,
                    "status": 200, "body_preview": preview, "body": j}
        except Exception as e:
            log(f"  GET {path} -> {e}")
    # Fallback: list /artifacts and grep.
    try:
        j = api("GET", "/artifacts?%24limit=200")
        data = j.get("data", []) if isinstance(j, dict) else []
        match = next((x for x in data if isinstance(x, dict)
                      and (x.get("id") == aid or x.get("artifact_id") == aid)),
                     None)
        if match:
            return {"fetchable": True, "endpoint": "/artifacts (list-grep)",
                    "status": 200, "body_preview": json.dumps(match)[:1500],
                    "body": match}
        return {"fetchable": False, "endpoint": "/artifacts (list-grep)",
                "status": 200, "body_preview": f"id {aid} not in {len(data)} artifacts"}
    except Exception as e:
        return {"fetchable": False, "endpoint": "/artifacts",
                "status": None, "body_preview": str(e)}


def main():
    t0 = time.time()
    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]

    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)

    # Refresh mcp_token immediately (F8) and rewrite config.
    new_tok = refresh_mcp_token(pilot_sid)
    if new_tok:
        write_mcp_config(new_tok, mcp_config)

    log("step 4 — spawning @geordi via captain")
    spawn = spawn_geordi(pilot_sid, mcp_config, wt_path)
    geordi_sid = spawn["sid"]
    if geordi_sid:
        step_lift_permission(geordi_sid)
    else:
        log("  WARNING: spawn never returned a session_id — aborting verify")

    geordi_state = "?"
    if geordi_sid:
        geordi_state = step_poll_geordi(geordi_sid)
    msgs = step_fetch_geordi_messages(geordi_sid) if geordi_sid else []

    extracted = step_extract_artifact(msgs)
    fetched = step_fetch_artifact(extracted["id"])

    # Aggregate sibling cost via REST (likely null per F7).
    sibling_cost_rest = 0.0
    if geordi_sid:
        try:
            got = api("GET", f"/sessions/{geordi_sid}")
            c = got.get("cost", {}) or {}
            sibling_cost_rest += float(c.get("total_usd")
                                       or got.get("total_cost_usd") or 0)
        except Exception:
            pass

    elapsed = int(time.time() - t0)
    captain_cost = float(spawn.get("cost", 0) or 0)

    # Verdict.
    have_id = bool(extracted["id"])
    fetchable = bool(fetched.get("fetchable"))
    if have_id and fetchable:
        outcome = "PASS"
    elif have_id or fetchable:
        outcome = "PARTIAL"
    else:
        outcome = "FAIL"

    live_preview_verdict = (
        "yes — artifact landed in DB and is fetchable via REST" if (have_id and fetchable)
        else "partial — see report" if have_id
        else "no — no artifact id surfaced"
    )

    discovery_summary = (extracted.get("discovery") or "")[:1200]

    report = f"""
PILOT: R3-Artifact
TEAM_SHAPE: solo @geordi emitting one live preview artifact
HARNESS_SCRIPT: scripts/_r3_artifact.py
AGOR_PRIMITIVES_USED: agor_sessions_spawn (sibling-by-boardId), agor_search_tools (artifact discovery), agor_execute_tool (publish), agor_artifacts_publish (canon name; verify via discovery), permission_config bypass, mcp_token after-get refresh, /messages?session_id={{id}} (R4), /artifacts/{{id}} REST
TASK_OUTCOME: {outcome} — have_id={have_id} fetchable={fetchable}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
GEORDI_SESSION_ID: {geordi_sid}
GEORDI_TERMINAL_STATE: {geordi_state}
ARTIFACT_TOOL_NAME_DISCOVERED: see ARTIFACT_TOOL_SCHEMA_SUMMARY below
ARTIFACT_TOOL_SCHEMA_SUMMARY: {discovery_summary or 'discovery output not parsed — see geordi-fulltext-{EPOCH}.txt'}
ARTIFACT_ID: {extracted['id'] or 'none'}
ARTIFACT_FETCHABLE: {('yes via ' + fetched['endpoint']) if fetchable else 'no — ' + fetched.get('body_preview', '')[:200]}
ARTIFACT_KIND: {extracted['kind'] or 'unknown'}
ARTIFACT_SIZE: {extracted['loc'] or 'unknown'} LoC
COST_USD: {captain_cost:.4f} (captain stream-json sum; sibling REST cost reported {sibling_cost_rest:.4f} — likely null per F7)
DURATION_SECONDS: {elapsed}
LIVE_PREVIEW_VERDICT: {live_preview_verdict}
"""
    print(report, flush=True)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    (WORK_DIR / f"extracted-{EPOCH}.json").write_text(
        json.dumps({"extracted": extracted, "fetched": fetched,
                    "geordi_state": geordi_state,
                    "captain_cost": captain_cost,
                    "sibling_cost_rest": sibling_cost_rest},
                   indent=2)[:200_000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
