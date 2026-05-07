#!/usr/bin/env python3
"""Round-8 — TWO-PHASE @picard prototype.

Splits the failure mode out of the LLM:
  PHASE 1 (LLM plans): direct HTTPS call to FOSS captain model. Output is
                       structured JSON. NO tool use required — model just
                       writes text.
  PHASE 2 (harness executes): deterministic Python parses the plan + issues
                              the Agor MCP spawns from outside the LLM.

This sidesteps the round-6 finding that opencode-FOSS sessions don't
issue MCP tool_use blocks. @picard plans (text only); harness orchestrates.

Prototype scope: 1 task (B1), 1 captain model (qwen30 via direct OpenRouter),
1 specialist model (qwen30 via opencode). If it works, we expand.
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
ARTIFACTS = REPO / "scripts/_round8_two_phase_proto_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"
PRIMER_FILE = REPO / "docs/wiki/picard-agor-primer.org"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 600

CAPTAIN_MODEL_ID = "qwen/qwen3-coder-30b-a3b-instruct"  # @picard via direct OR
SPECIALIST_MODEL = {"provider": "openrouter",
                     "model": "qwen/qwen3-coder-30b-a3b-instruct"}

TASK = {
    "id": "B1",
    "label": "cross-link orphan audit on literate-tools.org",
    "target_file": "docs/wiki/literate-tools.org",
    "goal": ("Audit `docs/wiki/literate-tools.org` (324 lines) for "
              "outgoing-cross-link opportunities. Add EXACTLY 5 "
              "`[[id:UUID][label]]` cross-link wrappers around existing "
              "prose mentions of canonical wiki concepts. Preserve `=...=` "
              "verbatim formatting INSIDE link labels. Apply edits to the "
              "worktree's copy of that file. Do NOT touch any other file."),
    "success_criterion": ("5 cross-link insertions; all UUIDs resolve to "
                          "existing org-roam entries in docs/wiki/*.org; "
                          "no broken syntax."),
}


def load_primer() -> str:
    text = PRIMER_FILE.read_text()
    text = re.sub(r"^:PROPERTIES:.*?:END:\s*", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"^#\+\w+:.*$\n", "", text, flags=re.MULTILINE)
    return text.strip()


PRIMER = load_primer()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(line + "\n")


# ── Phase 1: direct OpenRouter call to get a plan ──────────────────────────
PLAN_FORMAT_INSTRUCTIONS = """
You are @picard executing PHASE 1 of a two-phase architecture. Your only job
in this phase is to PRODUCE A STRUCTURED PLAN as JSON. You do NOT execute
any tool calls; the harness will execute your plan deterministically.

Output exactly this shape (no other text, no markdown fences, no commentary
before or after):

PICARD_PLAN_JSON_BEGIN
{
  "classification": {"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"},
  "playbook": "<flat|A|B|C|D|E>",
  "team": [
    {"handle": "@<bridge-crew-handle>",
     "task_brief": "<full multi-line task brief for this specialist — be specific; include success criterion>"}
  ],
  "rationale": "<one short paragraph explaining your choice>"
}
PICARD_PLAN_JSON_END
"""


def call_openrouter(model_id: str, prompt: str) -> tuple[str, float]:
    """Direct OpenRouter call. Returns (assistant_text, cost_usd)."""
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2500,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    log(f"  POST OpenRouter ({model_id})")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    text = data["choices"][0]["message"]["content"]
    cost = float(data.get("usage", {}).get("cost") or 0)
    log(f"  OpenRouter rc=ok elapsed={elapsed:.1f}s cost=${cost:.4f}")
    (ARTIFACTS / "phase1_raw_response.txt").write_text(text)
    return text, cost


def parse_plan(text: str) -> dict | None:
    """Extract JSON between PICARD_PLAN_JSON_BEGIN / _END markers."""
    m = re.search(r"PICARD_PLAN_JSON_BEGIN\s*(.+?)\s*PICARD_PLAN_JSON_END",
                   text, re.DOTALL)
    if not m:
        # Fallback: try first JSON object in the text
        m2 = re.search(r"(\{.+\})", text, re.DOTALL)
        if not m2:
            log("  parse_plan: no JSON delimiters found")
            return None
        json_text = m2.group(1)
    else:
        json_text = m.group(1).strip()
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        log(f"  parse_plan: JSONDecodeError: {e}")
        log(f"  raw JSON text: {json_text[:600]}")
        return None


# ── Phase 2: deterministic Agor execution ──────────────────────────────────
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


def poll_terminal(sid: str, deadline: int = 600) -> str:
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


def harness_spawn_specialist(wt_id: str, wt_name: str, handle: str,
                              task_brief: str) -> tuple[str | None, str]:
    """Phase 2: spawn ONE specialist via Agor REST + MCP, deterministically.

    No LLM in the loop here — pure Python issues the agor_sessions_spawn
    via a small `claude -p` subprocess as the MCP client (we still need
    SOMEONE to call MCP; Claude-Code is the MCP client we trust to fire
    tool_use correctly).
    """
    # Create captain pilot session (claude-code) to act as the MCP client
    cap = req("POST", "/sessions",
                {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    cap_id = cap["session_id"]
    req("PATCH", f"/sessions/{cap_id}",
          {"permission_config": {"mode": "bypassPermissions"}})
    mcp_token = cap.get("mcp_token")
    mcp_cfg = ARTIFACTS / f"mcp-{handle.lstrip('@')}-{EPOCH}.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))

    # Persona text — minimal; harness controls the brief
    persona_lookup = {
        "@atoz": "You are @atoz — Bridge Crew wiki concept-graph specialist.",
        "@data": "You are @data — Bridge Crew code + scribe specialist.",
        "@spock": "You are @spock — Bridge Crew logic + canonical-source reviewer.",
        "@geordi": "You are @geordi — Bridge Crew analytics + charts specialist.",
        "@boothby": "You are @boothby — Bridge Crew ops + hygiene specialist.",
        "@riker": "You are @riker — Bridge Crew process + scheduling specialist.",
    }
    persona = persona_lookup.get(handle, f"You are {handle}, a Bridge Crew specialist.")

    spawn_args = {
        "prompt": (persona + "\n\n" + task_brief
                    + f"\n\nWorktree path: /home/daniel/.agor/worktrees/local/org-llm/{wt_name}\n"
                    + "When done, run `git add` + `git commit` in your worktree, "
                    + f"then print `{handle.upper().lstrip('@')}_DONE` and stop."),
        "title": f"r8-twophase-{handle.lstrip('@')}",
        "agenticTool": "opencode",
        "modelConfig": SPECIALIST_MODEL,
    }
    cap_prompt = f"""You have one MCP server "agor". Spawn ONE specialist via:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(spawn_args)}

After spawn returns, print SPAWN_RESULT_SID=<sid> on its own line, then SPAWN_DONE."""
    out = ARTIFACTS / f"captain-{handle.lstrip('@')}-{EPOCH}.jsonl"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", "0.30",
            cap_prompt]
    t0 = time.time()
    with out.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                             timeout=PER_RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  captain TIMEOUT for {handle}")
    elapsed = time.time() - t0

    spec_sid = None
    for line in out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        # Check both result.result AND last assistant text (round-6 lesson)
        if ev.get("type") == "result":
            text = ev.get("result", "") or ""
            m = re.search(r"SPAWN_RESULT_SID=([a-f0-9-]{36})", text)
            if m: spec_sid = m.group(1)
        elif ev.get("type") == "assistant":
            for blk in (ev.get("message", {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    m = re.search(r"SPAWN_RESULT_SID=([a-f0-9-]{36})",
                                   blk.get("text", "") or "")
                    if m and not spec_sid: spec_sid = m.group(1)
    log(f"  spawned {handle} sid={spec_sid} captain_elapsed={elapsed:.1f}s")
    if spec_sid:
        try:
            req("PATCH", f"/sessions/{spec_sid}",
                  {"permission_config": {"mode": "bypassPermissions"}})
        except Exception as e:
            log(f"  bypass-PATCH failed: {e}")
    return spec_sid, "spawned" if spec_sid else "failed-to-spawn"


# ── Main ───────────────────────────────────────────────────────────────────
log(f"Round-8 two-phase prototype :: task={TASK['id']} :: captain={CAPTAIN_MODEL_ID}")

# PHASE 1: get the plan from @picard via direct OpenRouter
log("PHASE 1: requesting plan from @picard (direct OpenRouter call)...")
phase1_prompt = f"""{PRIMER}

---

TASK ({TASK['id']} — {TASK['label']}):
{TASK['goal']}

SUCCESS CRITERION:
{TASK['success_criterion']}

PRODUCTION CONSTRAINT (per primer SOP-2): specialists will spawn with this exact modelConfig:
  agenticTool: "opencode"
  modelConfig: {json.dumps(SPECIALIST_MODEL)}

---

{PLAN_FORMAT_INSTRUCTIONS}
"""
plan_text, p1_cost = call_openrouter(CAPTAIN_MODEL_ID, phase1_prompt)
plan = parse_plan(plan_text)
if plan is None:
    log("PHASE 1 FAILED — could not parse plan. Raw response in phase1_raw_response.txt")
    log(f"first 800 chars of response: {plan_text[:800]}")
    sys.exit(1)
(ARTIFACTS / "phase1_plan.json").write_text(json.dumps(plan, indent=2))
log(f"PHASE 1 ok: classification={plan.get('classification')} "
    f"playbook={plan.get('playbook')} team_size={len(plan.get('team', []))}")
for spec in plan.get("team", []):
    log(f"  - {spec.get('handle')}: {(spec.get('task_brief') or '')[:80]}...")

# PHASE 2: harness executes the plan
log("\nPHASE 2: harness executes the plan deterministically...")
relogin()
opencode_proc = ensure_opencode_serve()

specialist_results = []
diff_full = ""
diff_stat = ""
log_oneline = ""
wt_path = None
try:
    # Create worktree
    wt_name = f"r8-twophase-{TASK['id']}-{EPOCH}"
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt_name, "ref": wt_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt_id = wt["worktree_id"]
    wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
    log(f"worktree {wt_id[:18]} ({wt_name})")
    time.sleep(2)

    # Spawn each specialist
    for spec in plan.get("team", []):
        handle = spec.get("handle", "@unknown")
        brief = spec.get("task_brief", "")
        if not brief:
            log(f"  WARN: {handle} has empty task_brief; skipping")
            continue
        sid, status = harness_spawn_specialist(wt_id, wt_name, handle, brief)
        specialist_results.append({"handle": handle, "sid": sid, "spawn_status": status})

    # Poll all spawned specialists
    for sr in specialist_results:
        if sr["sid"]:
            t = poll_terminal(sr["sid"])
            sr["terminal_status"] = t
            log(f"  {sr['handle']} → {t}")

    # Capture diff
    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    log_oneline = subprocess.run(["git", "-C", str(wt_path), "log", "--oneline", "-2"],
                                   capture_output=True, text=True).stdout.strip()
    (ARTIFACTS / "phase2_diff.patch").write_text(diff_full)
finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()

# Report
print()
print("=" * 80)
print("ROUND-8 TWO-PHASE PROTOTYPE — REPORT")
print("=" * 80)
print(f"PHASE 1 (plan): @picard via direct OpenRouter ({CAPTAIN_MODEL_ID})")
print(f"  cost: ${p1_cost:.4f}")
print(f"  plan classification: {plan.get('classification')}")
print(f"  plan playbook: {plan.get('playbook')}")
print(f"  plan team size: {len(plan.get('team', []))}")
print()
print(f"PHASE 2 (execute): harness-driven Agor spawns")
for sr in specialist_results:
    print(f"  {sr['handle']:>10} sid={sr.get('sid','-')[:18] if sr.get('sid') else 'failed'} "
          f"terminal={sr.get('terminal_status','-')}")
print()
print(f"WORKTREE: {wt_path}")
print(f"DIFF: {diff_stat or '(none)'}")
print(f"COMMIT: {log_oneline or '(none)'}")

(ARTIFACTS / f"summary-{EPOCH}.json").write_text(json.dumps({
    "phase1_cost_usd": p1_cost,
    "plan": plan,
    "phase2_specialists": specialist_results,
    "diff_stat": diff_stat,
    "log_oneline": log_oneline,
    "worktree": str(wt_path) if wt_path else None,
}, indent=2))

print(f"\nartifacts: {ARTIFACTS}")
