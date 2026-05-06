"""Validation config for the recipe pre-fetch layer (Phase 22 v2).

Wraps the existing `scripts/recipe_ab_harness.py` arms so the recipe A/B
becomes runnable through the unified `org-llm validate recipes` verb.

The recipe harness is the seed of the validation framework — keeping its
arm functions in scripts/ for now (where they can also be invoked
directly), and exposing them through the framework via thin adapters.
"""
from __future__ import annotations

import sys
from pathlib import Path

from org_llm import validation


_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

# Imported lazily inside arm wrappers so the recipes config can be
# registered even when scripts/ isn't on the import path (e.g. in pure
# `org-llm validate --list` invocations from outside the repo).


def _arm_a(prompt: str, ctx: dict) -> dict:
    from scripts import recipe_ab_harness as rh   # type: ignore
    out = rh.run_arm_a(prompt,
                        model=ctx["model"],
                        endpoint=ctx["endpoint"],
                        api_key=ctx["api_key"])
    return out


def _arm_b(prompt: str, ctx: dict) -> dict:
    from scripts import recipe_ab_harness as rh   # type: ignore
    out = rh.run_arm_b(prompt,
                        model=ctx["model"],
                        endpoint=ctx["endpoint"],
                        api_key=ctx["api_key"])
    return out


def _prompts() -> list[tuple[str, str, str]]:
    from scripts import recipe_ab_harness as rh   # type: ignore
    return list(rh.PROMPTS)


validation.register(validation.ValidationConfig(
    name="recipes",
    description=(
        "Recipe pre-fetch layer (Phase 22 v2): does the deterministic "
        "pre-fetch + 1-cloud-call (arm A) beat the agent-with-tools "
        "loop (arm B) on quality, latency, and tokens?"
    ),
    arm_a=_arm_a,
    arm_b=_arm_b,
    arm_a_label="recipes_on",
    arm_b_label="recipes_off",
    prompts=_prompts(),
    ground_truth_path=Path(
        _REPO / "docs" / "wiki" / "2026-05-03-recipe-ab-ground-truth.org"
    ),
    # Inherit ValidationConfig's FOSS default (DEC-017 § Defaults rule,
    # 2026-05-05 EST). The original recipe A/B used Claude Opus 4.7 as
    # judge; defaults baked into runtime code now stay FOSS-licensed.
    # Users who want to re-run the original recipe A/B with the Claude
    # judge can override `judge_model` here at opt-in time.
))
