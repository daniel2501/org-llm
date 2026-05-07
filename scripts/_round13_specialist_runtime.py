#!/usr/bin/env python3
"""Round-13 — same 5-task × 5-variant matrix as round-12, but specialists
run via the new `org_llm.specialist` direct-API runtime (W4 / S-A).

Round-12 found that opencode + non-default FOSS models silently fail or
produce destructive edits. W2 confirmed the models themselves are fine
when called direct. S-A built the production direct-API runtime
(`org_llm/specialist.py`). Round-13 swaps it in and re-runs the same
matrix for direct A/B vs round-12.

Architecture changes from round-12:
- Specialist runtime: `org_llm.specialist.run_specialist()` (direct OpenRouter
  + tool-use + Python-applies-edits). NO opencode. NO Agor MCP for the LLM
  call. NO claude-code subprocess.
- Captain (planner): unchanged — direct OpenRouter call to qwen30.
- Agor: still used for worktree management only (skipped for K5 Claude-solo,
  which is also unchanged).
- K5 baseline: unchanged direct `claude -p`.

Expected: dramatically cleaner FOSS specialist outputs vs round-12.
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

# Ensure we can import org_llm.specialist
REPO = Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(REPO))
from org_llm.specialist import (  # noqa: E402
    SpecialistTask, SpecialistResult, run_specialist,
)

ARTIFACTS = REPO / "scripts/_round13_specialist_runtime_artifacts"
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

CAPTAIN_MODEL_ID = "qwen/qwen3-coder-30b-a3b-instruct"

VARIANTS = [
    ("K1-qwen30",       "qwen/qwen3-coder-30b-a3b-instruct",  "agor"),
    ("K2-kimi-k2.6",    "moonshotai/kimi-k2.6",                "agor"),
    ("K3-deepseekR1",   "deepseek/deepseek-r1",                "agor"),
    ("K4-gptoss120b",   "openai/gpt-oss-120b",                 "agor"),
    ("K5-claude-solo",  None,                                   "claude-solo"),
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
    if m_base: sections.append("# === ORG-MODE SHARED BASELINE ===\n\n" + m_base.group(0).strip())
    m_b = re.search(rf"\* @{h} —.*?(?=\n\* )", L1B_TEXT, re.DOTALL | re.IGNORECASE)
    if m_b: sections.append(f"# === ORG-MODE EXPERTISE for @{h} ===\n\n" + m_b.group(0).strip())
    m_a = re.search(rf"\*\* @{h} —.*?(?=\n\*\* @|\n\* )", L1A_TEXT, re.DOTALL | re.IGNORECASE)
    if m_a: sections.append(f"# === ORG-LLM CLI VERBS for @{h} ===\n\n" + m_a.group(0).strip())
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


# ── Bundle prefetch (same as round-12) ───────────────────────────────────
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
                 "target_size_bytes": len(text)}
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
     "goal": "Add EXACTLY 5 [[id:UUID][label]] cross-link wrappers around existing prose mentions. Use the canonical_id from the pre-fetched candidates list. Preserve =...= verbatim formatting INSIDE link labels. **CRITICAL: org-mode link syntax requires the literal `id:` prefix.** Do NOT touch any other file. Do NOT change section headings or structure.",
     "n_changes": 5, "prefetch": prefetch_b1},
    {"id": "B5", "label": "section rewrite for clarity",
     "target_file": "docs/wiki/agor-pilot-install.org",
     "goal": "Pick the longest paragraph (see prefetch.longest_paragraph_*) and rewrite IT for tightness — at least 30% shorter. Preserve meaning. Do NOT touch any other file.",
     "n_changes": 1, "prefetch": prefetch_b5},
    {"id": "B7", "label": "add docstrings + type hints",
     "target_file": "org_llm/avatars.py",
     "goal": "**EDIT ONLY THE FILE `org_llm/avatars.py`** — do not touch any other file. Add Python type hints + one-line docstrings to every public function/class. Existing behavior must not change. `python -c 'import org_llm.avatars'` must import cleanly. If the file is already type-hinted, no-op and explain.",
     "n_changes": None, "prefetch": prefetch_passthrough},
    {"id": "B11", "label": "draft DEC entry for an open question",
     "target_file": "docs/wiki/decisions.org",
     "problem_statement": "Should @picard be auto-created at `org-llm init` (eager) OR lazily-spawned on first team-spawn (lazy)?",
     "goal": "**APPEND-ONLY: do NOT delete, modify, or shrink any existing DEC.** Add a new DEC at the END of docs/wiki/decisions.org. Use next-available number (see prefetch.next_available). Format: ** DEC-N — title (status). Body: Context, Options (≥2), Tradeoffs, Decision, Rationale.",
     "n_changes": 1, "prefetch": prefetch_b11},
    {"id": "B13", "label": "update LICENSE copyright year",
     "target_file": "LICENSE",
     "goal": "Update copyright year `2025` → `2026`. If already 2026, no edit needed.",
     "n_changes": 1, "prefetch": prefetch_passthrough},
]


PLAN_FORMAT = """
You are @picard executing PHASE 1. Output a JSON plan.

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


PERSONA_LOOKUP = {
    "@atoz": "You are @atoz — Bridge Crew wiki concept-graph specialist.",
    "@data": "You are @data — Bridge Crew code + scribe specialist.",
    "@spock": "You are @spock — Bridge Crew logic + canonical-source reviewer.",
    "@geordi": "You are @geordi — Bridge Crew analytics + charts specialist.",
    "@boothby": "You are @boothby — Bridge Crew ops + hygiene specialist.",
    "@riker": "You are @riker — Bridge Crew process + scheduling specialist.",
}


def run_specialist_via_module(wt_path, task, handle, brief, prefetch,
                                specialist_model):
    """Invoke org_llm.specialist directly. NO opencode, NO claude-code."""
    persona = PERSONA_LOOKUP.get(handle, f"You are {handle}.")
    l1_primer = _extract_l1_for_handle(handle)
    instruction = (
        (l1_primer + "\n\n---\n\n" if l1_primer else "")
        + brief
        + (f"\n\nN-CHANGES RULE: make EXACTLY {task['n_changes']} change(s)."
           if task.get("n_changes") is not None else "")
        + f"\n\nPRE-FETCHED CONTEXT:\n{json.dumps(prefetch, indent=2)}"
    )
    target_files = []
    if task.get("target_file"):
        target_files.append(wt_path / task["target_file"])
    spec_task = SpecialistTask(
        handle=handle,
        persona=persona,
        instruction=instruction,
        workdir=wt_path,
        model=specialist_model,
        target_files=target_files,
        max_iterations=8,
        max_budget_usd=1.00,
    )
    result = run_specialist(spec_task)
    # Auto-commit if there are edits
    if result.edits_applied:
        subprocess.run(["git", "-C", str(wt_path), "add", "-A"],
                         capture_output=True, check=True)
        subprocess.run(["git", "-C", str(wt_path), "commit", "-m",
                          f"r13 {task['id']} {handle}: specialist edits"],
                         capture_output=True)
    return result


def run_claude_solo(task, run_dir):
    wt_name = f"r13-{task['id']}-K5-claude-solo-{EPOCH}"
    wt_path = REPO.parent / "org-llm-worktrees" / wt_name
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-b", wt_name,
                     str(wt_path), "trunk"], check=True, capture_output=True)
    prefetch = task.get("prefetch", lambda t: {})(task)
    prompt = (f"You are a software/wiki specialist working in worktree {wt_path}.\n\n"
               f"TASK ({task['id']} — {task['label']}):\n{task['goal']}\n\n"
               + (f"PRE-FETCHED CONTEXT:\n{json.dumps(prefetch, indent=2)}\n\n" if prefetch else "")
               + "When done, run `git add` + `git commit`, then print TASK_DONE. "
               "If no-op, explain in one line + commit nothing + print TASK_DONE.")
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    out = run_dir / "stream.jsonl"
    cmd = ["claude", "-p", "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", "1.00", prompt]
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
    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    (run_dir / "diff.patch").write_text(diff_full)
    return {"variant": "K5-claude-solo", "task_id": task['id'],
             "is_external_baseline": True, "cost_usd": round(cost, 4),
             "wall_seconds": round(elapsed, 1), "diff_stat": diff_stat,
             "wt_path": str(wt_path)}


def run_one_cell(task, variant_name, specialist_model_id, mode, run_dir):
    if mode == "claude-solo":
        log(f" -- {variant_name} (Claude solo, no Agor)")
        return run_claude_solo(task, run_dir)

    log(f" -- {variant_name} (FOSS via specialist module: {specialist_model_id})")
    prefetch = task["prefetch"](task)
    n_rule_in_brief = (f"\nN-CHANGES RULE: make EXACTLY {task['n_changes']} change(s)."
                        if task.get("n_changes") is not None else "")
    phase1_prompt = (
        PICARD_PRIMER + "\n\n---\n\n"
        + f"TASK ({task['id']} — {task['label']}):\n{task['goal']}"
        + n_rule_in_brief
        + "\n\nPRE-FETCHED CONTEXT:\n" + json.dumps(prefetch, indent=2)
        + f"\n\nPRODUCTION CONSTRAINT: specialist runs on `{specialist_model_id}` "
        + "via `org_llm.specialist` (direct API; no opencode)."
        + "\n\n---\n\n" + PLAN_FORMAT
    )
    plan_text, p1_cost = call_openrouter(CAPTAIN_MODEL_ID, phase1_prompt)
    (run_dir / "phase1_raw.txt").write_text(plan_text)
    plan = parse_plan(plan_text)
    if plan is None:
        return {"variant": variant_name, "task_id": task['id'],
                 "phase1_cost": p1_cost, "error": "plan_parse_failed"}
    (run_dir / "phase1_plan.json").write_text(json.dumps(plan, indent=2))

    # Worktree via Agor REST (still works fine for worktree mgmt)
    wt_name = f"r13-{task['id']}-{variant_name}-{EPOCH}"
    wt = req("POST", f"/repos/{REPO_ID}/worktrees", {
        "name": wt_name, "ref": wt_name, "createBranch": True,
        "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
    wt_path = Path.home() / ".agor/worktrees/local/org-llm" / wt_name
    time.sleep(2)

    specialists = []
    total_spec_cost = 0.0
    for spec in plan.get("team", []):
        handle = spec.get("handle", "@unknown")
        brief = spec.get("task_brief", "")
        if not brief: continue
        result = run_specialist_via_module(wt_path, task, handle, brief,
                                             prefetch, specialist_model_id)
        total_spec_cost += result.cost_usd
        specialists.append({
            "handle": handle,
            "success": result.success,
            "iterations": result.iterations,
            "edits_applied_count": len(result.edits_applied),
            "cost_usd": result.cost_usd,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
            "text_output": (result.text_output or "")[:400],
        })

    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", "trunk"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat", "trunk"],
                                 capture_output=True, text=True).stdout.strip()
    (run_dir / "diff.patch").write_text(diff_full)
    return {"variant": variant_name, "task_id": task['id'],
             "specialist_model": specialist_model_id,
             "phase1_cost_usd": round(p1_cost, 6),
             "specialist_cost_usd": round(total_spec_cost, 6),
             "plan_playbook": plan.get("playbook"),
             "specialists": specialists, "diff_stat": diff_stat,
             "wt_path": str(wt_path)}


# ── Main ────────────────────────────────────────────────────────────────
log(f"Round-13 — specialist runtime (W4); {len(TASKS)} tasks × {len(VARIANTS)} variants")
relogin()

results = []
for task in TASKS:
    log(f"\n=== TASK {task['id']} ({task['label']}) ===")
    for variant_name, model_id, mode in VARIANTS:
        cell_dir = ARTIFACTS / task['id'] / variant_name
        cell_dir.mkdir(parents=True, exist_ok=True)
        try:
            r = run_one_cell(task, variant_name, model_id, mode, cell_dir)
            results.append(r)
            if mode == "claude-solo":
                cost = r.get("cost_usd", 0)
            else:
                cost = r.get("phase1_cost_usd", 0) + r.get("specialist_cost_usd", 0)
            log(f"    {variant_name} cost=${cost:.4f} diff: {(r.get('diff_stat') or '(none)')[:50]}")
        except Exception as e:
            log(f"  {variant_name} ERROR: {e}")
            results.append({"variant": variant_name, "task_id": task['id'],
                              "error": str(e)})

summary = ARTIFACTS / f"summary-{EPOCH}.json"
summary.write_text(json.dumps(results, indent=2))
print()
print("=" * 80)
print(f"ROUND-13 — SPECIALIST RUNTIME (W4) SUMMARY")
print("=" * 80)
for r in results:
    if "error" in r:
        print(f"  {r['variant']:>16} {r['task_id']:>4}  ERROR")
        continue
    if r.get("is_external_baseline"):
        cost = r.get("cost_usd", 0)
    else:
        cost = r.get("phase1_cost_usd", 0) + r.get("specialist_cost_usd", 0)
    print(f"  {r['variant']:>16} {r['task_id']:>4}  ${cost:>5.3f}  "
          f"diff: {(r.get('diff_stat') or '(none)')[:50]}")
print(f"\nsummary: {summary}")
