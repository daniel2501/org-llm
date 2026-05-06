"""Validation framework — A/B + blinded-judge for any LLM-mediated feature.

Generalises the pattern from `scripts/recipe_ab_{harness,judge}.py` so it can
be reused for any feature whose value depends on subjective output quality:
interceptors, agent personas, model routing, walk-vs-search, auto-tagger, etc.

Each feature registers a `ValidationConfig` describing:

  - Two arms (callables): the feature ON vs OFF (or A vs B) variants.
  - A prompt set bucketed as anchor / control / bait.
  - Optional ground-truth file (literate org with =:GT:= blocks per prompt).
  - Per-bucket bars (quality win-rate, latency ratio, token ratio).
  - The judge model (defaults to a FOSS-licensed open-weight model
    per DEC-017 § Defaults rule; users can opt in to Claude / GPT
    judges by overriding `judge_model` on the config).

The driver (`run_validation` + `run_judge`) handles the harness pass and the
blinded judge call. Output JSONL + summary land under
=~/.local/share/org-llm/validation/<feature_name>/=.

The pattern is intentionally *thin* right now: each arm is a callable the
feature provides. We don't try to absorb feature-specific logic into the
framework — gate-2 says no premature abstraction. The framework owns
registration, run-loop, JSONL serialisation, judge invocation, and bar
evaluation. Everything else stays with the feature.

Adding a new validation:

    from org_llm import validation

    def my_arm_on(prompt: str, ctx: dict) -> dict: ...
    def my_arm_off(prompt: str, ctx: dict) -> dict: ...

    validation.register(validation.ValidationConfig(
        name="my_feature",
        description="What it validates",
        arm_a=my_arm_on,
        arm_b=my_arm_off,
        prompts=[("P1", "anchor", "user text"), ...],
        ground_truth_path=Path("docs/wiki/my-feature-gt.org"),
    ))

CLI: `org-llm validate --list` and `org-llm validate <name>`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# A single arm-call returns this shape. Features that wrap the existing
# recipe harness can use _adapt_recipe_arm below.
ArmResult = dict        # {latency_s, usage{prompt_tokens,completion_tokens,total_tokens},
                        #  narration, error?, plus anything feature-specific}

# Each arm is invoked as: fn(prompt, ctx) -> ArmResult
# `ctx` carries cloud config (model, endpoint, api_key) + run state (trial idx).
ArmCallable = Callable[[str, dict], ArmResult]


# A deterministic check returns (passed: bool, message: str).
# The framework calls each check with no arguments — checks close over
# whatever state they need.
DeterministicCheck = Callable[[], tuple[bool, str]]


@dataclass
class ValidationConfig:
    """One registered feature's validation spec.

    Two kinds:
    - =ab= (default): A/B comparison + blinded judge (gate-1 quality
      questions on subjective LLM output)
    - =deterministic=: ground-truth correctness checks (gate-1
      questions where the right answer is computable, e.g. inferrer
      accuracy, MCP tool output shape)

    A config provides EITHER A/B arms OR deterministic checks, not
    both. Use two separate configs if a feature needs both kinds of
    coverage.
    """
    name:            str
    description:     str
    kind:            str = "ab"   # "ab" | "deterministic"

    # A/B-style fields (used when kind="ab")
    arm_a:           Optional[ArmCallable] = None
    arm_b:           Optional[ArmCallable] = None
    arm_a_label:     str = "on"
    arm_b_label:     str = "off"
    prompts:         list[tuple[str, str, str]] = field(default_factory=list)
    ground_truth_path: Optional[Path] = None
    # FOSS-first default per DEC-017 § Defaults rule (2026-05-05 EST):
    # judge model baked into runtime code must be FOSS-licensed.
    # Qwen 2.5 72B Instruct is Apache 2.0, ships in the cloud catalog
    # (quality 195 — comparable to the prior Claude Opus default for
    # blinded-judge work), and routes through any OpenRouter-shape
    # provider the user already has wired. Users who want a Claude
    # judge can override `judge_model` per-config.
    judge_model:     str = "qwen/qwen-2.5-72b-instruct"
    judge_rubric:    Optional[str] = None
    bars: dict[str, dict] = field(default_factory=lambda: {
        "anchor":  {"min_quality_a_win":     0.55,
                     "max_p50_latency_ratio": 0.70,
                     "max_token_ratio":       0.80},
        "control": {"max_quality_a_win":     0.55},
    })

    # Deterministic-style fields (used when kind="deterministic")
    # Each check returns (passed, human-readable message). Group label
    # is used for grouping in the output; no semantic meaning.
    checks:          list[tuple[str, str, DeterministicCheck]] = \
                            field(default_factory=list)
    # Tuple shape: (group_label, check_label, callable).


# ── registry ─────────────────────────────────────────────────────────────────

_CONFIGS: dict[str, ValidationConfig] = {}


def register(config: ValidationConfig) -> None:
    """Register a validation config under `config.name`. Subsequent
    registrations with the same name replace the prior one."""
    _CONFIGS[config.name] = config


def get(name: str) -> Optional[ValidationConfig]:
    return _CONFIGS.get(name)


def list_registered() -> list[str]:
    return sorted(_CONFIGS)


# ── output paths ─────────────────────────────────────────────────────────────

def _validation_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME")
                or os.path.expanduser("~/.local/share"))
    return base / "org-llm" / "validation"


def output_paths(feature_name: str) -> dict[str, Path]:
    """Return the JSONL + summary paths for a given feature. Side-effect:
    creates the parent directory."""
    d = _validation_dir() / feature_name
    d.mkdir(parents=True, exist_ok=True)
    return {
        "results":   d / "results.jsonl",
        "judgments": d / "judgments.jsonl",
        "summary":   d / "summary.md",
    }


# ── harness driver ───────────────────────────────────────────────────────────

def run_validation(config: ValidationConfig, *,
                    trials: int = 3,
                    models: Optional[list[str]] = None,
                    cloud_endpoint: Optional[str] = None,
                    cloud_api_key: Optional[str] = None,
                    progress: Optional[Callable[[str], None]] = None,
                    ) -> Path:
    """Run the A/B harness for `config`. For each (prompt × arm × trial × model),
    invokes the arm callable and emits one JSONL line with the result.

    Returns the path to the results JSONL.

    Each arm is invoked with (prompt, ctx) where ctx is:
        {"trial": int, "model": str, "endpoint": str, "api_key": str,
         "prompt_id": str, "bucket": str}

    Features get to decide what cloud setup looks like; this driver only
    plumbs the ctx through. Pass cloud_endpoint=None to leave it to the
    arms (e.g. for local-only validations).
    """
    paths = output_paths(config.name)
    out_path = paths["results"]
    models = models or ["qwen/qwen-2.5-72b-instruct"]
    written = 0
    with out_path.open("w") as out:
        for prompt_id, bucket, prompt_text in config.prompts:
            for model in models:
                for trial in range(trials):
                    for arm_fn, arm_label in (
                        (config.arm_a, config.arm_a_label),
                        (config.arm_b, config.arm_b_label),
                    ):
                        ctx = {
                            "trial":     trial,
                            "model":     model,
                            "endpoint":  cloud_endpoint or "",
                            "api_key":   cloud_api_key or "",
                            "prompt_id": prompt_id,
                            "bucket":    bucket,
                        }
                        if progress:
                            progress(f"{prompt_id}/{arm_label}/trial{trial}/{model}")
                        t0 = time.time()
                        try:
                            result = arm_fn(prompt_text, ctx)
                            error = None
                        except Exception as e:
                            result = {"narration": "", "usage": {}, "latency_s": 0.0}
                            error = f"{type(e).__name__}: {e}"
                        wall_s = round(time.time() - t0, 3)
                        record: dict[str, Any] = {
                            "feature":   config.name,
                            "arm":       arm_label,
                            "prompt_id": prompt_id,
                            "bucket":    bucket,
                            "prompt":    prompt_text,
                            "trial":     trial,
                            "model":     model,
                            "wall_s":    wall_s,
                            "error":     error,
                        }
                        record.update(result or {})
                        out.write(json.dumps(record) + "\n")
                        out.flush()
                        written += 1
    return out_path


# ── judge driver — facade over the existing recipe_ab_judge.py for now ───────
# The recipe judge is mature; rather than rewrite it, we shell out to it
# until we have a SECOND validation use case that exposes the actual
# reusable shape. Gate-2: no premature abstraction.

def run_judge(config: ValidationConfig, *,
              results_path: Optional[Path] = None,
              out_summary: Optional[Path] = None,
              ) -> Path:
    """Run the blinded judge against the harness output. Currently delegates
    to the recipe_ab_judge facade — extracts the reusable bits from it as
    SECOND validation features expose what's truly common.

    Returns the path to the summary markdown.
    """
    paths = output_paths(config.name)
    results_path = results_path or paths["results"]
    out_summary  = out_summary  or paths["summary"]
    if not results_path.exists():
        raise FileNotFoundError(
            f"No harness output at {results_path}. Run validation first."
        )
    # TODO(P23.x): factor out the judge core from scripts/recipe_ab_judge.py
    # and call it directly here. For now, the recipe judge is hard-coded to
    # the recipe-specific JSONL shape (`recipe`, `runner_ms`, `tool_calls`),
    # so this facade is only useful for features whose JSONL matches that
    # shape — i.e. features that go through cloud_call + return narration.
    raise NotImplementedError(
        "Judge facade not yet wired. For the recipes feature, run:\n"
        "    python scripts/recipe_ab_judge.py\n"
        "directly. The framework will absorb the judge's core logic when a "
        "second validation feature lands and the truly-reusable shape is "
        "clearer (gate-2: no premature abstraction)."
    )


# ── deterministic-checks driver ──────────────────────────────────────────────

def run_deterministic(config: ValidationConfig,
                       progress: Optional[Callable[[str], None]] = None,
                       ) -> dict:
    """Run every check in `config.checks`. Each check returns
    (passed, message). Returns a result dict:

        {
          "feature":     <name>,
          "kind":        "deterministic",
          "groups":      {group_label: [{label, passed, message}]},
          "summary":     {passed: N, failed: N, total: N},
          "verdict":     "PASS" | "FAIL"
        }

    Persists to <output_paths>/results.jsonl (one JSON line per check)
    and writes a markdown summary at <output_paths>/summary.md.
    """
    if config.kind != "deterministic":
        raise ValueError(
            f"run_deterministic called on non-deterministic config "
            f"{config.name!r} (kind={config.kind!r})"
        )
    paths = output_paths(config.name)
    groups: dict[str, list[dict]] = {}
    passed_n = failed_n = 0
    with paths["results"].open("w") as out:
        for group_label, check_label, fn in config.checks:
            if progress:
                progress(f"{group_label} · {check_label}")
            try:
                passed, message = fn()
                err = None
            except Exception as e:
                passed, message, err = False, "", f"{type(e).__name__}: {e}"
            entry = {"group": group_label, "check": check_label,
                      "passed": passed, "message": message, "error": err}
            out.write(json.dumps(entry) + "\n")
            groups.setdefault(group_label, []).append(entry)
            if passed:
                passed_n += 1
            else:
                failed_n += 1
    total = passed_n + failed_n
    verdict = "PASS" if failed_n == 0 else "FAIL"
    summary = {
        "feature": config.name,
        "kind":    "deterministic",
        "groups":  groups,
        "summary": {"passed": passed_n, "failed": failed_n, "total": total},
        "verdict": verdict,
    }
    # Markdown summary
    md_lines = [
        f"# Validation summary — {config.name}",
        f"_{config.description}_",
        "",
        f"**{verdict}** — {passed_n}/{total} checks passed.",
        "",
    ]
    for group_label, entries in groups.items():
        md_lines.append(f"## {group_label}")
        for e in entries:
            mark = "✓" if e["passed"] else "✗"
            md_lines.append(f"- {mark} **{e['check']}** — {e['message']}"
                             + (f" _({e['error']})_" if e['error'] else ""))
        md_lines.append("")
    paths["summary"].write_text("\n".join(md_lines))
    return summary


# ── verdict evaluation ───────────────────────────────────────────────────────

def evaluate_bars(config: ValidationConfig, summary: dict) -> dict[str, bool]:
    """Given a parsed judge summary, return {bar_name: pass?}. The summary
    dict is whatever shape the judge produces; for the recipe judge it's
    {bucket: {a_wins, b_wins, ties, latency_p50_a, latency_p50_b, ...}}.
    """
    out: dict[str, bool] = {}
    for bucket, bars in config.bars.items():
        bucket_data = summary.get(bucket) or {}
        if not bucket_data:
            continue
        if "min_quality_a_win" in bars:
            n = bucket_data.get("a_wins", 0) + bucket_data.get("b_wins", 0)
            ratio = (bucket_data.get("a_wins", 0) / n) if n else 0
            out[f"{bucket}.quality_min"] = ratio >= bars["min_quality_a_win"]
        if "max_quality_a_win" in bars:
            n = bucket_data.get("a_wins", 0) + bucket_data.get("b_wins", 0)
            ratio = (bucket_data.get("a_wins", 0) / n) if n else 0
            out[f"{bucket}.quality_max"] = ratio <= bars["max_quality_a_win"]
        if "max_p50_latency_ratio" in bars:
            la = bucket_data.get("latency_p50_a") or 0
            lb = bucket_data.get("latency_p50_b") or 1
            out[f"{bucket}.latency"] = (la / lb) <= bars["max_p50_latency_ratio"]
        if "max_token_ratio" in bars:
            ta = bucket_data.get("tokens_median_a") or 0
            tb = bucket_data.get("tokens_median_b") or 1
            out[f"{bucket}.tokens"] = (ta / tb) <= bars["max_token_ratio"]
    return out
