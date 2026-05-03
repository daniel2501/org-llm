#!/usr/bin/env python3
"""Blinded judge for the recipe A/B harness output.

Reads /tmp/recipe_ab_results.jsonl produced by recipe_ab_harness.py.

For each prompt with both arms present, randomises (recipes_on, recipes_off)
into (X, Y), and shells out to `claude -p` (Claude CLI, model=Opus 4.7) with
the rubric + ground-truth hint (when available), capturing the judge's
preference + reasoning.

Authentication piggybacks on the Claude CLI's existing OAuth/keychain — no
ANTHROPIC_API_KEY is required. The judge is invoked from cwd=/tmp so the
user's project memory (which contains "orchestration top priority") cannot
bias the verdict.

Aggregates:
  - per-bucket win rate (anchor / control / bait)
  - per-recipe win rate (where applicable)
  - latency + token deltas (recipes_on vs recipes_off)

Outputs:
  /tmp/recipe_ab_judgments.jsonl   — one row per prompt × trial
  /tmp/recipe_ab_summary.md        — roll-up the user can read

Decision bar (from the experiment design):
  recipe-eligible (anchor) prompts must clear ALL THREE:
    p50 latency:   recipes_on ≥ 30% faster than recipes_off
    total tokens:  recipes_on uses ≥ 20% fewer tokens
    quality:       judge prefers recipes_on in ≥ 55% of pairs
  control prompts must clear:
    quality regression check — judge prefers recipes_off ≤ 55%
  bait prompts inform whether false-fires cause measurable harm.

The rubric document is /home/daniel/repos/org-llm/docs/wiki/2026-05-03-recipe-ab-ground-truth.org —
this script reads the ground-truth section so the judge sees facts the
narration must respect, not just the prompt-pair.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

REPO = Path("/home/daniel/repos/org-llm")
RESULTS = Path("/tmp/recipe_ab_results.jsonl")
JUDGMENTS = Path("/tmp/recipe_ab_judgments.jsonl")
SUMMARY = Path("/tmp/recipe_ab_summary.md")
GT_DOC = REPO / "docs/wiki/2026-05-03-recipe-ab-ground-truth.org"

JUDGE_MODEL = "claude-opus-4-7"
JUDGE_CWD   = "/tmp"   # isolate from project memory / CLAUDE.md auto-discovery

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "winner":     {"type": "string", "enum": ["X", "Y", "tie"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason":     {"type": "string"},
        "x_score":    {"type": "object",
                       "properties": {"correctness":        {"type": "integer"},
                                      "citations":          {"type": "integer"},
                                      "intent":             {"type": "integer"},
                                      "brevity":            {"type": "integer"},
                                      "hallucination_free": {"type": "integer"}}},
        "y_score":    {"type": "object",
                       "properties": {"correctness":        {"type": "integer"},
                                      "citations":          {"type": "integer"},
                                      "intent":             {"type": "integer"},
                                      "brevity":            {"type": "integer"},
                                      "hallucination_free": {"type": "integer"}}},
    },
    "required": ["winner", "confidence", "reason"],
}


def _load_results() -> list[dict]:
    if not RESULTS.exists():
        print(f"missing {RESULTS} — run recipe_ab_harness.py first.", file=sys.stderr)
        sys.exit(2)
    rows = []
    for line in RESULTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _parse_ground_truth(doc: Path) -> dict[str, str]:
    """Extract per-prompt-id ground truth blocks from the .org doc.
    Format expected: each block starts with a heading like `** A1 — ...`
    and has a `:GT:` block of facts the answer must respect.
    """
    if not doc.exists():
        return {}
    text = doc.read_text()
    out: dict[str, str] = {}
    for m in re.finditer(r"^\*+\s+([A-Z]\d+)\s*[—-]\s*(.+?)$\n(.*?)(?=^\*+\s+[A-Z]\d+\s*|\Z)",
                         text, re.MULTILINE | re.DOTALL):
        pid = m.group(1).strip()
        body = m.group(3)
        gtm = re.search(r"#\+begin_example\s*:GT:\n(.*?)#\+end_example",
                        body, re.DOTALL)
        if gtm:
            out[pid] = gtm.group(1).strip()
    return out


def _pair_results(rows: list[dict]) -> list[tuple[dict, dict]]:
    """Pair (recipes_on, recipes_off) by (prompt_id, trial). Skips
    prompts where one arm errored or is missing."""
    by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if "error" in r or "arm" not in r:
            continue
        by_key[(r["prompt_id"], r["trial"])][r["arm"]] = r
    pairs = []
    for key, arms in by_key.items():
        a = arms.get("recipes_on")
        b = arms.get("recipes_off")
        if a and b:
            pairs.append((a, b))
    return pairs


JUDGE_RUBRIC = """\
You are a strict, calibrated evaluator comparing two answers to the same user
question about the user's personal org-roam vault.

Compare RESPONSE_X and RESPONSE_Y on these axes (in priority order):

1. Factual correctness vs. ground truth (when provided). If the ground-truth
   block contradicts an answer, that answer LOSES on this axis even if it
   reads well.
2. Citation accuracy: any specific files, dates, counts, or quotes mentioned
   should be supported by the ground-truth or be plausibly grounded. Bare
   prose without citations is acceptable when the question doesn't demand
   them; invented files or dates are not.
3. Intent match: does the answer address what the user actually asked, or
   does it deflect / over-explain / answer a different question?
4. Brevity / clarity: 1-3 sentence answers are preferred unless the question
   genuinely demands more. A long verbose answer is NOT better than a tight
   correct one.
5. No hallucination of vault content. If neither response has data, the one
   that admits "I don't have data" beats the one that fabricates.

Output STRICT JSON (no markdown fence, no commentary):
{
  "winner":     "X" | "Y" | "tie",
  "confidence": "low" | "medium" | "high",
  "reason":     "<≤ 200 chars: the deciding factor>",
  "x_score":    {"correctness": 0-3, "citations": 0-3, "intent": 0-3,
                  "brevity": 0-3, "hallucination_free": 0-3},
  "y_score":    {"correctness": 0-3, "citations": 0-3, "intent": 0-3,
                  "brevity": 0-3, "hallucination_free": 0-3}
}

Use "tie" only when X and Y are genuinely indistinguishable on the priority
axes. Bias toward picking a winner when one is even modestly better.
"""


def call_judge(prompt: str, gt: str, x_text: str, y_text: str,
               *, claude_bin: str = "claude",
               timeout: int = 180) -> dict:
    """Shell out to `claude -p` (Claude CLI) with the rubric as system
    prompt and the prompt+gt+responses as the user message. Parses the
    JSON envelope and the inner verdict JSON.
    """
    user_block = f"""USER QUESTION:
{prompt}

GROUND TRUTH (facts the answer must respect; absent if N/A):
{gt or '(none provided — judge on internal consistency + plausibility only)'}

RESPONSE_X:
{x_text}

RESPONSE_Y:
{y_text}
"""
    cmd = [
        claude_bin, "-p",
        "--model", JUDGE_MODEL,
        "--system-prompt", JUDGE_RUBRIC,
        "--output-format", "json",
        "--no-session-persistence",
        user_block,
    ]
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout, cwd=JUDGE_CWD, check=False)
    except subprocess.TimeoutExpired:
        return {"winner": "error", "confidence": "low",
                "reason": f"claude CLI timed out after {timeout}s",
                "_judge_latency_s": round(time.time() - t0, 2)}
    elapsed = time.time() - t0
    if cp.returncode != 0:
        return {"winner": "error", "confidence": "low",
                "reason": f"claude exit={cp.returncode}: {cp.stderr[:200]}",
                "_judge_latency_s": round(elapsed, 2)}
    try:
        envelope = json.loads(cp.stdout)
    except Exception:
        return {"winner": "error", "confidence": "low",
                "reason": f"claude stdout not JSON: {cp.stdout[:200]}",
                "_judge_latency_s": round(elapsed, 2)}
    text = (envelope.get("result") or "").strip()
    # If the model wrapped its JSON in a fence, peel it.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        verdict = json.loads(text)
    except Exception:
        verdict = {"winner": "tie", "confidence": "low",
                   "reason": f"unparsed verdict: {text[:120]}"}
    verdict["_judge_latency_s"] = round(elapsed, 2)
    verdict["_total_cost_usd"]  = envelope.get("total_cost_usd")
    return verdict


def _summarise(judgments: list[dict]) -> str:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_recipe: dict[str, list[dict]] = defaultdict(list)
    lat_on, lat_off, tok_on, tok_off = [], [], [], []
    for j in judgments:
        by_bucket[j["bucket"]].append(j)
        if j.get("recipe"):
            by_recipe[j["recipe"]].append(j)
        if j["bucket"] == "anchor":
            lat_on.append(j["lat_on"]); lat_off.append(j["lat_off"])
            tok_on.append(j["tok_on"]); tok_off.append(j["tok_off"])

    def winrate(rows, mapped="recipes_on"):
        if not rows:
            return (0, 0, 0)
        on = sum(1 for r in rows if r["true_winner"] == mapped)
        off = sum(1 for r in rows if r["true_winner"] == ("recipes_off" if mapped == "recipes_on" else "recipes_on"))
        tie = len(rows) - on - off
        return (on, off, tie)

    out = ["# Recipe A/B — judge summary",
           f"trials judged: {len(judgments)}",
           ""]

    for bucket in ("anchor", "control", "bait"):
        rows = by_bucket.get(bucket, [])
        on, off, tie = winrate(rows)
        n = max(len(rows), 1)
        out.append(f"## {bucket} ({len(rows)} pairs)")
        out.append(f"  recipes_on  wins: {on}  ({on/n:.0%})")
        out.append(f"  recipes_off wins: {off} ({off/n:.0%})")
        out.append(f"  ties:             {tie} ({tie/n:.0%})")
        out.append("")

    if lat_on:
        out.append("## Latency + tokens (anchor bucket only)")
        out.append(f"  recipes_on  p50 latency: {median(lat_on):.2f}s   "
                   f"median tokens: {int(median(tok_on))}")
        out.append(f"  recipes_off p50 latency: {median(lat_off):.2f}s   "
                   f"median tokens: {int(median(tok_off))}")
        if median(lat_off) > 0:
            out.append(f"  latency reduction: {(1 - median(lat_on)/median(lat_off))*100:+.1f}%")
        if median(tok_off) > 0:
            out.append(f"  token reduction:   {(1 - median(tok_on)/median(tok_off))*100:+.1f}%")
        out.append("")

    if by_recipe:
        out.append("## Per-recipe win rate")
        for name, rows in sorted(by_recipe.items()):
            on, off, tie = winrate(rows)
            n = max(len(rows), 1)
            out.append(f"  {name:<28} on={on}/{n} ({on/n:.0%})  off={off}/{n}  tie={tie}/{n}")
        out.append("")

    # Decision bar
    anchor_rows = by_bucket.get("anchor", [])
    on_a, off_a, tie_a = winrate(anchor_rows)
    quality_pass  = (on_a / max(len(anchor_rows), 1)) >= 0.55 if anchor_rows else False
    latency_pass  = (lat_on and median(lat_off) > 0
                     and (1 - median(lat_on)/median(lat_off)) >= 0.30)
    tokens_pass   = (tok_on and median(tok_off) > 0
                     and (1 - median(tok_on)/median(tok_off)) >= 0.20)
    control_rows  = by_bucket.get("control", [])
    on_c, off_c, _ = winrate(control_rows)
    control_safe  = (off_c / max(len(control_rows), 1)) <= 0.55 if control_rows else True
    out.append("## Decision bar")
    out.append(f"  [anchor] quality ≥ 55% recipes_on:    {'PASS' if quality_pass else 'FAIL'}")
    out.append(f"  [anchor] p50 latency ≥ 30% reduction:  {'PASS' if latency_pass else 'FAIL'}")
    out.append(f"  [anchor] median tokens ≥ 20% reduction: {'PASS' if tokens_pass else 'FAIL'}")
    out.append(f"  [control] no regression (≤55% to off): {'PASS' if control_safe else 'FAIL'}")
    out.append("")
    if quality_pass and latency_pass and tokens_pass and control_safe:
        out.append("→ Recipes earn their keep across the board. Keep, expand the catalog.")
    elif (quality_pass + latency_pass + tokens_pass) >= 2 and control_safe:
        out.append("→ Mixed result. Consider narrowing the catalog to the recipes that "
                   "individually clear the bar (see per-recipe win rate above).")
    else:
        out.append("→ Recipes do NOT clear the bar. Recommend dumping or reducing to "
                   "the deterministic count_across_vault case if it's the only one with "
                   "real value.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude",
                    help="Path to the Claude CLI binary (default: $PATH `claude`).")
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--out-judgments", type=Path, default=JUDGMENTS)
    ap.add_argument("--out-summary",   type=Path, default=SUMMARY)
    ap.add_argument("--seed",          type=int, default=42,
                    help="Seed for X/Y label randomisation.")
    args = ap.parse_args()

    if not Path(args.claude_bin).exists() and not shutil.which(args.claude_bin):
        print(f"ERROR: claude CLI not found at {args.claude_bin}. "
              "Install Claude Code and try again.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    rows = _load_results()
    pairs = _pair_results(rows)
    if not pairs:
        print("no complete (recipes_on, recipes_off) pairs found.", file=sys.stderr)
        return 1
    gt_map = _parse_ground_truth(GT_DOC)
    print(f"judging {len(pairs)} pairs (model={JUDGE_MODEL}, gt={len(gt_map)} entries)",
          flush=True)

    args.out_judgments.write_text("")
    judgments = []
    for i, (a, b) in enumerate(pairs, 1):
        # Blind: randomise which arm gets X vs Y.
        if rng.random() < 0.5:
            x, y = a, b
            x_arm, y_arm = "recipes_on", "recipes_off"
        else:
            x, y = b, a
            x_arm, y_arm = "recipes_off", "recipes_on"
        gt = gt_map.get(a["prompt_id"], "")
        try:
            verdict = call_judge(prompt=a["prompt"], gt=gt,
                                 x_text=x.get("narration", ""),
                                 y_text=y.get("narration", ""),
                                 claude_bin=args.claude_bin)
        except Exception as e:
            verdict = {"winner": "error", "reason": f"{type(e).__name__}: {e}"}
        true_winner = (
            x_arm if verdict.get("winner") == "X" else
            y_arm if verdict.get("winner") == "Y" else
            "tie" if verdict.get("winner") == "tie" else "error"
        )
        rec = {
            "prompt_id":    a["prompt_id"],
            "bucket":       a["bucket"],
            "trial":        a["trial"],
            "prompt":       a["prompt"],
            "recipe":       a.get("recipe"),
            "x_arm":        x_arm,
            "y_arm":        y_arm,
            "true_winner":  true_winner,
            "verdict":      verdict,
            "lat_on":       a["latency_s"],
            "lat_off":      b["latency_s"],
            "tok_on":       a["usage"]["total_tokens"],
            "tok_off":      b["usage"]["total_tokens"],
            "rounds_off":   b.get("rounds", 1),
            "narration_on":  a.get("narration", "")[:600],
            "narration_off": b.get("narration", "")[:600],
        }
        judgments.append(rec)
        with args.out_judgments.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"  [{i}/{len(pairs)}] {rec['prompt_id']:<3} "
              f"winner={true_winner:<14} ({verdict.get('confidence','-')}) "
              f"— {verdict.get('reason','')[:80]}", flush=True)

    summary = _summarise(judgments)
    args.out_summary.write_text(summary)
    print(f"\n→ {args.out_judgments}\n→ {args.out_summary}")
    print("\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
