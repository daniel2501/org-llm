#!/usr/bin/env python3
"""PILOT R3-CrossTool — first end-to-end cross-tool execution.

Mission: prove DEC-017 § 6 (FOSS-defaults rule) end-to-end at the
*runtime* layer by spawning a child with `agenticTool="opencode"` —
not by passing a FOSS model id through claude-code's modelConfig
(which round-2 R7 proved gets rejected by the claude-code adapter).

Two children, sequential, one-spawn-per-claude-p (F9 amendment):

  Child A  agentic_tool=claude-code  prompt=print "CLAUDE_PROBE_DONE"
  Child B  agentic_tool=opencode     prompt=print "OPENCODE_PROBE_DONE"

For each child we verify:
  1. spawn returned a session_id
  2. session reached terminal status (idle is terminal — F1)
  3. session.agentic_tool field matches what we requested
  4. the child actually emitted its DONE token (proves runtime exec,
     not just the spawn config)

Pre-flight handles by this harness (so no manual setup needed):

  * If localhost:4096 (the opencode headless server) isn't up, start
    `opencode serve --port 4096` in the background and wait for it.
    Agor's executor handler routes opencode tasks to that port.

  * Patch ~/.agor/config.yaml to set `opencode.enabled: true`. The
    executor handler doesn't strictly require this (it reads
    serverUrl with a localhost:4096 fallback), but the REST endpoints
    /opencode/models + /opencode/health both gate on it, and surfacing
    those for diagnostic readback is helpful.

Honors round-1/2 findings:

  F1   `idle` IS terminal for spawned children
  F4   agor_execute_tool wants snake_case `tool_name`
  F8   JWT 15-min TTL; auto-refresh on 401 / when stale
  F9   one-spawn-per-claude-p (R2-D2 pattern)
  F10  Python+subprocess pattern
  R4   /messages?session_id={id} (NOT /sessions/{id}/messages)
  BUG-8 PATCH bypassPermissions on captain (and defensively on each child)

Underscore-prefixed: ad-hoc pilot driver, run once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml  # PyYAML — used to patch ~/.agor/config.yaml
    _HAVE_YAML = True
except ImportError:  # fallback to a tiny ad-hoc editor for our config shape
    _HAVE_YAML = False
    yaml = None  # type: ignore

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
AGOR_CONFIG = Path.home() / ".agor" / "config.yaml"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
SOURCE_BRANCH = "trunk"
EPOCH = int(time.time())
WT_NAME = f"pilot-R3CrossTool-{EPOCH}"
MODEL = "sonnet"  # captain's `claude -p` model alias
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r3_crosstool_artifacts")
WORK_DIR.mkdir(exist_ok=True)
HARD_BUDGET_USD = 5.00
SPAWN_BUDGET_USD = "1.00"
SPAWN_TIMEOUT_S = 360
SPAWN_MAX_RETRIES = 2
CHILD_POLL_DEADLINE_S = 600
INTER_SPAWN_SLEEP_S = 8
OPENCODE_PORT = 4096
OPENCODE_BIN = shutil.which("opencode") or "/home/daniel/.npm-global/bin/opencode"

# Two probe targets — the core experiment.
CHILDREN = [
    ("A", "claude-code", "CLAUDE_PROBE_DONE"),
    ("B", "opencode",    "OPENCODE_PROBE_DONE"),
]

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r3-crosstool {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ── token / auth (F8) ────────────────────────────────────────────────


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
        pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                            capture_output=True, text=True, timeout=10)
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
                BASE + "/authentication", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                got = json.loads(resp.read())
            if got.get("accessToken"):
                t = json.load(open(TOKEN_FILE))
                t["accessToken"] = got["accessToken"]
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
                pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                                    capture_output=True, text=True, timeout=10)
                if pw.returncode == 0:
                    body2 = json.dumps({
                        "strategy": "local",
                        "email": "admin@agor.live",
                        "password": pw.stdout.strip(),
                    }).encode()
                    req2 = urllib.request.Request(
                        BASE + "/authentication", data=body2, method="POST",
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


# ── opencode pre-flight ──────────────────────────────────────────────


def is_opencode_up() -> bool:
    try:
        with urllib.request.urlopen(
                f"http://localhost:{OPENCODE_PORT}/config",
                timeout=3) as r:
            return r.status < 500
    except Exception:
        return False


def patch_agor_config_for_opencode() -> dict:
    """Ensure ~/.agor/config.yaml has opencode.enabled: true + serverUrl.

    Uses PyYAML if importable; otherwise does a string-level edit safe
    for the simple top-level-key shape of the current config file.
    """
    info: dict = {"patched": False, "before": None, "after": None,
                  "needed_restart": False, "method": None}
    raw = AGOR_CONFIG.read_text()
    if _HAVE_YAML:
        try:
            cfg = yaml.safe_load(raw) or {}
        except Exception as e:
            log(f"  could not parse agor config (yaml): {e} — skip")
            return info
        info["before"] = dict(cfg.get("opencode") or {})
        cur = cfg.get("opencode") or {}
        desired = {"enabled": True,
                   "serverUrl": f"http://localhost:{OPENCODE_PORT}"}
        needs = (cur.get("enabled") is not True
                 or cur.get("serverUrl") != desired["serverUrl"])
        if needs:
            cfg["opencode"] = {**cur, **desired}
            AGOR_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
            info["patched"] = True
            info["method"] = "yaml"
            info["after"] = cfg["opencode"]
            info["needed_restart"] = True
            log(f"  patched ~/.agor/config.yaml opencode -> {cfg['opencode']}")
        else:
            info["after"] = cur
            info["method"] = "yaml-noop"
            log("  ~/.agor/config.yaml opencode already in place — no patch")
        return info
    # text-level fallback — append an opencode block if absent
    if re.search(r"(?m)^opencode:\s*$", raw):
        # block already present — assume it's fine; skip to avoid breaking
        info["before"] = "(opencode block already present, not parsed)"
        info["after"] = info["before"]
        info["method"] = "string-noop"
        log("  ~/.agor/config.yaml already has opencode: block — skipping "
            "(install pyyaml if you want to verify enabled+serverUrl)")
        return info
    info["before"] = "(no opencode block)"
    new = (raw.rstrip()
           + "\nopencode:\n"
           + "  enabled: true\n"
           + f"  serverUrl: http://localhost:{OPENCODE_PORT}\n")
    AGOR_CONFIG.write_text(new)
    info["patched"] = True
    info["method"] = "string-append"
    info["after"] = {"enabled": True,
                     "serverUrl": f"http://localhost:{OPENCODE_PORT}"}
    info["needed_restart"] = True
    log("  appended opencode block to ~/.agor/config.yaml (string-level)")
    return info


def start_opencode_serve() -> dict:
    """Start opencode serve on OPENCODE_PORT; wait for /config to respond.

    Returns {pid, log_path, started, was_already_up}.
    """
    if is_opencode_up():
        log(f"  opencode already listening on :{OPENCODE_PORT} — reusing")
        return {"pid": None, "log_path": None, "started": False,
                "was_already_up": True}
    log_path = WORK_DIR / f"opencode-serve-{EPOCH}.log"
    log(f"  starting opencode serve on :{OPENCODE_PORT} (log: {log_path})")
    f = open(log_path, "w")
    try:
        p = subprocess.Popen(
            [OPENCODE_BIN, "serve", "--port", str(OPENCODE_PORT),
             "--hostname", "127.0.0.1"],
            stdout=f, stderr=subprocess.STDOUT,
            cwd=str(Path.home()),
            preexec_fn=os.setsid,  # own process group → easier kill
        )
    except Exception as e:
        log(f"  opencode launch failed: {type(e).__name__}: {e}")
        return {"pid": None, "log_path": str(log_path), "started": False,
                "was_already_up": False, "error": str(e)}
    log(f"    pid={p.pid} — waiting up to 30s for HTTP readiness")
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_opencode_up():
            log("    opencode HTTP up")
            return {"pid": p.pid, "log_path": str(log_path),
                    "started": True, "was_already_up": False}
        if p.poll() is not None:
            log(f"    opencode died early rc={p.returncode}")
            return {"pid": p.pid, "log_path": str(log_path),
                    "started": False, "was_already_up": False,
                    "error": f"died rc={p.returncode}"}
        time.sleep(1)
    log("    opencode did not become healthy within 30s")
    return {"pid": p.pid, "log_path": str(log_path),
            "started": False, "was_already_up": False,
            "error": "timeout 30s"}


def stop_process_group(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        log(f"  sent SIGTERM to opencode pgid({pid})")
    except Exception as e:
        log(f"  could not stop opencode pid={pid}: {e}")


# ── REST primitives (same shape as R2-D2 / R2-Model-RETRY) ───────────


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


def parse_stream(text: str) -> dict:
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


# ── spawn ────────────────────────────────────────────────────────────


def spawn_one_child(label: str, agentic_tool: str, done_token: str,
                    mcp_config: str, attempt_label: str) -> dict:
    """Drive ONE captain `claude -p` invocation that issues exactly one
    `agor_sessions_spawn` with `agenticTool=<agentic_tool>`."""
    child_prompt = (
        f'Print exactly the literal text "{done_token}" on its own line, '
        "then stop. Do not use any tools. Do not narrate."
    )
    spawn_args = json.dumps({
        "prompt": child_prompt,
        "title": f"r3-crosstool-{label}-{agentic_tool}",
        "agenticTool": agentic_tool,
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
- The arguments JSON above is already serialized — pass it through verbatim.
  Note the camelCase `agenticTool` field — that's the spawn schema.

Once the spawn response comes back, print exactly this single line and
then STOP (do not poll, do not call any other tool, do not narrate):

CHILD_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR
                   / f"spawn-{label}-{agentic_tool}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(r"CHILD_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[{label}/{agentic_tool}] rc={rc} "
        f"rate_limits={info['rate_limits']} tool_uses={len(info['tool_uses'])} "
        f"cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(label: str, agentic_tool: str, done_token: str,
                     pilot_sid: str, mcp_config_path: str) -> dict:
    rl_total = 0
    cost_total = 0.0
    last: dict | None = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
        log(f"  spawn[{label}/{agentic_tool}] "
            f"attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_child(label, agentic_tool, done_token,
                               mcp_config_path, f"a{attempt}")
        rl_total += last["rate_limits"]
        cost_total += last["cost"]
        if last["sid"]:
            log(f"  spawn[{label}/{agentic_tool}] OK on attempt {attempt}")
            last["rl_total"] = rl_total
            last["cost_total"] = cost_total
            return last
        if attempt > SPAWN_MAX_RETRIES:
            break
        log(f"  spawn[{label}/{agentic_tool}] no sid — sleeping 30s before retry")
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


# ── readback ─────────────────────────────────────────────────────────


def fetch_session_row(sid: str) -> dict:
    try:
        got = api("GET", f"/sessions/{sid}")
    except Exception as e:
        return {"error": f"GET /sessions/{sid} failed: {e}"}
    return {
        "raw_keys": sorted(got.keys()) if isinstance(got, dict) else [],
        "status": got.get("status"),
        "agentic_tool": got.get("agentic_tool"),
        "model_config": got.get("model_config"),
        "sdk_session_id": got.get("sdk_session_id"),
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


def child_emitted_token(sid: str, token: str) -> tuple[bool, str]:
    """Return (assistant emitted token, last assistant excerpt).

    Checks ASSISTANT messages only — the user prompt itself contains the
    literal token, so a naive substring search across all messages would
    falsely match even if the model rejected the request.
    """
    msgs = fetch_child_messages(sid)
    assistant_texts: list[str] = []
    for m in msgs:
        role = m.get("role") or m.get("type") or ""
        if role != "assistant":
            continue
        assistant_texts.append(extract_text_from_message(m))
    blob = "\n".join(assistant_texts)
    got = token in blob
    excerpt = ""
    for t in reversed(assistant_texts):
        t = (t or "").strip()
        if t:
            excerpt = t[:120].replace("\n", " ")
            break
    return got, excerpt


# ── main ─────────────────────────────────────────────────────────────


def main() -> int:
    t0 = time.time()
    primitives_used: list[str] = []
    new_bugs: list[str] = []
    opencode_proc_pid: int | None = None

    # Pre-flight A: patch ~/.agor/config.yaml to enable opencode REST + serverUrl.
    log("pre-flight A — agor config opencode block")
    cfg_info = patch_agor_config_for_opencode()
    if cfg_info.get("patched"):
        primitives_used.append(
            "patched ~/.agor/config.yaml opencode.{enabled,serverUrl}")
        new_bugs.append(
            "opencode block missing from ~/.agor/config.yaml — Agor needs "
            "this to surface /opencode/health + /opencode/models. "
            "Patched in-place to {enabled:true, serverUrl:http://localhost:4096}.")

    # Pre-flight B: ensure opencode serve is running on :4096.
    log("pre-flight B — opencode serve on :4096")
    oc_info = start_opencode_serve()
    opencode_proc_pid = oc_info.get("pid") if oc_info.get("started") else None
    if oc_info.get("was_already_up"):
        primitives_used.append(f"opencode serve (already up on :{OPENCODE_PORT})")
    elif oc_info.get("started"):
        primitives_used.append(
            f"opencode serve --port {OPENCODE_PORT} (started by harness)")
    else:
        new_bugs.append(
            f"could not bring up opencode serve on :{OPENCODE_PORT} — "
            f"reason: {oc_info.get('error')}; opencode child will fail")

    try:
        # 1. worktree
        wt = step_create_worktree()
        wt_id = wt["worktree_id"]
        wt_path = wt["path"]
        primitives_used.append("POST /repos/{id}/worktrees")

        # 2. captain pilot session + bypass
        pilot_sid, mcp_token = step_create_pilot_session(wt_id)
        primitives_used.append("POST /sessions (captain claude-code)")
        primitives_used.append(
            "PATCH /sessions/{id} permission_config bypassPermissions (BUG-8)")
        mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
        write_mcp_config(mcp_token, mcp_config)
        primitives_used.append("MCP http transport (Bearer mcp_token)")

        # 3. F9-corrected: ONE captain claude -p subprocess per spawn.
        spawn_results: list[dict] = []
        for label, agentic_tool, done_token in CHILDREN:
            log(f"step 3.{label} — spawn child agentic_tool={agentic_tool}")
            cost_so_far = sum(r.get("cost_total", 0.0) for r in spawn_results)
            if cost_so_far > HARD_BUDGET_USD:
                log(f"  ABORT: cost ${cost_so_far:.4f} > cap ${HARD_BUDGET_USD}")
                spawn_results.append({"sid": None, "rl_total": 0,
                                      "cost_total": 0.0, "out_path": "",
                                      "skipped": True})
                continue
            result = spawn_with_retry(label, agentic_tool, done_token,
                                      pilot_sid, mcp_config)
            spawn_results.append(result)
            if result["sid"]:
                step_lift_permission(result["sid"])
            else:
                log(f"  WARNING: spawn[{label}/{agentic_tool}] failed after "
                    f"{SPAWN_MAX_RETRIES + 1} attempts")
            time.sleep(INTER_SPAWN_SLEEP_S)

        primitives_used.append(
            "agor_search_tools / agor_execute_tool (tool_name snake_case, F4)")
        primitives_used.append(
            "agor_sessions_spawn (sibling-by-worktree, no worktree_id)")
        primitives_used.append(
            "PER-SPAWN agenticTool OVERRIDE (camelCase, "
            "claude-code|opencode) — R3 focus")
        primitives_used.append("mcp_token after-get refresh (R2-D2 pattern, F9)")
        primitives_used.append("one-spawn-per-claude-p (F9 amendment)")

        # 4. poll children to terminal
        sids = [r.get("sid") for r in spawn_results]
        states = step_poll_children(sids)
        primitives_used.append(
            "GET /sessions/{id} poll until terminal incl. idle (F1)")

        # 5. readback rows + verify token emission
        spawn_table_rows: list[dict] = []
        for (label, requested_tool, done_token), result in zip(
                CHILDREN, spawn_results):
            sid = result.get("sid")
            if not sid:
                spawn_table_rows.append({
                    "label": label,
                    "requested_tool": requested_tool,
                    "session_field_observed": "n/a",
                    "assigned_tool": "<spawn failed>",
                    "child_id": "<no-sid>",
                    "got_token": False,
                    "excerpt": "(no session)",
                    "match": "no",
                    "status": "no-sid",
                })
                continue
            info = fetch_session_row(sid)
            assigned = info.get("agentic_tool") or "<null>"
            match = ("yes" if assigned == requested_tool
                     else "partial" if assigned and (
                         assigned.startswith(requested_tool)
                         or requested_tool.startswith(assigned))
                     else "no")
            got, excerpt = child_emitted_token(sid, done_token)
            spawn_table_rows.append({
                "label": label,
                "requested_tool": requested_tool,
                "session_field_observed": "session.agentic_tool",
                "assigned_tool": assigned,
                "child_id": sid,
                "got_token": got,
                "excerpt": excerpt,
                "match": match,
                "status": states.get(sid) or info.get("status"),
            })

        primitives_used.append(
            "GET /sessions/{id} agentic_tool readback")
        primitives_used.append(
            "GET /messages?session_id={id} content readback (R4 endpoint)")

        # 6. cost / duration
        total_cost = sum(r.get("cost_total", 0.0) for r in spawn_results)
        rl_total = sum(r.get("rl_total", 0) for r in spawn_results)
        elapsed = int(time.time() - t0)

        # ── verdict
        spawned_ok = sum(1 for r in spawn_table_rows
                         if r["child_id"] != "<no-sid>")
        matches = sum(1 for r in spawn_table_rows if r["match"] == "yes")
        token_count = sum(1 for r in spawn_table_rows if r["got_token"])
        n = len(CHILDREN)

        if spawned_ok == n and matches == n and token_count == n:
            verdict = "PASS"
        elif spawned_ok == n and matches == n:
            verdict = "PARTIAL"
        elif spawned_ok >= 1 and (matches >= 1 or token_count >= 1):
            verdict = "PARTIAL"
        else:
            verdict = "FAIL"

        # DEC-017 § 6 verdict — load-bearing for the round.
        opencode_row = next(r for r in spawn_table_rows
                            if r["requested_tool"] == "opencode")
        if (opencode_row["match"] == "yes" and opencode_row["got_token"]
                and opencode_row["status"] in {"idle", "completed",
                                               "stopped", "archived"}):
            dec_verdict = (
                "YES — opencode child spawned, agentic_tool field "
                f"persisted as 'opencode', child reached "
                f"{opencode_row['status']}, and emitted OPENCODE_PROBE_DONE. "
                "Cross-tool execution works end-to-end at the runtime layer "
                "(not just the config layer). FOSS-defaults rule is "
                "achievable via agenticTool='opencode' as predicted by R7.")
        elif (opencode_row["match"] == "yes"
              and opencode_row["status"] in {"idle", "completed",
                                             "stopped", "archived"}):
            dec_verdict = (
                "PARTIAL — opencode child spawned + reached "
                f"{opencode_row['status']} + Agor persisted the agentic_tool "
                "override, but no OPENCODE_PROBE_DONE token observed in "
                "child's assistant messages. Runtime executed but text "
                "didn't surface — possible message-readback projection gap "
                "for non-claude-code tasks, or model never produced the "
                f"token. Last excerpt: {opencode_row['excerpt'] or '(empty)'}")
        elif opencode_row["match"] == "yes":
            dec_verdict = (
                "PARTIAL — opencode child spawned + Agor persisted the "
                f"agentic_tool override, but reached non-terminal status "
                f"'{opencode_row['status']}'. Runtime executor likely failed "
                "before producing output. Check Agor daemon log and "
                f"opencode serve log at {oc_info.get('log_path')}.")
        elif opencode_row["child_id"] != "<no-sid>":
            dec_verdict = (
                "NO — spawn returned a session_id but Agor did not persist "
                f"agentic_tool='opencode' (got '{opencode_row['assigned_tool']}'). "
                "Override was rejected/coerced at the daemon layer.")
        else:
            dec_verdict = (
                "INCONCLUSIVE — could not get a session_id back from the "
                "agor_sessions_spawn call for the opencode child. Check "
                f"captain transcript at {spawn_results[1].get('out_path')}.")

        if oc_info.get("error"):
            dec_verdict += (f" NOTE: opencode pre-flight reported "
                            f"'{oc_info['error']}' — this likely caps the "
                            "child's runtime layer.")

        # bugs
        if not all(r.get("sid") for r in spawn_results):
            n_missing = sum(1 for r in spawn_results if not r.get("sid"))
            new_bugs.append(
                f"{n_missing}/{n} captain subprocesses failed to return a "
                "CHILD_SESSION_ID")
        if total_cost > HARD_BUDGET_USD:
            new_bugs.append(
                f"total cost ${total_cost:.4f} > cap ${HARD_BUDGET_USD}")
        if (opencode_row["child_id"] != "<no-sid>"
                and opencode_row["match"] == "yes"
                and not opencode_row["got_token"]
                and opencode_row["status"] in {"idle", "completed", "stopped"}):
            new_bugs.append(
                "opencode child reached terminal status with override "
                "honored, but assistant content did not surface "
                "OPENCODE_PROBE_DONE via /messages?session_id= — possible "
                "message-readback projection gap for opencode tasks.")
        new_bugs_str = ("none" if not new_bugs
                        else "\n  - " + "\n  - ".join(new_bugs))

        # what worked / didn't
        what_worked: list[str] = []
        what_didnt: list[str] = []
        if spawned_ok == n:
            what_worked.append(
                f"all {n} sequential one-spawn-per-claude-p subprocesses "
                "returned session_ids (F9 fix held)")
        else:
            what_didnt.append(
                f"only {spawned_ok}/{n} subprocesses returned session_ids")
        if matches == n:
            what_worked.append(
                "every requested agenticTool was persisted verbatim on the "
                "session row")
        elif matches:
            what_worked.append(
                f"{matches}/{n} agenticTool overrides persisted verbatim")
        if token_count:
            what_worked.append(
                f"{token_count}/{n} children actually emitted their "
                "DONE token (assigned tool was called and ran)")
        else:
            what_didnt.append(
                "no child emitted its DONE token — children may have "
                "spawned without running")
        if oc_info.get("started"):
            what_worked.append(
                f"opencode serve was off — harness brought it up on :"
                f"{OPENCODE_PORT} ({'PID ' + str(oc_info['pid']) if oc_info.get('pid') else 'no pid'})")
        if cfg_info.get("patched"):
            what_worked.append(
                "harness self-patched ~/.agor/config.yaml opencode block "
                "(no manual user step needed)")
        if rl_total:
            what_worked.append(
                f"observed {rl_total} rate_limit_event(s) without stalling "
                "(F9 amendment held)")
        if not what_didnt:
            what_didnt.append("no notable failures to report")

        # recommendation
        if verdict == "PASS":
            rec = ("keep — agenticTool='opencode' override verified end-to-end "
                   "at runtime; canonize as the FOSS path per DEC-017 § 6")
        elif verdict == "PARTIAL":
            rec = ("modify — partial; document which layer (config vs "
                   "executor vs message-readback) blocks the FOSS runtime; "
                   "consider a follow-up pilot once the failing layer is patched")
        else:
            rec = ("drop / file upstream — opencode runtime path does not "
                   "complete; FOSS-defaults rule per DEC-017 § 6 is not yet "
                   "achievable end-to-end on this stack")

        # render
        def _row(r: dict) -> str:
            return (
                f"  | {r['label']:6s} "
                f"| {r['requested_tool']:14s} "
                f"| {r.get('session_field_observed') or '<none>':22s} "
                f"| {(r.get('assigned_tool') or '<null>'):14s} "
                f"| {('yes' if r['got_token'] else 'no'):3s} "
                f"| {(r.get('excerpt') or '')[:80]}"
            )

        spawn_table = "\n".join(_row(r) for r in spawn_table_rows)
        primitives_block = "\n".join(f"  - {p}" for p in primitives_used)

        report = f"""
PILOT: R3-CrossTool
TEAM_SHAPE: 1 captain → 2 sequential children with distinct agentic_tools (claude-code + opencode)
HARNESS_SCRIPT: scripts/_r3_crosstool.py
AGOR_PRIMITIVES_USED:
{primitives_block}
TASK_OUTCOME: {verdict} — spawned_ok={spawned_ok}/{n} matches={matches}/{n} got_token={token_count}/{n}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
SPAWN_TABLE:
  | child  | requested_tool | session_field_observed | assigned_tool  | got | first 80 chars of response
{spawn_table}
SIBLING_STATES: { {s: states.get(s) for s in sids if s} }
COST_USD: {total_cost:.4f} (aggregated from per-spawn captain stream-json result events)
DURATION_SECONDS: {elapsed}
RATE_LIMIT_EVENTS_OBSERVED: {rl_total} (status=allowed treated as informational per F9 amendment)
DEC-017_§6_VERDICT: {dec_verdict}
NEW BUGS FOUND: {new_bugs_str}
WHAT_WORKED:
{chr(10).join('  - ' + s for s in what_worked)}
WHAT_DIDNT:
{chr(10).join('  - ' + s for s in what_didnt)}
RECOMMENDATION: {rec}
PRE_FLIGHT:
  agor_config_patch: {cfg_info}
  opencode_serve:    {oc_info}
"""
        print(report)
        (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
        (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
        (WORK_DIR / f"spawn-table-{EPOCH}.json").write_text(
            json.dumps({
                "spawn_table": spawn_table_rows,
                "states": {s: states.get(s) for s in sids if s},
                "cost_usd": total_cost,
                "duration_s": elapsed,
                "rate_limit_events": rl_total,
                "verdict": verdict,
                "dec_verdict": dec_verdict,
                "pre_flight": {"agor_config_patch": cfg_info,
                               "opencode_serve": oc_info},
            }, indent=2))
        return 0
    finally:
        # Per brief: KEEP_DIRTY — don't clean up the worktree. But do
        # stop the opencode serve process we spawned (otherwise it'll
        # squat on :4096 forever). If we didn't start it, leave it.
        if opencode_proc_pid:
            stop_process_group(opencode_proc_pid)


if __name__ == "__main__":
    sys.exit(main())
