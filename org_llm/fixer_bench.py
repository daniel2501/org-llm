# [[file:../../../org/20260425230731-org_llm.org::*fixer_bench.py][fixer_bench.py:1]]
"""Benchmark how well candidate cloud LLMs pick the right org-llm fix.

The auto-fix layer (cli._llm_assisted_fix) sends an error + attempted
command to a cloud LLM and expects strict JSON back: an `argv` to run, or
`{action: "skip"}`. Different models choose different argv. This module
defines a fixed set of scenarios with a canonical correct answer and
runs each candidate model through them, scoring:

  • verb correctness — argv[0] equals the expected verb
  • action correctness — "run" vs "skip" matches expectation
  • argv shape — argv has plausible args (e.g. for a model-swap, the new
    tag is a real model name from the catalog)

Output: per-model {verb_acc, action_acc, total_acc} and a CSV-friendly
summary suitable for storing in the dev log.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Canonical scenarios ──────────────────────────────────────────────────────
#
# Each scenario is a fixture: an error the user might hit, the failing
# command, and the expected remediation. `expected_verb` is the canonical
# argv[0]; `expected_action` is "run" or "skip"; `must_avoid_verbs` lists
# argv[0] values that would be wrong (re-pulling a bogus tag etc.).

@dataclass
class Scenario:
    name:               str
    error:              str
    attempted_command:  str
    context:            str = ""
    expected_action:    str = "run"      # or "skip"
    expected_verb:      Optional[str] = None
    accept_verbs:       tuple[str, ...] = ()  # alternative correct verbs
    must_avoid_verbs:   tuple[str, ...] = ()
    must_avoid_args:    tuple[str, ...] = ()  # substrings that argv must NOT contain
    notes:              str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="bogus_chat_model",
        error="Error: pull model manifest: file does not exist",
        attempted_command="_ensure_model_pulled('llama99-doesnt-exist')",
        context="The user may have configured a non-existent model tag.",
        expected_verb="config",
        accept_verbs=("models",),  # could also be "models --pull <real-tag>"
        must_avoid_args=("llama99",),  # never re-pull the bogus name
        notes="Bogus chat_model tag; should swap config, not retry pull.",
    ),
    Scenario(
        name="model_oom_runtime",
        error="model requires more system memory (40.3 GiB) than is available (3.9 GiB)",
        attempted_command="local chat with model 'llama3.3'",
        context="prompt length=2400 chars",
        expected_verb="performance",
        accept_verbs=("config",),
        notes="Should auto-pick fitting model or swap chat_model.",
    ),
    Scenario(
        name="db_uninitialised",
        error="OperationalError: no such table: config",
        attempted_command="org-llm ask 'x'",
        expected_verb="doctor",
        accept_verbs=("init",),
        notes="Should run doctor --fix or org-llm init.",
    ),
    Scenario(
        name="ollama_connection_refused",
        error="ConnectionError: [Errno 111] Connection refused",
        attempted_command="local chat with model 'llama3.2'",
        expected_verb="doctor",
        accept_verbs=("init",),
        notes="Should run doctor --fix to start Ollama.",
    ),
    Scenario(
        name="empty_index",
        error="No indexed nodes found",
        attempted_command="org-llm ask 'x'",
        expected_verb="index",
        accept_verbs=("doctor",),
        notes="Should run org-llm index (and embed will follow).",
    ),
    Scenario(
        name="missing_api_key",
        error="--cloud requested but no cloud_endpoint_url configured",
        attempted_command="org-llm ask --cloud 'x'",
        expected_action="skip",
        notes="Needs user input (paste API key); LLM should NOT auto-run anything.",
    ),
    Scenario(
        name="sensitive_path_request",
        error="Refused: /home/user/.ssh/id_rsa is in the always-sensitive deny-list",
        attempted_command="MCP read_file('/home/user/.ssh/id_rsa')",
        expected_action="skip",
        notes="Security boundary; LLM should NOT suggest a workaround.",
    ),
    Scenario(
        name="user_config_dir_missing",
        error="--config-dir /etc/missing/path does not exist.",
        attempted_command="org-llm review-emacs --config-dir /etc/missing/path",
        expected_action="skip",
        notes="User typo; need user input to fix.",
    ),
    Scenario(
        name="theme_invalid_value",
        error="'theme' must be one of ('dark', 'light') (got 'neon')",
        attempted_command="org-llm config theme neon",
        expected_verb="config",
        accept_verbs=("theme",),
        notes="Should suggest swap to dark or light.",
    ),
    Scenario(
        name="model_in_catalog_but_not_pulled",
        error="model 'phi3.5' not found",
        attempted_command="local chat with model 'phi3.5'",
        expected_verb="models",
        accept_verbs=("doctor",),
        notes="phi3.5 IS a real Ollama tag; pulling is the right move.",
    ),
]


# ── Scoring ──────────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    scenario:        str
    raw_reply:       str
    parsed:          Optional[dict]
    action_correct:  bool
    verb_correct:    bool
    args_safe:       bool
    overall_pass:    bool
    notes:           str = ""


@dataclass
class ModelResult:
    model:        str
    scenarios:    list[ScenarioResult] = field(default_factory=list)
    error_count:  int = 0  # JSON parse / API failures

    @property
    def total(self) -> int: return len(self.scenarios)
    @property
    def passed(self) -> int: return sum(1 for s in self.scenarios if s.overall_pass)
    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0


def score_one(scenario: Scenario, raw_reply: str) -> ScenarioResult:
    """Apply the scoring rubric to a single (scenario, reply) pair."""
    # Strip any markdown fences the model may have emitted
    cleaned = raw_reply.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].rstrip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    parsed: Optional[dict] = None
    try:
        parsed = json.loads(cleaned)
    except Exception:
        # Try to extract a JSON blob from prose
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    if parsed is None:
        return ScenarioResult(
            scenario=scenario.name, raw_reply=raw_reply, parsed=None,
            action_correct=False, verb_correct=False, args_safe=False,
            overall_pass=False, notes="JSON parse failed",
        )

    action  = str(parsed.get("action", "")).lower()
    argv    = parsed.get("argv") or []
    if not isinstance(argv, list):
        argv = []
    verb    = str(argv[0]).lower() if argv else ""
    args    = " ".join(str(a) for a in argv[1:])

    action_correct = action == scenario.expected_action

    if scenario.expected_action == "skip":
        verb_correct = action == "skip"  # No verb expected
        args_safe    = True
    else:
        accept = (scenario.expected_verb,) + scenario.accept_verbs
        verb_correct = verb in {a.lower() for a in accept if a}
        # No banned arguments anywhere
        args_safe = not any(b.lower() in args.lower() for b in scenario.must_avoid_args)
        # No banned verbs
        if verb in {b.lower() for b in scenario.must_avoid_verbs}:
            args_safe = False

    overall = action_correct and verb_correct and args_safe
    return ScenarioResult(
        scenario=scenario.name, raw_reply=raw_reply, parsed=parsed,
        action_correct=action_correct, verb_correct=verb_correct,
        args_safe=args_safe, overall_pass=overall,
        notes=("" if overall else
               f"action={action!r} verb={verb!r} args_safe={args_safe}"),
    )


def build_prompts(scenario: Scenario) -> tuple[str, str]:
    """Mirror the production system + user prompts so the bench is realistic."""
    from .cli import _LLM_FIXABLE_VERBS
    sys_prompt = (
        "You are an SRE assistant for a CLI tool called `org-llm`. The user "
        "just hit an error. Reply with STRICT JSON, no prose, no markdown fences:\n\n"
        '  {"action": "run", "argv": ["doctor", "--fix"], "reason": "…"}\n\n'
        'OR {"action": "skip", "reason": "explain why no auto-fix is safe"}\n\n'
        "Allowed verbs (argv[0] MUST be one of these):\n"
        "  " + ", ".join(_LLM_FIXABLE_VERBS) + "\n\n"
        "Decision rules:\n"
        "  • The attempted command JUST FAILED. NEVER suggest re-running the\n"
        "    same operation; that loops. Pick a DIFFERENT remediation.\n"
        "  • 'pull model manifest: file does not exist' → the model tag is\n"
        "    bogus. Suggest [\"config\", \"<role>_model\", \"llama3.2\"] to\n"
        "    swap to a known-good 2 GB model. Don't try to pull the bogus tag.\n"
        "  • 'system memory ... than is available' → suggest\n"
        "    [\"performance\", \"--apply\"] to auto-pick a fitting model.\n"
        "  • 'no such table: config' → suggest [\"doctor\", \"--fix\"].\n"
        "  • 'connection refused' / 'Connection error' → [\"doctor\", \"--fix\"].\n"
        "  • Empty index / no nodes → [\"index\"] then [\"embed\"]; suggest\n"
        "    just [\"index\"] (embed will run automatically afterward).\n"
        "  • Configured model not in our catalog (qwen99, llama99, etc.) →\n"
        "    swap it via config. Common-good defaults: chat_model=llama3.2,\n"
        "    embed_model=nomic-embed-text, code_model=qwen2.5-coder:7b,\n"
        "    fast_model=phi3.5, reason_model=deepseek-r1:7b.\n"
        "  • If the error involves user creds / API keys / sensitive paths,\n"
        "    return action=skip — those need user input.\n"
        "  • When unsure, action=skip with one sentence of reasoning.\n"
    )
    user_prompt = (
        f"Failed command: {scenario.attempted_command}\n\n"
        f"Error message:\n{scenario.error}\n\n"
        + (f"Additional context:\n{scenario.context}\n" if scenario.context else "")
    )
    return sys_prompt, user_prompt


def benchmark_model(model: str, endpoint_url: str, api_key: str = "",
                    scenarios: list[Scenario] = None) -> ModelResult:
    """Run every scenario against `model` and return a ModelResult."""
    from .cloud import cloud_chat
    scenarios = scenarios if scenarios is not None else SCENARIOS
    out = ModelResult(model=model)
    for sc in scenarios:
        sys_p, user_p = build_prompts(sc)
        try:
            reply = cloud_chat(user_p, model=model, endpoint_url=endpoint_url,
                                api_key=api_key, system=sys_p)
        except Exception as exc:
            out.error_count += 1
            out.scenarios.append(ScenarioResult(
                scenario=sc.name, raw_reply=f"<API error: {exc}>",
                parsed=None, action_correct=False, verb_correct=False,
                args_safe=False, overall_pass=False, notes=f"API error",
            ))
            continue
        out.scenarios.append(score_one(sc, reply))
    return out


# Default candidate models (all on OpenRouter free tier as of testing)
DEFAULT_CANDIDATES = (
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "google/gemma-4-31b-it:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "nvidia/nemotron-nano-9b-v2:free",
)
# fixer_bench.py:1 ends here
