#!/usr/bin/env python3
"""K5 (Claude opus-4 + sonnet-4) vs K6 (Llama-3.3-70b) expanded probe.
R28 quality-judge §7: K6's K5-Claude quality lead was partly verifier
loophole — destructive BK-task deletes earned "50" via structural
validity. R29-16 added BK preservation gate to =score_cell= (deletions-
vs-insertions penalty + floor_dropped flag). This probe re-runs K5-vs-K6
with the gate live and emits a G2 deprecation verdict. FOSS-only
narrative: K5 = sentinel-pending-verification per r28-pareto.
Usage: _k5_vs_k6_expanded_probe.py --output PATH [--n 10] [--smoke]"""
from __future__ import annotations
import argparse, json, re, statistics, subprocess, sys, time
import urllib.error, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from _round29_dials import score_cell  # R29-16 BK preservation gate

UA = "org-llm/0.1 (https://github.com/daniel2501/org-llm)"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

VARIANTS = [
    ("K5-claude-opus-4",   "anthropic/claude-opus-4",            None),
    ("K5-claude-sonnet-4", "anthropic/claude-sonnet-4",          None),
    ("K6-llama70b",        "meta-llama/llama-3.3-70b-instruct",  ["together", "parasail"]),
]
BASE_PROMPTS = {
    "B5":  ("docs/wiki/_probe.org", "Add a `* Tools` heading entry for `ripgrep`: one bullet describing it + a GitHub link. Reply with ONLY the new org-mode lines."),
    "B11": ("docs/wiki/_probe.org", "Draft DEC-099 — Use OpenTofu over Terraform. Sections: Status (Accepted), Context, Decision, Consequences. Org-mode only."),
    "B25": ("tests/_probe.el",      "Write an ERT test that asserts (string-trim ' foo ') returns 'foo'. Reply with ONLY the (ert-deftest ...) form."),
    "B26": ("tests/_probe.el",      "Write an ERT test that asserts (length '(a b c)) returns 3. Reply with ONLY the (ert-deftest ...) form."),
}
BK_IDS = ["BK1", "BK2", "BK11", "BK12", "BK14"]


def _or_key():
    return subprocess.run(["pass", "show", "org-llm/cloud/openrouter/api-key"],
        capture_output=True, text=True, check=True, timeout=5).stdout.strip().split("\n", 1)[0]


def _preflight(or_key):
    try:
        req = urllib.request.Request(MODELS_URL,
            headers={"Authorization": f"Bearer {or_key}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            ids = {m.get("id") for m in json.loads(r.read()).get("data", [])}
        return [m for _, m, _ in VARIANTS if m not in ids]
    except Exception as e:
        print(f"[preflight] /v1/models failed: {e}", file=sys.stderr); return []


def _load_bk_tasks():
    try:
        from _round19_long_horizon_tasks import LONG_HORIZON_TASKS  # type: ignore
        idx = {t["id"]: t for t in LONG_HORIZON_TASKS}
        out = []
        for bk in BK_IDS:
            t = idx.get(bk)
            if not t:
                print(f"[bk-load] {bk} missing — skipping", file=sys.stderr); continue
            out.append({"id": bk, "prompt": t.get("goal") or t.get("label", ""),
                        "target_files": list(t.get("target_files") or [])})
        return out
    except Exception as e:
        print(f"[bk-load] import failed: {e} — base only", file=sys.stderr); return []


def _call(model, prompt, auth, providers, max_tokens=1500):
    body = {"model": model, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if providers:
        body["provider"] = {"order": providers, "allow_fallbacks": False}
    req = urllib.request.Request(OR_URL, data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {auth}",
        "Content-Type": "application/json", "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        msg = d["choices"][0]["message"]
        return {"ok": True, "content": msg.get("content") or msg.get("reasoning") or "",
                "latency_s": time.time()-t0,
                "cost_usd": float(d.get("usage", {}).get("cost") or 0),
                "provider": d.get("provider", "?")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "latency_s": time.time()-t0, "cost_usd": 0.0}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "latency_s": time.time()-t0, "cost_usd": 0.0}


def _make_cell(variant, task_id, target_files, is_bk, resp):
    """Build score_cell-compatible cell. Parse +/- from diff-like output so
    R29-16 destructive gate fires on excess-delete patterns; else additive."""
    cell = {"variant": variant, "task_id": task_id,
            "layer": "layer_bk" if is_bk else "layer_b",
            "specialists": [{"error": ""}],
            "cost_usd": resp.get("cost_usd") or 0.0,
            "latency_s": resp["latency_s"],
            "provider": resp.get("provider"), "ok": resp["ok"]}
    if not resp["ok"]:
        cell["error"] = resp.get("error") or "unknown"; return cell
    content = resp.get("content") or ""
    ins = dele = 0
    if re.search(r"^(\+\+\+|---|@@)", content, re.M):
        for ln in content.splitlines():
            if ln.startswith(("+++", "---", "@@")): continue
            if ln.startswith("+"): ins += 1
            elif ln.startswith("-"): dele += 1
    else:
        ins = sum(1 for ln in content.splitlines() if ln.strip())
    cell["analysis"] = {"diff_in_scope": {"insertions": ins, "deletions": dele},
        "diff_out_of_scope": {"insertions": 0, "deletions": 0},
        "wrap_categories": {"on_candidate": 0, "creative": 0, "unmatched": 0},
        "id_fabrication_count": 0,
        "files_touched": list(target_files), "in_scope_files": list(target_files),
        "out_of_scope_files": [], "diff_text": content}
    return cell


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--smoke", action="store_true", help="n=2, 1 BK task")
    args = ap.parse_args()
    n = 2 if args.smoke else args.n
    or_key = _or_key()
    invalid = _preflight(or_key)
    if invalid:
        print(f"PREFLIGHT FAIL — invalid slugs: {invalid}", file=sys.stderr); return 2
    tasks = [(tid, p, [tgt], False) for tid, (tgt, p) in BASE_PROMPTS.items()]
    bk_loaded = _load_bk_tasks()
    if args.smoke: bk_loaded = bk_loaded[:1]
    for bk in bk_loaded:
        tasks.append((bk["id"], bk["prompt"], bk["target_files"], True))

    print(f"[k5-v-k6] firing {n}x per task across {len(tasks)} tasks "
          f"x {len(VARIANTS)} variants = {n*len(tasks)*len(VARIANTS)} cells "
          f"(BK loaded: {len(bk_loaded)}/{len(BK_IDS)})")

    cells = []
    for tid, prompt, tgt, is_bk in tasks:
        for vname, model, providers in VARIANTS:
            for rep in range(n):
                resp = _call(model, prompt, or_key, providers)
                cell = _make_cell(vname, tid, tgt, is_bk, resp)
                sc = score_cell(cell); cell["scored"] = sc; cell["rep"] = rep
                cells.append(cell)
                print(f"  [{'ok' if resp['ok'] else 'ERR'}] {vname:>20} {tid:>4} "
                      f"rep={rep} score={sc.get('score', 0):.1f} "
                      f"fab={sc.get('fab', 0)} destr={sc.get('destructive_penalty', 0):.1f} "
                      f"floor={sc.get('floor_dropped', False)}")

    summary = {}
    for v, _, _ in VARIANTS:
        per_task = {}
        for tid, *_ in tasks:
            ss = [c["scored"].get("score", 0) for c in cells
                  if c["variant"] == v and c["task_id"] == tid and c["ok"]
                  and not c["scored"].get("floor_dropped")]
            per_task[tid] = {"n": len(ss), "mean": statistics.mean(ss) if ss else 0.0}
        all_ok = [c for c in cells if c["variant"] == v and c["ok"]]
        kept = [c["scored"]["score"] for c in all_ok if not c["scored"].get("floor_dropped")]
        summary[v] = {"per_task": per_task,
            "overall_mean": statistics.mean(kept) if kept else 0.0,
            "n_total_ok": len(all_ok), "n_kept": len(kept),
            "total_cost": sum((c["cost_usd"] or 0) for c in all_ok),
            "fab_dist": [c["scored"].get("fab", 0) for c in all_ok],
            "destr_dist": [c["scored"].get("destructive_penalty", 0) for c in all_ok]}

    def _destr(v): return sum(1 for c in cells if c["variant"] == v and c["ok"]
                              and c["scored"].get("excess_deletions", 0) > 100)
    def _floor(v):
        ok = [c for c in cells if c["variant"] == v and c["ok"]]
        return round(100.0 * sum(1 for c in ok if c["scored"].get("floor_dropped"))
                     / max(1, len(ok)), 1)

    k6m = summary["K6-llama70b"]["overall_mean"]
    op4 = summary["K5-claude-opus-4"]["overall_mean"]
    so4 = summary["K5-claude-sonnet-4"]["overall_mean"]
    best_k5 = max(op4, so4)
    k6_destr_n = _destr("K6-llama70b")
    k6_destr_pct = 100.0 * k6_destr_n / max(1, summary["K6-llama70b"]["n_total_ok"])
    pass_q  = (k6m - best_k5) > 2.0
    pass_bk = k6_destr_pct < 20.0
    g2 = "PASS" if (pass_q and pass_bk) else (
         "FAIL" if (not pass_q and not pass_bk) else "MIXED")

    verdict = {"k6_vs_opus4_mean_delta": round(k6m - op4, 2),
        "k6_vs_sonnet4_mean_delta": round(k6m - so4, 2),
        "post_r29_16_bk_preservation": {
            "k6_destructive_count": k6_destr_n,
            "k5_destructive_count": _destr("K5-claude-opus-4") + _destr("K5-claude-sonnet-4"),
            "k6_floor_dropped_pct": _floor("K6-llama70b"),
            "k5_floor_dropped_pct": round((_floor("K5-claude-opus-4")
                                          + _floor("K5-claude-sonnet-4"))/2, 1)},
        "G2_DEPRECATION_VERDICT": g2,
        "PASS_if": "K6 honest mean (post-floor-drop) >= best K5 mean by >2pts AND K6 destructive count <20%"}

    out = {"cells": cells, "summary": summary, "verdict": verdict,
        "_meta": {"ts_est": time.strftime("%I:%M %p EST", time.localtime()),
            "n_per_task": n, "tasks": [t[0] for t in tasks],
            "variants": [v[0] for v in VARIANTS],
            "bk_loaded": [b["id"] for b in bk_loaded]}}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"\n[k5-v-k6] DONE -> {args.output}")
    for v, s in summary.items():
        print(f"  {v:>20}: kept={s['n_kept']}/{s['n_total_ok']} "
              f"mean={s['overall_mean']:.2f} cost=${s['total_cost']:.4f}")
    print(f"  G2 verdict: {g2} (k6-best_k5={k6m-best_k5:+.2f}, k6_destr_pct={k6_destr_pct:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
