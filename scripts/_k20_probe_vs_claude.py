#!/usr/bin/env python3
"""K20 vs Claude (K5) head-to-head — extends mini-A/B with K5 variant.

First direct K20-vs-Claude A/B data. K5 routes via OpenRouter Claude.
Bar (per r27-research-foss-strategies): K20 closes ≥50% of K5-vs-K1
quality gap on prose tasks.

Usage:
    K20_API_ENDPOINT=https://<pod>/v1/chat/completions \\
        python3 _k20_probe_vs_claude.py --output results.json [--n 10]
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

# R28 P1-7 FOLD §7-#11 (2026-05-09): anthropic/claude-3.5-sonnet returned
# 404 in R27 ("No endpoints found"). Updated to claude-opus-4 +
# claude-sonnet-4 — both verified valid 2026-05-09. Per [Pre-flight slug
# validation] memory rule, we run a HEAD probe before firing cells.
VARIANTS = [
    ("K20-foss-distill",   "k20-v1",                            "k20"),
    ("K1-qwen30",          "qwen/qwen3-coder-30b-a3b-instruct", "or"),
    ("K8-deepseekV3",      "deepseek/deepseek-chat-v3-0324",    "or"),
    ("K5-claude-opus-4",   "anthropic/claude-opus-4",           "or"),  # Claude ceiling sentinel
    ("K5-claude-sonnet-4", "anthropic/claude-sonnet-4",         "or"),  # Claude balanced sentinel
]


def _preflight_slugs(or_key: str) -> list[str]:
    """R28 P1-7: HEAD-check OR slugs against /v1/models before firing cells.
    Returns list of invalid slugs (empty list = all valid)."""
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {or_key}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        ids = {m.get("id") for m in data.get("data", [])}
        invalid = [m for _, m, mode in VARIANTS if mode == "or" and m not in ids]
        return invalid
    except Exception as e:
        print(f"[preflight] /v1/models probe failed: {e}", file=sys.stderr)
        return []  # let cells fire; per-cell errors will surface
TASK_PROMPTS = {
    "B1":  "Add an entry under * Tools heading for `ripgrep` (one-line + GitHub link). Reply with ONLY the new org-mode lines.",
    "B11": "Draft DEC-099 — Use OpenTofu over Terraform. Sections: Status (Accepted), Context, Decision, Consequences. Org-mode only.",
    "B25": "Write an ERT test that asserts (string-trim ' foo ') returns 'foo'. Reply with ONLY the (ert-deftest ...) form.",
}


def _or_key():
    return subprocess.run(["pass", "show", "org-llm/cloud/openrouter/api-key"],
                          capture_output=True, text=True, check=True, timeout=5
                          ).stdout.strip().split("\n", 1)[0]


def _score(text):
    if not text or not text.strip():
        return 0.0
    primary = min(20, len(text) // 20)
    fab = count_fabricated_uuids(text)
    brk = count_bracket_errors(text)
    nst = count_nested_bracket_depth(text)
    return max(0.0, float(primary) - 3*fab - brk - nst)


def _call(url, model, prompt, auth, max_tokens=1500):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.1, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Authorization": f"Bearer {auth}",
                                           "Content-Type": "application/json", "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        return {"ok": True, "content": d["choices"][0]["message"].get("content") or "",
                "latency_s": time.time()-t0, "cost_usd": float(d.get("usage", {}).get("cost") or 0)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "latency_s": time.time()-t0, "cost_usd": 0.0}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}", "latency_s": time.time()-t0, "cost_usd": 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    k20_url = os.environ.get("K20_API_ENDPOINT", "").strip()
    if not k20_url:
        print("ERR K20_API_ENDPOINT", file=sys.stderr); return 1
    or_key = _or_key()
    # R28 P1-7: preflight slug validation
    invalid = _preflight_slugs(or_key)
    if invalid:
        print(f"PREFLIGHT FAIL — invalid slugs: {invalid}", file=sys.stderr)
        return 2
    cells = []
    for task in TASK_PROMPTS:
        prompt = TASK_PROMPTS[task]
        for vname, model, mode in VARIANTS:
            url, auth = (k20_url, "ignored") if mode == "k20" else (OR_URL, or_key)
            for rep in range(args.n):
                resp = _call(url, model, prompt, auth)
                cells.append({"variant": vname, "task": task, "rep": rep, "ok": resp["ok"],
                              "score": _score(resp.get("content") or "") if resp["ok"] else 0,
                              "latency_s": resp["latency_s"], "cost_usd": resp.get("cost_usd")})
    summary = {}
    for v, _, _ in VARIANTS:
        per_task = {}
        for t in TASK_PROMPTS:
            scores = [c["score"] for c in cells if c["variant"]==v and c["task"]==t and c["ok"]]
            per_task[t] = {"n": len(scores),
                            "mean": statistics.mean(scores) if scores else 0.0}
        all_s = [c["score"] for c in cells if c["variant"]==v and c["ok"]]
        summary[v] = {"per_task": per_task, "overall_mean": statistics.mean(all_s) if all_s else 0.0,
                       "total_cost": sum(c["cost_usd"] or 0 for c in cells if c["variant"]==v),
                       "n_total": len(all_s)}
    # K20 closes K5-vs-K1 gap? Use opus-4 as ceiling sentinel.
    k1m = summary["K1-qwen30"]["overall_mean"]
    k5m = summary["K5-claude-opus-4"]["overall_mean"]
    k20m = summary["K20-foss-distill"]["overall_mean"]
    gap = k5m - k1m
    closure = (k20m - k1m) / gap if gap > 0.01 else 0.0
    verdict = {"k1_mean": k1m, "k5_opus4_mean": k5m, "k20_mean": k20m,
                "k5_minus_k1_gap": gap, "k20_closure_pct": round(closure*100, 1),
                "PASS_50pct": closure >= 0.5}
    out = {"cells": cells, "summary": summary, "verdict": verdict}
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"K20 vs Claude: K1={k1m:.2f} K5={k5m:.2f} K20={k20m:.2f}")
    print(f"  closure: {closure*100:.1f}% (PASS_50pct={verdict['PASS_50pct']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
