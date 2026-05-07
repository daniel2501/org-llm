#!/usr/bin/env python3
"""Round-6 — primed @picard benchmark starter (3 tasks × 5 conditions).

Same shape as `_benchmark_starter.py` but @picard's system prompt now
includes `docs/wiki/picard-agor-primer.org` (drafted 2026-05-07).
The primer addresses the round-5 starter bug: @picard treated
PICARD_DONE as session-end and never issued spawn calls.

Also fixes the parser bug in round-5: PICARD_SESSION_ID extraction
now uses regex robust to surrounding backticks.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
ARTIFACTS = REPO / "scripts/_round6_primed_picard_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"
PRIMER_FILE = REPO / "docs/wiki/picard-agor-primer.org"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 600
POLL_DEADLINE = 600

# Same tasks as round-5 starter for direct comparison
TASKS = [
    {
        "id": "B1",
        "label": "cross-link orphan audit on literate-tools.org",
        "target_file": "docs/wiki/literate-tools.org",
        "goal": ("Audit `docs/wiki/literate-tools.org` (324 lines) for "
                  "outgoing-cross-link opportunities. Add EXACTLY 5 "
                  "`[[id:UUID][label]]` cross-link wrappers around existing "
                  "prose mentions of canonical wiki concepts. Preserve "
                  "`=...=` verbatim formatting INSIDE link labels. Apply "
                  "edits to the worktree's copy of that file. Do NOT touch "
                  "any other file."),
        "success_criterion": ("5 cross-link insertions; all UUIDs resolve to "
                              "existing org-roam entries in docs/wiki/*.org; "
                              "no broken syntax."),
    },
    {
        "id": "B7",
        "label": "add docstrings + type hints to a small Python module",
        "target_file": "org_llm/avatars.py",  # Tracked-on-trunk fallback
        "goal": ("Add Python type hints + one-line docstrings to every "
                  "public function/class in `org_llm/avatars.py` (~100 lines, "
                  "tracked on trunk). Existing behavior must not change. "
                  "Target: well-typed, documented file; "
                  "`python -c 'import org_llm.avatars'` imports cleanly. Do "
                  "NOT touch any other file."),
        "success_criterion": ("file has type hints on every public def/class; "
                              "every public def has a one-line docstring; "
                              "import smoke passes."),
    },
    {
        "id": "B13",
        "label": "update LICENSE copyright year",
        "target_file": "LICENSE",
        "goal": ("Update the copyright year in `LICENSE` from `2025` to "
                  "`2026`. Single-file, single-line edit. If the year is "
                  "already 2026, no edit needed — emit a clean "
                  "no-op-with-explanation."),
        "success_criterion": ("LICENSE shows copyright year 2026 (or already "
                              "2026 with explanation); no other files "
                              "touched."),
    },
]

VARIANTS = [
    ("Claude-solo", None),
    ("F1-qwen30", {"provider": "openrouter",
                   "model": "qwen/qwen3-coder-30b-a3b-instruct"}),
    ("F2-llama70", {"provider": "openrouter",
                     "model": "meta-llama/llama-3.3-70b-instruct"}),
    ("F3-deepseekR1", {"provider": "openrouter",
                        "model": "deepseek/deepseek-r1"}),
    ("F4-gptoss120b", {"provider": "openrouter",
                        "model": "openai/gpt-oss-120b"}),
]


def load_primer() -> str:
    """Load the primer + strip org-mode property header for prompt injection."""
    text = PRIMER_FILE.read_text()
    # Strip :PROPERTIES: ... :END: block at top
    text = re.sub(r"^:PROPERTIES:.*?:END:\s*", "", text, count=1, flags=re.DOTALL)
    # Strip top-level org-export keywords
    text = re.sub(r"^#\+\w+:.*$\n", "", text, flags=re.MULTILINE)
    return text.strip()


PRIMER = load_primer()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(line + "\n")


def tok() -> str:
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin() -> None:
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                    capture_output=True, env=env, check=True)


def req(method: str, path: str, body: dict | None = None, retries: int = 1) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(BASE + path, data=data, method=method,
                headers={"Authorization": f"Bearer {tok()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries:
                log("  401 — re-login + retry")
                relogin()
                continue
            raise


def ensure_opencode_serve() -> subprocess.Popen | None:
    try:
        urllib.request.urlopen("http://localhost:4096/", timeout=2)
        return None
    except Exception:
        pass
    log("starting opencode serve...")
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                             capture_output=True, text=True, check=True).stdout.strip()
    env["OPENROUTER_API_KEY"] = or_key
    serve_log = ARTIFACTS / f"opencode-serve-{EPOCH}.log"
    proc = subprocess.Popen(["opencode", "serve", "--port", "4096"],
                             stdout=serve_log.open("w"),
                             stderr=subprocess.STDOUT, env=env)
    time.sleep(3)
    return proc


def poll_terminal(sid: str, deadline: int = POLL_DEADLINE) -> str:
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    end = time.time() + deadline
    while time.time() < end:
        try:
            s = req("GET", f"/sessions/{sid}", retries=1)
            if s.get("status") in terminal:
                return s.get("status")
        except Exception:
            pass
        time.sleep(15)
    return "timeout"


def run_claude_solo(task: dict, run_dir: Path) -> dict:
    wt_name = f"r6-{task['id']}-claude-solo-{EPOCH}"
    wt_path = REPO.parent / "org-llm-worktrees" / wt_name
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-b", wt_name,
                     str(wt_path), "trunk"], check=True, capture_output=True)
    log(f"  Claude-solo worktree at {wt_path.name}")
    prompt = f"""You are a software/wiki specialist working in this org-llm worktree at {wt_path}.

TASK ({task['id']} — {task['label']}):
{task['goal']}

SUCCESS CRITERION:
{task['success_criterion']}

When done, run `git add` + `git commit` with an appropriate message in the worktree, then print `TASK_DONE` and stop. If the work is a no-op (e.g. file already correct), explain in one line + commit nothing + print `TASK_DONE`.
"""
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    out = run_dir / "stream.jsonl"
    cmd = ["claude", "-p", "--model", "sonnet",
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions",
           "--max-budget-usd", "0.50", prompt]
    t0 = time.time()
    with out.open("w") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                              cwd=str(wt_path), timeout=PER_RUN_TIMEOUT).returncode
    elapsed = time.time() - t0
    cost = 0.0
    for line in out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            cost += float(ev.get("total_cost_usd") or 0)
    diff = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                            capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    log_oneline = subprocess.run(["git", "-C", str(wt_path), "log", "--oneline", "-2"],
                                   capture_output=True, text=True).stdout.strip()
    (run_dir / "diff.patch").write_text(diff)
    return {"condition": "Claude-solo", "wt": str(wt_path), "rc": rc,
             "cost_usd": cost, "wall_seconds": round(elapsed, 1),
             "diff_stat": diff_stat, "log": log_oneline}


def run_foss_agor(task: dict, variant_name: str, model_config: dict,
                   run_dir: Path) -> dict:
    wt_name = f"r6-{task['id']}-{variant_name}-{EPOCH}"
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt_name, "ref": wt_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt_id = wt["worktree_id"]
    wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
    log(f"  {variant_name} wt={wt_id[:18]}")
    time.sleep(2)

    cap = req("POST", "/sessions",
                {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    cap_id = cap["session_id"]
    req("PATCH", f"/sessions/{cap_id}",
          {"permission_config": {"mode": "bypassPermissions"}})
    mcp_token = cap.get("mcp_token")
    mcp_cfg = run_dir / "mcp.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))

    # @picard prompt = primer (system layer) + task brief
    picard_task = f"""TASK ({task['id']} — {task['label']}):
{task['goal']}

SUCCESS CRITERION:
{task['success_criterion']}

PRODUCTION CONSTRAINT (per primer SOP-2): spawn specialists with this exact modelConfig:
  agenticTool: "opencode"
  modelConfig: {json.dumps(model_config)}

Worktree path (specialists will work here): /home/daniel/.agor/worktrees/local/org-llm/{wt_name}

Now execute your full @picard cycle (assess → pick → form → spawn → supervise → integrate). Remember: PICARD_DONE is a marker, not a session-terminator. After PICARD_DONE, IMMEDIATELY issue the actual mcp__agor__agor_execute_tool spawn call(s) per primer SOP-2. Then poll specialist(s) per SOP-4. When the team's deliverable lands clean, print TASK_DONE and stop.
"""
    picard_full_prompt = PRIMER + "\n\n---\n\n" + picard_task
    picard_spawn_args = {
        "prompt": picard_full_prompt,
        "title": f"r6-{task['id']}-{variant_name}-picard",
        "agenticTool": "opencode",
        "modelConfig": {"provider": "openrouter",
                         "model": "qwen/qwen3-coder-30b-a3b-instruct"},
    }
    captain_prompt = f"""You have one MCP server "agor". Spawn @picard via:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(picard_spawn_args)}

After spawn returns, print PICARD_SESSION_ID=<sid> on its own line (no backticks, no markdown), then SPAWN_DONE on the next line, then stop."""
    parent_out = run_dir / "captain.jsonl"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", "0.20", captain_prompt]
    t0 = time.time()
    with parent_out.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                             timeout=PER_RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  {variant_name} captain TIMEOUT")

    captain_cost = 0.0
    picard_sid = None
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            captain_cost += float(ev.get("total_cost_usd") or 0)
            # Robust parser: extract UUID even if surrounded by backticks/markdown
            m = re.search(r"PICARD_SESSION_ID=([a-f0-9-]{36})",
                           ev.get("result", "") or "")
            if m:
                picard_sid = m.group(1)

    if picard_sid:
        try:
            req("PATCH", f"/sessions/{picard_sid}",
                  {"permission_config": {"mode": "bypassPermissions"}})
        except Exception as e:
            log(f"  bypass-PATCH on @picard failed: {e}")
        picard_status = poll_terminal(picard_sid, deadline=PER_RUN_TIMEOUT)
    else:
        picard_status = "no-spawn"

    elapsed = time.time() - t0
    diff = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                            capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    log_oneline = subprocess.run(["git", "-C", str(wt_path), "log", "--oneline", "-2"],
                                   capture_output=True, text=True).stdout.strip()
    (run_dir / "diff.patch").write_text(diff)

    # Capture @picard's actual messages for hand review
    picard_messages = []
    if picard_sid:
        try:
            msgs = req("GET", f"/messages?session_id={picard_sid}", retries=1)
            picard_messages = msgs.get("data", [])
            (run_dir / "picard_messages.json").write_text(json.dumps(picard_messages, indent=2))
        except Exception as e:
            log(f"  fetch @picard messages failed: {e}")

    # Count @picard's children (did it actually spawn anything?)
    picard_children = []
    if picard_sid:
        try:
            s = req("GET", f"/sessions/{picard_sid}", retries=1)
            picard_children = (s.get("genealogy") or {}).get("children") or []
        except Exception:
            pass

    return {"condition": variant_name, "wt": str(wt_path),
             "captain_pilot_sid": cap_id, "picard_sid": picard_sid,
             "picard_status": picard_status,
             "picard_children_count": len(picard_children),
             "picard_children": picard_children,
             "captain_cost_usd": round(captain_cost, 4),
             "wall_seconds": round(elapsed, 1),
             "diff_stat": diff_stat, "log": log_oneline}


# ── Main loop ────────────────────────────────────────────────────────────
log(f"Round-6 primed @picard: primer {len(PRIMER):,} chars; "
    f"{len(TASKS)} tasks × {len(VARIANTS)} conditions × 1 trial")
relogin()
opencode_proc = ensure_opencode_serve()

results = {}
try:
    for task in TASKS:
        log(f"\n=== TASK {task['id']} ({task['label']}) ===")
        task_dir = ARTIFACTS / task['id']
        task_dir.mkdir(exist_ok=True)
        results[task['id']] = {}
        for variant_name, model_config in VARIANTS:
            log(f" -- {variant_name}")
            run_dir = task_dir / variant_name
            run_dir.mkdir(exist_ok=True)
            try:
                if model_config is None:
                    r = run_claude_solo(task, run_dir)
                else:
                    r = run_foss_agor(task, variant_name, model_config, run_dir)
                results[task['id']][variant_name] = r
                cost = r.get("cost_usd", r.get("captain_cost_usd", 0))
                children = r.get("picard_children_count", "n/a")
                log(f"    diff: {r.get('diff_stat') or '(none)'} "
                    f"cost=${cost:.4f} wall={r.get('wall_seconds')}s "
                    f"children={children}")
            except Exception as e:
                log(f"    ERROR: {e}")
                results[task['id']][variant_name] = {"error": str(e)}

    summary = ARTIFACTS / f"summary-{EPOCH}.json"
    summary.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 90)
    print("ROUND-6 PRIMED @PICARD — STARTER SUMMARY")
    print("=" * 90)
    for tid, conds in results.items():
        print(f"\n{tid}:")
        for cn, r in conds.items():
            if "error" in r:
                print(f"  {cn:18} ERROR: {r['error'][:60]}")
                continue
            cost = r.get("cost_usd", r.get("captain_cost_usd", 0))
            children = r.get("picard_children_count", "—")
            picard_status = r.get("picard_status", "—")
            print(f"  {cn:18} ${cost:>5.3f} {r.get('wall_seconds', 0):>5}s "
                  f"children={str(children):>3} picard={str(picard_status):>10} "
                  f"{(r.get('diff_stat') or '(none)')[:40]}")
    print(f"\nsummary: {summary}")
    print(f"diffs: {ARTIFACTS}/<task>/<variant>/diff.patch")
    print(f"@picard messages: {ARTIFACTS}/<task>/<variant>/picard_messages.json")
finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
