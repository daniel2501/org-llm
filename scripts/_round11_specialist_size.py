#!/usr/bin/env python3
"""Round-11 — vary SPECIALIST model size, hold @picard at qwen30.

Tests the round-10 hypothesis: does a bigger FOSS specialist close the
substantive-editing competence gap that L1 primers couldn't fix?

Same setup as round-10 (5 tasks, two-phase, L1 primers injected) but
specialist model varies:
  S1 — qwen3-coder-30b   (round-10 baseline; control)
  S2 — llama-3.3-70b
  S3 — deepseek-r1
  S4 — gpt-oss-120b

3 variants (skip S1 control — already in round-10 data) × 5 tasks =
15 runs sequential. Compare to round-10 task-by-task.

@picard captain stays qwen30 in all variants — isolating the
specialist axis.
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
ARTIFACTS = REPO / "scripts/_round11_specialist_size_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"

PICARD_PRIMER_FILE = REPO / "docs/wiki/picard-agor-primer.org"
L1A_FILE = REPO / "docs/wiki/org-llm-cli-primer.org"
L1B_FILE = REPO / "docs/wiki/org-mode-per-agent-primer.org"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 600

CAPTAIN_MODEL_ID = "qwen/qwen3-coder-30b-a3b-instruct"  # locked

SPECIALIST_VARIANTS = [
    ("S2-llama70", {"provider": "openrouter",
                     "model": "meta-llama/llama-3.3-70b-instruct"}),
    ("S3-deepseekR1", {"provider": "openrouter",
                        "model": "deepseek/deepseek-r1"}),
    ("S4-gptoss120b", {"provider": "openrouter",
                        "model": "openai/gpt-oss-120b"}),
]


def _strip_org_meta(text):
    text = re.sub(r"^:PROPERTIES:.*?:END:\s*", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"^#\+\w+:.*$\n", "", text, flags=re.MULTILINE)
    return text.strip()


PICARD_PRIMER = _strip_org_meta(PICARD_PRIMER_FILE.read_text())
L1A_TEXT = _strip_org_meta(L1A_FILE.read_text())
L1B_TEXT = _strip_org_meta(L1B_FILE.read_text())


def _extract_l1_for_handle(handle):
    h = handle.lstrip("@").lower()
    sections = []
    m_base = re.search(r"\* Shared baseline.*?(?=\n\* )", L1B_TEXT, re.DOTALL)
    if m_base:
        sections.append("# === ORG-MODE SHARED BASELINE ===\n\n" + m_base.group(0).strip())
    m_b = re.search(rf"\* @{h} —.*?(?=\n\* )", L1B_TEXT, re.DOTALL | re.IGNORECASE)
    if m_b:
        sections.append(f"# === ORG-MODE EXPERTISE for @{h} ===\n\n" + m_b.group(0).strip())
    m_a = re.search(rf"\*\* @{h} —.*?(?=\n\*\* @|\n\* )",
                     L1A_TEXT, re.DOTALL | re.IGNORECASE)
    if m_a:
        sections.append(f"# === ORG-LLM CLI VERBS for @{h} ===\n\n" + m_a.group(0).strip())
    m_anti = re.search(r"\* Anti-patterns.*?(?=\n\* )", L1A_TEXT, re.DOTALL)
    if m_anti:
        sections.append("# === CLI ANTI-PATTERNS ===\n\n" + m_anti.group(0).strip())
    return "\n\n---\n\n".join(sections) if sections else ""


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(line + "\n")


def tok():
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin():
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                    capture_output=True, env=env, check=True)


def req(method, path, body=None, retries=1):
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


def ensure_opencode_serve():
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


def poll_terminal(sid, deadline=600):
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


def build_known_ids_map():
    wiki = REPO / "docs/wiki"
    id_to_meta, basename_to_ids, dec_to_id = {}, {}, {}
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


def prefetch_b1(task):
    target = REPO / task["target_file"]
    text = target.read_text()
    lines = text.splitlines()
    id_to_meta, basename_to_ids, dec_to_id = build_known_ids_map()
    roadmap_id = (basename_to_ids.get("roadmap") or [None])[0]
    candidates = []
    for ln_no, line in enumerate(lines, 1):
        if "[[id:" in line: continue
        for m in re.finditer(r"Phase \d{4}-\d{2}\.\d{2}\b", line):
            if roadmap_id:
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": m.group(0),
                                    "canonical_owner": "roadmap.org",
                                    "canonical_id": roadmap_id, "kind": "phase"})
        for m in re.finditer(r"\bDEC-\d+\b", line):
            label = m.group(0)
            if label in dec_to_id:
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": label,
                                    "canonical_owner": "decisions.org",
                                    "canonical_id": dec_to_id[label], "kind": "dec"})
        for stem, ids in basename_to_ids.items():
            for m in re.finditer(r"\b" + re.escape(stem) + r"\.org\b", line):
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": stem + ".org",
                                    "canonical_owner": stem + ".org",
                                    "canonical_id": ids[0], "kind": "basename"})
                break
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(lines), "known_ids_count": len(id_to_meta),
             "candidates": candidates}


def prefetch_passthrough(task):
    target = REPO / task["target_file"]
    if target.exists():
        text = target.read_text()
        return {"target_path": str(target.relative_to(REPO)),
                 "target_lines": len(text.splitlines()),
                 "target_size_bytes": len(text),
                 "first_50_lines": "\n".join(text.splitlines()[:50])}
    return {"target_path": task.get("target_file", ""), "missing": True}


def prefetch_b5(task):
    target = REPO / task["target_file"]
    text = target.read_text()
    paragraphs = re.split(r"\n\s*\n", text)
    longest = max(((i, p) for i, p in enumerate(paragraphs)), key=lambda x: len(x[1]))
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(text.splitlines()),
             "longest_paragraph_index": longest[0],
             "longest_paragraph_chars": len(longest[1]),
             "longest_paragraph_preview": longest[1][:500]}


def prefetch_b11(task):
    decisions_text = (REPO / "docs/wiki/decisions.org").read_text()
    nums = sorted({int(m.group(1)) for m in re.finditer(r"^\*\* DEC-(\d+)\s",
                                                          decisions_text, re.MULTILINE)})
    return {"decisions_path": "docs/wiki/decisions.org",
             "existing_dec_numbers": nums,
             "next_available": (max(nums) + 1) if nums else 1,
             "problem_statement": task.get("problem_statement", "")}


TASKS = [
    {"id": "B1", "label": "cross-link audit on literate-tools.org",
     "target_file": "docs/wiki/literate-tools.org",
     "goal": "Add EXACTLY 5 [[id:UUID][label]] cross-link wrappers around existing prose mentions. Use the canonical_id from the pre-fetched candidates list. Preserve =...= verbatim formatting INSIDE link labels. **CRITICAL: org-mode link syntax requires the literal `id:` prefix — write `[[id:<uuid>][label]]`, NOT `[[<uuid>][label]]`. Without `id:` the link will not resolve.** Do NOT touch any other file. Do NOT change section headings or structure — only wrap mentions inline.",
     "n_changes": 5, "prefetch": prefetch_b1},
    {"id": "B5", "label": "section rewrite for clarity",
     "target_file": "docs/wiki/agor-pilot-install.org",
     "goal": "Pick the longest paragraph (see prefetch.longest_paragraph_*) and rewrite IT for tightness — at least 30% shorter. Preserve meaning. Do NOT touch any other file.",
     "n_changes": 1, "prefetch": prefetch_b5},
    {"id": "B7", "label": "add docstrings + type hints",
     "target_file": "org_llm/avatars.py",
     "goal": "**EDIT ONLY THE FILE `org_llm/avatars.py`** — do not touch any other file. Add Python type hints + one-line docstrings to every public function/class. Existing behavior must not change. `python -c 'import org_llm.avatars'` must import cleanly.",
     "n_changes": None, "prefetch": prefetch_passthrough},
    {"id": "B11", "label": "draft DEC entry for an open question",
     "target_file": "docs/wiki/decisions.org",
     "problem_statement": ("Should @picard be auto-created at `org-llm init` (eager) "
                            "OR lazily-spawned on first team-spawn (lazy)?"),
     "goal": "**APPEND-ONLY: do NOT delete, modify, or shrink any existing DEC.** Add a new DEC at the END of docs/wiki/decisions.org. Use next-available number (see prefetch.next_available). Format: ** DEC-N — title (status). Body: Context, Options (≥2), Tradeoffs, Decision, Rationale. The file currently has DEC-001 through DEC-019 — your edit MUST PRESERVE ALL OF THEM and only ADD a new one at the bottom.",
     "n_changes": 1, "prefetch": prefetch_b11},
    {"id": "B13", "label": "update LICENSE copyright year",
     "target_file": "LICENSE",
     "goal": "Update copyright year `2025` → `2026`. If already 2026, no edit needed — print 'NO_CHANGE_NEEDED' and stop. Do NOT touch any other file.",
     "n_changes": 1, "prefetch": prefetch_passthrough},
]


PLAN_FORMAT = """
You are @picard executing PHASE 1. Output a JSON plan; the harness executes it.

PICARD_PLAN_JSON_BEGIN
{
  "classification": {"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"},
  "playbook": "<flat|A|B|C|D|E>",
  "team": [{"handle": "@<handle>", "task_brief": "<full multi-line brief>"}],
  "rationale": "<one paragraph>"
}
PICARD_PLAN_JSON_END
"""


def call_openrouter(model_id, prompt):
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    body = json.dumps({"model": model_id,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1, "max_tokens": 3000}).encode()
    req2 = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req2, timeout=120) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    cost = float(data.get("usage", {}).get("cost") or 0)
    return text, cost


def parse_plan(text):
    m = re.search(r"PICARD_PLAN_JSON_BEGIN\s*(.+?)\s*PICARD_PLAN_JSON_END",
                   text, re.DOTALL)
    if not m:
        m2 = re.search(r"(\{[^}]*\"classification\".+\})", text, re.DOTALL)
        if not m2: return None
        json_text = m2.group(1)
    else:
        json_text = m.group(1).strip()
    try: return json.loads(json_text)
    except json.JSONDecodeError: return None


def harness_spawn_specialist(wt_id, wt_name, task, handle, brief, prefetch,
                              specialist_model, run_dir):
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
    l1_primer = _extract_l1_for_handle(handle)
    n_rule = ""
    if task.get("n_changes") is not None:
        n_rule = (f"\n\nN-CHANGES RULE: make EXACTLY {task['n_changes']} change(s). "
                   f"If only K<N strong candidates, output `LANDED <K>`.")
    spec_prompt = (
        persona + "\n\n"
        + (l1_primer + "\n\n---\n\n" if l1_primer else "")
        + brief
        + n_rule
        + f"\n\nPRE-FETCHED CONTEXT:\n{json.dumps(prefetch, indent=2)}"
        + f"\n\nWorktree path: /home/daniel/.agor/worktrees/local/org-llm/{wt_name}"
        + "\n\nWhen done, run `git add` + `git commit` in the worktree, then "
        + f"print `{handle.upper().lstrip('@')}_DONE` and stop."
    )
    spawn_args = {"prompt": spec_prompt,
                   "title": f"r11-{task['id']}-{handle.lstrip('@')}",
                   "agenticTool": "opencode",
                   "modelConfig": specialist_model}
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
            "--max-budget-usd", "0.50", cap_prompt]
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
        except Exception: pass
    return spec_sid


def commit_if_uncommitted(wt_path, task_id):
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    if not diff_stat: return
    head_msg = subprocess.run(["git", "-C", str(wt_path), "log", "-1", "--format=%s", "trunk..HEAD"],
                                capture_output=True, text=True).stdout.strip()
    if head_msg: return
    subprocess.run(["git", "-C", str(wt_path), "add", "-A"],
                     capture_output=True, check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m",
                     f"r11 {task_id}: harness-side commit"],
                     capture_output=True)


def run_one_cell(task, variant_name, specialist_model):
    log(f" -- {variant_name}")
    cell_dir = ARTIFACTS / task['id'] / variant_name
    cell_dir.mkdir(parents=True, exist_ok=True)
    prefetch = task["prefetch"](task)

    n_rule_in_brief = (f"\nN-CHANGES RULE: make EXACTLY {task['n_changes']} change(s)."
                        if task.get("n_changes") is not None else "")

    phase1_prompt = (
        PICARD_PRIMER + "\n\n---\n\n"
        + f"TASK ({task['id']} — {task['label']}):\n{task['goal']}"
        + n_rule_in_brief
        + "\n\nPRE-FETCHED CONTEXT:\n" + json.dumps(prefetch, indent=2)
        + f"\n\nPRODUCTION CONSTRAINT: specialists run with modelConfig:\n"
        + json.dumps(specialist_model, indent=2)
        + "\n\n---\n\n" + PLAN_FORMAT
    )
    plan_text, p1_cost = call_openrouter(CAPTAIN_MODEL_ID, phase1_prompt)
    (cell_dir / "phase1_raw.txt").write_text(plan_text)
    plan = parse_plan(plan_text)
    if plan is None:
        return {"variant": variant_name, "task_id": task['id'],
                 "phase1_cost": p1_cost, "error": "plan_parse_failed"}
    (cell_dir / "phase1_plan.json").write_text(json.dumps(plan, indent=2))

    wt_name = f"r11-{task['id']}-{variant_name}-{EPOCH}"
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt_name, "ref": wt_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt_id = wt["worktree_id"]
    wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
    time.sleep(2)

    specialists = []
    for spec in plan.get("team", []):
        handle = spec.get("handle", "@unknown")
        brief = spec.get("task_brief", "")
        if not brief: continue
        sid = harness_spawn_specialist(wt_id, wt_name, task, handle, brief,
                                         prefetch, specialist_model, cell_dir)
        if sid:
            t = poll_terminal(sid)
            specialists.append({"handle": handle, "sid": sid, "terminal": t})
        else:
            specialists.append({"handle": handle, "sid": None, "terminal": "no-spawn"})

    commit_if_uncommitted(wt_path, task['id'])

    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    (cell_dir / "diff.patch").write_text(diff_full)
    log(f"    {variant_name} diff: {diff_stat or '(none)'}")
    return {"variant": variant_name, "task_id": task['id'],
             "specialist_model": specialist_model.get("model"),
             "phase1_cost_usd": round(p1_cost, 6),
             "plan_playbook": plan.get("playbook"),
             "specialists": specialists, "diff_stat": diff_stat,
             "wt_path": str(wt_path)}


# ── Main ────────────────────────────────────────────────────────────────
log(f"Round-11 — {len(TASKS)} tasks × {len(SPECIALIST_VARIANTS)} variants")
relogin()
opencode_proc = ensure_opencode_serve()

results = []
try:
    for task in TASKS:
        log(f"\n=== TASK {task['id']} ({task['label']}) ===")
        for variant_name, specialist_model in SPECIALIST_VARIANTS:
            try:
                r = run_one_cell(task, variant_name, specialist_model)
                results.append(r)
            except Exception as e:
                log(f"  {variant_name} ERROR: {e}")
                results.append({"variant": variant_name, "task_id": task['id'],
                                  "error": str(e)})

    summary = ARTIFACTS / f"summary-{EPOCH}.json"
    summary.write_text(json.dumps(results, indent=2))
    print()
    print("=" * 80)
    print(f"ROUND-11 — SPECIALIST-SIZE SUMMARY")
    print("=" * 80)
    for r in results:
        if "error" in r:
            print(f"  {r['variant']:>16} {r['task_id']:>4}  ERROR")
            continue
        print(f"  {r['variant']:>16} {r['task_id']:>4}  "
              f"playbook={r.get('plan_playbook'):>5}  "
              f"diff: {(r.get('diff_stat') or '(none)')[:50]}")
    print(f"\nsummary: {summary}")
finally:
    if opencode_proc is not None:
        log("stopping opencode serve")
        opencode_proc.terminate()
