#!/usr/bin/env python3
"""Round-5 matrix harness — 8-cell sweep on @picard-role × crew-model.

Cells:
  A0-FOSS, A1-FOSS, A2-FOSS, A3-FOSS    (qwen3-coder-30b via opencode)
  A0-Claude, A1-Claude, A2-Claude, A3-Claude  (sonnet-4-6 via opencode)

@picard role depths:
  A0: no captain — pilot spawns @atoz + @spock directly with bare brief
  A1: pre-fetch only — pilot injects context_bundle.json into spawn prompts
  A2: + decompose — pilot spawns @picard ONCE to output role-tailored briefs,
                     then spawns @atoz + @spock with those briefs + bundle
  A3: full captain — pilot spawns @picard which spawns @atoz + @spock itself
                      + monitors + integrates; pilot just waits for @picard

Per the 'Agor crew uses no Claude' rule, FOSS row is the production shape;
Claude row is the labeled sanity baseline (encouraged for comparison).

Same task across all 8: cross-link audit on docs/wiki/literate-tools.org;
add 5 [[id:UUID]] cross-link insertions; commit on the cell's worktree branch.

Sequential execution; per-cell budget cap; per-cell artifacts dir.
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

# ── Config ────────────────────────────────────────────────────────────────
BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
TARGET_REL = "docs/wiki/literate-tools.org"
ARTIFACTS = Path("/home/daniel/repos/org-llm/scripts/_round5_matrix_artifacts")
BUNDLE_FILE = ARTIFACTS / "context_bundle.json"
LOG = ARTIFACTS / f"matrix-log-{int(time.time())}.txt"

PER_CELL_BUDGET = 0.50  # USD hard cap per cell
PER_SPAWN_TIMEOUT = 240  # seconds for one claude -p captain subprocess
POLL_DEADLINE = 600     # seconds to wait for specialists to reach idle

FOSS_MODEL_CONFIG = {"provider": "openrouter",
                      "model": "qwen/qwen3-coder-30b-a3b-instruct"}
CLAUDE_MODEL_CONFIG = {"provider": "anthropic",
                        "model": "claude-sonnet-4-6"}

CELLS = [
    ("A0-FOSS", "A0", FOSS_MODEL_CONFIG),
    ("A1-FOSS", "A1", FOSS_MODEL_CONFIG),
    ("A2-FOSS", "A2", FOSS_MODEL_CONFIG),
    ("A3-FOSS", "A3", FOSS_MODEL_CONFIG),
    ("A0-Claude", "A0", CLAUDE_MODEL_CONFIG),
    ("A1-Claude", "A1", CLAUDE_MODEL_CONFIG),
    ("A2-Claude", "A2", CLAUDE_MODEL_CONFIG),
    ("A3-Claude", "A3", CLAUDE_MODEL_CONFIG),
]

# ── Personas (FOSS-only crew rule: Claude allowed only as labeled baseline) ─
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


def relogin() -> None:
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                        capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                   capture_output=True, env=env, check=True)


def ensure_opencode_serve() -> subprocess.Popen | None:
    try:
        urllib.request.urlopen("http://localhost:4096/", timeout=2)
        log("opencode serve already up")
        return None
    except Exception:
        pass
    log("starting opencode serve")
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    try:
        or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                                capture_output=True, text=True, timeout=5,
                                check=True).stdout.strip()
        env["OPENROUTER_API_KEY"] = or_key
    except Exception as e:
        log(f"  WARN: no OpenRouter key: {e}")
    try:
        an_key = subprocess.run(["pass", "anthropic/api-key"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if an_key:
            env["ANTHROPIC_API_KEY"] = an_key
            log("  ANTHROPIC_API_KEY injected (for Claude row)")
    except Exception:
        pass
    serve_log = ARTIFACTS / f"opencode-serve-{int(time.time())}.log"
    proc = subprocess.Popen(["opencode", "serve", "--port", "4096"],
                            stdout=serve_log.open("w"),
                            stderr=subprocess.STDOUT, env=env)
    time.sleep(3)
    return proc


def run_captain_spawn(captain_id: str, label: str, cell_dir: Path,
                       title: str, persona: str, task: str,
                       model_config: dict,
                       budget_usd: float = 0.30) -> tuple[str | None, float]:
    """One short claude -p captain subprocess that issues ONE FOSS spawn."""
    fresh = req("GET", f"/sessions/{captain_id}", retries=1)
    mcp_token = fresh.get("mcp_token")
    mcp_cfg = cell_dir / f"mcp-{label}.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))

    spawn_args = {"prompt": persona + "\n\n" + task,
                  "title": title,
                  "agenticTool": "opencode",
                  "modelConfig": model_config}
    captain_prompt = f"""You have one MCP server "agor" exposing two tools.

Spawn ONE child by issuing a single mcp__agor__agor_execute_tool call:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(spawn_args)}

After the spawn response comes back, print exactly:
SPAWN_SESSION_ID=<session_id>
SPAWN_DONE
Then stop. Do not poll, do not narrate.
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
            log(f"  TIMEOUT: {label} captain exceeded {PER_SPAWN_TIMEOUT}s")
    elapsed = time.time() - t0

    sid = None
    cost = 0.0
    for line in parent_out.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
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
            st = s.get("status")
            if st in terminal:
                return st
        except Exception as e:
            log(f"  poll err: {e}")
        time.sleep(15)
    return "timeout"


def get_picard_decomposition(captain_id: str, cell_dir: Path, bundle: dict,
                              model_config: dict) -> tuple[dict, float]:
    """A2/A3 only: spawn @picard for ONE turn to output role-tailored briefs.

    Returns ({atoz_brief, spock_brief}, cost).
    """
    decompose_task = (
        f"You are @picard. Your specialists @atoz (author) and @spock "
        f"(reviewer) will audit this wiki page for cross-link insertions:\n\n"
        f"TARGET: {bundle['target_path']} ({bundle['target_lines']} lines)\n\n"
        f"PRE-FETCHED CONTEXT (deterministic, from grep + file reads):\n"
        f"- {bundle['known_ids_count']} known wiki IDs available for linking\n"
        f"- {len(bundle['candidate_concepts'])} candidate insertion points "
        f"already located by name-matching scan\n\n"
        f"CANDIDATE CONCEPTS (line, snippet, candidate_ids):\n"
        + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                    f"{c.get('matched_basename', c.get('matched_dec',''))} "
                    f"({len(c['candidate_ids'])} candidate id(s))"
                    for c in bundle['candidate_concepts'][:15])
        + "\n\nYour job: write ONE-PARAGRAPH role briefs for @atoz and @spock. "
        "Tailor each brief based on what the pre-fetched candidates suggest "
        "(e.g. 'pick the strongest 5 of these 7 candidates', or 'look for "
        "missed concepts beyond basename matches'). Be specific.\n\n"
        "Output exactly:\n"
        "ATOZ_BRIEF: <one paragraph>\n"
        "SPOCK_BRIEF: <one paragraph>\n"
        "PICARD_DONE"
    )
    sid, cost = run_captain_spawn(
        captain_id, "picard-decompose", cell_dir,
        "round5-picard-decompose", PICARD_PERSONA, decompose_task,
        model_config, budget_usd=0.20)
    if not sid:
        return {"atoz_brief": "", "spock_brief": ""}, cost
    # Poll @picard to terminal + read its output
    poll_terminal(sid, deadline_s=180)
    msgs = req("GET", f"/messages?session_id={sid}&$limit=50", retries=1)
    asst_text = "\n".join(
        str(m.get("content_preview") or m.get("content") or "")
        for m in msgs.get("data", []) if m.get("role") == "assistant"
    )
    atoz_m = re.search(r"ATOZ_BRIEF:\s*(.+?)(?=SPOCK_BRIEF:|$)", asst_text, re.DOTALL)
    spock_m = re.search(r"SPOCK_BRIEF:\s*(.+?)(?=PICARD_DONE|$)", asst_text, re.DOTALL)
    return {
        "atoz_brief": (atoz_m.group(1).strip() if atoz_m else ""),
        "spock_brief": (spock_m.group(1).strip() if spock_m else ""),
        "picard_session_id": sid,
    }, cost


def build_atoz_task(depth: str, bundle: dict, picard_brief: str = "") -> str:
    base = (
        f"Audit `{bundle['target_path']}` ({bundle['target_lines']} lines) "
        f"for outgoing-cross-link opportunities. The page currently has "
        f"ZERO outgoing `[[id:UUID]]` links — concept-graph orphan.\n\n"
        f"Your job: identify EXACTLY 5 places to add `[[id:UUID][label]]` "
        f"cross-link wrappers. Apply them directly. Do NOT touch any other "
        f"file. Do NOT change prose meaning — only wrap existing mentions.\n\n"
        f"When done, run `git add` + `git commit -m \"docs(wiki): add 5 "
        f"cross-links to literate-tools.org (round-5)\"` in your worktree, "
        f"then print `ATOZ_DONE` and stop."
    )
    if depth == "A0":
        return base + (
            f"\n\nDiscovery: build the known-IDs set yourself with "
            f"`grep -h '^:ID:' docs/wiki/*.org | awk '{{print $2}}' | sort -u`."
        )
    # A1/A2/A3 inject pre-fetched bundle
    bundle_summary = (
        f"\n\nPRE-FETCHED CONTEXT (from @picard; trust it, do not re-discover):\n"
        f"- {bundle['known_ids_count']} known wiki IDs catalogued\n"
        f"- {len(bundle['candidate_concepts'])} candidate insertion points "
        f"already located:\n"
        + "\n".join(f"  L{c['line']:3d}: {c['snippet'][:80]} -> "
                    f"{c.get('matched_basename', c.get('matched_dec',''))} "
                    f"-> id {c['candidate_ids'][0][:8]}..."
                    for c in bundle['candidate_concepts'][:12])
        + "\n\nFull bundle: see `scripts/_round5_matrix_artifacts/context_bundle.json`."
    )
    if depth in ("A2", "A3") and picard_brief:
        return base + bundle_summary + f"\n\n@picard's brief for you:\n{picard_brief}"
    return base + bundle_summary


def build_spock_task(depth: str, atoz_sid: str, picard_brief: str = "") -> str:
    base = (
        f"You are reviewer for a cross-link audit. @atoz "
        f"(session_id={atoz_sid}) is editing `{TARGET_REL}` to add 5 cross-links.\n\n"
        f"Wait for @atoz's diff to land (poll the worktree's git log every "
        f"~30s; give up after 5 min). Read @atoz's diff. For up to 3 of the "
        f"5 link insertions, send ONE redirecting `mode:'btw'` question to "
        f"@atoz (use mcp__agor__agor_execute_tool -> agor_sessions_prompt "
        f"with mode='btw' and target='{atoz_sid}'). Examples: 'is X the "
        f"canonical owner?', 'does this wrapping change meaning?'. After "
        f"@atoz responds (poll their genealogy.children for btw kids), "
        f"write your verdict (APPROVE / REQUEST-REVISE) and print "
        f"SPOCK_DONE."
    )
    if depth in ("A2", "A3") and picard_brief:
        return base + f"\n\n@picard's brief for you:\n{picard_brief}"
    return base


# ── Main loop ─────────────────────────────────────────────────────────────
ARTIFACTS.mkdir(exist_ok=True)
LOG.parent.mkdir(parents=True, exist_ok=True)

bundle = json.loads(BUNDLE_FILE.read_text())
log(f"Round-5 matrix start :: {len(CELLS)} cells :: target={TARGET_REL}")

opencode_proc = ensure_opencode_serve()
relogin()  # fresh token at start

results = []
try:
    for cell_name, depth, model_config in CELLS:
        log(f"\n=== CELL {cell_name} (depth={depth}, model={model_config['model'][:30]}) ===")
        cell_start = time.time()
        cell_dir = ARTIFACTS / cell_name
        cell_dir.mkdir(exist_ok=True)
        cell_cost = 0.0

        try:
            # 1. Worktree
            wt_name = f"pilot-R5-{cell_name}-{int(time.time())}"
            wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
                "name": wt_name, "ref": wt_name, "createBranch": True,
                "sourceBranch": "trunk", "pullLatest": False,
                "refType": "branch"}, retries=1)
            wt_id = wt["worktree_id"]
            wt_path = (Path.home() / ".agor/worktrees/local/org-llm" / wt_name)
            log(f"  wt_id={wt_id[:18]}")
            time.sleep(2)  # FS-population race

            # 2. Captain pilot session
            cap = req("POST", "/sessions",
                      {"worktree_id": wt_id, "agentic_tool": "claude-code"},
                      retries=1)
            cap_id = cap["session_id"]
            req("PATCH", f"/sessions/{cap_id}",
                {"permission_config": {"mode": "bypassPermissions"}}, retries=1)
            log(f"  captain_pilot={cap_id[:18]}")

            # 3. Cell-specific @picard handling
            picard_brief = {"atoz_brief": "", "spock_brief": ""}
            if depth in ("A2", "A3"):
                log(f"  @picard decompose pass...")
                picard_brief, picard_cost = get_picard_decomposition(
                    cap_id, cell_dir, bundle, model_config)
                cell_cost += picard_cost
                log(f"  @picard atoz_brief={'YES' if picard_brief['atoz_brief'] else 'NONE'} "
                    f"spock_brief={'YES' if picard_brief['spock_brief'] else 'NONE'}")

            # 4. Spawn @atoz
            atoz_task = build_atoz_task(depth, bundle, picard_brief["atoz_brief"])
            atoz_sid, atoz_cost = run_captain_spawn(
                cap_id, f"{cell_name}-atoz", cell_dir,
                f"r5-{cell_name}-atoz", ATOZ_PERSONA, atoz_task,
                model_config, budget_usd=PER_CELL_BUDGET / 2)
            cell_cost += atoz_cost

            # 5. Spawn @spock
            spock_sid = None
            if atoz_sid:
                req("PATCH", f"/sessions/{atoz_sid}",
                    {"permission_config": {"mode": "bypassPermissions"}}, retries=1)
                spock_task = build_spock_task(depth, atoz_sid, picard_brief["spock_brief"])
                spock_sid, spock_cost = run_captain_spawn(
                    cap_id, f"{cell_name}-spock", cell_dir,
                    f"r5-{cell_name}-spock", SPOCK_PERSONA, spock_task,
                    model_config, budget_usd=PER_CELL_BUDGET / 2)
                cell_cost += spock_cost
                req("PATCH", f"/sessions/{spock_sid}",
                    {"permission_config": {"mode": "bypassPermissions"}}, retries=1)

            # 6. Poll both to terminal
            atoz_status = poll_terminal(atoz_sid) if atoz_sid else "n/a"
            spock_status = poll_terminal(spock_sid) if spock_sid else "n/a"

            # 7. Capture diff vs trunk
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

            # 8. BTW probe (per R2: on TARGET = @atoz's genealogy)
            btw_count = 0
            if atoz_sid:
                gen = req("GET", f"/sessions/{atoz_sid}", retries=1).get("genealogy", {})
                for child_id in gen.get("children", []):
                    try:
                        c = req("GET", f"/sessions/{child_id}", retries=1)
                        if c.get("fork_origin") == "btw":
                            btw_count += 1
                    except Exception:
                        pass

            cell_wall = time.time() - cell_start
            cell_result = {
                "cell": cell_name, "depth": depth,
                "model": model_config["model"],
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
            }
            results.append(cell_result)
            log(f"  CELL {cell_name} done: cost=${cell_cost:.4f} wall={cell_wall:.0f}s "
                f"diff={diff_stat or '(none)'}")
        except Exception as e:
            log(f"  CELL {cell_name} ERROR: {e}")
            results.append({"cell": cell_name, "depth": depth,
                            "model": model_config["model"], "error": str(e)})

    # ── Summary report ────────────────────────────────────────────────────
    summary = ARTIFACTS / f"summary-{int(time.time())}.json"
    summary.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 80)
    print("ROUND-5 MATRIX SUMMARY (8 cells, sequential)")
    print("=" * 80)
    total_cost = sum(r.get("cell_cost_usd", 0) for r in results)
    total_wall = sum(r.get("cell_wall_seconds", 0) for r in results)
    print(f"Total spend: ${total_cost:.4f}  Total wall: {total_wall:.0f}s")
    print()
    print(f"{'CELL':12} {'DEPTH':5} {'MODEL':10} {'COST':>7} {'WALL':>5} {'BTW':>3} DIFF")
    for r in results:
        if "error" in r:
            print(f"{r['cell']:12} {r['depth']:5} {r['model'][:10]:10} ERROR: {r['error'][:50]}")
            continue
        model_short = "qwen30" if "qwen" in r["model"] else "sonnet46"
        diff_short = (r["diff_stat"] or "(none)")[:40]
        print(f"{r['cell']:12} {r['depth']:5} {model_short:10} "
              f"${r['cell_cost_usd']:>5.3f} {r['cell_wall_seconds']:>4.0f}s "
              f"{r['btw_children_observed']:>3} {diff_short}")
    print()
    print(f"summary: {summary}")
    print(f"diffs: {ARTIFACTS}/<cell-name>/diff.patch")

finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
