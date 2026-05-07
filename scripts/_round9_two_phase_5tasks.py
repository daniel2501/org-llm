#!/usr/bin/env python3
"""Round-9 — refined two-phase + 5-task pilot.

Three round-8 fixes:
  A. Pre-fetch bundle pre-resolves "Phase YYYY-MM.PP" → roadmap.org and
     "DEC-N" → decisions.org canonical owners. Specialist gets an explicit
     `canonical_id` per candidate, not a guess.
  B. Brief tightens insertion-count rule: "make N changes; if you can only
     find K < N strong candidates, output LANDED <K> and stop — never pad."
  C. Harness post-step: if specialist didn't commit, harness commits with
     a generic message.

5 tasks spanning the matrix:
  B1  — cross-link audit on literate-tools.org    (text, pattern-match, low)
  B5  — section rewrite for clarity               (text, nuanced, low)
  B7  — docstrings on org_llm/avatars.py          (code, pattern-match, low)
  B11 — DEC draft on an open question             (decision, open-ended, high)
  B13 — LICENSE copyright year                    (code, mechanical, low)
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
ARTIFACTS = REPO / "scripts/_round9_two_phase_5tasks_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"
PRIMER_FILE = REPO / "docs/wiki/picard-agor-primer.org"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 600

CAPTAIN_MODEL_ID = "qwen/qwen3-coder-30b-a3b-instruct"
SPECIALIST_MODEL = {"provider": "openrouter",
                     "model": "qwen/qwen3-coder-30b-a3b-instruct"}


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


# ── Bundle pre-fetch helpers ─────────────────────────────────────────────
def build_known_ids_map() -> tuple[dict[str, dict], dict[str, list[str]], dict[str, str]]:
    """Returns (id_to_meta, basename_to_ids, dec_to_id)."""
    wiki = REPO / "docs/wiki"
    id_to_meta: dict[str, dict] = {}
    basename_to_ids: dict[str, list[str]] = {}
    dec_to_id: dict[str, str] = {}
    for org in sorted(wiki.glob("*.org")):
        text = org.read_text()
        m = re.search(r"^:ID:\s+([a-f0-9-]+)", text, re.MULTILINE)
        if m:
            uid = m.group(1)
            id_to_meta[uid] = {"file": org.name}
            basename_to_ids.setdefault(org.stem, []).append(uid)
            if org.name == "decisions.org":
                for dm in re.finditer(r"^\*\* (DEC-\d+)\s", text, re.MULTILINE):
                    dec_to_id[dm.group(1)] = uid
    return id_to_meta, basename_to_ids, dec_to_id


def prefetch_b1_b14(task: dict) -> dict:
    """For cross-link audit tasks. Resolves Phase X → roadmap, DEC-N → decisions,
    basename mentions → that page's id."""
    target = REPO / task["target_file"]
    text = target.read_text()
    lines = text.splitlines()
    id_to_meta, basename_to_ids, dec_to_id = build_known_ids_map()
    roadmap_id = (basename_to_ids.get("roadmap") or [None])[0]

    candidates = []
    for ln_no, line in enumerate(lines, 1):
        if "[[id:" in line:
            continue  # skip already-linked
        # Phase YYYY-MM.PP — owns roadmap.org
        for m in re.finditer(r"Phase \d{4}-\d{2}\.\d{2}\b", line):
            if roadmap_id:
                candidates.append({
                    "line": ln_no, "snippet": line.strip()[:120],
                    "matched_text": m.group(0),
                    "canonical_owner": "roadmap.org",
                    "canonical_id": roadmap_id,
                    "kind": "phase",
                })
        # DEC-N — owns decisions.org
        for m in re.finditer(r"\bDEC-\d+\b", line):
            label = m.group(0)
            if label in dec_to_id:
                candidates.append({
                    "line": ln_no, "snippet": line.strip()[:120],
                    "matched_text": label,
                    "canonical_owner": "decisions.org",
                    "canonical_id": dec_to_id[label],
                    "kind": "dec",
                })
        # basename mentions (skip if Phase or DEC already matched on same line)
        for stem, ids in basename_to_ids.items():
            for m in re.finditer(r"\b" + re.escape(stem) + r"\.org\b", line):
                candidates.append({
                    "line": ln_no, "snippet": line.strip()[:120],
                    "matched_text": stem + ".org",
                    "canonical_owner": stem + ".org",
                    "canonical_id": ids[0],
                    "kind": "basename",
                })
                break  # one basename per line is enough
    return {
        "target_path": str(target.relative_to(REPO)),
        "target_lines": len(lines),
        "known_ids_count": len(id_to_meta),
        "candidates": candidates,
    }


def prefetch_passthrough(task: dict) -> dict:
    """For tasks that don't need a wiki bundle."""
    target = REPO / task["target_file"]
    if target.exists():
        text = target.read_text()
        return {
            "target_path": str(target.relative_to(REPO)),
            "target_lines": len(text.splitlines()),
            "target_size_bytes": len(text),
        }
    return {"target_path": task.get("target_file", ""), "missing": True}


def prefetch_b5(task: dict) -> dict:
    """Section-rewrite: identify the longest paragraph in the target."""
    target = REPO / task["target_file"]
    text = target.read_text()
    paragraphs = re.split(r"\n\s*\n", text)
    longest = max(((i, p) for i, p in enumerate(paragraphs)), key=lambda x: len(x[1]))
    return {
        "target_path": str(target.relative_to(REPO)),
        "target_lines": len(text.splitlines()),
        "longest_paragraph_index": longest[0],
        "longest_paragraph_chars": len(longest[1]),
        "longest_paragraph_preview": longest[1][:300],
    }


def prefetch_b11(task: dict) -> dict:
    """DEC draft — list existing DEC numbers so the new one is the next."""
    decisions_text = (REPO / "docs/wiki/decisions.org").read_text()
    nums = sorted({int(m.group(1)) for m in re.finditer(r"^\*\* DEC-(\d+)\s",
                                                          decisions_text, re.MULTILINE)})
    return {
        "decisions_path": "docs/wiki/decisions.org",
        "existing_dec_numbers": nums,
        "next_available": (max(nums) + 1) if nums else 1,
        "problem_statement": task.get("problem_statement", ""),
    }


# ── Tasks ────────────────────────────────────────────────────────────────
TASKS = [
    {
        "id": "B1",
        "label": "cross-link orphan audit on literate-tools.org",
        "target_file": "docs/wiki/literate-tools.org",
        "goal": "Add EXACTLY 5 [[id:UUID][label]] cross-link wrappers around existing prose mentions. Use the canonical_id from the pre-fetched candidates list — do NOT guess. Preserve =...= verbatim formatting INSIDE link labels. Do NOT touch any other file.",
        "n_changes": 5,
        "prefetch": prefetch_b1_b14,
    },
    {
        "id": "B5",
        "label": "section rewrite for clarity in agor-pilot-install.org",
        "target_file": "docs/wiki/agor-pilot-install.org",
        "goal": "Pick the longest paragraph (see prefetch.longest_paragraph_*) and rewrite it for tightness — preserve meaning, cut filler, no new claims. Do NOT touch any other file.",
        "n_changes": 1,
        "prefetch": prefetch_b5,
    },
    {
        "id": "B7",
        "label": "add docstrings + type hints to org_llm/avatars.py",
        "target_file": "org_llm/avatars.py",
        "goal": "Add Python type hints + one-line docstrings to every public function/class. Existing behavior must not change. `python -c 'import org_llm.avatars'` must import cleanly. Do NOT touch any other file.",
        "n_changes": None,  # not enforced — touch every public def
        "prefetch": prefetch_passthrough,
    },
    {
        "id": "B11",
        "label": "draft DEC entry for an open question",
        "target_file": "docs/wiki/decisions.org",
        "problem_statement": (
            "Should @picard be auto-created at `org-llm init` (eager — every user "
            "has it from day-one) OR lazily-spawned on first team-spawn (lazy — "
            "@picard only exists when a multi-agent task starts)? Tradeoffs: "
            "discoverability vs. footprint; user-mental-model vs. resource cost; "
            "team-formation latency on first use."
        ),
        "goal": "Append a new DEC entry to docs/wiki/decisions.org. Use the next-available DEC number (see prefetch). Format: ** DEC-N — title (status). Body: Context, Options (≥2), Tradeoffs, Decision (your recommendation), Rationale. Do NOT touch any other file.",
        "n_changes": 1,
        "prefetch": prefetch_b11,
    },
    {
        "id": "B13",
        "label": "update LICENSE copyright year",
        "target_file": "LICENSE",
        "goal": "Update copyright year `2025` → `2026`. If already 2026, no edit needed — just print 'NO_CHANGE_NEEDED' and stop. Single-file edit. Do NOT touch any other file.",
        "n_changes": 1,
        "prefetch": prefetch_passthrough,
    },
]


# ── Phase 1: planner via OpenRouter ──────────────────────────────────────
PLAN_FORMAT = """
You are @picard executing PHASE 1 of a two-phase architecture. Your only job
in this phase is to PRODUCE A STRUCTURED PLAN as JSON. The harness executes
your plan deterministically.

Output exactly this shape, no other text, no markdown fences, no commentary:

PICARD_PLAN_JSON_BEGIN
{
  "classification": {"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"},
  "playbook": "<flat|A|B|C|D|E>",
  "team": [
    {"handle": "@<bridge-crew-handle>",
     "task_brief": "<full multi-line brief — be specific; include success criterion + N-changes rule>"}
  ],
  "rationale": "<one short paragraph>"
}
PICARD_PLAN_JSON_END
"""


def call_openrouter(model_id: str, prompt: str) -> tuple[str, float]:
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 3000,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    cost = float(data.get("usage", {}).get("cost") or 0)
    return text, cost


def parse_plan(text: str) -> dict | None:
    m = re.search(r"PICARD_PLAN_JSON_BEGIN\s*(.+?)\s*PICARD_PLAN_JSON_END",
                   text, re.DOTALL)
    if not m:
        m2 = re.search(r"(\{[^}]*\"classification\".+\})", text, re.DOTALL)
        if not m2:
            return None
        json_text = m2.group(1)
    else:
        json_text = m.group(1).strip()
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        return None


# ── Phase 2: deterministic Agor execution ────────────────────────────────
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


def harness_spawn_specialist(wt_id: str, wt_name: str, task: dict, handle: str,
                              brief: str, prefetch: dict, run_dir: Path) -> str | None:
    cap = req("POST", "/sessions",
                {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    cap_id = cap["session_id"]
    req("PATCH", f"/sessions/{cap_id}",
          {"permission_config": {"mode": "bypassPermissions"}})
    mcp_token = cap.get("mcp_token")
    mcp_cfg = run_dir / f"mcp-{handle.lstrip('@')}.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {"agor": {
        "type": "http", "url": f"{BASE}/mcp",
        "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))

    persona_lookup = {
        "@atoz": "You are @atoz — Bridge Crew wiki concept-graph specialist.",
        "@data": "You are @data — Bridge Crew code + scribe specialist.",
        "@spock": "You are @spock — Bridge Crew logic + canonical-source reviewer.",
        "@geordi": "You are @geordi — Bridge Crew analytics + charts specialist.",
        "@boothby": "You are @boothby — Bridge Crew ops + hygiene specialist.",
        "@riker": "You are @riker — Bridge Crew process + scheduling specialist.",
    }
    persona = persona_lookup.get(handle, f"You are {handle}, a Bridge Crew specialist.")

    n_rule = ""
    if task.get("n_changes") is not None:
        n = task["n_changes"]
        n_rule = (f"\n\nN-CHANGES RULE: make EXACTLY {n} change(s). If you can "
                   f"only find K < {n} strong candidates, output `LANDED <K>` "
                   f"and stop — never pad to {n}.")

    spec_prompt = (
        persona + "\n\n"
        + brief
        + n_rule
        + f"\n\nPRE-FETCHED CONTEXT:\n{json.dumps(prefetch, indent=2)}"
        + f"\n\nWorktree path: /home/daniel/.agor/worktrees/local/org-llm/{wt_name}"
        + "\n\nWhen done, run `git add` + `git commit` in the worktree, then "
        + f"print `{handle.upper().lstrip('@')}_DONE` and stop."
    )
    spawn_args = {
        "prompt": spec_prompt,
        "title": f"r9-{task['id']}-{handle.lstrip('@')}",
        "agenticTool": "opencode",
        "modelConfig": SPECIALIST_MODEL,
    }
    cap_prompt = f"""You have one MCP server "agor". Spawn ONE specialist via:

  tool_name: "agor_sessions_spawn"
  arguments: {json.dumps(spawn_args)}

After spawn returns, print SPAWN_RESULT_SID=<sid> on its own line, then SPAWN_DONE."""
    out = run_dir / f"captain-{handle.lstrip('@')}.jsonl"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", "0.30",
            cap_prompt]
    with out.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                             timeout=PER_RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  captain TIMEOUT for {handle}")

    spec_sid = None
    for line in out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
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
    if spec_sid:
        try:
            req("PATCH", f"/sessions/{spec_sid}",
                  {"permission_config": {"mode": "bypassPermissions"}})
        except Exception:
            pass
    return spec_sid


def commit_if_uncommitted(wt_path: Path, task_id: str) -> None:
    """Round-9 fix C: harness commits if specialist didn't."""
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    if not diff_stat:
        return  # no diff, nothing to commit
    # Check if specialist already committed
    log_oneline = subprocess.run(["git", "-C", str(wt_path), "log", "--oneline", "-1"],
                                   capture_output=True, text=True).stdout.strip()
    head_msg = subprocess.run(["git", "-C", str(wt_path), "log", "-1", "--format=%s", "trunk..HEAD"],
                                capture_output=True, text=True).stdout.strip()
    if head_msg:
        return  # specialist committed
    # Harness commits
    log(f"  harness commits uncommitted changes for {task_id}")
    subprocess.run(["git", "-C", str(wt_path), "add", "-A"],
                     capture_output=True, check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m",
                     f"r9 {task_id}: harness-side commit (specialist didn't)"],
                     capture_output=True)


def run_one_task(task: dict, opencode_proc) -> dict:
    log(f"\n=== TASK {task['id']} ({task['label']}) ===")
    task_dir = ARTIFACTS / task['id']
    task_dir.mkdir(exist_ok=True)

    # PHASE 1
    log(" PHASE 1 — plan")
    prefetch = task["prefetch"](task)
    (task_dir / "prefetch.json").write_text(json.dumps(prefetch, indent=2))

    n_rule_in_brief = ""
    if task.get("n_changes") is not None:
        n_rule_in_brief = (f"\nN-CHANGES RULE (load-bearing): make EXACTLY "
                           f"{task['n_changes']} change(s). If you can only "
                           f"find K < {task['n_changes']} strong candidates, "
                           f"output `LANDED <K>` and stop — never pad.")

    phase1_prompt = (
        PRIMER + "\n\n---\n\n"
        + f"TASK ({task['id']} — {task['label']}):\n{task['goal']}"
        + n_rule_in_brief
        + "\n\nPRE-FETCHED CONTEXT (use canonical_id where present; never guess):\n"
        + json.dumps(prefetch, indent=2)
        + f"\n\nPRODUCTION CONSTRAINT: specialists run with modelConfig:\n"
        + json.dumps(SPECIALIST_MODEL, indent=2)
        + "\n\n---\n\n" + PLAN_FORMAT
    )
    plan_text, p1_cost = call_openrouter(CAPTAIN_MODEL_ID, phase1_prompt)
    (task_dir / "phase1_raw.txt").write_text(plan_text)
    plan = parse_plan(plan_text)
    if plan is None:
        log(f"  PLAN PARSE FAILED for {task['id']}")
        return {"task_id": task['id'], "phase1_cost": p1_cost, "error": "plan_parse_failed"}
    (task_dir / "phase1_plan.json").write_text(json.dumps(plan, indent=2))
    log(f"  plan: classification={plan.get('classification')} playbook={plan.get('playbook')} team={[s.get('handle') for s in plan.get('team', [])]}")

    # PHASE 2
    log(" PHASE 2 — execute")
    wt_name = f"r9-{task['id']}-{EPOCH}"
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt_name, "ref": wt_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt_id = wt["worktree_id"]
    wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
    log(f"  worktree {wt_id[:18]} ({wt_name})")
    time.sleep(2)

    specialists = []
    for spec in plan.get("team", []):
        handle = spec.get("handle", "@unknown")
        brief = spec.get("task_brief", "")
        if not brief:
            log(f"  WARN {handle}: empty brief; skipping")
            continue
        sid = harness_spawn_specialist(wt_id, wt_name, task, handle, brief,
                                         prefetch, task_dir)
        if sid:
            t = poll_terminal(sid)
            specialists.append({"handle": handle, "sid": sid, "terminal": t})
            log(f"  {handle} → {t}")
        else:
            specialists.append({"handle": handle, "sid": None, "terminal": "no-spawn"})
            log(f"  {handle} → no-spawn")

    # Round-9 fix C: harness-commit if needed
    commit_if_uncommitted(wt_path, task['id'])

    # Capture diff
    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    log_oneline = subprocess.run(["git", "-C", str(wt_path), "log", "--oneline", "-2"],
                                   capture_output=True, text=True).stdout.strip()
    (task_dir / "diff.patch").write_text(diff_full)
    log(f"  diff: {diff_stat or '(none)'}")

    return {
        "task_id": task['id'],
        "phase1_cost_usd": round(p1_cost, 6),
        "plan_classification": plan.get("classification"),
        "plan_playbook": plan.get("playbook"),
        "specialists": specialists,
        "diff_stat": diff_stat,
        "log_oneline": log_oneline,
        "wt_path": str(wt_path),
    }


# ── Main ─────────────────────────────────────────────────────────────────
log(f"Round-9 — {len(TASKS)} tasks × two-phase, captain={CAPTAIN_MODEL_ID}")
relogin()
opencode_proc = ensure_opencode_serve()

results = []
try:
    for task in TASKS:
        try:
            r = run_one_task(task, opencode_proc)
            results.append(r)
        except Exception as e:
            log(f"  TASK {task['id']} ERROR: {e}")
            results.append({"task_id": task['id'], "error": str(e)})

    summary = ARTIFACTS / f"summary-{EPOCH}.json"
    summary.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 80)
    print(f"ROUND-9 — 5-TASK TWO-PHASE SUMMARY")
    print("=" * 80)
    for r in results:
        if "error" in r:
            print(f"  {r['task_id']:>4}  ERROR: {r['error'][:60]}")
            continue
        n_specs = len(r.get("specialists", []))
        n_terminal = sum(1 for s in r.get("specialists", []) if s.get("terminal") == "idle")
        print(f"  {r['task_id']:>4}  cost=${r.get('phase1_cost_usd',0):.4f}+spec  "
              f"playbook={r.get('plan_playbook'):>5}  "
              f"specialists={n_terminal}/{n_specs}  "
              f"diff: {(r.get('diff_stat') or '(none)')[:50]}")
    print(f"\nsummary: {summary}")
finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
