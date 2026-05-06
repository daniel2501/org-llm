# [[file:../../../org/20260425230731-org_llm.org::*tests/test_validation.py][test_validation.py:1]]
"""Tests for org_llm.validation — A/B + blinded-judge framework.

The DEC-017 § Defaults rule (2026-05-05 EST) requires that any model
default baked into runtime code be FOSS-licensed. This test guards the
validation framework's `judge_model` default against silently regressing
to a closed-API model.
"""
from __future__ import annotations

from org_llm import validation
from org_llm.cloud import _classify_license_tier


# Allow-list: FOSS / open-weight slugs the project ships in its cloud
# catalog. Keep this list narrow — it's deliberately not pulled from
# the catalog so a regression there can't silently widen the gate.
_FOSS_JUDGE_ALLOW_LIST = {
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-r1",
    "deepseek/deepseek-r1-distill-llama-70b:free",
    "openai/gpt-oss-120b",
    "meta-llama/llama-3.3-70b-instruct",
}


def test_validation_judge_default_is_foss():
    """The default ValidationConfig.judge_model must be FOSS-licensed.

    Guards against re-introducing a closed-API model (Claude / GPT /
    Gemini) as the baked-in default for blinded-judge runs. Per
    DEC-017 § Defaults rule, closed-API judges remain available as
    user opt-in but cannot be the runtime default.
    """
    cfg = validation.ValidationConfig(name="t", description="t")
    assert cfg.judge_model in _FOSS_JUDGE_ALLOW_LIST, (
        f"Validation judge default {cfg.judge_model!r} is not in the "
        f"FOSS allow-list. DEC-017 § Defaults rule: defaults baked "
        f"into runtime code must be FOSS-licensed."
    )


def test_validation_judge_default_classifies_as_open():
    """The default judge_model's slug must classify as foss or
    open_weight under the cloud catalog license-tier heuristic."""
    cfg = validation.ValidationConfig(name="t", description="t")
    tier = _classify_license_tier("", cfg.judge_model)
    assert tier in ("foss", "open_weight"), (
        f"Validation judge default {cfg.judge_model!r} classifies as "
        f"{tier!r} — DEC-017 § Defaults rule requires foss/open_weight."
    )


def test_recipes_config_inherits_foss_judge_default():
    """The bundled `recipes` validation config must not pin a closed
    judge — it should inherit the FOSS default from ValidationConfig.

    This catches the original Phase 22 v2 hard-pin of
    `judge_model="claude-opus-4-7"` regressing back into the file.
    """
    import org_llm.validation_configs.recipes  # noqa: F401  (registers)
    cfg = validation.get("recipes")
    assert cfg is not None, "recipes validation config not registered"
    assert cfg.judge_model in _FOSS_JUDGE_ALLOW_LIST, (
        f"Recipes judge {cfg.judge_model!r} is not FOSS — DEC-017 "
        f"§ Defaults rule violation."
    )
# test_validation.py:1 ends here
