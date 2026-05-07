#!/usr/bin/env python3
"""
PILOT R2-Model-RETRY — modelConfig override probe, F9-corrected pattern.

Round-2 R2-Model stalled because the captain queued 3 sequential
``agor_sessions_spawn`` tool_uses inside ONE ``claude -p`` context — same
failure mode as round-1 D2 (see findings F9 amendment). R2-D2 proved the
fix: *one ``claude -p`` subprocess per spawn*, with ``mcp_token`` refresh
between invocations.

This harness applies the F9-corrected pattern to the F5 question — does
``modelConfig.model`` actually take effect end-to-end? Three children,
each with a different requested model, each spawned via its own
``claude -p`` subprocess.

Three model targets:
  1. claude-sonnet-4-6              — sanity baseline (captain's parent model)
  2. claude-haiku-4-5-20251001      — Agor's apparent default for sub-tasks
  3. qwen/qwen-2.5-72b-instruct     — FOSS slot (DEC-017 § 6 floor)
                                      — most important data point

Per-child task: print "MODEL_PROBE_DONE" on its own line and stop. No
tools, no narration. Trivial — minimizes spend, but *requires the model
actually run* so we know the assigned model was called.

Round-1/2 findings honored:
  F1  — `idle` IS terminal for spawned children
  F4  — agor_execute_tool wants snake_case `tool_name`
  F8  — JWT 15-min TTL; auto-refresh on 401
  F9  — one-spawn-per-claude-p (R2-D2 pattern)
  F10 — Python+subprocess pattern
  R4  — /messages?session_id={id} (NOT /sessions/{id}/messages)
  BUG-8 — PATCH bypassPermissions on captain (and defensively on each child)

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
WT_NAME = f"pilot-R2ModelRetry-{EPOCH}"
MODEL = "sonnet"  # captain's `claude -p` model alias
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r2_model_retry_artifacts")
WORK_DIR.mkdir(exist_ok=True)
HARD_BUDGET_USD = 5.00
SPAWN_BUDGET_USD = "1.00"
SPAWN_TIMEOUT_S = 360
SPAWN_MAX_RETRIES = 2
CHILD_POLL_DEADLINE_S = 600
INTER_SPAWN_SLEEP_S = 8

# Three model targets — the core probe.
MODEL_TARGETS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "qwen/qwen-2.5-72b-instruct",
]

CHILD_PROMPT = (
    'Print exactly the literal text "MODEL_PROBE_DONE" on its own line, '
    "then stop. Do not use any tools. Do not narrate."
)

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r2-model-retry {time.strftime('%H:%M:%S')}] {msg}"
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


# ── Spawn helpers ──────────────────────────────────────────────────────


def parse_stream(text: str) -> dict:
    """Extract rate_limit_events, cost, tool_uses, and result presence."""
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


def spawn_one_child(idx: int, model: str, mcp_config: str,
                    attempt_label: str) -> dict:
    """Drive ONE captain `claude -p` invocation that issues exactly one
    `agor_sessions_spawn` with `modelConfig.model = <model>`. Returns
    {rc, sid, out_path, rate_limits, cost, has_result, tool_uses}.
    """
    spawn_args = json.dumps({
        "prompt": CHILD_PROMPT,
        "title": f"r2model-probe-{idx}-{model}",
        # modelConfig accepts either a bare string or a {model, mode,
        # effort, provider} object. Use the OBJECT form (canonical shape
        # per agor-live v0.17.3 modelConfigInputSchema).
        "modelConfig": {"model": model},
    })
    captain_prompt = f"""You have one MCP server "agor" with tools
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Spawn ONE child session by issuing exactly ONE
mcp__agor__agor_execute_tool call with these arguments:

  tool_name: "agor_sessions_spawn"
  arguments: {spawn_args}

Notes:
- The field name inside agor_execute_tool is `tool_name` (snake_case),
  NOT `toolName`.
- Do NOT pass a `worktree_id` — the child must inherit your worktree.
- Pass the `modelConfig` object EXACTLY as given. Do not modify it.
- The arguments JSON above is already serialized — pass it through.

Once the spawn response comes back, print exactly this single line and
then STOP (do not poll, do not call any other tool, do not narrate):

CHILD_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR
                   / f"spawn-{idx}-{model.replace('/', '_')}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(r"CHILD_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[{idx}/{model}] rc={rc} rate_limits={info['rate_limits']} "
        f"tool_uses={len(info['tool_uses'])} cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(idx: int, model: str, pilot_sid: str,
                     mcp_config_path: str) -> dict:
    """Sequential spawn driver with rate-limit / stall retry. Refreshes
    pilot session's mcp_token before each attempt (R2-D2 pattern)."""
    rl_total = 0
    cost_total = 0.0
    last: dict | None = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
        log(f"  spawn[{idx}/{model}] attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_child(idx, model, mcp_config_path, f"a{attempt}")
        rl_total += last["rate_limits"]
        cost_total += last["cost"]
        if last["sid"]:
            log(f"  spawn[{idx}/{model}] OK on attempt {attempt}")
            last["rl_total"] = rl_total
            last["cost_total"] = cost_total
            return last
        if attempt > SPAWN_MAX_RETRIES:
            break
        if last["rate_limits"] > 0:
            log(f"  spawn[{idx}/{model}] rate_limit_event seen — sleeping 30s before retry")
        else:
            log(f"  spawn[{idx}/{model}] no sid + no rate_limit — sleeping 30s "
                "anyway (stall mode)")
        time.sleep(30)
    last = last or {"sid": None, "out_path": "", "rate_limits": rl_total,
                    "cost": cost_total, "tool_uses": [], "has_result": False}
    last["rl_total"] = rl_total
    last["cost_total"] = cost_total
    return last


def step_lift_permission(sid: str | None) -> None:
    if not sid:
        return
    try:
        api("PATCH", f"/sessions/{sid}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  PATCHed bypass on child {sid}")
    except Exception as e:
        log(f"  PATCH child {sid} failed (non-fatal): {e}")


def step_poll_children(sids: list[str | None],
                       deadline_s: int = CHILD_POLL_DEADLINE_S) -> dict:
    log(f"step 5 — polling children to terminal status (≤ {deadline_s}s)")
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
            f"{(s or 'none')[:8]}={states.get(s, '?')}" for s in sids if s))
        if all_done:
            break
        time.sleep(15)
    return states


# ── Readback helpers ──────────────────────────────────────────────────


def fetch_session_row(sid: str) -> dict:
    """GET /sessions/{id} — return full row + extract candidate model fields."""
    try:
        got = api("GET", f"/sessions/{sid}")
    except Exception as e:
        return {"error": f"GET /sessions/{sid} failed: {e}"}
    # Probe several candidate fields where Agor might surface the
    # assigned model. Per agor-live v0.17.3 source, the canonical shape
    # is `model_config.model`, but fallbacks are worth a glance.
    candidates: dict[str, str | None] = {}
    mc = got.get("model_config")
    if isinstance(mc, dict):
        candidates["model_config.model"] = mc.get("model")
    elif isinstance(mc, str):
        candidates["model_config(string)"] = mc
    for key in ("model", "modelConfig", "assigned_model"):
        v = got.get(key)
        if isinstance(v, str):
            candidates[key] = v
        elif isinstance(v, dict):
            candidates[key + ".model"] = v.get("model")
    return {
        "raw": got,
        "candidates": candidates,
        "status": got.get("status"),
        "agentic_tool": got.get("agentic_tool"),
        "raw_keys": sorted(got.keys()) if isinstance(got, dict) else [],
    }


def fetch_child_messages(sid: str, limit: int = 40) -> list[dict]:
    """R4: /messages?session_id={sid} (Feathers list-with-filter shape)."""
    try:
        path = (f"/messages?session_id={sid}&%24limit={limit}"
                "&%24sort%5Bcreated_at%5D=1")
        got = api("GET", path)
    except Exception as e:
        log(f"  /messages?session_id={sid} fetch failed: {e}")
        return []
    if isinstance(got, dict):
        return got.get("data") or []
    if isinstance(got, list):
        return got
    return []


def extract_text_from_message(msg: dict) -> str:
    bits: list[str] = []
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
    """Return (emitted_probe_done, last_assistant_excerpt).

    Checks ASSISTANT messages only — the user prompt itself contains the
    literal "MODEL_PROBE_DONE" string, so a naive substring search across
    all messages would falsely match even when the model rejected the
    request and never ran.
    """
    msgs = fetch_child_messages(sid)
    assistant_texts: list[str] = []
    for m in msgs:
        role = m.get("role") or m.get("type") or ""
        if role != "assistant":
            continue
        assistant_texts.append(extract_text_from_message(m))
    blob = "\n".join(assistant_texts)
    got = "MODEL_PROBE_DONE" in blob
    excerpt = ""
    for t in reversed(assistant_texts):
        t = (t or "").strip()
        if t:
            excerpt = t[:120].replace("\n", " ")
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

    # 2. captain pilot session + bypass PATCH
    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    primitives_used.append("POST /sessions (claude-code)")
    primitives_used.append("PATCH /sessions/{id} permission_config bypassPermissions")
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)
    primitives_used.append("MCP http transport (Bearer mcp_token)")

    # 3. F9-corrected: ONE captain `claude -p` SUBPROCESS PER SPAWN, with
    #    mcp_token refresh between invocations. Sequential — one model
    #    per subprocess.
    spawn_results: list[dict] = []
    for idx, model in enumerate(MODEL_TARGETS, start=1):
        log(f"step 3.{idx} — spawn child for model={model}")
        cost_so_far = sum(r.get("cost_total", 0.0) for r in spawn_results)
        if cost_so_far > HARD_BUDGET_USD:
            log(f"  ABORT: cost ${cost_so_far:.4f} > hard cap ${HARD_BUDGET_USD}")
            spawn_results.append({"sid": None, "rl_total": 0,
                                  "cost_total": 0.0, "out_path": "",
                                  "skipped": True})
            continue
        result = spawn_with_retry(idx, model, pilot_sid, mcp_config)
        spawn_results.append(result)
        if result["sid"]:
            step_lift_permission(result["sid"])
        else:
            log(f"  WARNING: spawn[{idx}/{model}] failed after "
                f"{SPAWN_MAX_RETRIES + 1} attempts")
        time.sleep(INTER_SPAWN_SLEEP_S)

    primitives_used.append("agor_search_tools / agor_execute_tool (tool_name snake_case, F4)")
    primitives_used.append("agor_sessions_spawn (sibling-by-worktree, no worktree_id)")
    primitives_used.append("PER-SPAWN modelConfig.model OVERRIDE (object form) — F5 focus")
    primitives_used.append("mcp_token after-get refresh (R2-D2 pattern, F9 fix)")
    primitives_used.append("one-spawn-per-claude-p invocation (F9 amendment)")

    # 4. poll children to terminal
    sids = [r.get("sid") for r in spawn_results]
    states = step_poll_children(sids)
    primitives_used.append("GET /sessions/{id} poll until terminal incl. idle (F1)")

    # 5. readback assigned model + verify probe-done emission
    spawn_table_rows: list[dict] = []
    response_table_rows: list[dict] = []
    for idx, (requested, result) in enumerate(zip(MODEL_TARGETS, spawn_results),
                                              start=1):
        sid = result.get("sid")
        if not sid:
            spawn_table_rows.append({
                "child_id": "<no-sid>",
                "requested_model": requested,
                "session_field_observed": "n/a",
                "assigned_model": "<spawn failed>",
                "match": "no",
                "status": "no-sid",
                "raw_candidates": {},
            })
            response_table_rows.append({
                "child_id": "<no-sid>",
                "got_probe_done": False,
                "excerpt": "(no session)",
            })
            continue
        info = fetch_session_row(sid)
        cands = info.get("candidates", {}) or {}
        # Pick first non-null candidate; prefer model_config.model.
        chosen_field, chosen_value = "<none>", None
        for pref in ("model_config.model", "model_config(string)",
                     "modelConfig.model", "model", "assigned_model"):
            if pref in cands and cands[pref]:
                chosen_field, chosen_value = pref, cands[pref]
                break
        if chosen_value is None:
            for k, v in cands.items():
                if v:
                    chosen_field, chosen_value = k, v
                    break
        match = (
            "yes" if chosen_value == requested
            else ("partial" if chosen_value
                  and (requested in (chosen_value or "")
                       or (chosen_value or "") in requested)
                  else "no")
        )
        spawn_table_rows.append({
            "child_id": sid,
            "requested_model": requested,
            "session_field_observed": chosen_field,
            "assigned_model": chosen_value,
            "match": match,
            "status": states.get(sid) or info.get("status"),
            "raw_candidates": cands,
        })
        got, excerpt = child_emitted_probe_done(sid)
        response_table_rows.append({
            "child_id": sid,
            "got_probe_done": got,
            "excerpt": excerpt,
        })

    primitives_used.append("GET /sessions/{id} model readback")
    primitives_used.append("GET /messages?session_id={id} content readback (R4 endpoint)")

    # 6. cost / duration
    total_cost = sum(r.get("cost_total", 0.0) for r in spawn_results)
    rl_total = sum(r.get("rl_total", 0) for r in spawn_results)
    elapsed = int(time.time() - t0)

    # ── Verdict
    spawn_attempts = len(MODEL_TARGETS)
    spawned_ok = sum(1 for r in spawn_table_rows
                     if r["child_id"] != "<no-sid>")
    assigned_observed = sum(1 for r in spawn_table_rows
                            if r["assigned_model"]
                            and r["assigned_model"] != "<spawn failed>")
    matches = sum(1 for r in spawn_table_rows if r["match"] == "yes")
    probe_done_count = sum(1 for r in response_table_rows
                           if r["got_probe_done"])

    if (spawned_ok == spawn_attempts and matches == spawn_attempts
            and probe_done_count == spawn_attempts):
        verdict = "PASS"
    elif spawned_ok == spawn_attempts and matches == spawn_attempts:
        # Config layer fully works; runtime layer may have rejected one
        # or more models (e.g. non-Anthropic via claude-code adapter).
        verdict = "PARTIAL"
    elif spawned_ok >= 1 and (matches >= 1 or probe_done_count >= 1
                              or assigned_observed >= 1):
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    # F5 verdict — load-bearing. Distinguish CONFIG layer (Agor persists
    # the override) from RUNTIME layer (the agentic tool actually runs
    # the requested model).
    if matches == spawn_attempts and probe_done_count == spawn_attempts:
        f5 = ("YES (both layers) — per-spawn modelConfig.model takes effect "
              f"end-to-end. All {matches}/{spawn_attempts} children's "
              "persisted model field matches the requested override AND the "
              "model actually ran (emitted MODEL_PROBE_DONE).")
    elif matches == spawn_attempts and probe_done_count < spawn_attempts:
        rejected = spawn_attempts - probe_done_count
        f5 = ("PARTIAL — CONFIG layer YES (Agor persists every requested "
              f"modelConfig.model verbatim, {matches}/{spawn_attempts}); "
              f"RUNTIME layer NO for {rejected}/{spawn_attempts} (model "
              "rejected by the agentic tool — likely 'model not available' "
              "for non-Anthropic IDs through the claude-code adapter). "
              "Override is plumbed through Agor; the floor is the agentic "
              "tool's model registry, not Agor.")
    elif matches >= 1:
        f5 = (f"PARTIAL — {matches}/{spawn_attempts} children honored the "
              "override; see SPAWN_TABLE for which model IDs Agor accepted "
              "vs. silently substituted/rejected.")
    elif assigned_observed >= 1:
        f5 = (f"NO match — Agor persisted a different model than requested "
              f"for all {assigned_observed} spawned children. Override does "
              "not take effect at the documented field.")
    else:
        f5 = ("INCONCLUSIVE — could not read assigned_model from any child "
              "session row (model_config field empty/None). May indicate "
              "the GET /sessions projection doesn't surface model_config "
              "for spawned-by-MCP children — separate finding candidate.")

    # F9 fix verdict — load-bearing for harness pattern
    spawned_count = sum(1 for r in spawn_results if r.get("sid"))
    if spawned_count == spawn_attempts:
        f9_fix = ("YES — one-spawn-per-claude-p eliminated the round-2 stall. "
                  f"All {spawned_count}/{spawn_attempts} captain subprocesses "
                  "issued their tool_use and returned a session_id.")
    elif spawned_count >= 1:
        f9_fix = (f"PARTIAL — {spawned_count}/{spawn_attempts} subprocesses "
                  "completed; remaining stalled despite F9-corrected pattern. "
                  "Investigate per-spawn prompt or MCP token state.")
    else:
        f9_fix = ("NO — even with one-spawn-per-claude-p, captain subprocesses "
                  "failed to surface session_ids. Stall is not solely the "
                  "multi-spawn-per-context shape.")

    # New bugs
    new_bugs: list[str] = []
    if not all(r.get("sid") for r in spawn_results):
        n_missing = sum(1 for r in spawn_results if not r.get("sid"))
        new_bugs.append(
            f"{n_missing}/{spawn_attempts} captain subprocesses failed to "
            "return a CHILD_SESSION_ID")
    if total_cost > HARD_BUDGET_USD:
        new_bugs.append(
            f"total cost ${total_cost:.4f} exceeded hard cap ${HARD_BUDGET_USD}")
    if spawned_ok and assigned_observed == 0:
        new_bugs.append(
            "GET /sessions/{id} returned no usable model field for ANY "
            "spawned child — assigned_model unreadable via REST projection. "
            "Possible v0.17.3 projection gap (independent of F5 directly).")
    if not new_bugs:
        new_bugs_str = "none"
    else:
        new_bugs_str = "\n  - " + "\n  - ".join(new_bugs)

    # What worked / didn't
    what_worked: list[str] = []
    what_didnt: list[str] = []
    if spawned_ok == spawn_attempts:
        what_worked.append(
            f"all {spawn_attempts} sequential one-spawn-per-claude-p "
            "subprocesses returned session_ids (F9 fix held)")
    else:
        what_didnt.append(
            f"only {spawned_ok}/{spawn_attempts} subprocesses returned "
            "session_ids")
    if matches == spawn_attempts:
        what_worked.append(
            "every requested modelConfig.model was persisted verbatim")
    elif matches:
        what_worked.append(
            f"{matches}/{spawn_attempts} models persisted verbatim — "
            "partial override acceptance")
    if probe_done_count:
        what_worked.append(
            f"{probe_done_count}/{spawn_attempts} children actually "
            "emitted MODEL_PROBE_DONE (assigned model was called)")
    else:
        what_didnt.append(
            "no child emitted MODEL_PROBE_DONE — children may have "
            "spawned without running")
    if assigned_observed == 0 and spawned_ok:
        what_didnt.append(
            "GET /sessions/{id} did not surface model_config for any "
            "spawned child — readback evidence missing")
    if rl_total:
        what_worked.append(
            f"observed {rl_total} rate_limit_event(s) without stalling "
            "(F9 amendment held: status=allowed is informational)")
    if not what_didnt:
        what_didnt.append("no notable failures to report")

    # Recommendation
    if verdict == "PASS":
        rec = ("keep — per-spawn modelConfig.model verified end-to-end; "
               "canonize as a stable primitive in multi-agent-org-llm.org")
    elif verdict == "PARTIAL":
        rec = ("modify — partial; document which models Agor accepts vs. "
               "silently coerces; consider non-Anthropic via dedicated "
               "provider param if rejected")
    else:
        rec = ("drop / file upstream — modelConfig override does not take "
               "effect; rely on user-default model_config at session-create "
               "instead and petition agor for proper validation")

    # ── Render report
    def _row(r: dict) -> str:
        return (f"  | {str(r['child_id'])[:36]:36s} "
                f"| {str(r['requested_model']):30s} "
                f"| {str(r.get('session_field_observed') or '<none>'):24s} "
                f"| {str(r.get('assigned_model') or '<null>'):28s} "
                f"| {r['match']:7s} |")

    spawn_table = "\n".join(_row(r) for r in spawn_table_rows)
    resp_table = "\n".join(
        f"  | {str(r['child_id'])[:36]:36s} "
        f"| {'yes' if r['got_probe_done'] else 'no':3s} "
        f"| {(r['excerpt'] or '')[:80]}"
        for r in response_table_rows
    )

    primitives_block = "\n".join(f"  - {p}" for p in primitives_used)

    report = f"""
PILOT: R2-Model-RETRY
TEAM_SHAPE: focused modelConfig override probe — one-spawn-per-claude-p (3 captain subprocesses)
HARNESS_SCRIPT: scripts/_r2_model_retry.py
AGOR_PRIMITIVES_USED:
{primitives_block}
TASK_OUTCOME: {verdict} — spawned_ok={spawned_ok}/{spawn_attempts} matches={matches}/{spawn_attempts} probe_done={probe_done_count}/{spawn_attempts}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
SPAWN_TABLE:
  | child_id                             | requested_model                | session_field_observed   | assigned_model                | match   |
{spawn_table}
RESPONSE_TABLE:
  | child_id                             | got | first 80 chars of actual response
{resp_table}
SIBLING_STATES: { {s: states.get(s) for s in sids if s} }
COST_USD: {total_cost:.4f} (aggregated from per-spawn captain stream-json result events)
DURATION_SECONDS: {elapsed}
RATE_LIMIT_EVENTS_OBSERVED: {rl_total} (status=allowed treated as informational per F9 amendment)
F5_VERDICT: {f5}
F9_FIX_VERDICT: {f9_fix}
NEW BUGS FOUND: {new_bugs_str}
WHAT_WORKED:
  - {chr(10).join('  - ' + s for s in what_worked).lstrip('  - ')}
WHAT_DIDNT:
  - {chr(10).join('  - ' + s for s in what_didnt).lstrip('  - ')}
RECOMMENDATION: {rec}
"""
    print(report)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    (WORK_DIR / f"spawn-table-{EPOCH}.json").write_text(
        json.dumps({
            "spawn_table": spawn_table_rows,
            "response_table": response_table_rows,
            "states": {s: states.get(s) for s in sids if s},
            "cost_usd": total_cost,
            "duration_s": elapsed,
            "rate_limit_events": rl_total,
            "verdict": verdict,
            "f5_verdict": f5,
            "f9_fix_verdict": f9_fix,
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
