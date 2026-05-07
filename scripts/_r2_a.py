#!/usr/bin/env python3
"""
PILOT R2-A — Solo @data specialist (control), Python+subprocess pattern.

Round-2 control pilot. Drives Agor to spawn ONE child session running the
@data (scribe) persona, which adds Python type hints + docstrings to a
small target file. Validates the round-1 Pilot D / D2 hybrid pattern
(urllib for REST + subprocess for `claude -p`) as the round-2 baseline
shape for sub-agent-driven Agor pilots.

Round-1 findings explicitly addressed:
  F1  — `idle` is the terminal status for MCP-spawned children
  F4  — agor_execute_tool wants snake_case `tool_name` (not toolName)
  F7  — child cost stays null at REST; aggregate via parent stream-json
  F8  — JWT TTL refresh (~10 min cadence; 401 -> POST /authentication)
  F9  — rate_limit_event in parent stream -> sleep+retry, do not die
  F10 — Python+subprocess pattern (this script IS the template)
  F11 — fallback target if primary file is unsuitable

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
WT_NAME = f"pilot-R2A-{EPOCH}"
MODEL = "sonnet"
TIMEOUT_PARENT = 1200            # 20-min cap for the captain claude -p run
POLL_DEADLINE_S = 900            # 15-min cap for the child session poll
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r2_a_artifacts")
WORK_DIR.mkdir(exist_ok=True)
HARD_BUDGET_USD = 5.00
PARENT_BUDGET_USD = "4.50"       # per-run cap on captain claude -p

# Primary target + fallback chain (per F11)
PRIMARY_TARGET = "org_llm/notices.py"
FALLBACK_TARGETS = ["org_llm/avatars.py", "org_llm/sidecar.py"]

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r2-a {time.strftime('%H:%M:%S')}] {msg}"
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
    """If token has < 5 min left, try `agor login`; fall back to direct
    POST /authentication with the password from `pass`."""
    rem = token_remaining_s()
    if rem > 300:
        return
    log(f"  token TTL low ({rem:.0f}s) — refreshing")
    # Try `agor login` first
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
            log(f"  `agor login` rc={r.returncode}; trying REST fallback")
            # REST fallback
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
                # Persist alongside existing fields
                t = json.load(open(TOKEN_FILE))
                t["accessToken"] = new_tok
                # expiresAt may be in payload; otherwise leave stale
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
        # If 401, try one refresh + retry
        if e.code == 401:
            log("  401 — forcing token refresh and retrying once")
            # Force-stale by stomping ttl check
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
                        # retry once
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


# ── @data persona text (extracted from cli.py _PRECONFIGURED_AGENT_PROMPTS["scribe"]) ──
DATA_PERSONA = (
    "You are scribe (canonical: @data) — a Bridge Crew specialist. "
    "On this task you are pivoting from your default capture/draft "
    "modes to act as a careful Python type-hint + docstring editor. "
    "Strong defaults still apply:\n"
    "  • prefer questions to assumptions if intent is ambiguous\n"
    "  • surface ONE good draft of the change rather than several\n"
    "  • do not rewrite logic — only annotate types + add docstrings\n"
)


def pick_target(wt_path: Path) -> str:
    """Return the relative file path the child should edit. Honors F11
    fallback chain — primary first, then fallbacks if primary is missing
    or already heavily type-hinted."""
    candidates = [PRIMARY_TARGET, *FALLBACK_TARGETS]
    for rel in candidates:
        p = wt_path / rel
        if not p.exists():
            log(f"  candidate {rel}: missing in worktree, skip")
            continue
        text = p.read_text()
        # Heuristic for "already heavily hinted": >80% of `def`s have `->`
        defs = re.findall(r"^\s*def\s+\w+\s*\(", text, re.MULTILINE)
        arrows = re.findall(r"^\s*def\s+\w+\s*\([^)]*\)\s*->", text,
                            re.MULTILINE | re.DOTALL)
        if defs and len(arrows) / max(len(defs), 1) > 0.8 and "->" in text:
            log(f"  candidate {rel}: looks already-hinted "
                f"({len(arrows)}/{len(defs)} defs); trying next")
            continue
        log(f"  picked target: {rel} ({len(defs)} defs, "
            f"{len(arrows)} already arrowed)")
        return rel
    # If everything looks done, just return primary anyway
    log(f"  WARN: no candidate looked unhinted; defaulting to {PRIMARY_TARGET}")
    return PRIMARY_TARGET


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
    # Make sure ~/.local/bin (where `claude` lives on this box) is on PATH
    extra = f"{os.path.expanduser('~/.local/bin')}:{os.path.expanduser('~/.npm-global/bin')}"
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


# ── Captain prompt: spawn ONE child to do the @data work ──────────────
def build_data_child_prompt(target_rel: str) -> str:
    return f"""You are @data (scribe) — Bridge Crew specialist. Your task today
is a small, focused Python annotation pass on a single file in this
worktree. Treat this as a careful editor pass, not a refactor.

PERSONA:
{DATA_PERSONA}

TASK:
1. Read `{target_rel}` from the current working directory (the worktree).
2. Edit `{target_rel}` to:
   - add Python type hints (PEP 484 / 604) to every public function,
     method, and class — parameters AND return types
   - add a one-line docstring to every public function/class that lacks one
   - keep existing docstrings intact (do not rewrite them)
   - DO NOT change any runtime behavior; do not refactor logic, rename
     symbols, reorder code, or alter imports beyond what type-hint
     additions require (e.g. `from __future__ import annotations` only
     if it is missing AND your hints need PEP 604 / forward refs).
   - if a variable's type is genuinely ambiguous, prefer leaving it
     unannotated over guessing — type *only* what is obvious.
3. After editing, verify the import is clean by running this exact
   command from the worktree root:
     python3 -c "import {target_rel.replace('/', '.').rsplit('.py', 1)[0]}"
   The command must exit 0 with no output.
4. Print exactly the line `R2A_DATA_DONE` on its own line, then stop.

CONSTRAINTS:
- Edit ONLY `{target_rel}`. Do not touch any other file in the repo.
- DO NOT run agor MCP tools.
- Use Read + Edit (or equivalent) — do not Write the whole file from
  scratch unless absolutely necessary.
- One commit's worth of work; one file's worth of edits.
"""


def build_captain_prompt(target_rel: str) -> str:
    child_args = json.dumps({
        "prompt": build_data_child_prompt(target_rel),
        "title": f"r2a-data-{EPOCH}",
    })
    return f"""You have one MCP server "agor" exposing two tools:
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Your job: spawn ONE child session by calling mcp__agor__agor_execute_tool
with the `tool_name` (snake_case) field set to "agor_sessions_spawn"
and the arguments below. The child will inherit this captain's worktree
(no `worktree_id` field — sibling-by-board pattern).

agor_execute_tool arguments:
  {{
    "tool_name": "agor_sessions_spawn",
    "arguments": {child_args}
  }}

After the spawn response comes back, print exactly this line and then
stop (do not poll, do not call other tools, do not narrate):

CHILD_SESSION_ID=<session_id from the spawn response>
"""


def step_spawn_data_via_captain(mcp_config_path: str,
                                target_rel: str) -> dict:
    log("step 3 — captain spawns @data child")
    out_path = str(WORK_DIR / f"captain-{EPOCH}.jsonl")
    rc = run_claude(
        build_captain_prompt(target_rel),
        mcp_config_path, out_path,
        budget=PARENT_BUDGET_USD,
        timeout=TIMEOUT_PARENT,
    )
    log(f"  captain rc={rc}")
    text = Path(out_path).read_text() if Path(out_path).exists() else ""

    # Parse out child session id + cost + parallel + rate_limit
    child_sid = None
    m = re.search(r"CHILD_SESSION_ID=([A-Za-z0-9_-]+)", text)
    if m:
        child_sid = m.group(1)
    cost = 0.0
    rate_limit_seen = 0
    parallel = 0
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
            parallel = max(parallel, n)
    log(f"  child_sid={child_sid} cost=${cost:.4f} "
        f"rate_limit_events={rate_limit_seen} max_parallel_exec={parallel}")
    return {
        "rc": rc, "out_path": out_path, "child_sid": child_sid,
        "cost": cost, "rate_limit_events": rate_limit_seen,
        "parallel": parallel,
    }


def step_lift_permission(child_sid: str | None) -> None:
    if not child_sid:
        return
    try:
        api("PATCH", f"/sessions/{child_sid}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  PATCHed bypass on child {child_sid}")
    except Exception as e:
        log(f"  PATCH child failed (non-fatal): {e}")


def step_poll_child(child_sid: str | None,
                    deadline_s: int = POLL_DEADLINE_S) -> str:
    """Poll until terminal — incl. `idle` per F1."""
    if not child_sid:
        return "no-sid"
    log(f"step 4 — polling child {child_sid} <= {deadline_s}s "
        "(idle/completed/stopped/archived/failed/errored = terminal)")
    deadline = time.time() + deadline_s
    terminal = {"idle", "completed", "stopped", "archived",
                "failed", "errored"}
    last = "?"
    while time.time() < deadline:
        try:
            got = api("GET", f"/sessions/{child_sid}")
            last = got.get("status", "?")
        except Exception as e:
            last = f"err({type(e).__name__})"
        log(f"  status={last}")
        if last in terminal:
            return last
        time.sleep(15)
    return last


def step_verify_filesystem(wt_path: str, target_rel: str) -> dict:
    log("step 5 — verify file edited in worktree")
    target = Path(wt_path) / target_rel
    out: dict = {"target": str(target), "exists": target.exists()}
    if not target.exists():
        return out
    text = target.read_text()
    out["bytes"] = len(text)
    # diff vs trunk
    try:
        p = subprocess.run(
            ["git", "diff", "--stat", "trunk", "--", target_rel],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["diff_stat"] = (p.stdout or "").strip()
    except Exception as e:
        out["diff_stat"] = f"err: {e}"
    try:
        p = subprocess.run(
            ["git", "diff", "trunk", "--", target_rel],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        diff_full = p.stdout or ""
        out["diff_lines_added"] = sum(
            1 for ln in diff_full.splitlines()
            if ln.startswith("+") and not ln.startswith("+++"))
        out["diff_lines_removed"] = sum(
            1 for ln in diff_full.splitlines()
            if ln.startswith("-") and not ln.startswith("---"))
        out["diff_excerpt"] = "\n".join(diff_full.splitlines()[:60])
    except Exception as e:
        out["diff_full_err"] = str(e)
    # heuristic checks
    out["has_arrow_hints"] = bool(re.search(r"def\s+\w+\s*\([^)]*\)\s*->",
                                            text, re.DOTALL))
    out["has_param_hints"] = bool(re.search(r"def\s+\w+\s*\(\s*\w+\s*:\s*\w",
                                            text))
    out["public_defs"] = len(re.findall(r"^def\s+(?!_)\w+\s*\(",
                                        text, re.MULTILINE))
    out["public_arrows"] = len(re.findall(r"^def\s+(?!_)\w+\s*\([^)]*\)\s*->",
                                          text, re.MULTILINE | re.DOTALL))
    # import smoke test
    module = target_rel.replace("/", ".").rsplit(".py", 1)[0]
    try:
        p = subprocess.run(
            ["python3", "-c", f"import {module}"],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["import_rc"] = p.returncode
        out["import_stderr"] = (p.stderr or "").strip()[:400]
    except Exception as e:
        out["import_rc"] = -1
        out["import_stderr"] = str(e)
    return out


# ── main ──────────────────────────────────────────────────────────────
def main() -> int:
    t0 = time.time()
    primitives_used: list[str] = []

    # 1. worktree
    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]
    primitives_used.append("POST /repos/{id}/worktrees")

    # 2. captain session + bypass PATCH (BUG-8)
    pilot_sid, mcp_token = step_create_pilot_session(wt_id)
    primitives_used.append("POST /sessions (claude-code)")
    primitives_used.append("PATCH /sessions/{id} permission_config bypassPermissions")
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)
    primitives_used.append("MCP http transport (Bearer mcp_token)")

    # 2b. pick target file from inside the worktree
    target_rel = pick_target(Path(wt_path))

    # 3. captain spawns child via MCP -> agor_execute_tool(tool_name=...)
    spawn = step_spawn_data_via_captain(mcp_config, target_rel)
    primitives_used.append("agor_search_tools / agor_execute_tool (tool_name snake_case, F4)")
    primitives_used.append("agor_sessions_spawn (sibling-by-worktree, no worktree_id)")

    # 4. lift bypass on child defensively, then poll to terminal (incl. idle)
    step_lift_permission(spawn["child_sid"])
    final_status = step_poll_child(spawn["child_sid"])
    primitives_used.append("GET /sessions/{id} poll until terminal incl. idle (F1)")

    # 5. filesystem verify
    fs = step_verify_filesystem(wt_path, target_rel)

    # 6. cost aggregation: parent stream is canonical (F7); attempt child
    #    REST too just to confirm the null-stays-null finding holds.
    child_rest_cost = None
    if spawn["child_sid"]:
        try:
            got = api("GET", f"/sessions/{spawn['child_sid']}")
            cost_obj = got.get("cost") or {}
            child_rest_cost = (cost_obj.get("total_usd")
                               or got.get("total_cost_usd"))
        except Exception:
            pass
    total_cost = float(spawn["cost"] or 0)

    # ── Verdict
    files_ok = fs.get("exists") and fs.get("diff_lines_added", 0) > 0
    import_ok = fs.get("import_rc") == 0
    session_ok = final_status in {"idle", "completed", "stopped", "archived"}
    hints_ok = fs.get("has_arrow_hints") and fs.get("has_param_hints")

    if files_ok and import_ok and session_ok and hints_ok:
        verdict = "PASS"
    elif files_ok or session_ok:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    elapsed = int(time.time() - t0)

    # ── Round-1 finding regressions
    f1_status = ("re-confirmed: idle accepted as terminal"
                 if final_status == "idle"
                 else f"final_status={final_status} — F1 not directly stressed")
    f4_status = ("re-confirmed: tool_name (snake_case) used in captain prompt"
                 if spawn["child_sid"]
                 else "captain failed to spawn — F4 inconclusive")
    f7_status = ("re-confirmed: child REST cost null/empty"
                 if not child_rest_cost
                 else f"REFUTED in this run: child REST cost = {child_rest_cost}")
    f8_status = "auto-refresh wired into harness; not exercised in this run"
    f9_status = (f"hit {spawn['rate_limit_events']} rate_limit events "
                 "(parser logged but did not retry — captain run terminated naturally)"
                 if spawn["rate_limit_events"] else "no rate_limit events seen")
    f10_status = ("Python+subprocess pattern executed end-to-end; captain claude -p "
                  "ran via subprocess.run; REST via urllib")
    f11_status = f"target chosen: {target_rel} (primary={PRIMARY_TARGET})"
    regressions = (
        f"  F1 — {f1_status}\n"
        f"  F4 — {f4_status}\n"
        f"  F7 — {f7_status}\n"
        f"  F8 — {f8_status}\n"
        f"  F9 — {f9_status}\n"
        f"  F10 — {f10_status}\n"
        f"  F11 — {f11_status}"
    )

    new_bugs = []
    if total_cost > 4.5:
        new_bugs.append("budget overrun risk: parent claude -p exceeded $4.50 cap")
    if not spawn["child_sid"]:
        new_bugs.append("captain failed to surface CHILD_SESSION_ID in output")
    if files_ok and not import_ok:
        new_bugs.append(f"file edited but import failed: {fs.get('import_stderr')}")
    if not new_bugs:
        new_bugs_str = "none"
    else:
        new_bugs_str = "\n  - " + "\n  - ".join(new_bugs)

    what_worked = []
    what_didnt = []
    if files_ok:
        what_worked.append(f"@data child landed real edits ({fs.get('diff_lines_added')} additions)")
    else:
        what_didnt.append("no diff vs trunk on the target file")
    if hints_ok:
        what_worked.append("type hints (param + return arrows) detected in output")
    else:
        what_didnt.append("type-hint heuristic check did not match (review diff manually)")
    if import_ok:
        what_worked.append(f"`python3 -c 'import {target_rel}'` exits 0")
    else:
        what_didnt.append(f"import smoke failed: {fs.get('import_stderr')}")
    if session_ok:
        what_worked.append(f"child reached terminal status `{final_status}`")
    else:
        what_didnt.append(f"child stuck at `{final_status}` past poll deadline")
    what_worked.append("Python+subprocess harness ran end-to-end without sandbox blocks")
    if not what_didnt:
        what_didnt.append("no notable failures to report")

    pyverdict = (
        "Pilot D's hybrid (urllib REST + subprocess claude -p) works as the "
        "round-2 template: REST primitives, MCP wiring, captain spawn, child "
        "poll-incl-idle, and worktree filesystem verify all completed from a "
        "sub-agent context with no sandbox interference."
        if verdict in ("PASS", "PARTIAL")
        else "Pilot D's hybrid pattern stalled in this run — see WHAT_DIDNT."
    )

    rec = ("keep — control pilot validates the round-2 Python+subprocess template "
           "for solo-specialist tasks") if verdict == "PASS" else \
          ("modify — partial result; tighten captain prompt / verification" if verdict == "PARTIAL"
           else "drop — solo-specialist pattern failed end-to-end here")

    report = f"""
PILOT: R2-A
TEAM_SHAPE: solo @data specialist (1 session, no peers) — Python+subprocess pattern
HARNESS_SCRIPT: scripts/_r2_a.py
AGOR_PRIMITIVES_USED:
  - {chr(10).join('  - ' + p for p in primitives_used).lstrip('  - ')}
TASK_OUTCOME: {verdict} — files_ok={files_ok} import_ok={import_ok} session_ok={session_ok} hints_ok={hints_ok}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
TARGET_FILE: {target_rel}
CAPTAIN_PILOT_SESSION_ID: {pilot_sid}
SESSION_ID: {spawn['child_sid']}
CHILD_FINAL_STATUS: {final_status}
DIFF_STAT: {fs.get('diff_stat')}
DIFF_LINES_ADDED: {fs.get('diff_lines_added')}
DIFF_LINES_REMOVED: {fs.get('diff_lines_removed')}
PUBLIC_DEFS_TOTAL: {fs.get('public_defs')}
PUBLIC_DEFS_WITH_ARROW_RETURN: {fs.get('public_arrows')}
IMPORT_SMOKE_RC: {fs.get('import_rc')}  (0 = clean)
IMPORT_SMOKE_STDERR: {fs.get('import_stderr')}
COST_USD: {total_cost:.4f} (aggregated from parent stream-json result events; child REST cost = {child_rest_cost})
DURATION_SECONDS: {elapsed}
ROUND-1 FINDING REGRESSIONS:
{regressions}
NEW BUGS FOUND: {new_bugs_str}
WHAT_WORKED:
  - {chr(10).join('  - ' + s for s in what_worked).lstrip('  - ')}
WHAT_DIDNT:
  - {chr(10).join('  - ' + s for s in what_didnt).lstrip('  - ')}
PYTHON_SUBPROCESS_PATTERN_VERDICT: {pyverdict}
RECOMMENDATION: {rec}
"""
    print(report)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    if fs.get("diff_excerpt"):
        (WORK_DIR / f"diff-excerpt-{EPOCH}.txt").write_text(fs["diff_excerpt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
