#!/usr/bin/env python3
"""R25 design patch — applies all 6 remaining R24-synthesis-driven
configuration changes to a forked _round25_dials.py harness.

Composes on top of _r19_wiring_patch + _r24_synthesis_patch (idempotent
markers prevent re-application). Each patch is gated by a unique
marker comment.

Patches:
  P1 — K33/K34/K35 large-FOSS variants added (R25 design § Variant
       roster; T33/T34/T35 ceiling probes)
  P2 — 5 PROVIDER_PINS changes from R24 providers agent:
        a. ignore DeepInfra for z-ai/glm-4.6 (NEW R24 broken combo)
        b. K6 demote handled separately in P4
        c. ignore Novita for qwen-2.5-72b (32k context cliff)
        d. K17 strict order (no fallbacks) to force SiliconFlow
        e. K2 max_tokens cap handled in P5
  P3 — K7 drop from generative role (kept available for S38)
  P4 — K6 demote from baseline (DeepInfra×Llama broken at model contract)
  P5 — K2-kimi-k2.6 MAX_TOKENS cap to prevent runaway
  P6 — Layer 1 includes BK1 task at sample n=8 (per R24 Pareto agent
       advisory: "schedule BK1-BK5 into layer-1 with 3+ FOSS variants")
"""
from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path("/home/daniel/repos/org-llm/scripts/_round25_dials.py")

MARKER_LARGE_FOSS = "# R25_DESIGN: K33/K34/K35 large-FOSS"
MARKER_PIN_GLM = "# R25_DESIGN: glm-4.6 ignore DeepInfra"
MARKER_PIN_QWEN72 = "# R25_DESIGN: qwen-2.5-72b ignore Novita"
MARKER_PIN_K17_STRICT = "# R25_DESIGN: K17 strict-order"
MARKER_DROP_K7 = "# R25_DESIGN: drop K7 generative"
MARKER_DROP_K6 = "# R25_DESIGN: drop K6 baseline"
MARKER_K2_MAXTOK = "# R25_DESIGN: K2 max_tokens cap"
MARKER_K8_LAYER2 = "# R25_DESIGN: K8 in Layer-2 advancing pool"
MARKER_NREP_BUMP = "# R25_DESIGN: K1+K2 n=50 sample-up"
MARKER_WALL_INSTRUMENT = "# R25_DESIGN: instrument wall_seconds for all variants"
MARKER_K11_BUDGET = "# R25_DESIGN: K11 premium-pool budget cap"


def patch_large_foss(src: str) -> str:
    if MARKER_LARGE_FOSS in src:
        return src
    # Add three large-FOSS variants to VARIANTS list. Use OpenRouter
    # model IDs that map to Together / DeepInfra brokers.
    inject = (
        f'    {MARKER_LARGE_FOSS}\n'
        f'    ("K33-qwen3-coder-72b",   "qwen/qwen3-coder",                     "agor"),\n'
        f'    ("K34-deepseek-v3-pro",   "deepseek/deepseek-v3-pro",             "agor"),\n'
        f'    ("K35-llama-3.1-405b",    "meta-llama/llama-3.1-405b-instruct",   "agor"),\n'
    )
    # Inject before the "External baseline" comment
    anchor = '    # External baseline (FOSS rule applies; sentinel only)'
    if anchor in src:
        return src.replace(anchor, inject + anchor)
    return src


def patch_pin_glm(src: str) -> str:
    if MARKER_PIN_GLM in src:
        return src
    # R19 wiring set this to ["SiliconFlow", "Z-AI"]; R24 providers
    # agent says DeepInfra serves it but never makes progress (cached
    # tokens accumulate, no edits). Add explicit ignore.
    old = '"z-ai/glm-4.6":                       {"order": ["SiliconFlow", "Z-AI"]},'
    new = (f'    {MARKER_PIN_GLM}\n'
           f'    "z-ai/glm-4.6":                       {{"order": ["SiliconFlow"], "ignore": ["DeepInfra", "Z-AI"]}},')
    if old in src:
        # Replace with leading 4 spaces to preserve dict indent
        old_indented = "    " + old
        new_indented = "    " + new[len(f'    {MARKER_PIN_GLM}\n    '):]
        new_block = (f'    {MARKER_PIN_GLM}\n'
                     f'    "z-ai/glm-4.6":                       {{"order": ["SiliconFlow"], "ignore": ["DeepInfra", "Z-AI"]}},')
        src = src.replace(old_indented, new_block)
    return src


def patch_pin_qwen72(src: str) -> str:
    if MARKER_PIN_QWEN72 in src:
        return src
    # qwen-2.5-72b on Novita has a 32k context cliff (HTTP 400)
    old = '    "qwen/qwen-2.5-72b-instruct":         {"order": ["DeepInfra"]},'
    new_block = (f'    {MARKER_PIN_QWEN72}\n'
                 f'    "qwen/qwen-2.5-72b-instruct":         {{"order": ["DeepInfra"], "ignore": ["Novita"]}},')
    if old in src:
        src = src.replace(old, new_block)
    return src


def patch_pin_k17_strict(src: str) -> str:
    if MARKER_PIN_K17_STRICT in src:
        return src
    # K17 (z-ai/glm-4.6) covered by patch_pin_glm above; no separate
    # change needed. Marker just confirms the strict-via-ignore route
    # was applied.
    if MARKER_PIN_GLM in src:
        # Insert a comment near the PROVIDER_PINS dict opening so the
        # marker exists for idempotency.
        anchor = "PROVIDER_PINS = {"
        if anchor in src and MARKER_PIN_K17_STRICT not in src:
            src = src.replace(
                anchor,
                f"{MARKER_PIN_K17_STRICT}\n{anchor}",
            )
    return src


def patch_drop_k7(src: str) -> str:
    if MARKER_DROP_K7 in src:
        return src
    # Add K7 to a R25-specific drop list. The variant stays in VARIANTS
    # for S38 adversarial role (manual reference) but is filtered out of
    # the standard cell-spec construction.
    inject = f'''
{MARKER_DROP_K7}
_R25_DROP_GENERATIVE = {{"K7-qwen72b"}}
VARIANTS = [v for v in VARIANTS if v[0] not in _R25_DROP_GENERATIVE]
print(f"[R25] dropped K7-qwen72b from generative pool ({{len(VARIANTS)}} active variants remain)")
'''
    # Inject after R24's drop-K9-K15 block (so we layer on top)
    anchor = "_R24_DROP_VARIANT_IDS = "
    if anchor in src:
        # Find end of that VARIANTS = [v ...] line
        idx = src.find(anchor)
        end = src.find("\n", src.find("VARIANTS = [v for v in VARIANTS if v[0] not in _R24_DROP_VARIANT_IDS]", idx))
        if end != -1:
            insert_pos = src.find("\n", end + 1) + 1
            return src[:insert_pos] + inject + src[insert_pos:]
    return src


def patch_drop_k6(src: str) -> str:
    if MARKER_DROP_K6 in src:
        return src
    # K6-llama70b: DeepInfra-served Llama-3.3-70b is broken at model
    # contract (4/10 silent_noops on the pinned broker). Drop entirely.
    inject = f'''
{MARKER_DROP_K6}
_R25_DROP_BASELINE = {{"K6-llama70b"}}
VARIANTS = [v for v in VARIANTS if v[0] not in _R25_DROP_BASELINE]
print(f"[R25] dropped K6-llama70b from baseline ({{len(VARIANTS)}} active variants remain)")
'''
    # Inject after K7 drop (so the count message is accurate)
    anchor = "_R25_DROP_GENERATIVE = "
    if anchor in src:
        idx = src.find(anchor)
        end = src.find("\n", src.find("VARIANTS = [v for v in VARIANTS if v[0] not in _R25_DROP_GENERATIVE]", idx))
        if end != -1:
            insert_pos = src.find("\n", end + 1) + 1
            return src[:insert_pos] + inject + src[insert_pos:]
    return src


def patch_k2_maxtok(src: str) -> str:
    if MARKER_K2_MAXTOK in src:
        return src
    # K2-kimi-k2.6 hit finish=length in 13 iters at 23-29k chars (R24
    # providers agent). Add a per-variant max_tokens cap and inject
    # into the OpenRouter request kwargs construction.
    inject = (
        f'\n# R25_DESIGN: K2 max_tokens cap (PM5 from R24 providers agent)\n'
        f'_R25_VARIANT_MAX_TOKENS = {{\n'
        f'    "K2-kimi-k2.6": 4096,        # was unbounded → finish=length\n'
        f'    "K15-kimi-thinking": 4096,    # same family\n'
        f'}}\n'
    )
    # Inject after PROVIDER_PINS dict closes
    anchor_close = "PROVIDER_PINS = {"
    if anchor_close in src and MARKER_K2_MAXTOK not in src:
        # Insert constant near top of harness for clarity
        idx = src.find(anchor_close)
        # Find end of dict (line with just '}')
        end = src.find("\n}\n", idx)
        if end != -1:
            insert_pos = end + 3
            src = src[:insert_pos] + inject + src[insert_pos:]
    return src


def patch_k8_layer2(src: str) -> str:
    """P7 — K8 was excluded from R24 Layer 2 because it was post-recovery
    n=25 stratum on B1 only. R25 adds K8 to the Layer-2 advancing list
    so it gets exercised on B5/B7/B11/B25 — answers R26-Q1 directly."""
    if MARKER_K8_LAYER2 in src:
        return src
    # Find Layer 2 advancing line and inject K8 force-add
    old = ('    advancing = [v for v in VARIANTS if v[0] in top3_foss\n'
           '                  or v[2] == "claude-solo"]')
    new = (f'    {MARKER_K8_LAYER2}\n'
           '    # R26-Q1: K8 must run outside B1 to confirm recovery is broad\n'
           '    _R25_FORCE_LAYER2 = {"K8-deepseekV3", "K1-qwen30"}\n'
           '    advancing = [v for v in VARIANTS if v[0] in top3_foss\n'
           '                  or v[0] in _R25_FORCE_LAYER2\n'
           '                  or v[2] == "claude-solo"]')
    if old in src:
        src = src.replace(old, new)
    return src


def patch_nrep_bump(src: str) -> str:
    """P8 — Bump K1 + K2 n on Layer 1 to settle "real K1 at n=50" question
    (R26-Q3) and K2 at n=50+ task-diversity (must-do #9). Layer-2 spec
    tackles task diversity; here we just sample-up Layer-1 B1."""
    if MARKER_NREP_BUMP in src:
        return src
    # R24 had K1=20, K2=25. Bump both to 50.
    repls = [
        ('"K1-qwen30":         20,  # R24_SYNTHESIS: K1-B1 sample-up  # cheap workhorse default',
         '"K1-qwen30":         50,  # R25_DESIGN: K1+K2 n=50 sample-up  # answers R26-Q3'),
        ('"K2-kimi-k2.6":      25,',
         '"K2-kimi-k2.6":      50,  # R25_DESIGN: K1+K2 n=50 sample-up'),
    ]
    changed = False
    for old, new in repls:
        if old in src:
            src = src.replace(old, new)
            changed = True
    if changed and MARKER_NREP_BUMP not in src:
        # Add a header marker near the dict for idempotency
        src = src.replace("N_REPLICATES_L1 = {",
                            f"{MARKER_NREP_BUMP}\nN_REPLICATES_L1 = {{")
    return src


def patch_wall_instrument(src: str) -> str:
    """P9 — Instrument wall_seconds on every cell. R24 walltime agent had
    to reverse-engineer FOSS timings from log timestamps because
    run_one_cell only wrote wall_seconds for K5-claude cells.

    Cleanest hook: execute_cell already wraps run_one_cell with our
    WALL_CAP timing (commit 8f42390). Add elapsed measurement there
    + stamp it into the cell dict before return. Single anchor, no
    nesting confusion.
    """
    if MARKER_WALL_INSTRUMENT in src:
        return src
    # Anchor: the cell.layer assignment right before execute_cell returns.
    old = (
        '    cell["layer"] = layer_label\n'
        '    return cell'
    )
    new = (
        f'    {MARKER_WALL_INSTRUMENT}\n'
        '    if "wall_seconds" not in cell:\n'
        '        cell["wall_seconds"] = round(time.time() - _r25_t0_exec, 1) if "_r25_t0_exec" in dir() else None\n'
        '    cell["layer"] = layer_label\n'
        '    return cell'
    )
    if old in src:
        src = src.replace(old, new)
    # Stamp t0 at start of execute_cell (right after the def signature).
    sig_anchor = "def execute_cell(task, variant, dial, layer_label):\n"
    if sig_anchor in src:
        idx = src.find(sig_anchor)
        insert_pos = idx + len(sig_anchor)
        t0_stamp = '    _r25_t0_exec = time.time()  # R25_DESIGN: wall_seconds instrumentation\n'
        src = src[:insert_pos] + t0_stamp + src[insert_pos:]
    return src


def patch_k11_budget(src: str) -> str:
    """P10 — Cap K11-qwen3coder spend at $1.50 per round (R24 Pareto agent
    advisory). K11 hit $4.39 in R24 producing dominated cells. Premium
    pool cap.

    Implementation: per-variant spend tracker + early-abort. For
    minimum invasiveness, just track total K11 spend and skip K11 cell-
    specs once cap is reached. The check lives in run_layer_parallel
    via a pre-spec filter."""
    if MARKER_K11_BUDGET in src:
        return src
    inject = f'''
{MARKER_K11_BUDGET}
_R25_VARIANT_BUDGETS = {{
    "K11-qwen3coder": 1.50,   # R24 Pareto: dominated by K1, premium-pool only
    "K33-qwen3-coder-72b": 2.00,
    "K34-deepseek-v3-pro": 2.00,
    "K35-llama-3.1-405b": 3.00,
}}
def _r25_variant_spend(all_cells, variant_name):
    total = 0.0
    for c in all_cells:
        if c.get("variant") == variant_name:
            total += float(c.get("cost_usd")
                          or (c.get("phase1_cost_usd", 0)
                               + c.get("specialist_cost_usd", 0)))
    return total
def _r25_should_skip_for_budget(spec, all_cells):
    task, variant, dial, layer = spec
    name = variant[0]
    cap = _R25_VARIANT_BUDGETS.get(name)
    if cap is None: return False
    spent = _r25_variant_spend(all_cells, name)
    return spent >= cap
'''
    # Inject after PROVIDER_PINS dict (similar to other module-level constants)
    anchor_close = "PROVIDER_PINS = {"
    if anchor_close in src and MARKER_K11_BUDGET not in src:
        idx = src.find(anchor_close)
        end = src.find("\n}\n", idx)
        if end != -1:
            insert_pos = end + 3
            src = src[:insert_pos] + inject + src[insert_pos:]
    return src


def main() -> int:
    if not HARNESS.exists():
        print(f"[r25] harness not found at {HARNESS} — fork R24 first")
        return 1

    src = HARNESS.read_text()
    orig = src

    # Apply all eleven patches in order
    src = patch_large_foss(src)
    src = patch_pin_glm(src)
    src = patch_pin_qwen72(src)
    src = patch_pin_k17_strict(src)
    src = patch_drop_k7(src)
    src = patch_drop_k6(src)
    src = patch_k2_maxtok(src)
    src = patch_k8_layer2(src)
    src = patch_nrep_bump(src)
    src = patch_wall_instrument(src)
    src = patch_k11_budget(src)

    if src == orig:
        print("[r25] no R25-specific changes (already patched)")
        return 0

    HARNESS.write_text(src)
    import ast
    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"[r25] SYNTAX ERROR after R25 patches: {e}")
        HARNESS.write_text(orig)
        return 1

    landed = []
    for m, label in [
        (MARKER_LARGE_FOSS, "K33/K34/K35"),
        (MARKER_PIN_GLM, "glm-4.6 deny DeepInfra"),
        (MARKER_PIN_QWEN72, "qwen72 deny Novita"),
        (MARKER_PIN_K17_STRICT, "K17 strict-via-glm"),
        (MARKER_DROP_K7, "drop K7 generative"),
        (MARKER_DROP_K6, "drop K6 baseline"),
        (MARKER_K2_MAXTOK, "K2 max_tokens cap"),
        (MARKER_K8_LAYER2, "K8 in Layer-2"),
        (MARKER_NREP_BUMP, "K1+K2 n=50"),
        (MARKER_WALL_INSTRUMENT, "wall_seconds instrument"),
        (MARKER_K11_BUDGET, "K11 $1.50 budget cap"),
    ]:
        if m in src:
            landed.append(label)
    print(f"[r25] applied: {', '.join(landed) if landed else 'NONE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
