#!/usr/bin/env python3
"""R25 walltime analysis. Reads R25 cell_result.json + log."""
import json, os, glob, statistics
from collections import defaultdict

ROOT = "/home/daniel/repos/org-llm/scripts/_round25_dials_artifacts"
cells = []
summary_path = f"{ROOT}/summary-1778251044.json"
with open(summary_path) as f:
    summary = json.load(f)
print(f"summary total_cells={summary.get('total_cells')} total_wall={summary.get('total_wall_seconds')} cost=${summary.get('total_cost_usd'):.2f}")
for d in summary["cells"]:
    cells.append({
        "layer": d.get("layer", "?"),
        "task": d.get("task_id"),
        "variant": d.get("variant"),
        "wall": d.get("wall_seconds"),
        "dial": d.get("dial_label", ""),
        "error": d.get("error"),
        "score": d.get("score"),
    })

print(f"TOTAL CELLS w/ result.json: {len(cells)}")
missing = [c for c in cells if c["wall"] is None]
print(f"missing wall_seconds: {len(missing)}")
walls = [c["wall"] for c in cells if c["wall"] is not None]
print(f"\nwall stats: n={len(walls)}, sum={sum(walls):.0f}s, mean={statistics.mean(walls):.1f}s, med={statistics.median(walls):.1f}s, max={max(walls):.0f}s")

def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f, c = int(k), min(int(k)+1, len(xs)-1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)

print(f"p10={pct(walls,10):.1f} p25={pct(walls,25):.1f} p50={pct(walls,50):.1f} p75={pct(walls,75):.1f} p90={pct(walls,90):.1f} p95={pct(walls,95):.1f} p99={pct(walls,99):.1f}")

print("\n=== BY LAYER ===")
by_layer = defaultdict(list)
for c in cells:
    by_layer[c["layer"]].append(c)
for layer, lcs in sorted(by_layer.items()):
    lwalls = [x["wall"] for x in lcs if x["wall"] is not None]
    if not lwalls: continue
    print(f"  {layer:<22} n={len(lcs):>3} sum={sum(lwalls):>6.0f}s max={max(lwalls):>6.0f}s med={statistics.median(lwalls):>6.1f}s mean={statistics.mean(lwalls):>6.1f}s")

print("\n=== BY VARIANT ===")
by_var = defaultdict(list)
for c in cells:
    by_var[c["variant"]].append(c)
for v, vcs in sorted(by_var.items(), key=lambda x: -sum(c["wall"] or 0 for c in x[1])):
    vwalls = [x["wall"] for x in vcs if x["wall"] is not None]
    if not vwalls: continue
    print(f"  {v:<28} n={len(vcs):>3} sum={sum(vwalls):>6.0f}s max={max(vwalls):>6.0f}s med={statistics.median(vwalls):>6.1f}s mean={statistics.mean(vwalls):>6.1f}s")

print("\n=== TOP 20 LONGEST CELLS ===")
for c in sorted([c for c in cells if c["wall"]], key=lambda x: -(x["wall"] or 0))[:20]:
    print(f"  {c['wall']:>7.1f}s  {c['variant']:<27}  {c['task']:<6}  {c['layer']:<22}  {c['dial']}")

print("\n=== CELLS NEAR WALL_CAP=400 (350-420s) ===")
near_cap = [c for c in cells if c["wall"] and 350 <= c["wall"] <= 420]
for c in sorted(near_cap, key=lambda x: -x["wall"]):
    print(f"  {c['wall']:>6.1f}s  {c['variant']:<27}  {c['task']:<6}  {c['layer']:<22}  {c['dial']}")

print(f"\n=== K2-only DETAIL ===")
k2 = [c for c in cells if c["variant"] and "K2" in c["variant"]]
k2_walls = sorted([(c["wall"] or 0, c["task"], c["layer"], c["dial"], c.get("error")) for c in k2], reverse=True)
print(f"  K2 n={len(k2)}  sum={sum(c['wall'] or 0 for c in k2):.0f}s")
killed_k2 = sum(1 for c in k2 if c.get("error") and "wall_cap_killed" in str(c["error"]))
print(f"  K2 wall_cap_killed: {killed_k2}/{len(k2)}")
for w, t, l, d, err in k2_walls[:25]:
    print(f"  {w:>6.1f}s  {t:<6}  {l:<22}  {d:<20}  err={err}")

# Layer 3 + Layer-BK + comp run check
print("\n=== layer counts in summary ===")
from collections import Counter
print(Counter(c["layer"] for c in cells))

# Layer wall = max FIFO simulation
def fifo_wall(walls, W):
    walls = sorted([w for w in walls if w], reverse=True)
    workers = [0] * W
    for w in walls:
        i = workers.index(min(workers))
        workers[i] += w
    return max(workers)

print("\n=== PARALLEL EFFICIENCY (W=32) ===")
print(f"  {'layer':<22} {'n':>4} {'sum':>7} {'max':>6} {'fifo':>6} {'actual':>7} {'eff_par':>8}")
# Layer wall from log timestamps
layer_walls = {
    "layer1":           1723,  # 10:38:39 -> 11:07:22
    "layer2":            381,  # 11:07:22 -> 11:13:43
    "layer_k11b25":       29,  # 11:13:43 -> 11:14:12
    "layer3_d5_loose":    73,  # nominal max of L3 cells
    "layer_bk":           82,  # 11:15:25 -> 11:16:47
}
for layer, lcs in sorted(by_layer.items()):
    # cap wall at 400s for killed cells (outer worker freed at cap)
    lwalls_capped = [min(x["wall"] or 0, 400) if (x.get("error") and "wall_cap_killed" in str(x["error"])) else (x["wall"] or 0) for x in lcs]
    s = sum(lwalls_capped)
    m = max(lwalls_capped)
    f = fifo_wall(lwalls_capped, 32)
    actual = layer_walls.get(layer, m)
    eff = s / actual if actual else 0
    print(f"  {layer:<22} {len(lcs):>4} {s:>7.0f} {m:>6.0f} {f:>6.0f} {actual:>7} {eff:>7.2f}x")

print("\n=== ALL wall_cap_killed cells ===")
killed = [c for c in cells if c.get("error") and "wall_cap_killed" in str(c["error"])]
print(f"total killed: {len(killed)}")
for c in killed:
    print(f"  {c['variant']:<25} {c['task']:<6} {c['layer']:<22} {c['dial']}")

# Distribution: <=400 vs >400
above_400 = [c for c in cells if c["wall"] and c["wall"] > 400]
print(f"\n=== cells with wall > 400s: {len(above_400)} ===")
for c in sorted(above_400, key=lambda x: -x["wall"]):
    print(f"  {c['wall']:>7.1f}s  {c['variant']:<27}  {c['task']:<6}  {c['layer']:<22}  {c['dial']}")

# Cells per layer per task (to see what's missing in L3/BK/comp)
print("\n=== layer3 cells (per dir) ===")
for layer in sorted(by_layer):
    if layer.startswith("layer3") or layer == "layer_bk" or layer == "layer_k11b25" or layer == "layer1" or layer == "layer2":
        tasks = defaultdict(list)
        for c in by_layer[layer]:
            tasks[c["task"]].append(c)
        for t, tcs in sorted(tasks.items()):
            walls_s = [x["wall"] for x in tcs if x["wall"] is not None]
            print(f"  {layer:<22}/{t:<6}  n={len(tcs)} sum={sum(walls_s):.0f}s")
