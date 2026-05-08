#!/usr/bin/env python3
"""R19 wiring patch — applied after round-chain forks _round19_dials.py.

Layers on top of the R18 v3 harness:
  1. BK1-BK5 long-horizon tasks imported + appended to TASKS
  2. Explicit broker deny-lists (provider.ignore) per R18 broker forensics
  3. quality_lint penalty hooked into score_cell (N5 from synthesis)

Idempotent — re-running on an already-patched file is a no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path("/home/daniel/repos/org-llm/scripts/_round19_dials.py")
MARKER_BK = "# R19_WIRING: BK1-BK5"
MARKER_DENY = "# R19_WIRING: provider.ignore"
MARKER_LINT = "# R19_WIRING: quality_lint"


def patch_bk_tasks(src: str) -> str:
    if MARKER_BK in src:
        return src
    # Append after the TASKS list close.
    inject = f'''
{MARKER_BK}
try:
    from scripts._round19_long_horizon_tasks import LONG_HORIZON_TASKS
    TASKS.extend(LONG_HORIZON_TASKS)
    print(f"[R19] Loaded {{len(LONG_HORIZON_TASKS)}} long-horizon tasks (BK1-BK5)")
except Exception as _e:
    print(f"[R19] BK1-BK5 import failed: {{_e}} — proceeding without")
'''
    # Find the closing `]` of TASKS = [ ... ] at module top level.
    # We look for the first standalone `]\n` after `TASKS = [`.
    idx = src.find("TASKS = [")
    if idx == -1:
        print("[patch] TASKS = [ not found — skipping BK injection")
        return src
    close_idx = src.find("\n]\n", idx)
    if close_idx == -1:
        print("[patch] TASKS close not found — skipping BK injection")
        return src
    insert_pos = close_idx + 3   # after "\n]\n"
    return src[:insert_pos] + inject + src[insert_pos:]


def patch_provider_ignore(src: str) -> str:
    """Add provider.ignore deny-lists per R18 broker forensics findings.
    These are alongside the existing `order` preferences — OpenRouter
    accepts both."""
    if MARKER_DENY in src:
        return src
    # Replace each known-broken pin with ignore-equipped version.
    # NOTE: trailing `,` belongs to the dict literal and lives on the
    # same line — keep it in both old and new strings to avoid creating
    # a dangling dict entry. Comments go on a separate line so the dict
    # ',' stays terminating.
    repls = [
        (
            '"moonshotai/kimi-k2.6":               {"order": ["Moonshot", "Parasail"]},',
            '# R19_WIRING: provider.ignore — DeepInfra tool-format gap (R18 broker forensics)\n    "moonshotai/kimi-k2.6":               {"order": ["Moonshot", "Parasail"], "ignore": ["DeepInfra"]},',
        ),
        (
            '"qwen/qwen3-coder":                   {"order": ["Together"]},',
            '# R19_WIRING: provider.ignore — SiliconFlow silent-text fallback\n    "qwen/qwen3-coder":                   {"order": ["Together"], "ignore": ["SiliconFlow"]},',
        ),
        (
            '"meta-llama/llama-3.3-70b-instruct":  {"order": ["DeepInfra"]},',
            '# R19_WIRING: provider.ignore — AkashML silent_noop\n    "meta-llama/llama-3.3-70b-instruct":  {"order": ["DeepInfra"], "ignore": ["AkashML"]},',
        ),
        (
            '"z-ai/glm-4.6":                       {"order": ["Z-AI", "SiliconFlow"]},',
            '# R19_WIRING: Z-AI never actually served — flip SiliconFlow first\n    "z-ai/glm-4.6":                       {"order": ["SiliconFlow", "Z-AI"]},',
        ),
    ]
    for old, new in repls:
        if old in src:
            src = src.replace(old, new)
        # silently skip if the line doesn't match — the harness may have
        # slightly different formatting after sed-rename.
    return src


def patch_quality_lint(src: str) -> str:
    if MARKER_LINT in src:
        return src
    # Add a lint import + hook the score_cell to apply lint_patch penalty.
    # Idempotent: only inject if "from scripts.quality_lint" not present.
    if "from scripts.quality_lint" in src or "import quality_lint" in src:
        return src

    import_block = f'''
{MARKER_LINT}
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from quality_lint import lint_patch as _r19_lint_patch
    _R19_LINT_AVAILABLE = True
except Exception as _e:
    print(f"[R19] quality_lint import failed: {{_e}}")
    _R19_LINT_AVAILABLE = False
'''
    # Inject the import block right after the existing imports (we look
    # for `state_lock = None` which sits in module-level state).
    anchor = "state_lock = None"
    if anchor not in src:
        return src
    src = src.replace(anchor, import_block + "\n" + anchor)

    # Make analyze_diff stash the raw diff so score_cell can lint it.
    old_ret = (
        '        "wrap_categories": cat_counts,\n'
        '        "inserted_links": inserted_links,\n'
        '    }'
    )
    new_ret = (
        '        "wrap_categories": cat_counts,\n'
        '        "inserted_links": inserted_links,\n'
        '        "diff_text": diff_text,  # R19_WIRING: stash for quality_lint\n'
        '    }'
    )
    if old_ret in src:
        src = src.replace(old_ret, new_ret)

    # Hook score_cell — wrap the final return with a penalty pass.
    # The harness's score_cell ends with:
    #     return {"score": round(score, 2), "primary": primary,
    #              "in_total": in_total, "out_total": out_total, "fab": fab}
    # We replace that with a version that subtracts lint_patch's
    # score_penalty when a diff is available on the cell.
    old_return = (
        '    score = primary - fab * 3 - min(out_total, 100) * 0.05\n'
        '    return {"score": round(score, 2), "primary": primary,\n'
        '             "in_total": in_total, "out_total": out_total, "fab": fab}'
    )
    new_return = (
        '    score = primary - fab * 3 - min(out_total, 100) * 0.05\n'
        '    # R19_WIRING: quality_lint penalty (N5)\n'
        '    lint_pen = 0.0\n'
        '    lint = {}\n'
        '    if _R19_LINT_AVAILABLE:\n'
        '        diff_text = (cell.get("analysis") or {}).get("diff_text") or ""\n'
        '        if diff_text:\n'
        '            try:\n'
        '                lint = _r19_lint_patch(diff_text)\n'
        '                lint_pen = lint.get("score_penalty", 0.0)\n'
        '            except Exception:\n'
        '                lint = {}\n'
        '    score = score - lint_pen\n'
        '    return {"score": round(score, 2), "primary": primary,\n'
        '             "in_total": in_total, "out_total": out_total,\n'
        '             "fab": fab, "lint_penalty": lint_pen,\n'
        '             "lint": lint}'
    )
    if old_return in src:
        src = src.replace(old_return, new_return)
    return src


def main() -> int:
    if not HARNESS.exists():
        print(f"[patch] harness not found at {HARNESS} — round-chain hasn't forked yet")
        return 1
    src = HARNESS.read_text()
    orig = src
    src = patch_bk_tasks(src)
    src = patch_provider_ignore(src)
    src = patch_quality_lint(src)
    if src == orig:
        print("[patch] no changes (already patched or markers present)")
        return 0
    HARNESS.write_text(src)
    # Syntax check
    import ast
    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"[patch] SYNTAX ERROR after patching: {e}")
        # Restore
        HARNESS.write_text(orig)
        return 1
    print(f"[patch] applied to {HARNESS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
