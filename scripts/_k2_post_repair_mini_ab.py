#!/usr/bin/env python3
"""K2-Kimi-K2.6 post-warmup-repair mini-A/B (R28 follow-up).

R28 main round dropped K2 at warmup because the harness probe checked
=message.content= only, while Kimi-K2.6 emits its reply in
=message.reasoning= (chain-of-thought enabled by default). The fix
landed in =_round28_dials.py:_WARMUP_REASONING_DISABLE= but the round
was already past warmup. This script fires K2 cells across K2-relevant
tasks (wiki, dec_draft, elisp) so we have K2 data alongside R28's main
results — without re-running the whole round.

Tasks: B5 (wiki_edit) · B11 (dec_draft) · B25 (elisp) · B26 (elisp).
Per-task N=5 by default (20 cells; ~$0.40-0.60).

Usage:
    python3 scripts/_k2_post_repair_mini_ab.py \\
        --output scripts/_round28_dials_artifacts/k2_post_repair_results.json \\
        [--n 5]
"""
from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, time
import urllib.error, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from quality_lint import (count_bracket_errors, count_fabricated_uuids,
                          count_nested_bracket_depth)

UA = "org-llm/0.1 (https://github.com/daniel2501/org-llm)"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

VARIANTS = [
    ("K1-qwen30",      "qwen/qwen3-coder-30b-a3b-instruct", False),  # baseline
    ("K2-kimi-k2.6",   "moonshotai/kimi-k2.6",               True),  # disable reasoning
]

TASK_PROMPTS = {
    "B5":  "Add a `* Tools` heading entry for `ripgrep`: one bullet describing it + a GitHub link. Reply with ONLY the new org-mode lines.",
    "B11": "Draft DEC-099 — Use OpenTofu over Terraform. Sections: Status (Accepted), Context, Decision, Consequences. Org-mode only.",
    "B25": "Write an ERT test that asserts (string-trim ' foo ') returns 'foo'. Reply with ONLY the (ert-deftest ...) form.",
    "B26": "Write an ERT test that asserts (length '(a b c)) returns 3. Reply with ONLY the (ert-deftest ...) form.",
}


def _or_key() -> str:
    return subprocess.run(
        ["pass", "show", "org-llm/cloud/openrouter/api-key"],
        capture_output=True, text=True, check=True, timeout=5
    ).stdout.strip().split("\n", 1)[0]


def _score(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    primary = min(20, len(text) // 20)
    fab = count_fabricated_uuids(text)
    brk = count_bracket_errors(text)
    nst = count_nested_bracket_depth(text)
    return max(0.0, float(primary) - 3*fab - brk - nst)


def _call(model: str, prompt: str, auth: str, disable_reasoning: bool,
          max_tokens: int = 800) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1, "max_tokens": max_tokens,
    }
    if disable_reasoning:
        body["reasoning"] = {"enabled": False}
    req = urllib.request.Request(
        OR_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {auth}",
                 "Content-Type": "application/json", "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        msg = d["choices"][0]["message"]
        # Be defensive — even with reasoning disabled, some providers may
        # still emit it. Prefer content; fall back to reasoning.
        content = msg.get("content") or msg.get("reasoning") or ""
        return {"ok": True, "content": content,
                "latency_s": time.time() - t0,
                "cost_usd": float(d.get("usage", {}).get("cost") or 0),
                "provider": d.get("provider", "?")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}",
                "latency_s": time.time() - t0, "cost_usd": 0.0}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "latency_s": time.time() - t0, "cost_usd": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    or_key = _or_key()
    cells: list[dict] = []
    print(f"[k2-mini] firing {args.n}× per task across {len(TASK_PROMPTS)} tasks "
          f"× {len(VARIANTS)} variants = {args.n * len(TASK_PROMPTS) * len(VARIANTS)} cells")
    for task in TASK_PROMPTS:
        prompt = TASK_PROMPTS[task]
        for vname, model, disable_reasoning in VARIANTS:
            for rep in range(args.n):
                resp = _call(model, prompt, or_key, disable_reasoning)
                cells.append({
                    "variant": vname, "task": task, "rep": rep,
                    "ok": resp["ok"],
                    "content_len": len(resp.get("content") or ""),
                    "score": _score(resp.get("content") or "") if resp["ok"] else 0,
                    "latency_s": resp["latency_s"],
                    "cost_usd": resp.get("cost_usd"),
                    "provider": resp.get("provider"),
                    "error": resp.get("error"),
                })
                tag = "✓" if resp["ok"] else "✗"
                print(f"  {tag} {vname:>15} {task} rep={rep} "
                      f"score={cells[-1]['score']:.1f} "
                      f"latency={resp['latency_s']:.1f}s "
                      f"prov={resp.get('provider','?')}")

    summary = {}
    for v, _, _ in VARIANTS:
        per_task = {}
        for t in TASK_PROMPTS:
            scores = [c["score"] for c in cells
                      if c["variant"] == v and c["task"] == t and c["ok"]]
            per_task[t] = {"n": len(scores),
                            "mean": statistics.mean(scores) if scores else 0.0}
        all_s = [c["score"] for c in cells if c["variant"] == v and c["ok"]]
        summary[v] = {
            "per_task": per_task,
            "overall_mean": statistics.mean(all_s) if all_s else 0.0,
            "total_cost": sum((c["cost_usd"] or 0) for c in cells if c["variant"] == v),
            "n_total_ok": len(all_s),
            "n_total_attempted": sum(1 for c in cells if c["variant"] == v),
        }

    out = {"cells": cells, "summary": summary,
           "_meta": {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "n_per_task": args.n,
                      "tasks": list(TASK_PROMPTS.keys()),
                      "variants": [v[0] for v in VARIANTS]}}
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"\n[k2-mini] DONE → {args.output}")
    for v, s in summary.items():
        print(f"  {v}: ok={s['n_total_ok']}/{s['n_total_attempted']} "
              f"mean={s['overall_mean']:.2f} cost=${s['total_cost']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
