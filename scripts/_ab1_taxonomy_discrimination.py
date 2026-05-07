#!/usr/bin/env python3
"""A/B 1 — Taxonomy discrimination harness.

Test whether 3 judges classify the same 6 tasks the same way on
Tier-1 axes (Judgment / Recurrence / Stakes). Outputs an inter-judge
agreement matrix + flags axes whose level definitions need sharpening.

Judges:
  1. @picard-Claude (sonnet-4-6 via claude -p)
  2. @picard-Opus   (opus-4-7   via claude -p)
  3. @picard-FOSS   (qwen3-coder-30b via OpenRouter HTTP API)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
ARTIFACTS = REPO / "scripts/_ab1_taxonomy_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())

# ── Axis definitions ─────────────────────────────────────────────────────
AXIS_DEFS = """
TIER-1 AXES (each task is classified on all three):

AXIS 1 — Judgment level (4 levels):
  mechanical     — Rule-based; no nuance; a regex could do it.
                    e.g. "rename `foo` → `bar` across the codebase".
                    LLM is *worse* than sed for this.
  pattern-match  — Regular structure with light recognition judgment.
                    e.g. "wrap canonical-page mentions in [[id:UUID][label]]".
                    Pattern is mechanical; recognition of "canonical mention"
                    is the LLM bit.
  nuanced        — Multiple subjective choices; none individually correct;
                    reviewer questions can redirect.
                    e.g. "refactor this function for clarity" — which split?
                    what new names? where to keep coupling?
  open-ended     — No clear success criterion until done; multiple
                    legitimate answers.
                    e.g. "design a new agent for X" / "what should we
                    deprecate?"

AXIS 2 — Recurrence (2 levels):
  one-off    — Task class fires once or a few times.
  recurring  — Task class fires repeatedly across many inputs.
  THRESHOLD QUESTION: "Will this same task class fire again with different
  inputs in the foreseeable future?" Yes → recurring.

AXIS 3 — Stakes (2 levels):
  low   — Cheap revert; no user-visible impact if wrong.
          e.g. wiki edit (`git revert` fixes it).
  high  — Expensive revert OR user-facing impact OR security-relevant.
          e.g. code shipping to prod; auth changes; vault corruption risk.
  THRESHOLD QUESTION: "If the worst-case output landed unreviewed, would
  it materially harm the project?" Yes → high.
"""

TASKS = [
    {
        "id": "task_1",
        "label": "remove `extract` verb",
        "description": "Remove the `org-llm extract` Typer command, "
                       "delete `org_llm/extract.py` and `tests/test_extract.py`. "
                       "User decided the verb is out of scope. Trunk-level "
                       "cleanup; no parallel callers.",
    },
    {
        "id": "task_2",
        "label": "audit cross-links on agent-time-awareness.org",
        "description": "Wiki page `docs/wiki/agent-time-awareness.org` "
                       "(213 lines) is a concept-graph orphan with zero "
                       "outgoing `[[id:UUID]]` cross-links. Identify 5 "
                       "places to add cross-link wrappers around existing "
                       "prose mentions of canonical wiki concepts. Apply "
                       "the edits + commit.",
    },
    {
        "id": "task_3",
        "label": "rename test_perf.py → test_perf_helpers.py + apply option a+b",
        "description": "Refactor opportunity in `tests/test_perf.py` "
                       "(committed earlier today). Pick ONE of three "
                       "improvements (rename file / convert classes to flat "
                       "functions / add property-based coverage) — "
                       "consideration of project conventions matters; "
                       "reviewer questions can redirect the choice.",
    },
    {
        "id": "task_4",
        "label": "fix BUG-9 (opencode block missing in ~/.agor/config.yaml default)",
        "description": "Round-3 finding: `~/.agor/config.yaml` ships without "
                       "an `opencode:` block, making /opencode/health REST "
                       "endpoint return error until user manually adds one. "
                       "Fix is to bake the block (3 lines) into the agor "
                       "init template upstream.",
    },
    {
        "id": "task_5",
        "label": "wire literate MCP tools at startup (Phase 2026-05.16 — literate MCP tools)",
        "description": "Phase 2026-05.16.01 — literate MCP tools — register "
                       "user-supplied MCP tools at app startup from a "
                       "literate `~/org/org-llm-tools.org`. Touches the "
                       "MCP server bootstrap path; involves design choices "
                       "(error handling, naming convention, capability "
                       "gating). Affects every agent's tool surface.",
    },
    {
        "id": "task_6",
        "label": "decide where multi-agent-org-llm sits in the YYYY-MM.PP roadmap scheme",
        "description": "The `multi-agent-org-llm` feature has been validated "
                       "across rounds 1-4 but lacks a roadmap phase number. "
                       "Decide: position relative to v1.0 gate, sub-phases, "
                       "dependency on other phases, naming. No clear right "
                       "answer; multiple legitimate placements.",
    },
]

# Pre-committed bar (for evaluation only — judges are NOT shown this)
EXPECTED = {
    "task_1": {"judgment": "mechanical", "recurrence": "one-off", "stakes": "low"},
    "task_2": {"judgment": "pattern-match", "recurrence": "recurring", "stakes": "low"},
    "task_3": {"judgment": "nuanced", "recurrence": "one-off", "stakes": "low"},
    "task_4": {"judgment": "mechanical", "recurrence": "one-off", "stakes": "low"},
    "task_5": {"judgment": "nuanced", "recurrence": "one-off", "stakes": "high"},
    "task_6": {"judgment": "open-ended", "recurrence": "one-off", "stakes": "high"},
}

# ── Judge prompt ─────────────────────────────────────────────────────────
def build_prompt() -> str:
    tasks_block = "\n".join(
        f"  {t['id']}: {t['label']}\n"
        f"    Description: {t['description']}"
        for t in TASKS
    )
    return f"""You are @picard, Bridge Crew captain. Your job right now is to classify 6 real org-llm tasks on three taxonomy axes. This classification will drive which playbook (A/B/C/D/E or flat) gets used to execute each task.

{AXIS_DEFS}

TASKS TO CLASSIFY:
{tasks_block}

For each task, classify it on ALL THREE axes. Return ONLY a JSON object with the exact shape below — no other text, no markdown fence, no commentary:

{{
  "task_1": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}},
  "task_2": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}},
  "task_3": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}},
  "task_4": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}},
  "task_5": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}},
  "task_6": {{"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"}}
}}

Valid levels per axis:
  judgment: mechanical | pattern-match | nuanced | open-ended
  recurrence: one-off | recurring
  stakes: low | high

Output the JSON object only. Begin:"""


# ── Judge runners ────────────────────────────────────────────────────────
def run_claude_judge(model: str, label: str) -> tuple[dict | None, float]:
    """Run claude -p with the chosen model. Uses the auth'd CLI."""
    prompt = build_prompt()
    out_path = ARTIFACTS / f"{label}-{EPOCH}.txt"
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    cmd = ["claude", "-p", "--model", model,
           "--output-format", "text",
           "--max-budget-usd", "0.50",
           prompt]
    print(f"[{label}] running claude -p --model {model}...")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    elapsed = time.time() - t0
    out_path.write_text(result.stdout)
    print(f"[{label}] rc={result.returncode} elapsed={elapsed:.1f}s")
    parsed = parse_json_output(result.stdout, label)
    return parsed, elapsed


def run_openrouter_judge(model_id: str, label: str) -> tuple[dict | None, float]:
    """Direct OpenRouter HTTPS call for the FOSS judge."""
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                             capture_output=True, text=True, check=True).stdout.strip()
    prompt = build_prompt()
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1500,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    print(f"[{label}] HTTPS POST to OpenRouter ({model_id})...")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    content = data["choices"][0]["message"]["content"]
    out_path = ARTIFACTS / f"{label}-{EPOCH}.txt"
    out_path.write_text(content)
    print(f"[{label}] elapsed={elapsed:.1f}s tokens={data.get('usage', {})}")
    parsed = parse_json_output(content, label)
    return parsed, elapsed


def parse_json_output(text: str, label: str) -> dict | None:
    """Extract the JSON object from possibly-fenced output."""
    # Strip markdown fences if present
    text = text.strip()
    if text.startswith("```"):
        # Remove first line + trailing fence
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    # Find first { and last }
    try:
        start = text.index("{")
        end = text.rindex("}")
        return json.loads(text[start:end + 1])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[{label}] JSON parse FAILED: {e}")
        print(f"[{label}] raw output (first 400 chars): {text[:400]}")
        return None


# ── Main ─────────────────────────────────────────────────────────────────
print(f"=== A/B 1 — Taxonomy Discrimination ({len(TASKS)} tasks, 3 judges) ===")
print()

results = {}
results["claude_sonnet"], _ = run_claude_judge("sonnet", "claude_sonnet")
results["claude_opus"], _ = run_claude_judge("opus", "claude_opus")
results["foss_qwen30"], _ = run_openrouter_judge(
    "qwen/qwen3-coder-30b-a3b-instruct", "foss_qwen30")

# ── Compute agreement matrix ──────────────────────────────────────────────
print()
print("=" * 80)
print("CLASSIFICATION TABLE")
print("=" * 80)
axes = ["judgment", "recurrence", "stakes"]
judges = ["claude_sonnet", "claude_opus", "foss_qwen30"]

print(f"{'task':>9} | {'axis':>11} | {'expected':>14} | "
      + " | ".join(f"{j:>14}" for j in judges) + " | unanimous?")
print("-" * 110)

axis_agreement = {a: 0 for a in axes}
axis_total = {a: 0 for a in axes}
expected_match = {a: {j: 0 for j in judges} for a in axes}

for task in TASKS:
    tid = task["id"]
    for axis in axes:
        exp = EXPECTED[tid][axis]
        cells = []
        for judge in judges:
            r = results.get(judge) or {}
            v = (r.get(tid) or {}).get(axis, "?")
            cells.append(v)
        unanimous = len(set(cells)) == 1 and "?" not in cells
        axis_total[axis] += 1
        if unanimous:
            axis_agreement[axis] += 1
        for j, v in zip(judges, cells):
            if v == exp:
                expected_match[axis][j] += 1
        print(f"{tid:>9} | {axis:>11} | {exp:>14} | "
              + " | ".join(f"{v:>14}" for v in cells)
              + f" | {'✓' if unanimous else '✗'}")

# ── Summary ──────────────────────────────────────────────────────────────
print()
print("=" * 80)
print("INTER-JUDGE AGREEMENT (% unanimous across all 6 tasks)")
print("=" * 80)
for axis in axes:
    pct = 100 * axis_agreement[axis] / axis_total[axis] if axis_total[axis] else 0
    band = ("CLEAN" if pct >= 85
             else "SHARPEN" if pct >= 70
             else "RE-THINK")
    print(f"  {axis:>11} : {axis_agreement[axis]}/{axis_total[axis]} = "
          f"{pct:.0f}%  [{band}]")
print()
print("PER-JUDGE MATCH vs PRE-COMMITTED BAR (% match on each axis)")
print("=" * 80)
for axis in axes:
    print(f"  {axis:>11} : "
          + " ".join(f"{j}={100 * expected_match[axis][j] / axis_total[axis]:.0f}%"
                     for j in judges))

summary = ARTIFACTS / f"summary-{EPOCH}.json"
summary.write_text(json.dumps({
    "tasks": TASKS,
    "expected": EXPECTED,
    "results": results,
    "axis_agreement": axis_agreement,
    "axis_total": axis_total,
    "expected_match": expected_match,
}, indent=2))
print(f"\nsummary: {summary}")
