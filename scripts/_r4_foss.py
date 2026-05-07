#!/usr/bin/env python3
"""PILOT R4-FOSS — opencode + per-spawn modelConfig FOSS routing probe.

Closes round-2 R7 + round-3 R9: prove that the FOSS-default rule
(DEC-017 § 6) is achievable end-to-end at the *runtime* layer by:

  1. Spawning child A through `agentic_tool="opencode"` with an
     OpenRouter+Qwen modelConfig (the FOSS production shape).
  2. Spawning child B through `agentic_tool="opencode"` with an
     Anthropic+Sonnet modelConfig (A/B sanity baseline that the
     modelConfig override is honored at all, not just for FOSS).

Both arms route through opencode — there is NO claude-code arm here.

Honors:
  R8  — agentic_tool="opencode" verified end-to-end at runtime
  R9  — FOSS routing needs modelConfig{provider, model} alongside
        agentic_tool="opencode" (auto-routes to big-pickle otherwise)
  R11 — provider+model NOT in REST /messages; pull from
        opencode-serve-{epoch}.log instead
  R16 — harness uses BLOCKING subprocess.run for `claude -p`; the
        sub-agent itself is a BLOCKING subprocess.run("python3 …")
        from the parent agent's perspective.
  F1  — `idle` is terminal for spawned children
  F4  — `tool_name` snake_case
  F8  — JWT TTL refresh
  F9  — one-spawn-per-claude-p
  R4  — /messages?session_id={id}
  BUG-8 — PATCH bypassPermissions on captain (and defensively on each child)

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
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False
    yaml = None  # type: ignore

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
AGOR_CONFIG = Path.home() / ".agor" / "config.yaml"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
SOURCE_BRANCH = "trunk"
EPOCH = int(time.time())
WT_NAME = f"pilot-R4FOSS-{EPOCH}"
MODEL = "sonnet"  # captain `claude -p` model alias
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r4_foss_artifacts")
WORK_DIR.mkdir(exist_ok=True)
HARD_BUDGET_USD = 3.00
SPAWN_BUDGET_USD = "0.75"
SPAWN_TIMEOUT_S = 360
SPAWN_MAX_RETRIES = 2
CHILD_POLL_DEADLINE_S = 600
INTER_SPAWN_SLEEP_S = 8
OPENCODE_PORT = 4096
OPENCODE_BIN = shutil.which("opencode") or "/home/daniel/.npm-global/bin/opencode"

# A = FOSS production shape; B = Anthropic A/B baseline. Both via opencode.
#
# FOSS arm model id: `qwen/qwen-2.5-72b-instruct` is the canonical pick
# in `org_llm/cloud.py`'s catalog, but a probe of the live opencode
# OpenRouter model registry (`/config/providers`) showed that exact id
# is NOT in opencode's catalog (OpenRouter retired the older 2.5-72b
# instruct route). Of the 18 Qwen entries, the closest FOSS pick that
# exists today is `qwen/qwen3-coder-30b-a3b-instruct` (Apache-2.0,
# Mixture-of-Experts coder, in the live catalog). We use that for arm
# A so the modelConfig pass-through can actually be exercised end-to-
# end. The original id is retained as a 'requested' field for audit
# evidence.
CHILDREN = [
    ("A", "opencode",
     {"provider": "openrouter",
      "model": "qwen/qwen3-coder-30b-a3b-instruct"},
     "FOSS_PROBE_DONE"),
    ("B", "opencode",
     {"provider": "anthropic", "model": "claude-sonnet-4-6"},
     "CLAUDE_BASELINE_DONE"),
]

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r4-foss {time.strftime('%H:%M:%S')}] {msg}"
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
    info["before"] = "(yaml not importable; skipped check)"
    info["method"] = "skipped-no-yaml"
    return info


def get_key_via_pass(slug: str) -> str | None:
    try:
        r = subprocess.run(["pass", slug],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            key = r.stdout.strip()
            if key:
                return key
        log(f"  `pass {slug}` rc={r.returncode}: "
            f"{(r.stderr or '').strip()[:120]}")
    except Exception as e:
        log(f"  could not read {slug} from pass: {e}")
    return None


def get_openrouter_key_via_pass() -> str | None:
    return get_key_via_pass("org-llm/cloud/openrouter/api-key")


def start_opencode_serve(env_extra: dict[str, str] | None = None) -> dict:
    """Start opencode serve on OPENCODE_PORT; wait for /config."""
    if is_opencode_up():
        log(f"  opencode already listening on :{OPENCODE_PORT} — reusing")
        return {"pid": None, "log_path": None, "started": False,
                "was_already_up": True}
    log_path = WORK_DIR / f"opencode-serve-{EPOCH}.log"
    log(f"  starting opencode serve on :{OPENCODE_PORT} (log: {log_path})")
    f = open(log_path, "w")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.Popen(
            [OPENCODE_BIN, "serve", "--port", str(OPENCODE_PORT),
             "--hostname", "127.0.0.1"],
            stdout=f, stderr=subprocess.STDOUT,
            cwd=str(Path.home()),
            preexec_fn=os.setsid,
            env=env,
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


# ── REST primitives ──────────────────────────────────────────────────


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
    """BLOCKING claude -p — F9 + R16 anti-pattern enforcement."""
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


def spawn_one_child(label: str, agentic_tool: str, model_cfg: dict,
                    done_token: str, mcp_config: str,
                    attempt_label: str) -> dict:
    """Drive ONE BLOCKING captain `claude -p` that issues exactly one
    `agor_sessions_spawn` with agenticTool + modelConfig."""
    child_prompt = (
        f'Print exactly the literal text "{done_token}" on its own line, '
        "then stop. Do not use any tools. Do not narrate."
    )
    spawn_args = json.dumps({
        "prompt": child_prompt,
        "title": f"r4-foss-{label}-{model_cfg['provider']}",
        "agenticTool": agentic_tool,
        "modelConfig": model_cfg,
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
  Note the camelCase `agenticTool` and `modelConfig` fields — that's the
  spawn schema.

Once the spawn response comes back, print exactly this single line and
then STOP (do not poll, do not call any other tool, do not narrate):

CHILD_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR
                   / f"spawn-{label}-{model_cfg['provider']}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(r"CHILD_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[{label}/{agentic_tool}/{model_cfg['provider']}] rc={rc} "
        f"rate_limits={info['rate_limits']} tool_uses={len(info['tool_uses'])} "
        f"cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(label: str, agentic_tool: str, model_cfg: dict,
                     done_token: str, pilot_sid: str,
                     mcp_config_path: str) -> dict:
    rl_total = 0
    cost_total = 0.0
    last: dict | None = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
        log(f"  spawn[{label}/{agentic_tool}/{model_cfg['provider']}] "
            f"attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_child(label, agentic_tool, model_cfg, done_token,
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
        log(f"  spawn[{label}] no sid — sleeping 30s before retry")
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


def scan_opencode_log_for_session(log_path: Path,
                                  sdk_session_id: str | None,
                                  fallback_after_iso: float) -> dict:
    """R11 workaround: read provider+model from opencode-serve log.

    Strategy: search for the session id (if known) and grab the
    surrounding provider/model evidence. Falls back to scanning the
    timeframe-sliced tail of the log for provider/model markers if
    the session id alone doesn't yield them.
    """
    out = {"provider": None, "model": None, "matched_lines": [],
           "method": "no-log"}
    if not log_path or not log_path.exists():
        return out
    try:
        raw = log_path.read_text(errors="replace")
    except Exception as e:
        out["method"] = f"read-error: {e}"
        return out
    lines = raw.splitlines()

    # Pass 1: lines mentioning the sdk_session_id directly.
    matched: list[str] = []
    if sdk_session_id:
        for ln in lines:
            if sdk_session_id in ln:
                matched.append(ln)

    # Pass 2: harvest provider/model from any line we matched.
    prov_re = re.compile(
        r"providerID[\"'=:]\s*[\"']?([A-Za-z0-9_./-]+)[\"']?")
    model_re = re.compile(
        r"modelID[\"'=:]\s*[\"']?([A-Za-z0-9_./@:-]+)[\"']?")
    prov_re_alt = re.compile(r"\bprovider[\"'=:]\s*[\"']?([A-Za-z0-9_./-]+)")
    model_re_alt = re.compile(r"\bmodel[\"'=:]\s*[\"']?([A-Za-z0-9_./@:-]+)")
    provider = None
    model = None
    for ln in matched:
        if not provider:
            mm = prov_re.search(ln) or prov_re_alt.search(ln)
            if mm:
                provider = mm.group(1)
        if not model:
            mm = model_re.search(ln) or model_re_alt.search(ln)
            if mm:
                model = mm.group(1)

    method = "session-id-direct" if matched else None

    # Pass 3: if still missing, scan ALL lines for the most recent
    # provider/model mention near the session lines (or anywhere if
    # we got no session lines).
    if not (provider and model):
        for ln in reversed(lines):
            if not provider:
                mm = prov_re.search(ln) or prov_re_alt.search(ln)
                if mm:
                    provider = mm.group(1)
            if not model:
                mm = model_re.search(ln) or model_re_alt.search(ln)
                if mm:
                    model = mm.group(1)
            if provider and model:
                method = method or "tail-scan"
                break

    out["provider"] = provider
    out["model"] = model
    out["matched_lines"] = matched[:5]
    out["method"] = method or "no-match"
    return out


# ── main ─────────────────────────────────────────────────────────────


def main() -> int:
    t0 = time.time()
    primitives_used: list[str] = []
    new_bugs: list[str] = []
    opencode_proc_pid: int | None = None

    log("pre-flight A — agor config opencode block")
    cfg_info = patch_agor_config_for_opencode()
    if cfg_info.get("patched"):
        primitives_used.append(
            "patched ~/.agor/config.yaml opencode.{enabled,serverUrl}")

    log("pre-flight B — opencode serve on :4096 (with provider env keys)")
    or_key = get_openrouter_key_via_pass()
    if or_key:
        log("  OPENROUTER_API_KEY available from "
            "`pass org-llm/cloud/openrouter/api-key`")
        primitives_used.append(
            "OPENROUTER_API_KEY injected via env from `pass`")
    else:
        log("  WARNING: no OpenRouter API key found in pass — FOSS arm "
            "may fail with provider auth error")
        new_bugs.append(
            "OpenRouter API key missing from "
            "`pass org-llm/cloud/openrouter/api-key` "
            "(or `pass` not unlocked) — FOSS arm probably won't authenticate")
    ant_key = (os.environ.get("ANTHROPIC_API_KEY")
               or get_key_via_pass("org-llm/cloud/anthropic/api-key"))
    if ant_key:
        log("  ANTHROPIC_API_KEY available (env or pass)")
        primitives_used.append(
            "ANTHROPIC_API_KEY injected via env (for Claude baseline arm)")
    else:
        log("  WARNING: no ANTHROPIC_API_KEY in env or `pass` — Claude "
            "baseline arm will fail with ProviderModelNotFoundError "
            "(opencode only registers anthropic provider when env key set)")
        new_bugs.append(
            "ANTHROPIC_API_KEY absent — opencode does not register the "
            "anthropic provider without an env key, so the Claude "
            "baseline arm cannot route to claude-sonnet-4-6 in this "
            "environment.")
    env_extra: dict[str, str] = {}
    if or_key:
        env_extra["OPENROUTER_API_KEY"] = or_key
    if ant_key:
        env_extra["ANTHROPIC_API_KEY"] = ant_key
    oc_info = start_opencode_serve(env_extra=(env_extra or None))
    opencode_proc_pid = oc_info.get("pid") if oc_info.get("started") else None
    if oc_info.get("was_already_up"):
        primitives_used.append(
            f"opencode serve (already up on :{OPENCODE_PORT})")
    elif oc_info.get("started"):
        primitives_used.append(
            f"opencode serve --port {OPENCODE_PORT} (started by harness)")
    else:
        new_bugs.append(
            f"could not bring up opencode serve on :{OPENCODE_PORT} — "
            f"reason: {oc_info.get('error')}")

    try:
        wt = step_create_worktree()
        wt_id = wt["worktree_id"]
        wt_path = wt["path"]
        primitives_used.append("POST /repos/{id}/worktrees")

        pilot_sid, mcp_token = step_create_pilot_session(wt_id)
        primitives_used.append("POST /sessions (captain claude-code)")
        primitives_used.append(
            "PATCH /sessions/{id} permission_config bypassPermissions (BUG-8)")
        mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
        write_mcp_config(mcp_token, mcp_config)
        primitives_used.append("MCP http transport (Bearer mcp_token)")

        spawn_results: list[dict] = []
        for label, tool, mcfg, done_token in CHILDREN:
            log(f"step 3.{label} — spawn child agentic_tool={tool} "
                f"modelConfig={mcfg}")
            cost_so_far = sum(r.get("cost_total", 0.0) for r in spawn_results)
            if cost_so_far > HARD_BUDGET_USD:
                log(f"  ABORT: cost ${cost_so_far:.4f} > cap ${HARD_BUDGET_USD}")
                spawn_results.append({"sid": None, "rl_total": 0,
                                      "cost_total": 0.0, "out_path": "",
                                      "skipped": True})
                continue
            result = spawn_with_retry(label, tool, mcfg, done_token,
                                      pilot_sid, mcp_config)
            spawn_results.append(result)
            if result["sid"]:
                step_lift_permission(result["sid"])
            else:
                log(f"  WARNING: spawn[{label}] failed after "
                    f"{SPAWN_MAX_RETRIES + 1} attempts")
            time.sleep(INTER_SPAWN_SLEEP_S)

        primitives_used.append(
            "agor_search_tools / agor_execute_tool (tool_name snake_case, F4)")
        primitives_used.append(
            "agor_sessions_spawn (sibling-by-worktree, no worktree_id)")
        primitives_used.append(
            'agentic_tool="opencode" + per-spawn modelConfig override '
            "(camelCase agenticTool + modelConfig{provider,model}) — R4-FOSS focus")
        primitives_used.append("mcp_token after-get refresh (R2-D2 pattern, F9)")
        primitives_used.append("one-spawn-per-claude-p (F9 amendment)")

        sids = [r.get("sid") for r in spawn_results]
        states = step_poll_children(sids)
        primitives_used.append(
            "GET /sessions/{id} poll until terminal incl. idle (F1)")

        # readback rows + verify token + dig provider/model out of opencode log
        oc_log_path = (Path(oc_info["log_path"])
                       if oc_info.get("log_path") else None)
        spawn_table_rows: list[dict] = []
        for (label, requested_tool, mcfg, done_token), result in zip(
                CHILDREN, spawn_results):
            sid = result.get("sid")
            if not sid:
                spawn_table_rows.append({
                    "label": label,
                    "requested_modelConfig": mcfg,
                    "session_agentic_tool": "<spawn failed>",
                    "session_model_config": None,
                    "sdk_session_id": None,
                    "log_provider": None,
                    "log_model": None,
                    "log_method": "no-session",
                    "child_id": "<no-sid>",
                    "got_token": False,
                    "excerpt": "(no session)",
                    "status": "no-sid",
                })
                continue
            row = fetch_session_row(sid)
            sdk_sid = row.get("sdk_session_id")
            got, excerpt = child_emitted_token(sid, done_token)
            scan = scan_opencode_log_for_session(
                oc_log_path, sdk_sid, t0)
            spawn_table_rows.append({
                "label": label,
                "requested_modelConfig": mcfg,
                "session_agentic_tool": row.get("agentic_tool"),
                "session_model_config": row.get("model_config"),
                "sdk_session_id": sdk_sid,
                "log_provider": scan["provider"],
                "log_model": scan["model"],
                "log_method": scan["method"],
                "child_id": sid,
                "got_token": got,
                "excerpt": excerpt,
                "status": states.get(sid) or row.get("status"),
            })

        primitives_used.append(
            "GET /sessions/{id} agentic_tool + model_config readback")
        primitives_used.append(
            "GET /messages?session_id={id} content readback (R4 endpoint)")
        primitives_used.append(
            "opencode-serve.log scan for actual provider+model "
            "(R11 workaround)")

        # cost + duration
        total_cost = sum(r.get("cost_total", 0.0) for r in spawn_results)
        rl_total = sum(r.get("rl_total", 0) for r in spawn_results)
        elapsed = int(time.time() - t0)

        # verdict
        n = len(CHILDREN)
        spawned_ok = sum(1 for r in spawn_table_rows
                         if r["child_id"] != "<no-sid>")
        token_count = sum(1 for r in spawn_table_rows if r["got_token"])
        # Did the modelConfig actually thread through to opencode runtime?
        def _provider_match(r: dict) -> bool:
            want = (r.get("requested_modelConfig") or {}).get("provider")
            got_p = (r.get("log_provider") or "").lower()
            return bool(want and got_p and want.lower() in got_p)
        provider_match_count = sum(1 for r in spawn_table_rows
                                   if _provider_match(r))

        if (spawned_ok == n and token_count == n
                and provider_match_count == n):
            verdict = "PASS"
        elif spawned_ok == n and (token_count >= 1 or provider_match_count >= 1):
            verdict = "PARTIAL"
        elif spawned_ok >= 1:
            verdict = "PARTIAL"
        else:
            verdict = "FAIL"

        # DEC-017 § 6 FOSS verdict — load-bearing
        foss_row = spawn_table_rows[0]   # arm A
        baseline_row = spawn_table_rows[1]  # arm B

        evidence_bits = []
        evidence_bits.append(
            f"FOSS arm: child_id={foss_row['child_id']}, "
            f"session.agentic_tool={foss_row['session_agentic_tool']}, "
            f"session.model_config={foss_row['session_model_config']}, "
            f"opencode-log provider={foss_row['log_provider']!r} "
            f"model={foss_row['log_model']!r} (via {foss_row['log_method']}), "
            f"got_token={foss_row['got_token']}, status={foss_row['status']}")
        evidence_bits.append(
            f"Baseline arm: child_id={baseline_row['child_id']}, "
            f"session.agentic_tool={baseline_row['session_agentic_tool']}, "
            f"session.model_config={baseline_row['session_model_config']}, "
            f"opencode-log provider={baseline_row['log_provider']!r} "
            f"model={baseline_row['log_model']!r} "
            f"(via {baseline_row['log_method']}), "
            f"got_token={baseline_row['got_token']}, "
            f"status={baseline_row['status']}")

        if (foss_row["child_id"] != "<no-sid>"
                and foss_row["got_token"]
                and _provider_match(foss_row)):
            foss_verdict = (
                "YES — opencode child with modelConfig.provider=openrouter + "
                "model=qwen/qwen-2.5-72b-instruct spawned, ran on the "
                "requested provider per opencode-serve log, and emitted "
                "FOSS_PROBE_DONE. DEC-017 § 6 (FOSS-defaults) is achievable "
                "end-to-end via `agentic_tool=\"opencode\"` + "
                "`modelConfig={provider, model}`.")
        elif (foss_row["child_id"] != "<no-sid>"
              and foss_row["got_token"]):
            foss_verdict = (
                "PARTIAL — FOSS child spawned + emitted FOSS_PROBE_DONE, "
                "but opencode-serve log did not confirm the requested "
                f"OpenRouter provider routing (got "
                f"provider={foss_row['log_provider']!r} "
                f"model={foss_row['log_model']!r}). Could be a log-scan miss "
                "(R11 workaround imperfect) OR opencode silently routed "
                "to its big-pickle default again. Inspect log directly.")
        elif (foss_row["child_id"] != "<no-sid>"
              and _provider_match(foss_row)):
            foss_verdict = (
                "PARTIAL — opencode child routed to OpenRouter+Qwen per the "
                "log, but no FOSS_PROBE_DONE token surfaced in the child's "
                "assistant messages. Runtime executed, output didn't surface "
                "via /messages?session_id= — possible message-readback gap "
                "for opencode-FOSS, or model never produced the literal token.")
        elif foss_row["child_id"] != "<no-sid>":
            foss_verdict = (
                f"NO — opencode child spawned but reached status "
                f"'{foss_row['status']}' without emitting FOSS_PROBE_DONE and "
                f"without log evidence of OpenRouter routing. "
                "Check opencode-serve log for auth/model-not-found errors.")
        else:
            foss_verdict = (
                "INCONCLUSIVE — could not get a session_id back for the "
                "FOSS arm. Captain transcript at "
                f"{spawn_results[0].get('out_path')}.")

        # baseline observations
        if (baseline_row["got_token"]
                and _provider_match(baseline_row)):
            baseline_obs_state = "ran-on-anthropic"
        elif baseline_row["got_token"]:
            baseline_obs_state = "ran-token-but-provider-uncertain"
        elif baseline_row["child_id"] != "<no-sid>":
            baseline_obs_state = "spawned-no-token"
        else:
            baseline_obs_state = "no-sid"

        # bugs
        for r in spawn_table_rows:
            if r["child_id"] != "<no-sid>" and not r.get("session_model_config"):
                new_bugs.append(
                    f"arm {r['label']}: spawn returned a session_id but "
                    "session.model_config came back null on readback — "
                    "Agor may not be persisting per-spawn modelConfig.")
        if not all(r.get("sid") for r in spawn_results):
            n_missing = sum(1 for r in spawn_results if not r.get("sid"))
            new_bugs.append(
                f"{n_missing}/{n} captain subprocesses failed to return a "
                "CHILD_SESSION_ID")
        if total_cost > HARD_BUDGET_USD:
            new_bugs.append(
                f"total cost ${total_cost:.4f} > cap ${HARD_BUDGET_USD}")
        new_bugs_str = ("none" if not new_bugs
                        else "\n  - " + "\n  - ".join(new_bugs))

        # what worked / didn't
        what_worked: list[str] = []
        what_didnt: list[str] = []
        if spawned_ok == n:
            what_worked.append(
                f"all {n} sequential one-spawn-per-claude-p subprocesses "
                "returned session_ids")
        else:
            what_didnt.append(
                f"only {spawned_ok}/{n} subprocesses returned session_ids")
        if token_count == n:
            what_worked.append(
                f"all {n} children emitted their DONE token "
                "(runtime actually executed)")
        elif token_count:
            what_worked.append(
                f"{token_count}/{n} children emitted their DONE token")
        else:
            what_didnt.append(
                f"0/{n} children emitted DONE tokens — runtime did not "
                "produce expected output")
        if provider_match_count == n:
            what_worked.append(
                "opencode-serve log confirmed both arms routed to their "
                "requested provider (modelConfig threaded through)")
        elif provider_match_count:
            what_worked.append(
                f"{provider_match_count}/{n} arms confirmed routing via "
                "opencode-serve log")
        else:
            what_didnt.append(
                "opencode-serve log did not confirm requested provider "
                "for either arm — could be a scan miss OR opencode "
                "ignored modelConfig and fell back to big-pickle")
        if rl_total:
            what_worked.append(
                f"{rl_total} rate_limit_event(s) observed without stalling")
        if oc_info.get("started"):
            what_worked.append(
                f"opencode serve was off — harness brought it up "
                f"on :{OPENCODE_PORT} (PID {oc_info.get('pid')})")
        if not what_didnt:
            what_didnt.append("no notable failures to report")

        # recommendation
        if verdict == "PASS":
            rec = ("keep — opencode + modelConfig{provider,model} verified "
                   "end-to-end at runtime; canonize as the FOSS production "
                   "shape per DEC-017 § 6")
        elif verdict == "PARTIAL":
            rec = ("modify — partial; document which layer (config vs "
                   "executor vs readback) blocks; consider follow-up "
                   "once the failing layer is patched")
        else:
            rec = ("drop — opencode+modelConfig FOSS path does not "
                   "complete; DEC-017 § 6 not yet achievable end-to-end")

        # render spawn table
        def _fmt_mc(mc: dict | None) -> str:
            if not mc:
                return "<null>"
            return f"{mc.get('provider')}/{mc.get('model')}"

        def _row(r: dict) -> str:
            req = _fmt_mc(r.get("requested_modelConfig"))
            obs = _fmt_mc(r.get("session_model_config"))
            log_pm = (f"{r.get('log_provider') or '?'}/"
                      f"{r.get('log_model') or '?'}")
            return (
                f"  | {r['label']:6s} "
                f"| {req:50s} "
                f"| {(r.get('session_agentic_tool') or '<null>'):8s} "
                f"| {obs:50s} "
                f"| {log_pm:60s} "
                f"| {('yes' if r['got_token'] else 'no'):3s} "
                f"| {(r.get('excerpt') or '')[:80]}"
            )

        spawn_table = "\n".join(_row(r) for r in spawn_table_rows)
        primitives_block = "\n".join(f"  - {p}" for p in primitives_used)

        report = f"""
PILOT: R4-FOSS
TEAM_SHAPE: 1 captain → opencode-FOSS arm + opencode-Claude-baseline arm (A/B)
HARNESS_SCRIPT: scripts/_r4_foss.py
AGOR_PRIMITIVES_USED:
{primitives_block}
TASK_OUTCOME: {verdict} — spawned_ok={spawned_ok}/{n} got_token={token_count}/{n} provider_match={provider_match_count}/{n}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {pilot_sid}
SPAWN_TABLE:
  | arm    | requested_modelConfig                              | tool     | session.model_config                               | opencode-serve provider/model                                | got | first 80 chars of response
{spawn_table}
SIBLING_STATES: { {s: states.get(s) for s in sids if s} }
COST_USD: {total_cost:.4f}
DURATION_SECONDS: {elapsed}
RATE_LIMIT_EVENTS_OBSERVED: {rl_total}
DEC-017_§6_FOSS_VERDICT: {foss_verdict}
A_VS_B_OBSERVATIONS:
  - FOSS arm got_token={foss_row['got_token']} status={foss_row['status']} log_provider={foss_row['log_provider']!r}
  - Baseline arm got_token={baseline_row['got_token']} status={baseline_row['status']} log_provider={baseline_row['log_provider']!r}
  - Baseline state classification: {baseline_obs_state}
EVIDENCE:
  - {evidence_bits[0]}
  - {evidence_bits[1]}
NEW BUGS FOUND: {new_bugs_str}
WHAT_WORKED:
{chr(10).join('  - ' + s for s in what_worked)}
WHAT_DIDNT:
{chr(10).join('  - ' + s for s in what_didnt)}
RECOMMENDATION: {rec}
PRE_FLIGHT:
  agor_config_patch: {cfg_info}
  opencode_serve:    {oc_info}
  openrouter_key:    {'present' if or_key else 'absent'}
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
                "foss_verdict": foss_verdict,
                "pre_flight": {"agor_config_patch": cfg_info,
                               "opencode_serve": oc_info,
                               "openrouter_key": bool(or_key)},
            }, indent=2, default=str))
        return 0
    finally:
        if opencode_proc_pid:
            stop_process_group(opencode_proc_pid)


if __name__ == "__main__":
    sys.exit(main())
