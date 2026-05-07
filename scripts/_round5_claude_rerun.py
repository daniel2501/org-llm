#!/usr/bin/env python3
"""Re-run round-5 matrix Claude row only — using agentic_tool=claude-code.

Per user 2026-05-07: don't use ANTHROPIC_API_KEY in env; use the
already-authenticated `claude` CLI directly. Spawn shape:
    agentic_tool="claude-code"   (no modelConfig — uses claude CLI's default)
NOT
    agentic_tool="opencode" + modelConfig={provider:"anthropic", ...}

Only re-runs the 4 Claude cells (A0..A3). FOSS row results from the
original matrix run are kept as-is.
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

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
TARGET_REL = "docs/wiki/literate-tools.org"
ARTIFACTS = Path("/home/daniel/repos/org-llm/scripts/_round5_matrix_artifacts")
BUNDLE_FILE = ARTIFACTS / "context_bundle.json"
LOG = ARTIFACTS / f"claude-rerun-log-{int(time.time())}.txt"

PER_CELL_BUDGET = 0.50
PER_SPAWN_TIMEOUT = 240
POLL_DEADLINE = 600

# Claude row only, agentic_tool=claude-code (no modelConfig)
CELLS = [
    ("A0-Claude", "A0"),
    ("A1-Claude", "A1"),
    ("A2-Claude", "A2"),
    ("A3-Claude", "A3"),
]

ATOZ_PERSONA = (
    "You are @atoz — Bridge Crew wiki concept-graph specialist. "
    "Meticulous, archive-grade. You keep the wiki's [[id:UUID]] cross-link "
    "mesh well-knit by adding canonical link wrappers around prose mentions."
)
SPOCK_PERSONA = (
    "You are @spock — Bridge Crew logic + canonical-source reviewer. "
    "You ask sharp redirecting questions; you do not rubber-stamp."
)
PICARD_PERSONA = (
    "You are @picard — Bridge Crew captain. "
    "Your job is to coordinate specialists, NOT to do their work. "
    "You always do deterministic data work first (DB queries, grep, file "
    "reads) and inject pre-fetched context into specialist prompts. "
    "You are FAST: pre-work coordination must complete in under 60 seconds. "
    "You delegate specialist work; you never do it yourself."
)


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
            r = urllib.request.Request(
                BASE + path, data=data, method=method,
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


def run_captain_spawn(captain_id: str, label: str, cell_dir: Path,
                       title: str, persona: str, task: str,
                       budget_usd: float = 0.30) -> tuple[str | None, float]:
    """Spawn shape for Claude row: agentic_tool=claude-code, NO modelConfig."""
    fresh = req("GET", f"/sessions/{captain_id}", retries=1)
    mcp_token = fresh.get("mcp_token")
    mcp_cfg = cell_dir / f"mcp-{label}.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))

    spawn_args = {"prompt": persona + "\n\n" + task,
                  "title": title,
                  "agenticTool": "claude-code"}  # NO modelConfig
    captain_prompt = f"""You have one MCP server "agor" exposing two tools.

Spawn ONE child by issuing a single mcp__agor__agor_execute_tool call:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(spawn_args)}

After the spawn response comes back, print exactly:
SPAWN_SESSION_ID=<session_id>
SPAWN_DONE
Then stop.
"""
    parent_out = cell_dir / f"captain-{label}.jsonl"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", "sonnet",
           "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
           "--permission-mode", "bypassPermissions",
           "--max-budget-usd", str(budget_usd), captain_prompt]
    log(f"  captain claude -p for {label} (budget=${budget_usd})")
    t0 = time.time()
    with parent_out.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                           timeout=PER_SPAWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  TIMEOUT: {label}")
    elapsed = time.time() - t0

    sid = None
    cost = 0.0
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            cost += float(ev.get("total_cost_usd") or 0)
            for ln in (ev.get("result", "") or "").splitlines():
                if ln.startswith("SPAWN_SESSION_ID="):
                    sid = ln.split("=", 1)[1].strip()
    log(f"  {label} sid={sid} cost=${cost:.4f} elapsed={elapsed:.1f}s")
    return sid, cost


def poll_terminal(sid: str, deadline_s: int = POLL_DEADLINE) -> str:
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            s = req("GET", f"/sessions/{sid}", retries=1)
            if s.get("status") in terminal:
                return s.get("status")
        except Exception as e:
            log(f"  poll err: {e}")
        time.sleep(15)
    return "timeout"


def get_picard_decomposition(captain_id: str, cell_dir: Path,
                              bundle: dict) -> tuple[dict, float]:
    decompose_task = (
        f"You are @picard. Specialists @atoz + @spock will audit "
        f"`{bundle['target_path']}` ({bundle['target_lines']} lines) for "
        f"cross-link insertions.\n\nPRE-FETCHED:\n"
        f"- {bundle['known_ids_count']} known wiki IDs\n"
        f"- {len(bundle['candidate_concepts'])} candidate insertion points:\n"
        + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                    f"{c.get('matched_basename', c.get('matched_dec',''))} "
                    f"({len(c['candidate_ids'])} id(s))"
                    for c in bundle['candidate_concepts'][:15])
        + "\n\nWrite ONE-PARAGRAPH role briefs for @atoz and @spock. Output:\n"
        "ATOZ_BRIEF: <para>\nSPOCK_BRIEF: <para>\nPICARD_DONE"
    )
    sid, cost = run_captain_spawn(
        captain_id, "picard-decompose", cell_dir,
        "round5-claude-picard", PICARD_PERSONA, decompose_task,
        budget_usd=0.20)
    if not sid:
        return {"atoz_brief": "", "spock_brief": ""}, cost
    poll_terminal(sid, deadline_s=180)
    msgs = req("GET", f"/messages?session_id={sid}&$limit=50", retries=1)
    asst = "\n".join(str(m.get("content_preview") or m.get("content") or "")
                      for m in msgs.get("data", []) if m.get("role") == "assistant")
    atoz_m = re.search(r"ATOZ_BRIEF:\s*(.+?)(?=SPOCK_BRIEF:|$)", asst, re.DOTALL)
    spock_m = re.search(r"SPOCK_BRIEF:\s*(.+?)(?=PICARD_DONE|$)", asst, re.DOTALL)
    return {
        "atoz_brief": atoz_m.group(1).strip() if atoz_m else "",
        "spock_brief": spock_m.group(1).strip() if spock_m else "",
    }, cost


def build_atoz_task(depth: str, bundle: dict, picard_brief: str = "") -> str:
    base = (
        f"Audit `{bundle['target_path']}` ({bundle['target_lines']} lines) "
        f"for outgoing-cross-link opportunities. Page has ZERO `[[id:UUID]]` "
        f"links — concept-graph orphan.\n\nIdentify EXACTLY 5 places to add "
        f"`[[id:UUID][label]]` cross-link wrappers. Apply directly. Do NOT "
        f"touch any other file. Do NOT change prose meaning.\n\n"
        f"When done, run `git add` + `git commit -m \"docs(wiki): add 5 "
        f"cross-links to literate-tools.org (round-5)\"` in your worktree, "
        f"then print `ATOZ_DONE` and stop."
    )
    if depth == "A0":
        return base + ("\n\nDiscovery: build the known-IDs set yourself: "
                       "`grep -h '^:ID:' docs/wiki/*.org | awk '{print $2}' | sort -u`.")
    bundle_summary = (
        f"\n\nPRE-FETCHED CONTEXT (from @picard):\n"
        f"- {bundle['known_ids_count']} known wiki IDs catalogued\n"
        f"- {len(bundle['candidate_concepts'])} candidate insertion points:\n"
        + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                    f"{c.get('matched_basename', c.get('matched_dec',''))} "
                    f"-> id {c['candidate_ids'][0][:8]}..."
                    for c in bundle['candidate_concepts'][:12])
        + f"\n\nFull bundle: scripts/_round5_matrix_artifacts/context_bundle.json"
    )
    if depth in ("A2", "A3") and picard_brief:
        return base + bundle_summary + f"\n\n@picard's brief:\n{picard_brief}"
    return base + bundle_summary


def build_spock_task(depth: str, atoz_sid: str, picard_brief: str = "") -> str:
    base = (
        f"You are reviewer for cross-link audit. @atoz "
        f"(session_id={atoz_sid}) is editing `{TARGET_REL}` to add 5 cross-links.\n\n"
        f"Wait for @atoz's diff (poll worktree git log every ~30s; give up at 5 min). "
        f"Read the diff. Send up to 3 redirecting `mode:'btw'` questions to @atoz "
        f"via mcp__agor__agor_execute_tool -> agor_sessions_prompt mode='btw' "
        f"target='{atoz_sid}'. Examples: 'is X canonical owner?', 'does this "
        f"wrapping change meaning?'. Print verdict (APPROVE/REVISE) + SPOCK_DONE."
    )
    if depth in ("A2", "A3") and picard_brief:
        return base + f"\n\n@picard's brief:\n{picard_brief}"
    return base


# ── Main ───────────────────────────────────────────────────────────────────
ARTIFACTS.mkdir(exist_ok=True)
bundle = json.loads(BUNDLE_FILE.read_text())
log(f"Round-5 Claude rerun :: {len(CELLS)} cells :: agentic_tool=claude-code (no modelConfig)")
relogin()

results = []
for cell_name, depth in CELLS:
    log(f"\n=== CELL {cell_name} (depth={depth}, claude-code) ===")
    cell_start = time.time()
    cell_dir = ARTIFACTS / cell_name
    cell_dir.mkdir(exist_ok=True)
    # Wipe stale artifacts from earlier (failed) Claude run
    for f in cell_dir.glob("*"): f.unlink()
    cell_cost = 0.0

    try:
        wt_name = f"pilot-R5-{cell_name}-rerun-{int(time.time())}"
        wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
            "name": wt_name, "ref": wt_name, "createBranch": True,
            "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
        wt_id = wt["worktree_id"]
        wt_path = (Path.home() / ".agor/worktrees/local/org-llm" / wt_name)
        log(f"  wt_id={wt_id[:18]}")
        time.sleep(2)

        cap = req("POST", "/sessions",
                  {"worktree_id": wt_id, "agentic_tool": "claude-code"})
        cap_id = cap["session_id"]
        req("PATCH", f"/sessions/{cap_id}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  captain_pilot={cap_id[:18]}")

        picard_brief = {"atoz_brief": "", "spock_brief": ""}
        if depth in ("A2", "A3"):
            log(f"  @picard decompose pass...")
            picard_brief, picard_cost = get_picard_decomposition(
                cap_id, cell_dir, bundle)
            cell_cost += picard_cost
            log(f"  @picard atoz_brief={'YES' if picard_brief['atoz_brief'] else 'NONE'} "
                f"spock_brief={'YES' if picard_brief['spock_brief'] else 'NONE'}")

        atoz_task = build_atoz_task(depth, bundle, picard_brief["atoz_brief"])
        atoz_sid, atoz_cost = run_captain_spawn(
            cap_id, f"{cell_name}-atoz", cell_dir,
            f"r5-{cell_name}-atoz", ATOZ_PERSONA, atoz_task,
            budget_usd=PER_CELL_BUDGET / 2)
        cell_cost += atoz_cost

        spock_sid = None
        if atoz_sid:
            req("PATCH", f"/sessions/{atoz_sid}",
                {"permission_config": {"mode": "bypassPermissions"}})
            spock_task = build_spock_task(depth, atoz_sid, picard_brief["spock_brief"])
            spock_sid, spock_cost = run_captain_spawn(
                cap_id, f"{cell_name}-spock", cell_dir,
                f"r5-{cell_name}-spock", SPOCK_PERSONA, spock_task,
                budget_usd=PER_CELL_BUDGET / 2)
            cell_cost += spock_cost
            req("PATCH", f"/sessions/{spock_sid}",
                {"permission_config": {"mode": "bypassPermissions"}})

        atoz_status = poll_terminal(atoz_sid) if atoz_sid else "n/a"
        spock_status = poll_terminal(spock_sid) if spock_sid else "n/a"

        diff_stat = subprocess.run(
            ["git", "-C", str(wt_path), "diff", "--stat", "trunk", "--", TARGET_REL],
            capture_output=True, text=True).stdout.strip()
        diff_full = subprocess.run(
            ["git", "-C", str(wt_path), "diff", "trunk", "--", TARGET_REL],
            capture_output=True, text=True).stdout
        log_oneline = subprocess.run(
            ["git", "-C", str(wt_path), "log", "--oneline", "-2"],
            capture_output=True, text=True).stdout.strip()
        (cell_dir / "diff.patch").write_text(diff_full)

        btw_count = 0
        if atoz_sid:
            gen = req("GET", f"/sessions/{atoz_sid}").get("genealogy", {})
            for child_id in gen.get("children", []):
                try:
                    c = req("GET", f"/sessions/{child_id}")
                    if c.get("fork_origin") == "btw":
                        btw_count += 1
                except Exception: pass

        cell_wall = time.time() - cell_start
        results.append({
            "cell": cell_name, "depth": depth, "agentic_tool": "claude-code",
            "wt_name": wt_name, "wt_id": wt_id,
            "captain_pilot_sid": cap_id,
            "picard_brief_present": bool(picard_brief["atoz_brief"]),
            "atoz_sid": atoz_sid, "atoz_status": atoz_status,
            "spock_sid": spock_sid, "spock_status": spock_status,
            "btw_children_observed": btw_count,
            "diff_stat": diff_stat,
            "cell_cost_usd": round(cell_cost, 4),
            "cell_wall_seconds": round(cell_wall, 1),
            "log_oneline": log_oneline,
        })
        log(f"  CELL {cell_name} done: cost=${cell_cost:.4f} wall={cell_wall:.0f}s "
            f"diff={diff_stat or '(none)'}")
    except Exception as e:
        log(f"  CELL {cell_name} ERROR: {e}")
        results.append({"cell": cell_name, "depth": depth, "error": str(e)})

summary = ARTIFACTS / f"claude-rerun-summary-{int(time.time())}.json"
summary.write_text(json.dumps(results, indent=2))
print()
print("=" * 80)
print("CLAUDE ROW RE-RUN (claude-code agentic_tool, no modelConfig)")
print("=" * 80)
total_cost = sum(r.get("cell_cost_usd", 0) for r in results)
total_wall = sum(r.get("cell_wall_seconds", 0) for r in results)
print(f"Total: ${total_cost:.4f}  Wall: {total_wall:.0f}s\n")
print(f"{'CELL':12} {'DEPTH':5} {'COST':>7} {'WALL':>5} {'BTW':>3} DIFF")
for r in results:
    if "error" in r:
        print(f"{r['cell']:12} {r['depth']:5} ERROR: {r['error'][:50]}")
        continue
    diff_short = (r["diff_stat"] or "(none)")[:50]
    print(f"{r['cell']:12} {r['depth']:5} ${r['cell_cost_usd']:>5.3f} "
          f"{r['cell_wall_seconds']:>4.0f}s {r['btw_children_observed']:>3} {diff_short}")
print(f"\nsummary: {summary}")
