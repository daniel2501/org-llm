"""Quick slice analysis of R25 summary -- per-layer per-variant."""
import json
from collections import defaultdict

p = '/home/daniel/repos/org-llm/scripts/_round25_dials_artifacts/summary-1778251044.json'
with open(p) as f:
    data = json.load(f)

variants_of_interest = [
    'K1-qwen30', 'K8-deepseekV3', 'K5-claude-solo', 'K2-kimi-k2.6',
    'K11-qwen3coder', 'K17-glm46',
    'K33-qwen3-coder-72b', 'K34-deepseek-v3-pro', 'K35-llama-3.1-405b',
]

for vfilter in variants_of_interest:
    print('===', vfilter, 'by layer ===')
    bl = defaultdict(lambda: {'n': 0, 'err': 0, 'cost': 0.0, 'score': 0.0, 'prim': 0, 'lint': 0.0})
    for c in data['cells']:
        if c['variant'] != vfilter:
            continue
        L = c.get('layer', '?')
        bl[L]['n'] += 1
        if 'error' in c:
            bl[L]['err'] += 1
            continue
        cost = (c.get('phase1_cost_usd') or 0) + (c.get('specialist_cost_usd') or 0) + (c.get('cost_usd') or 0)
        bl[L]['cost'] += cost
        s = c.get('score') or {}
        if isinstance(s, dict):
            bl[L]['score'] += s.get('score', 0) or 0
            bl[L]['prim'] += s.get('primary', 0) or 0
            bl[L]['lint'] += s.get('lint_penalty', 0) or 0
    for L, d in sorted(bl.items()):
        qpt = d['cost']/d['score'] if d['score'] > 0 else float('inf')
        qs = '%.5f' % qpt if qpt != float('inf') else 'inf'
        print(f"  {L:<24} n={d['n']:>3} err={d['err']} cost={d['cost']:.4f} score={d['score']:.1f} prim={d['prim']} lint={d['lint']:.1f} $/qpt={qs}")
    print()


def slice_layer(layer):
    agg = defaultdict(lambda: {'n': 0, 'err': 0, 'cost': 0.0, 'score': 0.0})
    for c in data['cells']:
        if c.get('layer') != layer:
            continue
        v = c['variant']
        agg[v]['n'] += 1
        if 'error' in c:
            agg[v]['err'] += 1
            continue
        cost = (c.get('phase1_cost_usd') or 0) + (c.get('specialist_cost_usd') or 0) + (c.get('cost_usd') or 0)
        agg[v]['cost'] += cost
        s = c.get('score') or {}
        if isinstance(s, dict):
            agg[v]['score'] += s.get('score', 0) or 0
    return agg


for layer in ['layer1', 'layer2', 'layer_bk', 'layer_k11b25']:
    print(f'=== {layer} only ===')
    a = slice_layer(layer)
    for v, d in sorted(a.items(), key=lambda kv: kv[1]['cost']/max(kv[1]['score'], 0.001)):
        qpt = d['cost']/d['score'] if d['score'] > 0 else float('inf')
        qs = '%.5f' % qpt if qpt != float('inf') else 'inf'
        print(f"  {v:<24} n={d['n']:>3} err={d['err']} cost={d['cost']:.4f} score={d['score']:.1f} $/qpt={qs}")
    print()
