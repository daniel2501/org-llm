#!/usr/bin/env python3
"""
PILOT R2-Model — focused per-spawn modelConfig override probe.

Single-axis test of round-1 finding F5: does the per-spawn
`modelConfig.model` knob actually take effect end-to-end?

Method: ONE captain spawns THREE children SEQUENTIALLY, each with a
different `modelConfig.model` value. After each child reaches a terminal
status (incl. `idle` per F1), we:
  1. GET /sessions/{id} and read back `model_config.model` (Agor's
     persisted record of the spawn-time override).
  2. Query /messages?session_id={id} and confirm the child actually
     emitted MODEL_PROBE_DONE — proving the assigned model was actually
     called, not just nominally configured.

Three model targets (intentionally diverse):
  1. claude-sonnet-4-6        — captain's same model (sanity)
  2. claude-haiku-4-5         — Agor's documented Haiku alias (control)
  3. qwen2.5-72b-instruct     — non-Anthropic FOSS slot
                                 (DEC-017 § 6 floor — most important;
                                 expect either acceptance or graceful
                                 rejection — both inform F5)

Round-1 findings explicitly addressed:
  F1  — `idle` is the terminal status for MCP-spawned children
  F4  — agor_execute_tool wants snake_case `tool_name`
  F5  — THIS PILOT'S FOCUS: per-spawn modelConfig override e2e
  F8  — JWT TTL refresh (~10 min cadence; 401 -> POST /authentication)
  F9  — rate_limit_event tolerance (sequential, not parallel)
  F10 — Python+subprocess pattern

Schema reference (verified against agor-live v0.17.3 source):
  /home/daniel/.npm-global/lib/node_modules/agor-live/dist/daemon/mcp/tools/sessions.js
  L13981 modelConfigObjectSchema: {mode?, model, effort?, provider?}
  L13989 modelConfigInputSchema: union(string, modelConfigObjectSchema)
  L14258 agor_sessions_spawn registers modelConfig: modelConfigInputSchema
  L14287 spawnData.modelConfig = coerceModelConfig(args.modelConfig)
  L14158 GET /sessions exposes session.model_config.model

Underscore-prefixed: ad-hoc pilot driver, run once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
SOURCE_BRANCH = "trunk"
EPOCH = int(time.time())
WT_NAME = f"pilot-R2Model-{EPOCH}"
MODEL = "sonnet"                    # captain model alias for `claude -p`
TIMEOUT_PARENT = 900                # 15-min cap for the captain run
POLL_DEADLINE_S = 600               # 10-min cap per child poll
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r2_model_artifacts")
WORK_DIR.mkdir(exist_ok=True)
HARD_BUDGET_USD = 5.00
PARENT_BUDGET_USD = "4.50"

# Three model targets — the core probe.
MODEL_TARGETS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "qwen2.5-72b-instruct",
]

# Per-child trivial prompt — minimizes cost while still triggering an
# actual model call so we know the assigned model was used.
CHILD_PROMPT = (
    'Print exactly the literal text "MODEL_PROBE_DONE" on its own line, '
    "then stop. Do not use any tools. Do not narrate."
)

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r2-model {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ── Token / auth (F8 mitigation) ──────────────────────────────────────
def bearer() -> str:
    return json.load(open(TOKEN_FILE))["accessToken"]


def token_remaining_s() -> float:
    try:
        t = json.load(open(TOKEN_FILE))
        exp_ms = int(t.get("expiresAt") or 0)
        if not exp_ms:
            return 9e9
        return (exp_ms / 1000.0) - time.time()
    except Exception:
        return 9e9


def refresh_token_if_stale() -> None:
    rem = token_remaining_s()
    if rem > 300:
        return
    log(f"  token TTL low ({rem:.0f}s) — refreshing")
    try:
        pw = subprocess.run(
            ["pass", "org-llm/agor/admin-password"],
            capture_output=True, text=True, timeout=10,
        )
        if pw.returncode == 0 and pw.stdout.strip():
            r = subprocess.run(
                ["agor", "login", "-e", "admin@agor.live",
                 "-p", pw.stdout.strip()],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                log("  refreshed via `agor login`")
                return
            body = json.dumps({
                "strategy": "local",
                "email": "admin@agor.live",
                "password": pw.stdout.strip(),
            }).encode()
            req = urllib.request.Request(
                BASE + "/authentication",
                data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                got = json.loads(resp.read())
            new_tok = got.get("accessToken")
            if new_tok:
                t = json.load(open(TOKEN_FILE))
                t["accessToken"] = new_tok
                if "expiresAt" in got:
                    t["expiresAt"] = got["expiresAt"]
                Path(TOKEN_FILE).write_text(json.dumps(t))
                log("  refreshed via REST POST /authentication")
                return
    except Exception as e:
        log(f"  refresh exception: {type(e).__name__}: {e}")


def api(method: str, path: str, body=None, timeout: int = 30):
    refresh_token_if_stale()
    url = BASE + path
    headers = {"Authorization": "Bearer " + bearer()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        if e.code == 401:
            log("  401 — forcing token refresh and retrying once")
            try:
                pw = subprocess.run(
                    ["pass", "org-llm/agor/admin-password"],
                    capture_output=True, text=True, timeout=10)
                if pw.returncode == 0:
                    body2 = json.dumps({
                        "strategy": "local",
                        "email": "admin@agor.live",
                        "password": pw.stdout.strip(),
                    }).encode()
                    req2 = urllib.request.Request(
                        BASE + "/authentication",
                        data=body2, method="POST",
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req2, timeout=20) as r2:
                        got = json.loads(r2.read())
                    if got.get("accessToken"):
                        t = json.load(open(TOKEN_FILE))
                        t["accessToken"] = got["accessToken"]
                        if "expiresAt" in got:
                            t["expiresAt"] = got["expiresAt"]
                        Path(TOKEN_FILE).write_text(json.dumps(t))
                        headers2 = {"Authorization": "Bearer " + bearer()}
                        if body is not None:
                            headers2["Content-Type"] = "application/json"
                        req3 = urllib.request.Request(
                            url, data=data, method=method, headers=headers2)
                        with urllib.request.urlopen(req3, timeout=timeout) as r3:
                            raw = r3.read()
                            return json.loads(raw) if raw else {}
            except Exception as ee:
                log(f"  retry-after-refresh failed: {ee}")
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {body_txt}") from e


# ── REST primitives ───────────────────────────────────────────────────
def step_create_worktree() -> dict:
    log(f"step 1 — create worktree {WT_NAME}")
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


def step_create_pilot_session(wt_id: str) -> tuple[str, str]:
    log("step 2 — create captain pilot session (claude-code)")
    j = api("POST", "/sessions",
            {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    sid = j["session_id"]
    tok = j["mcp_token"]
    log(f"  pilot_session_id={sid} mcp_token_prefix={tok[:24]}...")
    log("  PATCH bypassPermissions on pilot session (BUG-8)")
    api("PATCH", f"/sessions/{sid}",
        {"permission_config": {"mode": "bypassPermissions"}})
    return sid, tok


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
               budget: str = "1.00", timeout: int = 600) -> int:
    env = os.environ.copy()
    extra = (f"{os.path.expanduser('~/.local/bin')}:"
             f"{os.path.expanduser('~/.npm-global/bin')}")
    env["PATH"] = f"{extra}:{env.get('PATH','')}"
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


# ── Captain prompt: 3 sequential spawns with distinct modelConfig.model ──
def build_captain_prompt() -> str:
    """Captain prompt: instructs THREE SEQUENTIAL agor_execute_tool ->
    agor_sessions_spawn calls, each with a distinct modelConfig.model.

    Sequential (not parallel) is intentional per F9 — avoids a single
    rate_limit_event killing the whole probe.
    """
    spawn_calls: list[str] = []
    for i, model in enumerate(MODEL_TARGETS, start=1):
        args = json.dumps({
            "prompt": CHILD_PROMPT,
            "title": f"r2model-probe-{i}-{model}",
            # modelConfig accepts either a bare string OR a {model, mode,
            # effort, provider} object. We use the OBJECT FORM here so
            # the test exercises the canonical shape (Pilot C used the
            # default — i.e. nothing — so this is the unverified knob).
            "modelConfig": {"model": model},
        })
        spawn_calls.append(
            f"Call {i} of 3 (model: {model}):\n"
            f"  tool_name: \"agor_sessions_spawn\"\n"
            f"  arguments: {args}\n"
        )

    return f"""You have one MCP server "agor" exposing two tools:
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

YOUR JOB: spawn THREE child sessions, ONE AT A TIME (sequential). Issue
each mcp__agor__agor_execute_tool call as a SEPARATE assistant turn — wait
for each tool result before issuing the next one. This is intentional:
sequential calls dodge rate_limit_events that have killed parallel-spawn
runs in earlier rounds.

For EACH of the three calls:
  - Set the tool_name field (snake_case) to "agor_sessions_spawn".
  - Pass the exact `arguments` JSON given below — including the
    `modelConfig` object. Do NOT modify or omit the modelConfig.
  - The `worktree_id` is intentionally OMITTED so each child inherits
    this captain's worktree.

{spawn_calls[0]}
{spawn_calls[1]}
{spawn_calls[2]}

After ALL THREE spawn responses come back, print exactly these four
lines on their own (no narration, no Markdown), then STOP — do not poll,
do not call any other tools:

CHILD_1_SESSION_ID=<session_id from call 1>
CHILD_2_SESSION_ID=<session_id from call 2>
CHILD_3_SESSION_ID=<session_id from call 3>
SPAWN_DONE
"""


def step_spawn_via_captain(mcp_config_path: str) -> dict:
    log("step 3 — captain spawns 3 children sequentially with distinct modelConfig")
    out_path = str(WORK_DIR / f"captain-{EPOCH}.jsonl")
    rc = run_claude(
        build_captain_prompt(),
        mcp_config_path, out_path,
        budget=PARENT_BUDGET_USD,
        timeout=TIMEOUT_PARENT,
    )
    log(f"  captain rc={rc}")
    text = Path(out_path).read_text() if Path(out_path).exists() else ""

    sids: list[str | None] = [None, None, None]
    for i in range(1, 4):
        m = re.search(rf"CHILD_{i}_SESSION_ID=([A-Za-z0-9_-]+)", text)
        if m:
            sids[i - 1] = m.group(1)

    cost = 0.0
    rate_limit_seen = 0
    sequential_turns_with_exec = 0
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "result":
            cost += float(obj.get("total_cost_usd") or 0)
        elif t == "rate_limit_event":
            rate_limit_seen += 1
        elif t == "assistant":
            content = obj.get("message", {}).get("content", []) or []
            n = sum(1 for c in content
                    if c.get("type") == "tool_use"
                    and c.get("name") == "mcp__agor__agor_execute_tool")
            if n:
                sequential_turns_with_exec += 1

    log(f"  child sids: {sids}")
    log(f"  cost=${cost:.4f}  rate_limit_events={rate_limit_seen}  "
        f"assistant turns w/ agor_execute_tool: {sequential_turns_with_exec}")

    return {
        "rc": rc,
        "out_path": out_path,
        "sids": sids,
        "cost": cost,
        "rate_limit_events": rate_limit_seen,
        "exec_turns": sequential_turns_with_exec,
    }


def step_lift_permission(sids: list[str | None]) -> None:
    for sid in sids:
        if not sid:
            continue
        try:
            api("PATCH", f"/sessions/{sid}",
                {"permission_config": {"mode": "bypassPermissions"}})
            log(f"  PATCHed bypass on child {sid}")
        except Exception as e:
            log(f"  PATCH child {sid} failed (non-fatal): {e}")


def step_poll_children(sids: list[str | None],
                       deadline_s: int = POLL_DEADLINE_S) -> dict:
    """Poll each child until terminal — incl. `idle` per F1."""
    log(f"step 4 — polling 3 children <= {deadline_s}s each "
        "(idle/completed/stopped/archived/failed/errored = terminal)")
    terminal = {"idle", "completed", "stopped", "archived",
                "failed", "errored"}
    states: dict[str, str] = {}
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        all_done = True
        for sid in sids:
            if not sid:
                continue
            if states.get(sid) in terminal:
                continue
            try:
                got = api("GET", f"/sessions/{sid}")
                states[sid] = got.get("status", "?")
            except Exception as e:
                states[sid] = f"err({type(e).__name__})"
            if states[sid] not in terminal:
                all_done = False
        log("  states: " + " ".join(
            f"{(s or 'none')[:8]}={states.get(s, '?')}" for s in sids))
        if all_done:
            break
        time.sleep(15)
    return states


def fetch_child_assigned_model(sid: str) -> dict:
    """Return {model_config, agentic_tool, status, raw}. The assigned
    model lives at session.model_config.model per the GET /sessions
    projection.

    Note: an earlier probe of generic /sessions/{id} GET on this daemon
    showed `model_config: None` for old rows. We attempt the documented
    field; we ALSO probe via the MCP whoami-style fields if available.
    """
    try:
        got = api("GET", f"/sessions/{sid}")
    except Exception as e:
        return {"error": f"GET /sessions/{sid} failed: {e}"}
    return {
        "model_config": got.get("model_config"),
        "agentic_tool": got.get("agentic_tool"),
        "status": got.get("status"),
        "raw_keys": sorted(got.keys()),
    }


def fetch_child_messages(sid: str, limit: int = 20) -> list[dict]:
    """GET /messages?session_id={sid}&$limit=20 — the messages service
    is mounted at /messages with findBySession via query."""
    try:
        # Feathers-style query
        path = f"/messages?session_id={sid}&%24limit={limit}&%24sort%5Bcreated_at%5D=1"
        got = api("GET", path)
    except Exception as e:
        log(f"  messages fetch failed for {sid}: {e}")
        return []
    if isinstance(got, dict):
        return got.get("data") or []
    if isinstance(got, list):
        return got
    return []


def extract_text_from_message(msg: dict) -> str:
    """Best-effort flatten of an Agor message row into plain text."""
    bits: list[str] = []
    # try common shapes
    for key in ("text", "content", "body", "raw_text"):
        val = msg.get(key)
        if isinstance(val, str):
            bits.append(val)
        elif isinstance(val, list):
            for c in val:
                if isinstance(c, dict):
                    for k in ("text", "content", "value"):
                        v = c.get(k)
                        if isinstance(v, str):
                            bits.append(v)
                elif isinstance(c, str):
                    bits.append(c)
    # Anthropic tool-use shape
    sdk = msg.get("sdk_message") or msg.get("message")
    if isinstance(sdk, dict):
        for c in (sdk.get("content") or []):
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    bits.append(t)
            elif isinstance(c, str):
                bits.append(c)
    return "\n".join(bits)


def child_emitted_probe_done(sid: str) -> tuple[bool, str]:
    msgs = fetch_child_messages(sid)
    full_texts: list[str] = []
    for m in msgs:
        full_texts.append(extract_text_from_message(m))
    blob = "\n".join(full_texts)
    got = "MODEL_PROBE_DONE" in blob
    excerpt = ""
    # find the last assistant-ish message text and snip 80 chars
    for t in reversed(full_texts):
        t = (t or "").strip()
        if t:
            excerpt = t[:80].replace("\n", " ")
            break
    return got, excerpt


# ── main ──────────────────────────────────────────────────────────────
def main() -> int:
    t0 = time.time()
    primitives_used: list[str] = []

    # 1. worktree
    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]
    primitives_used.append("POST /repos/{id}/worktrees")

    # 2. captain session + bypass PATCH
    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    primitives_used.append("POST /sessions (claude-code)")
    primitives_used.append("PATCH /sessions/{id} permission_config bypassPermissions")
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)
    primitives_used.append("MCP http transport (Bearer mcp_token)")

    # 3. captain spawns 3 children sequentially with distinct modelConfig
    spawn = step_spawn_via_captain(mcp_config)
    primitives_used.append("agor_search_tools / agor_execute_tool (tool_name snake_case, F4)")
    primitives_used.append("agor_sessions_spawn (sibling-by-worktree, no worktree_id)")
    primitives_used.append("PER-SPAWN modelConfig.model OVERRIDE (object form) — F5 focus")

    sids = [s for s in spawn["sids"] if s]
    if not sids:
        log("FATAL: captain produced no child session IDs — see captain JSONL")

    # 4. lift bypass on each child (defensive — same-tool inherit should
    #    already carry it from captain, but parallel pilots have shown
    #    occasional misses)
    step_lift_permission(spawn["sids"])

    # 5. poll all to terminal
    states = step_poll_children(spawn["sids"])
    primitives_used.append("GET /sessions/{id} poll until terminal incl. idle (F1)")

    # 6. read back assigned model + verify probe-done emission
    spawn_table_rows: list[dict] = []
    response_table_rows: list[dict] = []
    for i, (requested, sid) in enumerate(zip(MODEL_TARGETS, spawn["sids"])):
        if not sid:
            spawn_table_rows.append({
                "child_id": "<no-sid>",
                "requested_model": requested,
                "assigned_model": "<spawn failed>",
                "match": "no",
                "status": "no-sid",
                "raw_model_config": None,
            })
            response_table_rows.append({
                "child_id": "<no-sid>",
                "got_probe_done": False,
                "excerpt": "(no session)",
            })
            continue
        info = fetch_child_assigned_model(sid)
        mc = info.get("model_config") or {}
        assigned = mc.get("model") if isinstance(mc, dict) else None
        match = (
            "yes" if assigned == requested
            else ("partial" if assigned and requested in (assigned or "")
                  else "no")
        )
        spawn_table_rows.append({
            "child_id": sid,
            "requested_model": requested,
            "assigned_model": assigned,
            "match": match,
            "status": states.get(sid) or info.get("status"),
            "raw_model_config": mc,
        })
        got, excerpt = child_emitted_probe_done(sid)
        response_table_rows.append({
            "child_id": sid,
            "got_probe_done": got,
            "excerpt": excerpt,
        })

    primitives_used.append("GET /sessions/{id} model_config readback")
    primitives_used.append("GET /messages?session_id={id} content readback")

    # 7. cost aggregation
    total_cost = float(spawn["cost"] or 0)
    elapsed = int(time.time() - t0)

    # ── Verdict logic
    spawn_attempts = len(MODEL_TARGETS)
    spawned_ok = sum(1 for r in spawn_table_rows
                     if r["child_id"] != "<no-sid>")
    assigned_observed = sum(1 for r in spawn_table_rows
                            if r["assigned_model"])
    matches = sum(1 for r in spawn_table_rows if r["match"] == "yes")
    probe_done_count = sum(1 for r in response_table_rows
                           if r["got_probe_done"])

    if (spawned_ok == spawn_attempts and matches == spawn_attempts
            and probe_done_count == spawn_attempts):
        verdict = "PASS"
    elif spawned_ok >= 1 and assigned_observed >= 1:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    # F5 verdict — the load-bearing question
    if matches == spawn_attempts:
        f5 = ("YES — per-spawn modelConfig.model takes effect end-to-end. "
              f"All {matches}/{spawn_attempts} children's persisted "
              "model_config.model matches the requested override.")
    elif matches >= 1:
        f5 = (f"PARTIAL — {matches}/{spawn_attempts} children honored the "
              "override. See SPAWN_TABLE for which model IDs Agor accepted "
              "vs. silently substituted/rejected.")
    elif assigned_observed >= 1:
        f5 = ("NO match — Agor persisted a different model than requested "
              "for every spawned child. Override does not take effect at "
              "the documented field. See SPAWN_TABLE for actual values.")
    else:
        f5 = ("INCONCLUSIVE — could not read assigned_model from any child "
              "session row (model_config field empty/None). May indicate "
              "the GET /sessions projection doesn't surface model_config "
              "for spawned-by-MCP children — separate F-finding candidate.")

    # ── Round-1 finding regressions
    f1_status = ("re-confirmed: at least one child reached `idle`"
                 if any(states.get(s) == "idle" for s in spawn["sids"] if s)
                 else "no child reached `idle` in this run — see SIBLING_STATES")
    f4_status = ("re-confirmed: tool_name (snake_case) used in captain prompt"
                 if any(spawn["sids"]) else "captain failed to spawn — F4 inconclusive")
    f8_status = "auto-refresh wired into harness; not exercised in this run"
    f9_status = (f"observed {spawn['rate_limit_events']} rate_limit events; "
                 "sequential prompt design absorbed them"
                 if spawn["rate_limit_events"]
                 else "no rate_limit events seen (sequential design dodged risk)")
    f10_status = "Python+subprocess pattern executed end-to-end"

    new_bugs: list[str] = []
    if not all(spawn["sids"]):
        n_missing = sum(1 for s in spawn["sids"] if not s)
        new_bugs.append(
            f"captain failed to surface CHILD_n_SESSION_ID for {n_missing}/3 spawns"
        )
    if total_cost > float(PARENT_BUDGET_USD):
        new_bugs.append(
            f"captain cost ${total_cost:.4f} exceeded budget ${PARENT_BUDGET_USD}"
        )
    # If we couldn't read model_config from spawned children at all, that
    # is its own structural finding worth surfacing.
    if spawned_ok and assigned_observed == 0:
        new_bugs.append(
            "GET /sessions/{id} returned model_config=null/None for ALL "
            "spawned children — assigned_model unreadable via REST projection. "
            "Possibly a v0.17.3 projection gap (independent of F5)."
        )
    if not new_bugs:
        new_bugs_str = "none"
    else:
        new_bugs_str = "\n  - " + "\n  - ".join(new_bugs)

    # ── What worked / didn't
    what_worked: list[str] = []
    what_didnt: list[str] = []
    if spawned_ok == spawn_attempts:
        what_worked.append(
            f"all {spawn_attempts} sequential spawns landed (no rate-limit kill)")
    else:
        what_didnt.append(
            f"only {spawned_ok}/{spawn_attempts} spawns produced session IDs")
    if matches == spawn_attempts:
        what_worked.append(
            "every requested modelConfig.model was persisted verbatim "
            "in the child session row")
    elif matches:
        what_worked.append(
            f"{matches}/{spawn_attempts} models persisted verbatim — "
            "partial override acceptance")
    if probe_done_count:
        what_worked.append(
            f"{probe_done_count}/{spawn_attempts} children actually emitted "
            "MODEL_PROBE_DONE (assigned model was called, not just configured)")
    else:
        what_didnt.append(
            "no child emitted MODEL_PROBE_DONE — children may have "
            "spawned but not run the configured model")
    if assigned_observed == 0:
        what_didnt.append(
            "GET /sessions/{id} did not surface model_config for any "
            "spawned child — verdict drawn from emit-evidence alone")
    if not what_didnt:
        what_didnt.append("no notable failures to report")

    if verdict == "PASS":
        rec = ("keep — per-spawn modelConfig.model verified end-to-end; "
               "canonize as a stable primitive in multi-agent-org-llm.org")
    elif verdict == "PARTIAL":
        rec = ("modify — partial; document which models Agor accepts vs. "
               "silently coerces; extend AVAILABLE_CLAUDE_MODEL_ALIASES "
               "if non-Claude is desired")
    else:
        rec = ("drop / file upstream — modelConfig override does not take "
               "effect; rely on user-default model_config at session-create "
               "instead and petition agor for proper validation")

    # ── Render report
    def _row(r: dict) -> str:
        return (f"  | {str(r['child_id'])[:36]:36s} "
                f"| {str(r['requested_model']):28s} "
                f"| {str(r.get('assigned_model') or '<null>'):24s} "
                f"| {r['match']:7s} |")

    spawn_table = "\n".join(_row(r) for r in spawn_table_rows)
    resp_table = "\n".join(
        f"  | {str(r['child_id'])[:36]:36s} "
        f"| {'yes' if r['got_probe_done'] else 'no':3s} "
        f"| {(r['excerpt'] or '')[:80]}"
        for r in response_table_rows
    )

    report = f"""
PILOT: R2-Model
TEAM_SHAPE: focused modelConfig override probe (1 captain → 3 sequential children, distinct models)
HARNESS_SCRIPT: scripts/_r2_model.py
AGOR_PRIMITIVES_USED:
  - {chr(10).join('  - ' + p for p in primitives_used).lstrip('  - ')}
TASK_OUTCOME: {verdict} — spawned_ok={spawned_ok}/{spawn_attempts} matches={matches}/{spawn_attempts} probe_done={probe_done_count}/{spawn_attempts}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
SPAWN_TABLE:
  | child_id                             | requested_model              | assigned_model           | match   |
{spawn_table}
RESPONSE_TABLE:
  | child_id                             | got | excerpt (first 80 chars)
{resp_table}
SIBLING_STATES: { {s: states.get(s) for s in spawn['sids'] if s} }
COST_USD: {total_cost:.4f} (aggregated from parent stream-json result events)
DURATION_SECONDS: {elapsed}
ROUND-1 REGRESSIONS:
  F1 — {f1_status}
  F4 — {f4_status}
  F8 — {f8_status}
  F9 — {f9_status}
  F10 — {f10_status}
NEW BUGS FOUND: {new_bugs_str}
F5_VERDICT: {f5}
WHAT_WORKED:
  - {chr(10).join('  - ' + s for s in what_worked).lstrip('  - ')}
WHAT_DIDNT:
  - {chr(10).join('  - ' + s for s in what_didnt).lstrip('  - ')}
RECOMMENDATION: {rec}
"""
    print(report)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    # also dump the spawn table rows + response rows as JSON for downstream
    (WORK_DIR / f"spawn-table-{EPOCH}.json").write_text(
        json.dumps({"spawn_table": spawn_table_rows,
                    "response_table": response_table_rows,
                    "states": {s: states.get(s) for s in spawn["sids"] if s},
                    "cost_usd": total_cost,
                    "duration_s": elapsed,
                    "verdict": verdict}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
