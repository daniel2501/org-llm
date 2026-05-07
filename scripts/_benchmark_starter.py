#!/usr/bin/env python3
"""Benchmark starter — 3 tasks × 6 conditions × 1 trial = 18 runs.

Validates the binding-@picard harness before expanding to the full 15-task
benchmark. Per user 2026-05-07: '@picard binding, no guidance' — @picard
assesses each task on Tier-1 axes, picks a playbook, forms a team, and
spawns specialists. Human pilot drives top-level execution but does not
pre-screen @picard's plans.

Conditions per task:
  - Claude-solo (single `claude -p`, no Agor)
  - F1: opencode + qwen3-coder-30b
  - F2: opencode + llama-3.3-70b
  - F3: opencode + deepseek-r1
  - F4: opencode + gpt-oss-120b
  - F5: heterogeneous (qwen30 scanner + llama70 author + deepseek-r1 reviewer)

For all FOSS variants, @picard is always qwen30 (production default).
The variant model applies to specialists @picard spawns.
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
ARTIFACTS = REPO / "scripts/_benchmark_starter_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 600
POLL_DEADLINE = 600

# ── Task definitions ─────────────────────────────────────────────────────
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
        "tier1_hint": "pattern-match, recurring, low",
        "tier2_hint": "text, single-file",
    },
    {
        "id": "B7",
        "label": "add docstrings + type hints to org_llm/notices.py",
        "target_file": "org_llm/notices.py",
        "goal": ("Add Python type hints + one-line docstrings to every "
                  "public function/class in `org_llm/notices.py` (~61 lines). "
                  "Existing behavior must not change. Target: well-typed, "
                  "documented file; `python -c 'import org_llm.notices'` "
                  "imports cleanly. **NOTE**: this file is currently "
                  "UNTRACKED on trunk — fall back to `org_llm/avatars.py` "
                  "or `org_llm/sidecar.py` if absent in the worktree."),
        "success_criterion": ("file has type hints on every public def/class; "
                              "every public def has a one-line docstring; "
                              "import smoke passes."),
        "tier1_hint": "pattern-match, recurring, low",
        "tier2_hint": "code, single-file",
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
        "tier1_hint": "mechanical, one-off, low",
        "tier2_hint": "code, single-file",
    },
]

# ── Variants ─────────────────────────────────────────────────────────────
VARIANTS = [
    ("Claude-solo", None),  # Special: no Agor
    ("F1-qwen30", {"provider": "openrouter",
                   "model": "qwen/qwen3-coder-30b-a3b-instruct"}),
    ("F2-llama70", {"provider": "openrouter",
                     "model": "meta-llama/llama-3.3-70b-instruct"}),
    ("F3-deepseekR1", {"provider": "openrouter",
                        "model": "deepseek/deepseek-r1"}),
    ("F4-gptoss120b", {"provider": "openrouter",
                        "model": "openai/gpt-oss-120b"}),
    # F5 mix is more complex (different model per role); skip in starter,
    # add after starter validates the harness.
]

# ── Picard's binding system prompt ───────────────────────────────────────
PICARD_SYS = """You are @picard — Bridge Crew captain. Your job: assess the task on Tier-1 axes, pick a playbook, form a team, and spawn specialists. You do NOT do specialist work yourself. You are FAST: pre-work coordination must complete in under 60 seconds.

TIER-1 AXES (assess every task on these):
  Judgment: mechanical | pattern-match | nuanced | open-ended
  Recurrence: one-off | recurring
  Stakes: low | high

PLAYBOOK TABLE:
  flat       — Trivial / single-call task → 1 specialist, no team
  A          — Surgical specialist division (multiple narrow specialists, sequential)
  B          — Pre-compute heavy (you do most deterministic work; small specialist team)
  C          — Generate + vote (N parallel specialists, deterministic vote)
  D          — Iterative correction (FOSS draft + Claude-baseline check; LABEL BASELINE ARM)
  E          — Self-coded tool path (recognize the recurring shape; defer LLM, build a tool)

TEAM FORMATION:
  Available Bridge Crew personas: @atoz (concept-graph + wiki link), @data (code + scribe),
  @spock (logic + canonical-source review), @geordi (analytics + charts), @boothby (ops +
  hygiene), @riker (process + scheduling). You can spawn 0-N of any. If a needed specialist
  doesn't exist as a Bridge Crew member, design an EPHEMERAL custom agent for the task.

PRODUCTION RULE: spawned specialists MUST run on the FOSS modelConfig provided to you in
the task brief. You yourself run on qwen30 (production default).

OUTPUT SHAPE (always print exactly this, then stop):
PICARD_PLAN:
  classification: judgment=<level>, recurrence=<level>, stakes=<level>
  playbook: <flat|A|B|C|D|E>
  team: <comma-separated list of specialists with brief role labels>
  spawn_actions: <one line per specialist: agent_handle | task_brief_summary>
PICARD_DONE
"""

# ── Helpers ──────────────────────────────────────────────────────────────
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


# ── Run conditions ───────────────────────────────────────────────────────
def run_claude_solo(task: dict, run_dir: Path) -> dict:
    """Single `claude -p` does the task. No Agor."""
    wt_name = f"bench-{task['id']}-claude-solo-{EPOCH}"
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
    """Spawn @picard captain via Agor; @picard plans + spawns crew on FOSS variant."""
    wt_name = f"bench-{task['id']}-{variant_name}-{EPOCH}"
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

    # Spawn @picard (qwen30 captain, binding) — it assesses + plans + spawns crew
    picard_task = f"""TASK ({task['id']} — {task['label']}):
{task['goal']}

SUCCESS CRITERION:
{task['success_criterion']}

PRODUCTION CONSTRAINT: spawn specialists with this exact modelConfig:
  agenticTool: "opencode"
  modelConfig: {json.dumps(model_config)}

Your output (PICARD_PLAN ... PICARD_DONE) is your handoff. After PICARD_DONE, also issue the actual mcp__agor__agor_execute_tool calls to spawn the specialists you planned. Then poll their sessions to terminal status (idle is terminal). When the team is done and the worktree has the expected diff, print TASK_DONE and stop.
"""
    picard_spawn_args = {
        "prompt": PICARD_SYS + "\n\n" + picard_task,
        "title": f"bench-{task['id']}-{variant_name}-picard",
        "agenticTool": "opencode",
        "modelConfig": {"provider": "openrouter",
                         "model": "qwen/qwen3-coder-30b-a3b-instruct"},
    }
    captain_prompt = f"""You have one MCP server "agor". Spawn @picard via:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(picard_spawn_args)}

After spawn returns, print `PICARD_SESSION_ID=<sid>` and `SPAWN_DONE`. Stop."""
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
    captain_elapsed = time.time() - t0

    captain_cost = 0.0
    picard_sid = None
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            captain_cost += float(ev.get("total_cost_usd") or 0)
            for ln in (ev.get("result", "") or "").splitlines():
                if ln.startswith("PICARD_SESSION_ID="):
                    picard_sid = ln.split("=", 1)[1].strip()

    if picard_sid:
        req("PATCH", f"/sessions/{picard_sid}",
              {"permission_config": {"mode": "bypassPermissions"}})
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

    return {"condition": variant_name, "wt": str(wt_path),
             "captain_pilot_sid": cap_id, "picard_sid": picard_sid,
             "picard_status": picard_status, "captain_cost_usd": captain_cost,
             "wall_seconds": round(elapsed, 1),
             "diff_stat": diff_stat, "log": log_oneline}


# ── Main loop ────────────────────────────────────────────────────────────
log(f"Benchmark starter: {len(TASKS)} tasks × {len(VARIANTS)} conditions × 1 trial")
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
            log(f" -- condition {variant_name}")
            run_dir = task_dir / variant_name
            run_dir.mkdir(exist_ok=True)
            try:
                if model_config is None:
                    r = run_claude_solo(task, run_dir)
                else:
                    r = run_foss_agor(task, variant_name, model_config, run_dir)
                results[task['id']][variant_name] = r
                log(f"    diff: {r.get('diff_stat') or '(none)'} "
                    f"cost=${r.get('cost_usd', r.get('captain_cost_usd', 0)):.4f} "
                    f"wall={r.get('wall_seconds')}s")
            except Exception as e:
                log(f"    ERROR: {e}")
                results[task['id']][variant_name] = {"error": str(e)}

    summary = ARTIFACTS / f"summary-{EPOCH}.json"
    summary.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 80)
    print("BENCHMARK STARTER SUMMARY")
    print("=" * 80)
    for tid, conds in results.items():
        print(f"\n{tid}:")
        for cn, r in conds.items():
            if "error" in r:
                print(f"  {cn:20} ERROR: {r['error'][:60]}")
                continue
            cost = r.get("cost_usd", r.get("captain_cost_usd", 0))
            print(f"  {cn:20} ${cost:>5.3f} {r.get('wall_seconds', 0):>4}s "
                  f"{(r.get('diff_stat') or '(none)')[:50]}")
    print(f"\nsummary: {summary}")
finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
