#!/usr/bin/env python3
"""Round-15 — dial sweep + expanded FOSS variant pool, going for stunning
FOSS results.

Builds on R14's clean-baseline + diff-granularity + ID verification.
Adds six dials (D5-D10 from the R14 post-mortem) + a wider variant
pool (8 FOSS + Claude baseline) + a new harder task (B20). Three
layers:

  Layer 1 — Variant tournament on B1 with BEST CONFIG
            (every variant × B1 × dials-engaged) → identifies top-3
            FOSS performers by on_candidate count + cost-per-wrap.
  Layer 2 — Top-3 FOSS + Claude × all 6 tasks with BEST CONFIG.
            Measures generality of layer-1 winners across task shapes.
  Layer 3 — Dial ablation on B1, top-2 FOSS variants. Each cell flips
            ONE dial from BEST CONFIG to the inverted value. Tells us
            which dials carry the win.

Observability (six layers, see post-mortem):

  O1 per-iteration log lines (specialist runtime emits, harness mirrors)
  O2 per-cell events.jsonl (mirror of SpecialistResult.events)
  O3 running cost / ETA / progress in main log
  O4 live leaderboard every 5 cells
  O5 tool_use_breakdown in cell_result.json
  O6 R15_LIVE.json rewritten after each cell, jq-friendly

Configuration knobs (DialConfig):

  brief_mode: "tight" | "loose"
  prefetch_mode: "inline" | "tool" | "both"
  with_manager: bool          # @picard plan vs raw goal to specialist
  tool_surface: "minimal" | "broad"
  scope_strict: bool          # edit_file/write_file enforce target_files

The "BEST CONFIG" (chosen up-front for layers 1+2):

  brief_mode=tight, prefetch_mode=both, with_manager=True,
  tool_surface=broad, scope_strict=True

Best config bets on: tight scope + tool-on-tap + broad surface — the
combination that should unblock kimi's silent-no-op (D6 tool option
for canonical IDs) AND let exploration-friendly models (Claude,
deepseek-R1) reach for grep/list_dir without losing focused models
(qwen30) to noise.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(REPO))
from org_llm.specialist import (  # noqa: E402
    SpecialistTask, SpecialistResult, run_specialist,
    DEFAULT_TOOLS, BROAD_TOOLS, BROAD_TOOLS_PLUS_ELISP,
    FIND_CANONICAL_ID_TOOL,
)
try:
    from org_llm.specialist import BROAD_TOOLS_FULL, OS_TOOLS, ORG_TOOLS  # R16 add
except ImportError:
    BROAD_TOOLS_FULL = BROAD_TOOLS_PLUS_ELISP
    OS_TOOLS, ORG_TOOLS = [], []

ARTIFACTS = REPO / "scripts/_round26_dials_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())
LOG = ARTIFACTS / f"log-{EPOCH}.txt"
LIVE = ARTIFACTS / "R26_LIVE.json"

# R26 P1-11/P1-12/P1-13 — file contracts the self-healing daemon writes
# and the harness reads. See docs/wiki/2026-05-08-r26-boost-plan.org §12
# "Harness changes required" table.
COST_CIRCUIT_BREAKER_FILE = ARTIFACTS / "COST_CIRCUIT_BREAKER"
LIVE_DENY_LIST_FILE = ARTIFACTS / "LIVE_DENY_LIST"
CELL_REPLAY_FILE = ARTIFACTS / "cell_replay.jsonl"

PICARD_PRIMER_FILE = REPO / "docs/wiki/picard-agor-primer.org"
L1A_FILE = REPO / "docs/wiki/org-llm-cli-primer.org"
L1B_FILE = REPO / "docs/wiki/org-mode-per-agent-primer.org"

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
PER_RUN_TIMEOUT = 900   # 15 min per cell ceiling
PER_CELL_BUDGET_USD = 5.00   # R16 ramped: up from $2
PER_CELL_MAX_ITERS = 16      # R16 ramped: up from 8

# R16: Difficulty-Adaptive SC (DSC) instead of flat n=5
# Per research agent: NAACL 2025 — flat n=5 is wasteful; resample only on
# verdict failure. DSC matches n=5 quality at <50% cost.
DSC_BASE_SAMPLES = 1
DSC_MAX_RESAMPLES = 4   # cap retries on verdict-failure → effective max n=5

CAPTAIN_MODEL_ID = "qwen/qwen3-coder-30b-a3b-instruct"

# Variant pool — R15's 9 + R16 additions per design doc.
# All FOSS choices honor the user's anti-Groq + Llama/Qwen/DeepSeek/Mixtral rule.
# K10-K12 added per R16 design; K3 (deepseek-r1) on probation per failure-mode
# agent (emits tool calls as plain markdown, 0 actual tool_calls in R15).
# K5-claude-solo demoted from baseline to test variant per Pareto agent
# (strictly dominated on cost AND quality by 6 other variants).
VARIANTS = [
    ("K1-qwen30",       "qwen/qwen3-coder-30b-a3b-instruct",   "agor"),
    ("K2-kimi-k2.6",    "moonshotai/kimi-k2.6",                 "agor"),
    # K3-deepseekR1 DROPPED — emits plan but no edits
    # K4-gptoss120b DROPPED — silent_noop
    ("K6-llama70b",     "meta-llama/llama-3.3-70b-instruct",    "agor"),
    ("K7-qwen72b",      "qwen/qwen-2.5-72b-instruct",           "agor"),  # re-pinned to DeepInfra
    ("K8-deepseekV3",   "deepseek/deepseek-chat-v3-0324",       "agor"),  # R18 DEFAULT — stability winner
    ("K9-mixtral",      "mistralai/mixtral-8x22b-instruct",     "agor"),
    # K10-deepseekCv2 DROPPED
    ("K11-qwen3coder",  "qwen/qwen3-coder",                     "agor"),  # 480B Apache 2.0; reserve B11+B25
    # K12-llama405b DROPPED
    # K13-deepseekV4 DROPPED until reliable (3/3 R17 worktree+provider failures)
    # K14-gptoss20b DROPPED
    ("K15-kimi-thinking", "moonshotai/kimi-k2-thinking",          "agor"),  # side-pool only (long pole)
    # K16-qwen36-27b DROPPED until provider stabilizes
    ("K17-glm46",         "z-ai/glm-4.6",                         "agor"),  # n=10 layer1 probe
    # K18-llama4 DROPPED — invalid model id
    # K19-qwen25c-32b DROPPED — HTTP 404
    # R25_DESIGN: K33/K34/K35 large-FOSS
    ("K33-qwen3-coder-72b",   "qwen/qwen3-coder",                     "agor"),
    ("K34-deepseek-v3-pro",   "deepseek/deepseek-v3-pro",             "agor"),
    ("K35-llama-3.1-405b",    "meta-llama/llama-3.1-405b-instruct",   "agor"),
    # External baseline (FOSS rule applies; sentinel only)
    ("K5-claude-solo",  None,                                    "claude-solo"),
]

# R24_SYNTHESIS: drop K9 + K15
_R24_DROP_VARIANT_IDS = {"K9-mixtral", "K15-kimi-thinking"}
VARIANTS = [v for v in VARIANTS if v[0] not in _R24_DROP_VARIANT_IDS]
print(f"[R24] active variants: {[v[0] for v in VARIANTS]}")

# R25_DESIGN: drop K7 generative
_R25_DROP_GENERATIVE = {"K7-qwen72b"}
# R26 P1-1/P1-2/P1-5 — extend drops: K17 (loops both brokers), K33 (slug=K11), K34/K35 (dead OR slugs)
_R26_DROPPED = {"K17-glm46", "K33-qwen3-coder-72b", "K34-deepseek-v3-pro", "K35-llama-3.1-405b"}
VARIANTS = [v for v in VARIANTS if v[0] not in _R26_DROPPED]
print(f"[R26] dropped K17/K33/K34/K35 from active pool ({len(VARIANTS)} active variants remain)")
VARIANTS = [v for v in VARIANTS if v[0] not in _R25_DROP_GENERATIVE]
print(f"[R25] dropped K7-qwen72b from generative pool ({len(VARIANTS)} active variants remain)")

# R25_DESIGN: drop K6 baseline
# R26 P1-9: K6 re-introduced as alt-broker probe on Together/Parasail
# (R25 dropped K6 because DeepInfra×Llama-3.3-70b silent_noop'd 4/10
# cells at model contract; alt brokers untested).
_R25_DROP_BASELINE: set[str] = set()
VARIANTS = [v for v in VARIANTS if v[0] not in _R25_DROP_BASELINE]
print(f"[R26 P1-9] K6-llama70b re-introduced on Together/Parasail "
      f"({len(VARIANTS)} active variants remain)")


# R17 fix B1 — eager prefetch cache (compute each task's prefetch ONCE
# at run start, share across all cells of that task). Saves ~5s × 8
# tasks × N samples = significant.
_PREFETCH_CACHE: dict = {}


def get_prefetch(task: dict) -> dict:
    tid = task["id"]
    if tid not in _PREFETCH_CACHE:
        _PREFETCH_CACHE[tid] = task["prefetch"](task)
    return _PREFETCH_CACHE[tid]


# R17 fix B4 — per-provider warmup. At run start, send a 1-token
# completion to each variant so first real call is warm. Saves
# cold-start latency penalty per provider.
#
# R26 P0-5 — warmup is now a REAL pre-flight, not a non-fatal ping.
# Sends a "Reply with exactly: PING" prompt and asserts the response
# content matches /PING/i. Variants that fail (HTTP 4xx, missing
# content, no PING match, timeout, exception) are held out of the
# round via WARMED_VARIANTS gating. If >30% of variants fail, write
# PREFLIGHT_FAIL sentinel and SystemExit so the round refuses launch.
WARMED_VARIANTS: set = set()


def _warmup_one(name, model_id, or_key):
    """Send a real chat completion. Return (ok: bool, reason: str)."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user",
                      "content": "Reply with exactly: PING"}],
        "max_tokens": 10, "temperature": 0,
    }
    pin = PROVIDER_PINS.get(model_id)
    if pin: payload["provider"] = pin
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            d = json.loads(body)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"URLError {exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        content = d["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        return False, f"no content ({type(exc).__name__})"
    if "ping" not in content.lower():
        snippet = content.strip().replace("\n", " ")[:60]
        return False, f"no PING match (got: {snippet!r})"
    return True, f"provider={d.get('provider', '?')}"


def warmup_providers():
    """R26 P0-5 pre-flight. Mutates WARMED_VARIANTS + filters VARIANTS."""
    global VARIANTS
    log("warming providers (P0-5 pre-flight)...")
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    candidates = [(n, m, mode) for (n, m, mode) in VARIANTS
                   if mode == "agor" and m is not None]
    failed = []
    for name, model_id, _mode in candidates:
        ok, reason = _warmup_one(name, model_id, or_key)
        if ok:
            WARMED_VARIANTS.add(name)
            log(f"  warmed {name}: {reason}")
        else:
            failed.append(name)
            log(f"[warmup-fail {name}: {reason}]")
    total = len(candidates)
    n_fail = len(failed)
    fail_rate = (n_fail / total) if total else 0.0
    log(f"warmup: {total - n_fail}/{total} OK; fail-rate {fail_rate:.0%}")
    if total and fail_rate > 0.30:
        sentinel = ARTIFACTS / "PREFLIGHT_FAIL"
        sentinel.write_text(
            f"warmup fail-rate {fail_rate:.0%} > 30% threshold\n"
            f"failed: {failed}\n")
        log(f"PREFLIGHT_FAIL: {sentinel}")
        raise SystemExit("warmup pre-flight: too many variants failed")
    # Hold dead variants out of the round. Non-agor / model_id=None
    # variants (e.g. K5-claude-solo) are passed through unchanged.
    before = [v[0] for v in VARIANTS]
    VARIANTS = [v for v in VARIANTS
                if v[2] != "agor" or v[1] is None
                or v[0] in WARMED_VARIANTS]
    after = [v[0] for v in VARIANTS]
    dropped = [n for n in before if n not in after]
    if dropped:
        log(f"warmup: dropped {dropped} from round")


# R17 — provider pinning per cache audit 2026-05-07.
# Pin each route to a single OR provider to stabilize prefix cache + latency.
# R18 — provider pins per cache audit + R17 provider/cache agent findings.
# R18 v2: kept advisory pins (allow_fallbacks default True) — v1 hit a wall:
# DeepInfra serves Kimi/Llama with tools but NOT deepseek-chat-v3 / qwen-72b.
# Strict pinning made K7/K8/K17/K15/K5 silent. Falling back to advisory pin
# (preferred broker, but allow OR to use any tool-capable backup).
# R25_DESIGN: K17 strict-order
PROVIDER_PINS = {
    # R19_WIRING: provider.ignore — DeepInfra tool-format gap (R18 broker forensics)
    "moonshotai/kimi-k2.6":               {"order": ["Moonshot", "Parasail"], "ignore": ["DeepInfra"]},
    "moonshotai/kimi-k2-thinking":        {"order": ["Moonshot", "Novita"]},
    "deepseek/deepseek-r1":               {"order": ["DeepInfra"]},
    "deepseek/deepseek-chat-v3-0324":     {"order": ["DeepInfra"]},
    "deepseek/deepseek-coder":            {"order": ["DeepInfra"]},
    "qwen/qwen3-coder-30b-a3b-instruct":  {"order": ["Novita"]},
    # R25_DESIGN: qwen-2.5-72b ignore Novita
    "qwen/qwen-2.5-72b-instruct":         {"order": ["DeepInfra"], "ignore": ["Novita"]},
    # R19_WIRING: provider.ignore — SiliconFlow silent-text fallback
    "qwen/qwen3-coder":                   {"order": ["Together"], "ignore": ["SiliconFlow"]},
    "qwen/qwen3.6-27b":                   {"order": ["Together"]},
    "qwen/qwen-2.5-coder-32b-instruct":   {"order": ["DeepInfra"]},
    # R19_WIRING: provider.ignore — AkashML silent_noop
    # R26 P1-9: DeepInfra silent_noop'd 4/10 K6 cells in R25 (broken at model contract);
    # try Together + Parasail brokers as alt-broker probe; deny both known-broken brokers.
    "meta-llama/llama-3.3-70b-instruct":  {"order": ["Together", "Parasail"], "ignore": ["DeepInfra", "AkashML"]},
    "meta-llama/llama-3.1-405b-instruct": {"order": ["Together"]},
    "meta-llama/llama-4-instruct":        {"order": ["Together"]},
    "mistralai/mixtral-8x22b-instruct":   {"order": ["Mistral"]},
    "openai/gpt-oss-120b":                {"order": ["Parasail"]},
    "openai/gpt-oss-20b":                 {"order": ["Parasail"]},
    # R19_WIRING: Z-AI never actually served — flip SiliconFlow first
    # R25_DESIGN: glm-4.6 ignore DeepInfra
    "z-ai/glm-4.6":                       {"order": ["SiliconFlow"], "ignore": ["DeepInfra", "Z-AI"]},
}

# R25_DESIGN: K11 premium-pool budget cap
_R25_VARIANT_BUDGETS = {
    "K11-qwen3coder": 1.50,   # R24 Pareto: dominated by K1, premium-pool only
    "K33-qwen3-coder-72b": 2.00,
    "K34-deepseek-v3-pro": 2.00,
    "K35-llama-3.1-405b": 3.00,
}
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

# R25_DESIGN: K2 max_tokens cap (PM5 from R24 providers agent)
_R25_VARIANT_MAX_TOKENS = {
    "K2-kimi-k2.6": 4096,        # was unbounded → finish=length
    "K15-kimi-thinking": 4096,    # same family
}

# R18 — Modal-Kimi route override (S6 Tier-S strategy). Kimi cells use
# Modal-self-hosted endpoint when MODAL_KIMI_ENABLED env is "1".
# Sub-second TTFT vs Parasail's 2-200s tail.
#
# R26 P1-4 status (2026-05-08): the launch-checklist plan was to flip
# this default-ON for R26 to dodge Parasail's 60-180s prefill stall
# (R25 saw 16 K2 WALL_CAP_KILLED). Pre-flight ping returned HTTP 429
# "workspace billing cycle spend limit reached" (matches PM1 in
# docs/notes/2026-05-08-bench-arc-post-mortems.org). Default stays
# OFF until the Modal billing cycle resets; flip to default-ON by
# changing the gate below to `os.environ.get("MODAL_KIMI_ENABLED",
# "1") == "1"` once a curl ping returns HTTP 200 < 5s. P1-4 tracked
# PARTIAL in the launch checklist for this reason.
MODAL_KIMI_URL = "https://daniel2501--org-llm-kimi-k26-vllm-serve.modal.run/v1/chat/completions"
MODAL_KIMI_VARIANTS = {"K2-kimi-k2.6", "K15-kimi-thinking"}

# R18 — overnight scope: 16-way parallelism + n=10 default + $375 cap.
# All-night R18 plans for 8-12h wall, ~$200-350.
PER_CELL_MAX_ITERS_ALL_NIGHT = 24      # was 16; allow more on long-horizon
N_DEFAULT_LAYER_2 = 10                  # was n=1 in R17
N_PROSE_TASKS = 15                      # B5/B11/B20/B45 — quality-sensitive
N_K8_PROMOTE = 15                       # validate stability claim hard

# R18 — drop these variants entirely (per R17 quality + Pareto agents).
DEAD_VARIANTS = {"K13-deepseekV4", "K16-qwen36-27b",
                  "K18-llama4", "K19-qwen25c-32b"}


# ── Dial configuration ──────────────────────────────────────────────────
@dataclass
class DialConfig:
    """Per-cell dial knobs (R15 D5-D10)."""
    brief_mode: str = "tight"        # tight | loose
    prefetch_mode: str = "both"       # inline | tool | both
    with_manager: bool = True
    tool_surface: str = "broad"       # minimal | broad
    scope_strict: bool = True

    def label(self) -> str:
        return (f"b{self.brief_mode[0]}_p{self.prefetch_mode[0]}_"
                f"m{int(self.with_manager)}_t{self.tool_surface[0]}_"
                f"s{int(self.scope_strict)}")


BEST_CONFIG = DialConfig(
    brief_mode="tight", prefetch_mode="both", with_manager=True,
    tool_surface="broad", scope_strict=True,
)

# Each ablation cell flips ONE dial from BEST_CONFIG.
ABLATION_DIALS = [
    ("d5_loose",   DialConfig(brief_mode="loose", prefetch_mode="both",
                                with_manager=True, tool_surface="broad",
                                scope_strict=True)),
    ("d6_inline",  DialConfig(brief_mode="tight", prefetch_mode="inline",
                                with_manager=True, tool_surface="broad",
                                scope_strict=True)),
    ("d6_tool",    DialConfig(brief_mode="tight", prefetch_mode="tool",
                                with_manager=True, tool_surface="broad",
                                scope_strict=True)),
    ("d7_no_mgr",  DialConfig(brief_mode="tight", prefetch_mode="both",
                                with_manager=False, tool_surface="broad",
                                scope_strict=True)),
    ("d8_minimal", DialConfig(brief_mode="tight", prefetch_mode="both",
                                with_manager=True, tool_surface="minimal",
                                scope_strict=True)),
    ("d9_relaxed", DialConfig(brief_mode="tight", prefetch_mode="both",
                                with_manager=True, tool_surface="broad",
                                scope_strict=False)),
]


# ── Misc helpers (carried from R14) ──────────────────────────────────────
def _strip_org_meta(text):
    text = re.sub(r"^:PROPERTIES:.*?:END:\s*", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"^#\+\w+:.*$\n", "", text, flags=re.MULTILINE)
    return text.strip()


PICARD_PRIMER = _strip_org_meta(PICARD_PRIMER_FILE.read_text())
L1A_TEXT = _strip_org_meta(L1A_FILE.read_text())
L1B_TEXT = _strip_org_meta(L1B_FILE.read_text())


def _extract_l1_for_handle(handle):
    h = handle.lstrip("@").lower()
    sections = []
    m_base = re.search(r"\* Shared baseline.*?(?=\n\* )", L1B_TEXT, re.DOTALL)
    if m_base: sections.append("# === ORG-MODE SHARED BASELINE ===\n\n" + m_base.group(0).strip())
    m_b = re.search(rf"\* @{h} —.*?(?=\n\* )", L1B_TEXT, re.DOTALL | re.IGNORECASE)
    if m_b: sections.append(f"# === ORG-MODE EXPERTISE for @{h} ===\n\n" + m_b.group(0).strip())
    m_a = re.search(rf"\*\* @{h} —.*?(?=\n\*\* @|\n\* )", L1A_TEXT, re.DOTALL | re.IGNORECASE)
    if m_a: sections.append(f"# === ORG-LLM CLI VERBS for @{h} ===\n\n" + m_a.group(0).strip())
    return "\n\n---\n\n".join(sections) if sections else ""


# ── O3: running progress + cost ticker ───────────────────────────────────
class Progress:
    def __init__(self, total_cells: int, budget_usd: float):
        self.total = total_cells
        self.done = 0
        self.budget = budget_usd
        self.spent = 0.0
        self.t0 = time.time()
        self.cells: list[dict] = []

    def cell_done(self, cell: dict) -> str:
        self.done += 1
        cost = (cell.get("cost_usd")
                  or (cell.get("phase1_cost_usd", 0)
                       + cell.get("specialist_cost_usd", 0)))
        self.spent += cost
        self.cells.append(cell)
        elapsed = time.time() - self.t0
        avg_per_cell = elapsed / max(self.done, 1)
        eta_seconds = avg_per_cell * (self.total - self.done)
        return (f"({self.done}/{self.total} cells, ${self.spent:.3f}/${self.budget:.0f}, "
                f"~{int(eta_seconds/60)} min remaining)")


PROGRESS: Progress | None = None


def log(msg, also_to_stdout=True):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if also_to_stdout: print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(line + "\n")


# R26 P1-15 — BK3 bug-injection pre-stage hook. BK1-BK5 fixtures
# (from scripts/_round19_long_horizon_tasks.py) may carry an
# `apply_starting_state(workdir)` callable. BK3 uses it to install a
# failing test + invert two `_check_append_only` returns so the
# model has a real bug to fix. The hook must fire on a fresh
# worktree, AFTER `git worktree add`, BEFORE phase-1 prefetch +
# specialist run. Idempotent — re-runs are safe.
def maybe_apply_starting_state(task, wt_path):
    fn = task.get("apply_starting_state")
    if not fn:
        return
    ok, msg = fn(wt_path)
    if not ok:
        raise RuntimeError(
            f"apply_starting_state failed for {task.get('id')}: {msg}")
    log(f"  -- applied starting state for {task.get('id')}: {msg}")


def tok():
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin():
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                    capture_output=True, env=env, check=True)


def req(method, path, body=None, retries=1):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(BASE + path, data=data, method=method,
                headers={"Authorization": f"Bearer {tok()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries:
                relogin()
                continue
            raise


# ── Prefetch (carried from R14) ──────────────────────────────────────────
def build_known_ids_map():
    wiki = REPO / "docs/wiki"
    id_to_meta, basename_to_ids, dec_to_id = {}, {}, {}
    for org in sorted(wiki.glob("*.org")):
        text = org.read_text()
        m = re.search(r"^:ID:\s+([a-f0-9-]+)", text, re.MULTILINE)
        if m:
            uid = m.group(1)
            id_to_meta[uid] = {"file": org.name}
            basename_to_ids.setdefault(org.stem, []).append(uid)
            if org.name == "decisions.org":
                for dm in re.finditer(r"^\*\* (DEC-\d+)\s", text, re.MULTILINE):
                    dec_to_id[dm.group(1)] = uid
    return id_to_meta, basename_to_ids, dec_to_id


KNOWN_ID_TO_META, BASENAME_TO_IDS, DEC_TO_ID = build_known_ids_map()


def prefetch_b1(task):
    target = REPO / task["target_file"]
    text = target.read_text()
    lines = text.splitlines()
    roadmap_id = (BASENAME_TO_IDS.get("roadmap") or [None])[0]
    candidates = []
    for ln_no, line in enumerate(lines, 1):
        if "[[id:" in line: continue
        for m in re.finditer(r"Phase \d{4}-\d{2}\.\d{2}\b", line):
            if roadmap_id:
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": m.group(0),
                                    "canonical_owner": "roadmap.org",
                                    "canonical_id": roadmap_id, "kind": "phase"})
        for m in re.finditer(r"\bDEC-\d+\b", line):
            label = m.group(0)
            if label in DEC_TO_ID:
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": label,
                                    "canonical_owner": "decisions.org",
                                    "canonical_id": DEC_TO_ID[label], "kind": "dec"})
        for stem, ids in BASENAME_TO_IDS.items():
            for m in re.finditer(r"\b" + re.escape(stem) + r"\.org\b", line):
                candidates.append({"line": ln_no, "snippet": line.strip()[:120],
                                    "matched_text": stem + ".org",
                                    "canonical_owner": stem + ".org",
                                    "canonical_id": ids[0], "kind": "basename"})
                break
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(lines), "known_ids_count": len(KNOWN_ID_TO_META),
             "candidates": candidates}


def prefetch_passthrough(task):
    target = REPO / task["target_file"]
    if target.exists():
        text = target.read_text()
        return {"target_path": str(target.relative_to(REPO)),
                 "target_lines": len(text.splitlines()),
                 "target_size_bytes": len(text)}
    return {"target_path": task.get("target_file", ""), "missing": True}


def prefetch_b5(task):
    target = REPO / task["target_file"]
    text = target.read_text()
    paragraphs = re.split(r"\n\s*\n", text)
    longest = max(((i, p) for i, p in enumerate(paragraphs)), key=lambda x: len(x[1]))
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(text.splitlines()),
             "longest_paragraph_index": longest[0],
             "longest_paragraph_chars": len(longest[1]),
             "longest_paragraph_preview": longest[1][:500]}


def prefetch_b11(task):
    decisions_text = (REPO / "docs/wiki/decisions.org").read_text()
    nums = sorted({int(m.group(1)) for m in re.finditer(r"^\*\* DEC-(\d+)\s",
                                                          decisions_text, re.MULTILINE)})
    return {"decisions_path": "docs/wiki/decisions.org",
             "existing_dec_numbers": nums,
             "next_available": (max(nums) + 1) if nums else 1,
             "problem_statement": task.get("problem_statement", "")}


def prefetch_b20(task):
    """B20: lift a wiki page that lacks a Summary section, find one to add."""
    target = REPO / task["target_file"]
    text = target.read_text()
    has_summary = bool(re.search(r"^\s*\*Summary\.\*", text, re.MULTILINE))
    title_m = re.search(r"^#\+TITLE:\s*(.+)$", text, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else target.stem
    first_para = ""
    after_meta = re.sub(r"^(:PROPERTIES:.*?:END:|#\+\w+:.*)\n", "", text,
                         flags=re.MULTILINE | re.DOTALL).strip()
    paras = re.split(r"\n\s*\n", after_meta)
    for p in paras:
        if not p.startswith("*"):
            first_para = p.strip()[:600]
            break
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(text.splitlines()),
             "title": title,
             "has_summary": has_summary,
             "first_non_heading_paragraph_preview": first_para}


def prefetch_b25(task):
    """B25: write a NEW elisp helper file. Just confirm target dir is writable."""
    parent = (REPO / task["target_file"]).parent
    return {"target_path": task["target_file"],
             "target_dir_exists": parent.exists(),
             "target_dir_writable": os.access(parent, os.W_OK) if parent.exists() else False,
             "spec": ("Create a NEW elisp file at the target path with a single "
                       "defun named `org-llm-r26-shout` that takes one string arg "
                       "and returns it uppercased with three exclamation marks. "
                       "Then verify it loads via load_elisp_file, and call it "
                       "via eval_elisp on the input \"hello\" to confirm output "
                       "is \"HELLO!!!\".")}


def prefetch_b26(task):
    """B26: add a defcustom to existing doom/org-llm-specialist.el."""
    target = REPO / task["target_file"]
    text = target.read_text()
    has_defcustom_budget = "org-llm-specialist-default-budget" in text
    return {"target_path": str(target.relative_to(REPO)),
             "target_lines": len(text.splitlines()),
             "has_defcustom_budget_already": has_defcustom_budget,
             "spec": ("Add a NEW defcustom named `org-llm-specialist-default-budget` "
                       "to org-llm-specialist.el. Type :type 'number, default 1.0, "
                       "group 'org-llm-specialist, with a one-line docstring. Place "
                       "it next to the other defcustoms (around the existing "
                       "`org-llm-specialist-default-handle` definition). Then verify "
                       "the file still loads via load_elisp_file. If "
                       "has_defcustom_budget_already is true, no-op + explain.")}


TASKS = [
    {"id": "B1", "label": "cross-link audit on literate-tools.org",
     "target_file": "docs/wiki/literate-tools.org",
     "forbid_verbatim_in_labels": True,   # R17 fix #2 — enforce + revert
     "forbid_unknown_ids": True,          # R17 synthesis #5 — UUID enforcement
     "goal": "Add EXACTLY 5 [[id:UUID][label]] cross-link wrappers around existing prose mentions. Use the canonical_id from the pre-fetched candidates list (or call find_canonical_id when D6=tool). **STRIP =verbatim= markers BEFORE placing text inside link labels — org renders =foo= literally inside link descriptions; the harness will revert any wrap that contains = inside the label.** **EVERY [[id:UUID]] you insert MUST be either in the prefetched candidates list OR returned by find_canonical_id — the harness will revert any link to an unknown UUID.** Do NOT touch any other file. Do NOT change section headings or structure.",
     "n_changes": 5, "prefetch": prefetch_b1, "primary_metric": "wrap_categories"},
    {"id": "B5", "label": "section rewrite for clarity",
     "target_file": "docs/wiki/agor-pilot-install.org",
     "goal": (
        "Pick the longest paragraph (see prefetch.longest_paragraph_*) and "
        "rewrite IT for tightness — at least 30% shorter. Preserve meaning.\n\n"
        "**SURVEY-BEFORE-EDIT (REQUIRED):**\n"
        "1. read_file on the target. Locate the longest paragraph by looking "
        "at the prefetch's longest_paragraph_index + scanning around it.\n"
        "2. Read 1-2 ADJACENT paragraphs to get the page's tone + voice — "
        "your rewrite should match that voice.\n"
        "3. Then write your tightened replacement via edit_file (old_string = "
        "the full original paragraph; new_string = your shorter version).\n\n"
        "Quality cues:\n"
        "- Drop redundant phrasing (\"in order to\" → \"to\"; \"the fact that\" → \"that\")\n"
        "- Combine sentences only when they share a subject\n"
        "- Don't lose technical terms / IDs / file paths\n"
        "Do NOT touch any other file."),
     "n_changes": 1, "prefetch": prefetch_b5, "primary_metric": "in_scope_changes"},
    {"id": "B7", "label": "add docstrings + type hints",
     "target_file": "org_llm/avatars.py",
     "forbid_stacked_docstrings": True,   # R17 fix #5 — catch duplicates
     "goal": "**EDIT ONLY THE FILE `org_llm/avatars.py`** — do not touch any other file. Add Python type hints + one-line docstrings to every public function/class. **DO NOT STACK a new docstring on top of an existing one — the harness will revert if a function has 2 docstrings.** Existing behavior must not change. Use lowercase `list[str]` / `dict[str, int]` (Python 3.9+ idiom), NOT capital `List[str]`. `python -c 'import org_llm.avatars'` must import cleanly. If the file is already type-hinted, no-op and explain.",
     "n_changes": None, "prefetch": prefetch_passthrough,
     "primary_metric": "in_scope_changes"},
    {"id": "B11", "label": "draft DEC entry for an open question",
     "target_file": "docs/wiki/decisions.org",
     "problem_statement": "Should @picard be auto-created at `org-llm init` (eager) OR lazily-spawned on first team-spawn (lazy)?",
     "append_only": True,                 # R17 fix #1 — harness reverts deletions
     "max_file_inline_chars": 4000,        # R17 fix #3 — fit qwen 32k ctx
     "forbid_unknown_ids": True,          # R17 synthesis #5 — UUID enforcement (catches Claude's 1 fab/cell)
     "goal": (
        "**APPEND-ONLY: the harness will REVERT your edit if it produces ANY "
        "deletion line in `git diff`.** Add a new DEC at the END of "
        "docs/wiki/decisions.org via edit_file anchored on the file's tail.\n\n"
        "**SURVEY-BEFORE-EDIT (REQUIRED):**\n"
        "1. Call read_file on docs/wiki/decisions.org first (truncated at 4000 "
        "chars showing the tail) to see the existing format + the last DEC.\n"
        "2. Note the DEC numbering (zero-padded? what's the last?).\n"
        "3. Use ONE existing DEC body as your STYLE TEMPLATE (don't paraphrase "
        "headings — match the existing format).\n"
        "4. Generate a real UUID by running run_python with code "
        "`import uuid; print(uuid.uuid4())`. Do NOT invent the UUID from your "
        "own output — Claude has been caught fabricating UUIDs in this exact "
        "task; the harness now reverts unknown IDs.\n"
        "5. Then write your edit with edit_file."),
     "examples": [
        "REQUIRED HEADINGS (EXACTLY, in order — Rule 2b enforced):\n"
        "  *Summary.*    1-3 sentences\n"
        "  *Expanded.*   1 paragraph (REQUIRED — do not skip; FOSS R16 missed this)\n"
        "  *Context.*    bullet list, 3-6 items\n"
        "  *Options.*    numbered, 2+ named options w/ prose (not 1-line)\n"
        "  *Tradeoffs.*  org table OR bullet list\n"
        "  *Decision.*   one line OR \"Undecided — open for resolution.\"\n"
        "  *Rationale.*  1 paragraph (NOT \"[to be determined]\")\n"
        "\n"
        "FULL DEC SKELETON (fill in <BRACKETS>):\n\n"
        "  ** DEC-<NNN> — <title-in-kebab-case> (status: <CANDIDATE|ACCEPTED>)\n"
        "  :PROPERTIES:\n"
        "  :ID:       <real-uuid-from-uuidgen-tool-call>\n"
        "  :CREATED:  [2026-05-08]\n"
        "  :END:\n\n"
        "  *Summary.* <1-3 sentence one-line problem + decision>\n\n"
        "  *Expanded.* <1 paragraph context + decision rationale, fuller form>\n\n"
        "  *Context.*\n"
        "  - <bullet 1>\n"
        "  - <bullet 2>\n"
        "  - <bullet 3>\n\n"
        "  *Options.*\n"
        "  - <Option A name> — <prose explanation, 1-3 sentences>\n"
        "  - <Option B name> — <prose explanation, 1-3 sentences>\n\n"
        "  *Tradeoffs.* <one paragraph OR org table comparing options>\n\n"
        "  *Decision.* <chosen path OR \"Undecided — open for resolution.\">\n\n"
        "  *Rationale.* <one paragraph why; not [to be determined]>\n\n"
        "RULES:\n"
        "- DEC number is THREE digits, zero-padded (DEC-020 not DEC-20)\n"
        "- Title is kebab-case, lowercase\n"
        "- Status starts as CANDIDATE for new DECs\n"
        "- Use a REAL UUID via run_python (NOT date-string, NOT pattern-shaped, NOT a reused existing ID)\n"
        "- Do NOT modify any existing DEC\n"
        "- Use edit_file with old_string anchored on the last 3-5 lines\n"
        "  of the file (use read_file first to see what's there)"],
     "n_changes": 1, "prefetch": prefetch_b11, "primary_metric": "in_scope_changes"},
    {"id": "B13", "label": "update LICENSE copyright year",
     "target_file": "LICENSE",
     "goal": "Update copyright year `2025` → `2026`. If already 2026, no edit needed.",
     "n_changes": 1, "prefetch": prefetch_passthrough,
     "primary_metric": "in_scope_changes"},
    {"id": "B20", "label": "add Summary section to wiki page",
     "target_file": "docs/wiki/picard-agor-primer.org",
     "goal": (
        "If this page does NOT already begin with `*Summary.*` (per wiki Rule "
        "2b), add a 1-3 sentence summary block right after the page-level "
        "metadata (before any content sections).\n\n"
        "**SURVEY-BEFORE-EDIT (REQUIRED):**\n"
        "1. Check prefetch.has_summary. If TRUE, no-op + explain in your "
        "final message.\n"
        "2. If FALSE: read_file on the target. Look at the first non-heading "
        "paragraph (prefetch shows preview).\n"
        "3. Read another wiki page with a *Summary* section as a STYLE "
        "TEMPLATE (e.g. docs/wiki/decisions.org or docs/wiki/architecture.org).\n"
        "4. Then add your Summary via edit_file.\n\n"
        "FORMAT (Rule 2b — both REQUIRED if creating from scratch):\n"
        "  *Summary.* <1-3 sentence concrete one-line distillation.>\n\n"
        "  *Expanded.* <1 paragraph fuller version with cross-refs.>\n\n"
        "Pull the gist from the page's first non-heading paragraph (see "
        "prefetch.first_non_heading_paragraph_preview). Do NOT modify any "
        "other content."),
     "n_changes": 1, "prefetch": prefetch_b20,
     "primary_metric": "in_scope_changes"},
    # ── Elisp tasks (R15 — wired with primer + doom-conventions reference) ─
    {"id": "B25", "label": "write a new elisp helper file + validate",
     "target_file": "tests/manual_test_helper_r16.el",
     "goal": ("Create a NEW elisp file at the target path with a single "
                "defun named `org-llm-r26-shout` that takes one string arg "
                "and returns it uppercased with three exclamation marks. "
                "Use the elisp primer + doom conventions reference. After "
                "writing, call load_elisp_file to verify it loads cleanly, "
                "then call eval_elisp with code "
                "`(progn (load \"<absolute-path-to-your-file>\") "
                "(org-llm-r26-shout \"hello\"))` to confirm it returns "
                "\"HELLO!!!\". Do NOT modify any other file."),
     "n_changes": 1, "prefetch": prefetch_b25,
     "primary_metric": "in_scope_changes"},
    {"id": "B26", "label": "add a defcustom to existing doom/*.el",
     "target_file": "doom/org-llm-specialist.el",
     "goal": ("Add a NEW defcustom named `org-llm-specialist-default-budget` "
                "to org-llm-specialist.el. :type 'number, default 1.0, "
                ":group 'org-llm-specialist, with a one-line docstring. "
                "Place it alongside the other defcustoms (near "
                "`org-llm-specialist-default-handle`). After editing, call "
                "load_elisp_file to verify the file still loads cleanly. "
                "If `has_defcustom_budget_already` is true in prefetch, "
                "no-op + explain. Do NOT touch any other file."),
     "n_changes": 1, "prefetch": prefetch_b26,
     "primary_metric": "in_scope_changes"},
]

# R19_WIRING: BK1-BK5
try:
    from scripts._round19_long_horizon_tasks import LONG_HORIZON_TASKS
    TASKS.extend(LONG_HORIZON_TASKS)
    print(f"[R19] Loaded {len(LONG_HORIZON_TASKS)} long-horizon tasks (BK1-BK5)")
except Exception as _e:
    print(f"[R19] BK1-BK5 import failed: {_e} — proceeding without")


PLAN_FORMAT = """
You are @picard executing PHASE 1. Output a JSON plan.

PICARD_PLAN_JSON_BEGIN
{
  "classification": {"judgment": "<level>", "recurrence": "<level>", "stakes": "<level>"},
  "playbook": "<flat|A|B|C|D|E>",
  "team": [{"handle": "@<handle>", "task_brief": "<full multi-line brief>"}],
  "rationale": "<one paragraph>"
}
PICARD_PLAN_JSON_END
"""


def call_openrouter(model_id, prompt):
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    body = json.dumps({"model": model_id,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1, "max_tokens": 3000}).encode()
    req2 = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req2, timeout=180) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    cost = float(data.get("usage", {}).get("cost") or 0)
    return text, cost


def parse_plan(text):
    m = re.search(r"PICARD_PLAN_JSON_BEGIN\s*(.+?)\s*PICARD_PLAN_JSON_END",
                   text, re.DOTALL)
    if not m:
        m2 = re.search(r"(\{[^}]*\"classification\".+\})", text, re.DOTALL)
        if not m2: return None
        json_text = m2.group(1)
    else:
        json_text = m.group(1).strip()
    try: return json.loads(json_text)
    except json.JSONDecodeError: return None


PERSONA_LOOKUP = {
    "@atoz": "You are @atoz — Bridge Crew wiki concept-graph specialist.",
    "@data": "You are @data — Bridge Crew code + scribe specialist.",
    "@spock": "You are @spock — Bridge Crew logic + canonical-source reviewer.",
    "@geordi": "You are @geordi — Bridge Crew analytics + charts specialist.",
    "@boothby": "You are @boothby — Bridge Crew ops + hygiene specialist.",
    "@riker": "You are @riker — Bridge Crew process + scheduling specialist.",
}


def default_handle_for_task(task):
    """When D7=no_manager, pick the right specialist handle for the task."""
    tid = task["id"]
    if tid in ("B1", "B11"): return "@atoz"
    if tid in ("B5", "B20"): return "@atoz"
    if tid == "B7": return "@data"
    if tid == "B13": return "@boothby"
    if tid in ("B25", "B26"): return "@data"   # elisp = code-shaped
    return "@data"


# ── Diff analysis (carried from R14) ─────────────────────────────────────
INSERTED_LINK_RE = re.compile(r"\[\[id:([0-9a-f-]+)\]\[([^\]]+)\]\]")


def parse_diff_files(diff_text):
    files = {}
    cur = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                cur = m.group(2)
                files[cur] = {"insertions": 0, "deletions": 0,
                                "added_lines": [], "removed_lines": []}
        elif cur and line.startswith("+") and not line.startswith("+++"):
            files[cur]["insertions"] += 1
            files[cur]["added_lines"].append(line[1:])
        elif cur and line.startswith("-") and not line.startswith("---"):
            files[cur]["deletions"] += 1
            files[cur]["removed_lines"].append(line[1:])
    return files


def analyze_diff(diff_text, target_files, prefetch):
    files = parse_diff_files(diff_text)
    target_set = {str(p) for p in (target_files or [])}
    in_scope_ins = in_scope_del = 0
    out_scope_ins = out_scope_del = 0
    in_scope_files = []
    out_scope_files = []
    for fp, stats in files.items():
        if fp in target_set:
            in_scope_ins += stats["insertions"]
            in_scope_del += stats["deletions"]
            in_scope_files.append(fp)
        else:
            out_scope_ins += stats["insertions"]
            out_scope_del += stats["deletions"]
            out_scope_files.append(fp)

    inserted_links = []
    candidate_texts = {c.get("matched_text", "")
                        for c in (prefetch.get("candidates") or [])}
    for fp, stats in files.items():
        for added in stats["added_lines"]:
            for m in INSERTED_LINK_RE.finditer(added):
                uid, label = m.group(1), m.group(2)
                in_workdir = uid in KNOWN_ID_TO_META
                label_norm = label.strip().strip("=")
                on_candidate = any(
                    c and (c.lower() in label_norm.lower()
                            or label_norm.lower() in c.lower())
                    for c in candidate_texts
                )
                inserted_links.append({
                    "file": fp, "id": uid, "label": label,
                    "id_in_workdir": in_workdir,
                    "wrap_category": (
                        "on_candidate" if on_candidate
                        else ("creative" if in_workdir else "unmatched")
                    ),
                })
    fab_count = sum(1 for il in inserted_links if not il["id_in_workdir"])
    cat_counts = {"on_candidate": 0, "creative": 0, "unmatched": 0}
    for il in inserted_links:
        cat_counts[il["wrap_category"]] += 1

    return {
        "files_touched": list(files.keys()),
        "in_scope_files": in_scope_files,
        "out_of_scope_files": out_scope_files,
        "diff_in_scope": {"insertions": in_scope_ins, "deletions": in_scope_del},
        "diff_out_of_scope": {"insertions": out_scope_ins, "deletions": out_scope_del},
        "inserted_links_count": len(inserted_links),
        "id_fabrication_count": fab_count,
        "wrap_categories": cat_counts,
        "inserted_links": inserted_links,
        "diff_text": diff_text,  # R19_WIRING: stash for quality_lint
    }


def wt_diff_vs_base(wt_path):
    base = subprocess.run(["git", "-C", str(wt_path), "merge-base",
                            "HEAD", "trunk"],
                            capture_output=True, text=True).stdout.strip() or "trunk"
    diff_full = subprocess.run(["git", "-C", str(wt_path), "diff", base, "HEAD"],
                                 capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "-C", str(wt_path), "diff", "--stat",
                                 base, "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    return diff_full, diff_stat


# ── Cell execution ───────────────────────────────────────────────────────
def write_cell_result(cell_dir, payload):
    (cell_dir / "cell_result.json").write_text(json.dumps(payload, indent=2,
                                                             default=str))


def write_events_jsonl(cell_dir, events):
    """O2: per-cell events stream, jq-friendly."""
    p = cell_dir / "events.jsonl"
    with p.open("a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# R17 fix A3 + R16 L16 — judge's top-5 prompt fixes + R16 quality-judge
# negative examples. Negative space (real broken outputs from past runs)
# is documented INLINE so models see what to avoid, not just what to do.
JUDGE_FIXES_HEADER = """\
QUALITY GUARDRAILS (read carefully — these caught real R15+R16 failures):

1) LINK LABEL CRAFT — when you wrap a phrase as [[id:UUID][label]], the
   label should be the FULL descriptive phrase from the source line, not
   a bare ID/number. GOOD: "[[id:abc123][Phase 2026-05.14 — agent-framework
   refinements (legacy 23.2 — capability enforcement)]]". BAD:
   "[[id:abc123][Phase 2026-05.14]]".

2) NO VERBATIM-MARKERS INSIDE LINK DESCRIPTIONS. Org-mode renders
   "=foo=" inside a link label literally, not as code style. Strip =...=
   wrappers before placing text inside [[id:UUID][...]].

3) APPEND-ONLY rules are LITERAL: "0 deletions verifiable in `git diff`".
   If a task says APPEND-ONLY, your diff must show ZERO deletion lines (-).
   Use edit_file with surgical context, not write_file. Never overwrite a
   file that contains content you didn't author.

4) UUID INTEGRITY — every [[id:UUID]] you insert MUST be either:
   - present in the prefetch candidates list, OR
   - looked up via find_canonical_id (which queries the workdir vault).
   NEVER invent UUIDs from pattern (a8f4c2e1-... shaped IDs that are
   also random-looking).

5) FORMAT SCHEMAS — when emitting structured org content (DEC, daily-log,
   weekly-review), follow the exact section skeleton shown in the brief.
   Don't paraphrase headings.

NEGATIVE EXAMPLES (real broken outputs from R15/R16 — DO NOT REPRODUCE):

R15 K1-qwen30 on B1 produced this malformed wrap:
  [[*TODO [#A] Planned — Phase 2026-05.20 — sync verb POC: Self-coded tools]
  [[[id:43e1b335-656b-45ba-8d6d-7d9b9f2e9c4d]Phase 2026-05.20]] — sync verb
  POC — self-coded
  ↑ DOUBLE-BRACKET-NESTED LINK. Org-mode renders this as broken.

R15 K7-qwen72b on B1 produced:
  [[id:a8f4c2e1-9b3d-4e5f-87a6-c1d2e3f4b5a6][=~/org/org-llm-agents.org=]]
  ↑ =verbatim= MARKERS INSIDE LINK LABEL. Renders literally as "=foo=".

R16 K1-qwen30 + K7-qwen72b on B11 produced a 1500-line decisions.org
deletion (used edit_file with the entire file body as old_string).
↑ APPEND-ONLY VIOLATION — the harness now reverts these automatically,
but you should not even attempt them.

R15 K2-kimi-k2.6 on B7 wrote `List[str]` (legacy capital-L typing):
  ↑ Use lowercase `list[str]` — Python 3.9+ idiom.
"""


def render_specialist_brief(task, plan_brief, dial: DialConfig,
                              prefetch, handle, instr_prefix):
    """Build the actual instruction text sent to the specialist."""
    pieces = [instr_prefix] if instr_prefix else []

    if dial.brief_mode == "loose":
        pieces.append(
            f"TASK ({task['id']} — {task['label']}):\n{task['goal']}\n\n"
            "Audit the situation, decide a reasonable course of action, "
            "and execute it via the available tools."
        )
    else:
        pieces.append(plan_brief or task["goal"])

    if task.get("n_changes") is not None:
        pieces.append(f"N-CHANGES RULE: make EXACTLY {task['n_changes']} change(s).")

    # R16 L16: quality guardrails (judge's top-5 fixes)
    pieces.append(JUDGE_FIXES_HEADER)

    # R16: APPEND-ONLY enforcement — explicit warning when task is tagged.
    # Per failure-mode agent: K1-qwen30 deleted 1643 lines from decisions.org
    # despite "Do NOT delete" — needs sharper restating + tool gating.
    if task.get("append_only"):
        pieces.append(
            "**APPEND-ONLY ENFORCEMENT**\n"
            "This task is APPEND-ONLY. The harness has REMOVED write_file "
            "from your tool surface. You may ONLY edit_file at the END of "
            "the file (use surgical old_string→new_string with context "
            "from the file's tail). Your final `git diff` MUST show ZERO "
            "lines of deletion (no `-` lines) — only insertions (`+`)."
        )

    # R16 L16: per-task few-shot demos when defined
    examples = task.get("examples") or []
    if examples:
        pieces.append("WORKED EXAMPLES (apply this style):\n\n"
                       + "\n\n".join(examples))

    # T1 elisp mastery: primer + doom-conventions reference for elisp tasks
    if task_needs_elisp_kit(task) and dial.tool_surface == "broad":
        pieces.append(ELISP_PRIMER)
        ref = doom_conventions_reference()
        if ref: pieces.append(ref)

    if dial.prefetch_mode in ("inline", "both"):
        pieces.append("PRE-FETCHED CONTEXT:\n" + json.dumps(prefetch, indent=2))
    if dial.prefetch_mode in ("tool", "both"):
        pieces.append(
            "TOOL HINT: when you need a canonical UUID for an "
            "[[id:UUID][label]] cross-link, call find_canonical_id with the "
            "label. It searches the workdir's :ID:-bearing org files and "
            "returns matches with score. Use this to verify or look up IDs "
            "instead of guessing."
        )

    return "\n\n".join(pieces)


def select_tools(dial: DialConfig, task: Optional[dict] = None) -> list[dict]:
    """Tool surface per dial AND per-task gating (R16 L14).

    Per tool-use agent's R15 analysis:
      - list_dir was never called → drop entirely from all task surfaces
      - K7-qwen72b had 52% edit_file success rate from over-broad surface →
        gate elisp tools out of non-elisp tasks
      - APPEND-ONLY tasks (B11) should have write_file removed (per failure-
        mode agent: K1-qwen30 used write_file to overwrite 1643 lines)

    Per-task gating: if PER_TASK_ALLOWED_TOOLS has the task's id, intersect
    the dial-selected pool with that allow-list. Otherwise fall through.
    """
    pool = list(BROAD_TOOLS_FULL) if dial.tool_surface == "broad" else list(DEFAULT_TOOLS)
    if not task:
        return pool
    allowed = task.get("allowed_tools") or PER_TASK_ALLOWED_TOOLS.get(task["id"])
    if allowed is None:
        return pool
    return [t for t in pool if t["function"]["name"] in allowed]


# Per-task tool gating (R16 L14, from tool-use analysis agent).
# None = no override (use dial default). Otherwise: intersection.
PER_TASK_ALLOWED_TOOLS = {
    "B1":  {"read_file", "edit_file", "find_canonical_id", "validate_org"},
    "B5":  {"read_file", "edit_file", "grep", "validate_org"},
    "B7":  {"read_file", "edit_file", "grep", "run_pytest", "run_python"},
    # B11: APPEND-ONLY — write_file is REMOVED from surface (per R15 failure-mode)
    "B11": {"read_file", "edit_file", "find_canonical_id", "validate_org"},  # NO write_file
    "B13": {"read_file", "edit_file"},
    "B20": {"read_file", "edit_file", "validate_org"},
    "B25": {"read_file", "edit_file", "write_file", "eval_elisp", "load_elisp_file"},
    "B26": {"read_file", "edit_file", "load_elisp_file"},
    # New R16 tasks:
    "B30": {"read_file", "edit_file", "write_file", "run_pytest", "run_python", "grep"},
    "B40": {"read_file", "edit_file", "set_todo_state", "validate_org", "vault_query"},
}


# ── Elisp mastery — T1 primer + doom-conventions reference (R15 D11+) ────
ELISP_PRIMER = """\
ELISP PRIMER (read carefully — Emacs Lisp ≠ Common Lisp ≠ Scheme):

* Defining a function:
    (defun my-fn (x y) "Docstring." (+ x y))

* Defining a customizable variable (Doom / Emacs convention):
    (defcustom my-mode-foo 42
      "One-line docstring describing FOO."
      :type 'integer
      :group 'my-mode)
    Common :type values: 'string, 'integer, 'number, 'boolean, 'file,
    '(repeat string), '(choice (const :tag "Off" nil) (string :tag "Path")).

* Hooks:
    (add-hook 'after-save-hook #'my-fn)
    (remove-hook 'after-save-hook #'my-fn)

* Advice (modify existing functions):
    (advice-add 'foo :around #'my-around-advice)

* Org-mode parsing (preferred over regex when possible):
    (org-element-parse-buffer)        ; full AST
    (org-entry-properties)             ; props of entry at point
    (org-back-to-heading t)
    (org-element-property :title elem)

* Lisp gotchas FOSS models often get wrong:
  - Strings use double quotes only. Single quote = quote (not string).
  - There is NO Common-Lisp `if-let`, but `when-let` and `if-let*` exist.
  - `let` binds in parallel; `let*` binds sequentially.
  - `setq` not `set` for variable assignment.
  - Lists use `(list 1 2 3)` or `'(1 2 3)`. The quote prevents evaluation.
  - String concat: `(concat "a" "b")`. Format: `(format "x=%d" 42)`.
  - `print` adds quotes around strings. Use `princ` for raw output.

* Validation discipline (you have these tools — USE them):
  1. Write your elisp via edit_file/write_file.
  2. Call load_elisp_file to confirm it parses + loads cleanly.
  3. If your task involves a function call, also call eval_elisp with
     `(load \"path\")` then your invocation, to confirm runtime behavior.
  4. If it fails, READ the stderr carefully and fix. Do NOT guess.
"""


def doom_conventions_reference():
    """T1: Inline a similar existing .el file as the codebase-style reference."""
    sample = REPO / "doom/org-llm-specialist.el"
    try:
        text = sample.read_text()
    except FileNotFoundError:
        return ""
    head = "\n".join(text.splitlines()[:80])
    return ("DOOM CONVENTIONS REFERENCE — first ~80 lines of "
             f"{sample.relative_to(REPO)}; emulate this style "
             "(file header, lexical-binding, defgroup, defcustom shape, "
             "autoload markers):\n\n```elisp\n" + head + "\n```")


def task_needs_elisp_kit(task: dict) -> bool:
    return task["id"].startswith(("B25", "B26"))





def run_specialist_for_cell(wt_path, task, handle, plan_brief, prefetch,
                              specialist_model, dial: DialConfig, cell_dir,
                              variant_name: str = ""):
    persona = PERSONA_LOOKUP.get(handle, f"You are {handle}.")
    l1_primer = _extract_l1_for_handle(handle)
    instr_prefix = (l1_primer + "\n\n---\n\n") if l1_primer else ""
    instruction = render_specialist_brief(task, plan_brief, dial, prefetch,
                                            handle, instr_prefix)

    target_files = []
    if task.get("target_file"):
        target_files.append(wt_path / task["target_file"])

    # R18 S6 — Modal-Kimi route override for K2/K15 cells
    api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
    api_key_pass_slug = "org-llm/cloud/openrouter/api-key"
    if (os.environ.get("MODAL_KIMI_ENABLED") == "1"
          and variant_name in MODAL_KIMI_VARIANTS):
        api_endpoint = MODAL_KIMI_URL
        api_key_pass_slug = "org-llm/cloud/modal-kimi/bearer-key"

    # R18 S5 — constrained decoding for B11 DEC entries
    response_format = task.get("response_format")
    if task["id"] == "B11" and os.environ.get("R18_S5_CONSTRAINED") != "0":
        response_format = B11_RESPONSE_FORMAT

    # R26 P0-2 — variant max_tokens cap (R26 P5 dead-code fix). K2 family
    # produced 22-28k char prose at default 8000-token ceiling, hit 16
    # WALL_CAP_KILLED on long cells. Read _R25_VARIANT_MAX_TOKENS here so
    # the cap actually takes effect downstream in _chat_completions.
    variant_max_tokens = _R25_VARIANT_MAX_TOKENS.get(variant_name)

    spec_task = SpecialistTask(
        handle=handle,
        persona=persona,
        instruction=instruction,
        workdir=wt_path,
        model=specialist_model,
        target_files=target_files,
        max_iterations=PER_CELL_MAX_ITERS,    # R16: 8 → 16
        max_budget_usd=PER_CELL_BUDGET_USD,   # R16: $2 → $5
        scope_strict=dial.scope_strict,
        tools=select_tools(dial, task),       # R16 L14: per-task gating
        validate_after_edit=task.get("validate_after_edit", True),   # R16 L8 default ON
        inject_budget_status=task.get("inject_budget_status", True), # R16 L19 default ON
        provider_pin=PROVIDER_PINS.get(specialist_model),            # R17: provider pinning
        append_only=task.get("append_only", False),                  # R17 fix #1
        forbid_verbatim_in_labels=task.get("forbid_verbatim_in_labels", False),  # R17 fix #2
        forbid_stacked_docstrings=task.get("forbid_stacked_docstrings", False),  # R17 fix #5
        max_file_inline_chars=task.get("max_file_inline_chars"),     # R17 fix #3
        response_format=response_format,                              # R18 S5
        api_endpoint=api_endpoint,                                     # R18 S6 Modal-Kimi
        api_key_pass_slug=api_key_pass_slug,                           # R18 S6 Modal-Kimi
        max_tokens=variant_max_tokens,                                 # R26 P0-2 — K2 cap
    )

    # O1+O2: stream events to disk + main log as they fire
    prompt_dir = cell_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    safe_handle = handle.lstrip("@") or "unknown"
    (prompt_dir / f"{safe_handle}.txt").write_text(
        f"=== persona ===\n{persona}\n\n"
        f"=== model ===\n{specialist_model}\n\n"
        f"=== dial ===\n{json.dumps(asdict(dial), indent=2)}\n\n"
        f"=== target_files ===\n"
        + "\n".join(str(p) for p in target_files)
        + f"\n\n=== instruction ===\n{instruction}\n"
    )
    events_p = cell_dir / "events.jsonl"

    def cb(ev: dict) -> None:
        with events_p.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        if ev["event"] == "iter_end":
            log(f"      [{handle} iter {ev['iteration']}] "
                f"tools={ev.get('tool_calls', 0)} "
                f"cum=${ev.get('cum_cost', 0):.4f}",
                also_to_stdout=False)
        elif ev["event"] == "tool_call":
            log(f"      [{handle} iter {ev['iteration']}] "
                f"→ {ev.get('tool')}",
                also_to_stdout=False)
    spec_task._event_callback = cb

    result = run_specialist(spec_task)

    if result.edits_applied:
        subprocess.run(["git", "-C", str(wt_path), "add", "-A"],
                         capture_output=True, check=True)
        subprocess.run(["git", "-C", str(wt_path), "commit", "-m",
                          f"r20 {task['id']} {handle}: specialist edits"],
                         capture_output=True)
    return result


def safe_worktree_add(wt_name: str, wt_path, max_retries: int = 3):
    """R18+ fix — git worktree concurrency mutex with retry-on-collision.

    Mutex prevents concurrent `git worktree add` racing on the index lock
    (caught by R17 quality agent). Retry handles two further failure modes
    surfaced by R18 v2/v3 quality + walltime agents:
      1. Branch already exists from a prior aborted cell — `git branch -D`
         + retry.
      2. Worktree path already exists on disk — `git worktree remove
         --force` + rmtree + retry.

    On unrecoverable failure, raises the original CalledProcessError so
    the caller can record the cell as failed instead of all subsequent
    cells inheriting a poisoned worktree slot.
    """
    import shutil as _shutil
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    if worktree_lock is not None:
        worktree_lock.acquire()
    try:
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                subprocess.run(
                    ["git", "-C", str(REPO), "worktree", "add",
                     "-b", wt_name, str(wt_path), "trunk"],
                    check=True, capture_output=True,
                )
                return
            except subprocess.CalledProcessError as e:
                last_err = e
                stderr = (e.stderr or b"").decode("utf-8", errors="replace")

                # Best-effort cleanup of the two known collision modes
                # before retrying.
                if "already exists" in stderr or "is already checked out" in stderr or wt_path.exists():
                    subprocess.run(
                        ["git", "-C", str(REPO), "worktree", "remove",
                         "--force", str(wt_path)],
                        capture_output=True,
                    )
                    if wt_path.exists():
                        try:
                            _shutil.rmtree(wt_path, ignore_errors=True)
                        except Exception:
                            pass

                if "already exists" in stderr or "is not a valid branch" in stderr:
                    subprocess.run(
                        ["git", "-C", str(REPO), "branch", "-D", wt_name],
                        capture_output=True,
                    )

                # Stale `git worktree` admin cache for a removed path.
                subprocess.run(
                    ["git", "-C", str(REPO), "worktree", "prune"],
                    capture_output=True,
                )

                if attempt < max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise last_err
    finally:
        if worktree_lock is not None:
            worktree_lock.release()


def run_claude_solo_cell(task, dial, run_dir):
    wt_name = f"r26-{task['id']}-K5-claude-solo-{dial.label()}-{EPOCH}"
    wt_path = REPO.parent / "org-llm-worktrees" / wt_name
    safe_worktree_add(wt_name, wt_path)
    # R26 P1-15 — BK3 bug-injection (failing test + inverted
    # _check_append_only returns) on the fresh worktree, before any
    # prefetch / specialist work. No-op for tasks without the hook.
    maybe_apply_starting_state(task, wt_path)
    prefetch = get_prefetch(task) if task.get("prefetch") else {}

    if dial.brief_mode == "loose":
        prompt_body = (f"You are a software/wiki specialist working in "
                        f"worktree {wt_path}.\n\nTASK ({task['id']} — "
                        f"{task['label']}):\n{task['goal']}\n\n"
                        f"Decide on the right action and execute it.")
    else:
        prompt_body = (f"You are a software/wiki specialist working in "
                        f"worktree {wt_path}.\n\nTASK ({task['id']} — "
                        f"{task['label']}):\n{task['goal']}")

    prefetch_block = ""
    if dial.prefetch_mode in ("inline", "both"):
        prefetch_block = f"\n\nPRE-FETCHED CONTEXT:\n{json.dumps(prefetch, indent=2)}"

    prompt = (prompt_body + prefetch_block +
                "\n\nWhen done, run `git add` + `git commit`, then print "
                "TASK_DONE. If no-op, explain in one line + commit nothing "
                "+ print TASK_DONE.")

    (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (run_dir / "prompts" / "claude-solo.txt").write_text(prompt)

    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    out = run_dir / "stream.jsonl"
    cmd = ["claude", "-p", "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", f"{PER_CELL_BUDGET_USD:.2f}", prompt]
    t0 = time.time()
    with out.open("w") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env,
                              cwd=str(wt_path), timeout=PER_RUN_TIMEOUT).returncode
    elapsed = time.time() - t0
    cost = 0.0
    for line in out.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "result":
            cost += float(ev.get("total_cost_usd") or 0)
    diff_full, diff_stat = wt_diff_vs_base(wt_path)
    (run_dir / "diff.patch").write_text(diff_full)

    # R26 fix — BK1-BK5 fixtures use plural `target_files` (R17 task-dict
    # template); B1-B26 use singular `target_file`. Accept either; fall
    # back to []. Without this fix, all BK cells got primary=0 because
    # every edit landed in out-of-scope (R26 quality-judge agent finding).
    target_files = (
        list(task["target_files"]) if task.get("target_files")
        else ([task["target_file"]] if task.get("target_file") else [])
    )
    analysis = analyze_diff(diff_full, target_files, prefetch)

    cell = {"variant": "K5-claude-solo", "task_id": task['id'],
             "is_external_baseline": True, "cost_usd": round(cost, 4),
             "wall_seconds": round(elapsed, 1), "diff_stat": diff_stat,
             "wt_path": str(wt_path), "analysis": analysis,
             "prefetch": prefetch, "dial": asdict(dial),
             "dial_label": dial.label()}
    write_cell_result(run_dir, cell)
    return cell


def run_one_cell(task, variant_name, specialist_model_id, mode,
                  dial: DialConfig, run_dir):
    # R26 P2-4: per-cell wall_seconds for ALL variants. R25 P9 only set
    # wall_seconds in execute_cell's wrapper, AFTER write_cell_result had
    # already fired — so cell_result.json lacked wall_seconds for FOSS
    # cells (only K5-claude-solo had it via its own elapsed). Stamp here
    # at entry so every write below picks it up.
    # P1-21c re-apply: P1-7 commit (9fc4c2c) was branched off pre-P2-4
    # and reverted these three lines. Restored 2026-05-08 by harness
    # verification agent.
    _r26_t0 = time.time()
    log(f"  -- {variant_name} dial={dial.label()} ({mode})")
    if mode == "claude-solo":
        return run_claude_solo_cell(task, dial, run_dir)

    prefetch = get_prefetch(task)
    handle = None
    plan = None
    p1_cost = 0.0

    if dial.with_manager:
        n_rule_in_brief = (f"\nN-CHANGES RULE: make EXACTLY {task['n_changes']} change(s)."
                            if task.get("n_changes") is not None else "")
        phase1_prompt = (
            PICARD_PRIMER + "\n\n---\n\n"
            + f"TASK ({task['id']} — {task['label']}):\n{task['goal']}"
            + n_rule_in_brief
            + "\n\nPRE-FETCHED CONTEXT:\n" + json.dumps(prefetch, indent=2)
            + f"\n\nPRODUCTION CONSTRAINT: specialist runs on `{specialist_model_id}` "
            + "via `org_llm.specialist` (direct API; no opencode)."
            + "\n\n---\n\n" + PLAN_FORMAT
        )
        plan_text, p1_cost = call_openrouter(CAPTAIN_MODEL_ID, phase1_prompt)
        (run_dir / "phase1_raw.txt").write_text(plan_text)
        plan = parse_plan(plan_text)
        if plan is None:
            cell = {"variant": variant_name, "task_id": task['id'],
                     "phase1_cost": p1_cost, "error": "plan_parse_failed",
                     "dial": asdict(dial), "dial_label": dial.label()}
            cell["wall_seconds"] = round(time.time() - _r26_t0, 1)
            write_cell_result(run_dir, cell)
            return cell
        (run_dir / "phase1_plan.json").write_text(json.dumps(plan, indent=2))

    # NOTE: R15 uses `git worktree add` directly (sibling to repo) instead of
    # Agor REST. Agor caches trunk SHA at startup; if real trunk advances mid-
    # bench (it has — d07cd85), Agor's worktrees branch from the older SHA and
    # lack files committed since. Direct `git worktree add` always uses
    # fresh trunk. Same path Claude-solo cells already use.
    wt_name = f"r26-{task['id']}-{variant_name}-{dial.label()}-{EPOCH}"
    wt_path = REPO.parent / "org-llm-worktrees" / wt_name
    safe_worktree_add(wt_name, wt_path)   # R18 mutex fix
    # R26 P1-15 — BK3 bug-injection (failing test + inverted
    # _check_append_only returns) on the fresh worktree, after
    # `git worktree add` and before specialist phase 1 starts.
    # No-op for tasks without an `apply_starting_state` hook.
    maybe_apply_starting_state(task, wt_path)
    time.sleep(0.3)

    specialists = []
    total_spec_cost = 0.0

    if dial.with_manager and plan:
        team = plan.get("team", [])
    else:
        team = [{"handle": default_handle_for_task(task),
                  "task_brief": task["goal"]}]

    for spec in team:
        handle = spec.get("handle", "@unknown")
        brief = spec.get("task_brief", "")
        if not brief: continue
        result = run_specialist_for_cell(wt_path, task, handle, brief,
                                            prefetch, specialist_model_id,
                                            dial, run_dir,
                                            variant_name=variant_name)
        total_spec_cost += result.cost_usd
        specialists.append({
            "handle": handle,
            "success": result.success,
            "iterations": result.iterations,
            "edits_applied": result.edits_applied,
            "edits_applied_count": len(result.edits_applied),
            "cost_usd": result.cost_usd,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
            "text_output": result.text_output or "",
            "tool_use_breakdown": result.tool_use_breakdown,
        })

    diff_full, diff_stat = wt_diff_vs_base(wt_path)
    (run_dir / "diff.patch").write_text(diff_full)

    # R26 fix — BK1-BK5 fixtures use plural `target_files` (R17 task-dict
    # template); B1-B26 use singular `target_file`. Accept either; fall
    # back to []. Without this fix, all BK cells got primary=0 because
    # every edit landed in out-of-scope (R26 quality-judge agent finding).
    target_files = (
        list(task["target_files"]) if task.get("target_files")
        else ([task["target_file"]] if task.get("target_file") else [])
    )
    analysis = analyze_diff(diff_full, target_files, prefetch)

    cell = {"variant": variant_name, "task_id": task['id'],
             "specialist_model": specialist_model_id,
             "phase1_cost_usd": round(p1_cost, 6),
             "specialist_cost_usd": round(total_spec_cost, 6),
             "plan_playbook": (plan or {}).get("playbook"),
             "plan_classification": (plan or {}).get("classification"),
             "specialists": specialists, "diff_stat": diff_stat,
             "wt_path": str(wt_path), "analysis": analysis,
             "prefetch": prefetch, "dial": asdict(dial),
             "dial_label": dial.label()}
    cell["wall_seconds"] = round(time.time() - _r26_t0, 1)
    write_cell_result(run_dir, cell)
    return cell


# ── Scoring (D10) ────────────────────────────────────────────────────────
def score_cell(cell):
    """Return a per-cell composite score for ranking.

    Higher = better. Components:
    - on_candidate count for B1
    - in_scope_changes count (insertions+deletions, capped) for others
    - penalize id_fabrication
    - penalize out_of_scope changes
    - silent_noop = 0
    """
    if cell.get("error") and not cell.get("analysis"):
        return {"score": 0.0, "reason": "error"}
    an = cell.get("analysis") or {}
    in_s = an.get("diff_in_scope", {})
    out_s = an.get("diff_out_of_scope", {})
    cats = an.get("wrap_categories", {})
    in_total = in_s.get("insertions", 0) + in_s.get("deletions", 0)
    out_total = out_s.get("insertions", 0) + out_s.get("deletions", 0)
    fab = an.get("id_fabrication_count", 0)

    # silent_noop
    specs = cell.get("specialists") or []
    if specs and all((s.get("error") or "").startswith("silent_noop") for s in specs):
        return {"score": 0.0, "reason": "silent_noop"}

    if cell.get("task_id") == "B1":
        primary = cats.get("on_candidate", 0) * 2 + cats.get("creative", 0)
    else:
        primary = min(in_total, 50)
    score = primary - fab * 3 - min(out_total, 100) * 0.05
    # R19_WIRING: quality_lint penalty (N5)
    lint_pen = 0.0
    lint = {}
    if _R19_LINT_AVAILABLE:
        diff_text = (cell.get("analysis") or {}).get("diff_text") or ""
        if diff_text:
            try:
                lint = _r19_lint_patch(diff_text)
                lint_pen = lint.get("score_penalty", 0.0)
            except Exception:
                lint = {}
    score = score - lint_pen
    # R26 G1 — cell quality floor: cells with severe lint stack OR
    # negative primary are flagged for leaderboard exclusion (still
    # logged for forensics). PM3-G in 2026-05-08-bench-arc-post-mortems.
    floor_dropped = (lint_pen > 8.0) or (primary < 0)
    return {"score": round(score, 2), "primary": primary,
             "in_total": in_total, "out_total": out_total,
             "fab": fab, "lint_penalty": lint_pen,
             "lint": lint, "floor_dropped": floor_dropped}


def cost_per_unit(cell):
    cost = (cell.get("cost_usd")
              or (cell.get("phase1_cost_usd", 0)
                   + cell.get("specialist_cost_usd", 0)))
    s = score_cell(cell)
    primary = s.get("primary", 0)
    if primary <= 0: return None
    return round(cost / primary, 5)


def update_live_state(all_cells: list[dict]):
    """O6: rewrite R26_LIVE.json after each cell."""
    leaderboard: dict = {}
    for c in all_cells:
        v = c.get("variant", "?")
        s = score_cell(c)
        cost = (c.get("cost_usd")
                  or (c.get("phase1_cost_usd", 0)
                       + c.get("specialist_cost_usd", 0)))
        rec = leaderboard.setdefault(v, {"variant": v, "cells": 0,
                                            "total_score": 0.0,
                                            "total_cost": 0.0,
                                            "primary_total": 0,
                                            "fab_total": 0,
                                            "silent_noops": 0})
        rec["cells"] += 1
        rec["total_score"] += s.get("score", 0.0)
        rec["total_cost"] += cost
        rec["primary_total"] += s.get("primary", 0)
        rec["fab_total"] += s.get("fab", 0)
        if s.get("reason") == "silent_noop":
            rec["silent_noops"] += 1
    for rec in leaderboard.values():
        rec["cost_per_unit"] = (
            round(rec["total_cost"] / rec["primary_total"], 5)
            if rec["primary_total"] > 0 else None
        )
        rec["total_cost"] = round(rec["total_cost"], 4)
        rec["total_score"] = round(rec["total_score"], 2)
    LIVE.write_text(json.dumps({
        "epoch": EPOCH,
        "cells_done": len(all_cells),
        "leaderboard": sorted(leaderboard.values(),
                                 key=lambda r: -r["total_score"]),
    }, indent=2))


def print_leaderboard(all_cells: list[dict]):
    """O4: tail-able leaderboard appended to log every 5 cells."""
    leaderboard: dict = {}
    for c in all_cells:
        v = c.get("variant", "?")
        s = score_cell(c)
        cost = (c.get("cost_usd")
                  or (c.get("phase1_cost_usd", 0)
                       + c.get("specialist_cost_usd", 0)))
        rec = leaderboard.setdefault(v, {"cells": 0, "score": 0.0,
                                            "cost": 0.0, "primary": 0,
                                            "noops": 0})
        rec["cells"] += 1
        rec["score"] += s.get("score", 0.0)
        rec["cost"] += cost
        rec["primary"] += s.get("primary", 0)
        if s.get("reason") == "silent_noop": rec["noops"] += 1
    log("=" * 60)
    log(f"LEADERBOARD ({len(all_cells)} cells done)")
    rows = sorted(leaderboard.items(), key=lambda kv: -kv[1]["score"])
    for v, r in rows:
        cpu = (f"${r['cost']/r['primary']:.4f}/u" if r['primary'] > 0
                else "  no-units  ")
        log(f"  {v:>16}  cells={r['cells']}  score={r['score']:>5.1f}  "
            f"cost=${r['cost']:.3f}  {cpu}  noops={r['noops']}")
    log("=" * 60)


# ── Main ────────────────────────────────────────────────────────────────
PARALLELISM = int(os.environ.get("R18_PARALLELISM", "16"))   # all-night R18 doubles 8 → 16
WALL_CAP_PER_CELL_S = 400  # R24_SYNTHESIS: per-cell wall cap

# R19_WIRING: quality_lint
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from quality_lint import lint_patch as _r19_lint_patch
    _R19_LINT_AVAILABLE = True
except Exception as _e:
    print(f"[R19] quality_lint import failed: {_e}")
    _R19_LINT_AVAILABLE = False

state_lock = None   # initialized in main()
worktree_lock = None   # R18 fix — git worktree race fix from R17 quality agent
ALL_CELLS: list[dict] = []   # shared across composition strategies


# ── R18 specialty routing (S4 Tier-S) ────────────────────────────────────
# Per task family, which variants to run. Per-task tool gating already
# narrows surface; this narrows VARIANTS too. Drives per-task cell
# distribution.
TASK_FAMILY_VARIANTS = {
    "wiki_edit":    ["K1-qwen30", "K8-deepseekV3"],          # B1, B5, B20
    "code_edit":    ["K8-deepseekV3", "K11-qwen3coder"],     # B7, BK1-BK5
    "elisp":        ["K2-kimi-k2.6"],                          # B25, B26
    "dec_draft":    ["K11-qwen3coder", "K2-kimi-k2.6", "K5-claude-solo"],  # B11
    "small_atomic": ["K1-qwen30", "K8-deepseekV3"],          # B13
}

TASK_TO_FAMILY = {
    "B1": "wiki_edit", "B5": "wiki_edit", "B20": "wiki_edit",
    "B7": "code_edit",
    "B11": "dec_draft",
    "B13": "small_atomic",
    "B25": "elisp", "B26": "elisp",
}


# ── R18 hedged-strong execution (S1 Tier-S) ──────────────────────────────
# Run cheap variant first; if quality < threshold, escalate to premium.
HEDGED_QUALITY_THRESHOLD = 12   # primary score; below = escalate
HEDGED_ESCALATIONS = {
    "K8-deepseekV3":  "K11-qwen3coder",   # cheap → premium
    "K1-qwen30":      "K8-deepseekV3",     # super-cheap → cheap
}


# ── R18 best-of-N voting (S2 Tier-S) ────────────────────────────────────
# Run N samples of same (variant, task, dial); keep highest-scoring cell.
# Used for prose tasks where quality matters most.
BEST_OF_N_TASKS = {"B5", "B11", "B20", "B45"}
BEST_OF_N_VARIANT = "K8-deepseekV3"
BEST_OF_N_SAMPLES = 5


# ── R18 critique-then-revise (S3 Tier-S) ────────────────────────────────
# K1 drafts → K2 critiques → K1 revises with critique. Two-cheap-models
# pipeline that should match K11's premium output for prose tasks.
CRITIQUE_REVISE_TASKS = {"B11", "B5"}


# ── R18 constrained decoding (S5 Tier-S) ────────────────────────────────
# JSON schema for B11 DEC entries. Harness deterministically renders JSON
# to org. Format errors → 0; closes K2's missing-Expanded gap.
B11_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "dec_entry",
        "schema": {
            "type": "object",
            "required": ["dec_number", "title", "status", "summary",
                         "expanded", "context", "options", "tradeoffs",
                         "decision", "rationale"],
            "properties": {
                "dec_number":   {"type": "string", "pattern": "^DEC-[0-9]{3}$"},
                "title":        {"type": "string", "minLength": 5, "maxLength": 80},
                "status":       {"type": "string",
                                  "enum": ["CANDIDATE", "ACCEPTED", "DEPRECATED"]},
                "summary":      {"type": "string", "minLength": 50, "maxLength": 400},
                "expanded":     {"type": "string", "minLength": 100, "maxLength": 1500},
                "context":      {"type": "array", "items": {"type": "string"},
                                  "minItems": 3, "maxItems": 6},
                "options":      {"type": "array",
                                  "items": {
                                    "type": "object",
                                    "required": ["name", "summary"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "summary": {"type": "string", "minLength": 30}
                                    }
                                  },
                                  "minItems": 2, "maxItems": 5},
                "tradeoffs":    {"type": "string", "minLength": 100},
                "decision":     {"type": "string", "minLength": 5},
                "rationale":    {"type": "string", "minLength": 100},
            },
        },
    },
}


# ── R18 adaptive abort triggers (Tier-S infra) ───────────────────────────
# R17's silent-streak abort missed K7-thrash + K11-thrash. R18 adds:
#   - failed-edit-streak: 3 consecutive iters with all edit_file calls
#     returning failure → cut. Catches K11/B11 thrash.
#   - per-cell hard wall cap by class.
WALL_CAP_BY_CLASS = {
    "thinking":  240,   # K15-kimi-thinking, K3-deepseek-r1
    "frontier":  180,   # K11-qwen3coder, K12-llama405b, K13-deepseek-v4
    "efficient": 90,    # K1, K8
}
WALL_CAP_DEFAULT = 120
WALL_CAP_ABSOLUTE = 300

THINKING_VARIANTS = {"K15-kimi-thinking"}
FRONTIER_VARIANTS = {"K11-qwen3coder", "K12-llama405b", "K13-deepseekV4"}


# ── R26 P1-7: S1-S4 composition strategy wiring ────────────────────────
# The four constants above (HEDGED_ESCALATIONS, BEST_OF_N_TASKS,
# CRITIQUE_REVISE_TASKS, TASK_FAMILY_VARIANTS) shipped in R18 but
# nothing read them. The four helpers below are the runtime hooks.

def _resolve_variant_tuple(name, variants_pool=None):
    """Look up the (name, model_id, mode) tuple for a given variant name."""
    pool = variants_pool if variants_pool is not None else VARIANTS
    for v in pool:
        if v[0] == name:
            return v
    return None


# S1 hedged-strong escalation ──────────────────────────────────────────
def _should_escalate(cell):
    """Return the escalation target variant-name if the cell's variant is
    in HEDGED_ESCALATIONS AND its primary score is below threshold; else
    None.

    Acceptance probe (no API): synthesize a cell dict with primary < 12
    and variant in HEDGED_ESCALATIONS; assert this returns the mapped
    fallback name."""
    name = cell.get("variant")
    if not name or name not in HEDGED_ESCALATIONS:
        return None
    s = score_cell(cell) if (cell.get("analysis") or cell.get("error")
                              or cell.get("specialists")) else {}
    primary = s.get("primary", 0)
    if primary >= HEDGED_QUALITY_THRESHOLD:
        return None
    return HEDGED_ESCALATIONS[name]


def _build_escalation_spec(parent_spec, fallback_name, variants_pool=None):
    """Given a cell-spec tuple and a fallback variant-name, build a new
    cell-spec tuple for the fallback variant. Returns None if the
    fallback variant isn't in the active pool."""
    task, _variant, dial, layer_label = parent_spec
    fallback = _resolve_variant_tuple(fallback_name, variants_pool)
    if fallback is None:
        return None
    return (task, fallback, dial, f"{layer_label}_hedged")


# S2 best-of-N voting ──────────────────────────────────────────────────
def _expand_best_of_n(cell_specs):
    """Multiply specs for (task in BEST_OF_N_TASKS, variant=BEST_OF_N_VARIANT)
    by BEST_OF_N_SAMPLES. The execute_cell suffix logic (=__s1, __s2, ...=)
    already disambiguates artifact directories.

    Acceptance probe (no API): build cell_specs for B5 with a
    BEST_OF_N_VARIANT cell; assert resulting list has BEST_OF_N_SAMPLES
    copies (same task+variant+dial, different artifact suffixes via
    execute_cell)."""
    expanded = []
    for spec in cell_specs:
        task, variant, _dial, _layer = spec
        tid = task.get("id") if isinstance(task, dict) else None
        vname = variant[0]
        if tid in BEST_OF_N_TASKS and vname == BEST_OF_N_VARIANT:
            for _ in range(BEST_OF_N_SAMPLES):
                expanded.append(spec)
        else:
            expanded.append(spec)
    return expanded


# S3 critique-revise ──────────────────────────────────────────────────
def _build_critique_prompt(task, draft_diff_text):
    """Brief for the critic (K2). Reads the K1 draft's diff and produces
    short, specific feedback the reviser can act on."""
    return (
        "You are a senior reviewer. A drafter just produced the patch below "
        f"for task {task['id']} ({task.get('label','')}).\n\n"
        f"TASK GOAL:\n{task.get('goal','')}\n\n"
        "DRAFT PATCH:\n```\n"
        + (draft_diff_text or "(empty)") + "\n```\n\n"
        "Write 3-6 bullet points of specific, actionable critique. Focus on:\n"
        "- Missing required sections (Summary./Expanded.)\n"
        "- ID fabrication or out-of-scope edits\n"
        "- Bracket / link / table syntax problems\n"
        "- Anything that would lower a quality_lint score\n"
        "Do NOT rewrite the patch; only critique it.\n"
    )


def _build_revise_brief(original_brief, critique_text):
    """The second K1 cell receives the original brief plus the critic's
    feedback, prefixed with the load-bearing string the acceptance probe
    asserts on."""
    return (
        original_brief.rstrip()
        + "\n\nReviewer feedback: "
        + (critique_text or "").strip()
        + "\n\nApply this feedback to the patch you produce.\n"
    )


def run_layer_critique_revise(advancing, all_cells, variants_pool=None):
    """S3 layer — for each task in CRITIQUE_REVISE_TASKS where K1 + K2 are
    both in =advancing=, run K1-draft → K2-critique-via-call_openrouter →
    K1-revise (with critic feedback appended to the brief)."""
    pool = variants_pool if variants_pool is not None else VARIANTS
    advancing_names = {v[0] for v in advancing}
    if not ({"K1-qwen30", "K2-kimi-k2.6"} <= advancing_names):
        log("S3 layer_critique_revise — skipped (need both K1 + K2 in advancing)")
        return []
    k1 = _resolve_variant_tuple("K1-qwen30", pool)
    k2 = _resolve_variant_tuple("K2-kimi-k2.6", pool)
    if k1 is None or k2 is None:
        log("S3 layer_critique_revise — skipped (K1 or K2 not in active VARIANTS)")
        return []
    revise_cells: list[dict] = []
    for task in TASKS:
        if task.get("id") not in CRITIQUE_REVISE_TASKS:
            continue
        log(f"S3 critique-revise on {task['id']} (K1 → K2 critic → K1 revise)")
        # 1. K1 draft
        draft_cell = execute_cell(task, k1, BEST_CONFIG, "layer_cr_draft")
        all_cells.append(draft_cell)
        diff_text = (draft_cell.get("analysis") or {}).get("diff_text") or ""
        # 2. K2 critique via call_openrouter (prose, not a tool-using cell)
        critique_text = ""
        try:
            critique_text, _cost = call_openrouter(
                k2[1], _build_critique_prompt(task, diff_text))
        except Exception as exc:
            log(f"  S3 critic call failed on {task['id']}: {exc}")
            critique_text = ""
        # 3. K1 revise with critique appended to the brief
        original_brief = task.get("goal", "")
        revised_task = dict(task)
        revised_task["goal"] = _build_revise_brief(original_brief, critique_text)
        revise_cell = execute_cell(revised_task, k1, BEST_CONFIG, "layer_cr_revise")
        revise_cell["s3_critique_text"] = critique_text
        revise_cells.append(revise_cell)
        all_cells.append(revise_cell)
    return revise_cells


# S4 specialty routing ─────────────────────────────────────────────────
def _filter_specs_by_family(cell_specs):
    """For each spec, if TASK_TO_FAMILY has the task AND TASK_FAMILY_VARIANTS
    has that family, keep the spec only when the variant name is in the
    family's allowed list. Specs whose task has no family entry pass
    through unchanged.

    Acceptance probe (no API): Layer-2 cell_specs for B25 (family=elisp)
    with a wiki_edit-only variant (e.g. K1-qwen30) get filtered out;
    only K2-kimi-k2.6 (the elisp-family-tagged variant) survives."""
    kept = []
    for spec in cell_specs:
        task, variant, _dial, _layer = spec
        tid = task.get("id") if isinstance(task, dict) else None
        family = TASK_TO_FAMILY.get(tid)
        if family is None:
            kept.append(spec)
            continue
        allowed = TASK_FAMILY_VARIANTS.get(family)
        if allowed is None:
            kept.append(spec)
            continue
        if variant[0] in allowed:
            kept.append(spec)
        # else: filter out
    return kept


def execute_cell(task, variant, dial, layer_label):
    _r25_t0_exec = time.time()  # R25_DESIGN: wall_seconds instrumentation
    name, model_id, mode = variant
    base = ARTIFACTS / layer_label / task['id'] / f"{name}__{dial.label()}"
    # R17 — n=3 promotion: when same (variant, dial) runs multiple times
    # (e.g. K8-V3 stability stratum), append a sample suffix.
    cell_dir = base
    suffix = 1
    while cell_dir.exists() and any(cell_dir.iterdir()):
        suffix += 1
        cell_dir = base.with_name(f"{base.name}__s{suffix}")
    cell_dir.mkdir(parents=True, exist_ok=True)
    # R26 P0-3 — enforce WALL_CAP at runtime via TPE-without-context-manager.
    # The R26 version used `with TPE(max_workers=1) as _inner` which
    # called shutdown(wait=True) on context-exit, blocking the OUTER
    # worker until the inner thread finished. Cells got the
    # `wall_cap_killed:400s` LABEL but `wall_seconds` actually ran
    # 455-1697s (R26 walltime agent §2). The cap was a label, not a kill.
    #
    # Fix: drop the `with` so shutdown(wait=False) lets the outer worker
    # return immediately. The inner thread keeps running in background
    # until natural completion (Python can't kill threads cleanly), but
    # the outer pool worker is FREED — parallelism keeps flowing.
    #
    # Acceptance probe: spawn synthetic 500s cell; assert outer worker
    # exits within 410s. (Inner thread leak is bounded; tracked in P2-?
    # for future multiprocessing.Process replacement when we're willing
    # to handle pickling all task/variant/dial/cell_dir args.)
    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _FTO
    _wall_cap = globals().get("WALL_CAP_PER_CELL_S", 600)
    _inner = _TPE(max_workers=1)
    try:
        _fut = _inner.submit(run_one_cell, task, name, model_id, mode,
                              dial, cell_dir)
        cell = _fut.result(timeout=_wall_cap)
        _inner.shutdown(wait=False)
    except _FTO:
        log(f"      ! {name} WALL_CAP_KILLED on {task['id']} (>{_wall_cap}s)")
        # Don't wait for inner thread; let it leak. Outer worker freed.
        _inner.shutdown(wait=False)
        cell = {"variant": name, "task_id": task['id'],
                  "error": f"wall_cap_killed:{_wall_cap}s",
                  "wall_cap_killed": True,
                  "dial": asdict(dial),
                  "dial_label": dial.label()}
        write_cell_result(cell_dir, cell)
    except Exception as exc:
        _inner.shutdown(wait=False)
        log(f"      ! {name} ERROR: {exc}")
        cell = {"variant": name, "task_id": task['id'],
                  "error": str(exc), "dial": asdict(dial),
                  "dial_label": dial.label()}
        write_cell_result(cell_dir, cell)
    # R25_DESIGN: instrument wall_seconds for all variants
    if "wall_seconds" not in cell:
        cell["wall_seconds"] = round(time.time() - _r25_t0_exec, 1) if "_r25_t0_exec" in dir() else None
    cell["layer"] = layer_label
    return cell


# ── R26 P1-11/P1-12/P1-13 — daemon ↔ harness file contracts ─────────────
def _primary_broker_for_variant(variant):
    """Return the primary broker name for (variant_name, model_id, mode).

    Used by P1-12 deny-list filtering and P1-13 cell_replay.jsonl. The
    primary preferred broker (first of PROVIDER_PINS["order"]) is the
    deny-list key; we don't yet know what OpenRouter actually routed
    to at submission time. Falls back to "openrouter" if no pin
    exists.
    """
    name, model_id, mode = variant
    if mode == "claude-solo":
        return "anthropic"
    pin = PROVIDER_PINS.get(model_id) if model_id else None
    if not pin:
        return "openrouter"
    order = pin.get("order") or []
    return order[0] if order else "openrouter"


def _check_cost_circuit_breaker(layer_name):
    """P1-11. If $ARTIFACTS/COST_CIRCUIT_BREAKER exists, abort gracefully.

    Writes a layer-skipped marker, logs the reason, and raises
    SystemExit(0). Caller is run_layer_parallel; aborting via
    SystemExit drops out of main() cleanly and lets atexit hooks fire
    (e.g. K20 endpoint pause).
    """
    if not COST_CIRCUIT_BREAKER_FILE.exists():
        return
    try:
        reason = COST_CIRCUIT_BREAKER_FILE.read_text().strip()
    except Exception:
        reason = "(unreadable)"
    log("=" * 80)
    log(f"[P1-11] COST_CIRCUIT_BREAKER present — aborting {layer_name!r}")
    log(f"[P1-11] sentinel content: {reason[:200]}")
    log("=" * 80)
    safe_layer = re.sub(r"[^A-Za-z0-9_]+", "_", layer_name)[:60]
    marker = ARTIFACTS / f"LAYER_SKIPPED_{safe_layer}.json"
    try:
        marker.write_text(json.dumps({
            "layer_name": layer_name,
            "reason": "cost_circuit_breaker",
            "sentinel": reason,
            "epoch": int(time.time()),
        }, indent=2))
    except Exception as exc:
        log(f"[P1-11] could not write layer-skipped marker: {exc}")
    raise SystemExit(0)


def _load_live_deny_list():
    """P1-12. Parse $ARTIFACTS/LIVE_DENY_LIST as JSONL; return set of
    (variant, broker) tuples to filter at cell-spec construction time.

    File format: each line is a JSON object
      {"variant": "K11-...", "broker": "Together", "reason": "..."}
    Tolerant of: missing file, blank lines, malformed lines (skipped
    with a warning log). A wildcard broker "*" matches any broker for
    that variant. Also accepts a single JSON array (compat).
    """
    if not LIVE_DENY_LIST_FILE.exists():
        return set()
    denied = set()
    try:
        text = LIVE_DENY_LIST_FILE.read_text()
    except Exception as exc:
        log(f"[P1-12] could not read LIVE_DENY_LIST: {exc}")
        return set()
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            arr = json.loads(stripped)
            for entry in arr:
                v = entry.get("variant"); b = entry.get("broker")
                if v and b:
                    denied.add((v, b))
        except Exception as exc:
            log(f"[P1-12] LIVE_DENY_LIST array parse failed: {exc}")
        return denied
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception as exc:
            log(f"[P1-12] LIVE_DENY_LIST line {i} skipped: {exc}")
            continue
        v = entry.get("variant"); b = entry.get("broker")
        if v and b:
            denied.add((v, b))
    return denied


def _filter_cell_specs_via_deny_list(cell_specs):
    """P1-12. Drop cell_specs whose (variant, broker) is denied.

    Wildcard broker "*" denies all brokers for that variant. Returns
    (kept_specs, dropped_specs).
    """
    denied = _load_live_deny_list()
    if not denied:
        return cell_specs, []
    kept = []
    dropped = []
    for s in cell_specs:
        _task, variant, _dial, _layer = s
        v_name = variant[0]
        broker = _primary_broker_for_variant(variant)
        if (v_name, broker) in denied or (v_name, "*") in denied:
            dropped.append((s, broker))
        else:
            kept.append(s)
    return kept, dropped


def _classify_cell_status(cell):
    """P1-13. Map a cell-result dict to a coarse status enum.

    Returns one of: ok | wall_cap | silent_noop | broker_error | exception
    """
    if cell.get("wall_cap_killed"):
        return "wall_cap"
    err = cell.get("error") or ""
    if err:
        if "silent_noop" in err:
            return "silent_noop"
        if "wall_cap" in err:
            return "wall_cap"
        if "HTTP" in err or "URLError" in err or "broker" in err.lower():
            return "broker_error"
        return "exception"
    # Inspect specialists for silent_noop streaks
    specs = cell.get("specialists") or []
    if specs and all((s.get("error") or "").startswith("silent_noop") for s in specs):
        return "silent_noop"
    return "ok"


def _append_cell_replay(cell, layer_label):
    """P1-13. Append one JSONL line to cell_replay.jsonl per cell
    completion. Schema:
      task_id, variant_name, dial_label, layer, status, wall_seconds,
      cost_usd, score, broker

    Status from _classify_cell_status. Self-healing daemon reads on
    round-crash to identify which cells didn't finish; relaunches
    only those.
    """
    try:
        s = score_cell(cell)
    except Exception:
        s = {"score": 0.0}
    cost = (cell.get("cost_usd")
              or (cell.get("phase1_cost_usd", 0)
                   + cell.get("specialist_cost_usd", 0)))
    v_name = cell.get("variant", "?")
    broker = "openrouter"
    for v in VARIANTS:
        if v[0] == v_name:
            broker = _primary_broker_for_variant(v)
            break
    entry = {
        "task_id": cell.get("task_id", "?"),
        "variant_name": v_name,
        "dial_label": cell.get("dial_label", ""),
        "layer": cell.get("layer", layer_label),
        "status": _classify_cell_status(cell),
        "wall_seconds": cell.get("wall_seconds"),
        "cost_usd": round(float(cost or 0.0), 6),
        "score": s.get("score", 0.0),
        "broker": broker,
        "epoch": int(time.time()),
    }
    try:
        with CELL_REPLAY_FILE.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        log(f"[P1-13] cell_replay append failed: {exc}")


def _completed_cells_from_replay():
    """P1-13/P1-20 helper. Read cell_replay.jsonl + return a set of
    (task_id, variant_name, dial_label, layer) tuples for cells whose
    last status is "ok". Used by the self-healing daemon's resumption
    path (and by harness when relaunching from a previous crash).
    """
    if not CELL_REPLAY_FILE.exists():
        return set()
    completed = set()
    try:
        text = CELL_REPLAY_FILE.read_text()
    except Exception:
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("status") == "ok":
            completed.add((
                entry.get("task_id"),
                entry.get("variant_name"),
                entry.get("dial_label"),
                entry.get("layer"),
            ))
    return completed


def run_layer_parallel(layer_name, cell_specs, all_cells):
    """R17 — execute a layer's cells in a thread pool.

    cell_specs: list of (task, variant, dial, layer_label) tuples.
    all_cells: shared mutable list for state across layers.
    Returns the layer's results.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    log("=" * 80)
    log(f"{layer_name} — {len(cell_specs)} cells, {PARALLELISM}-way parallel")
    log("=" * 80)
    # R26 P1-11 — daemon-authored COST_CIRCUIT_BREAKER sentinel. If
    # present, abort the layer (and the round) gracefully. SystemExit(0)
    # lets atexit hooks (K20 pause) run.
    _check_cost_circuit_breaker(layer_name)
    # R26 P1-12 — daemon-authored LIVE_DENY_LIST. Filter (variant,
    # broker) combos out before submission so denied cells never start.
    cell_specs, deny_dropped = _filter_cell_specs_via_deny_list(cell_specs)
    if deny_dropped:
        sample = ", ".join(f"{s[1][0]}/{br}" for (s, br) in deny_dropped[:5])
        log(f"  [P1-12] LIVE_DENY_LIST dropped {len(deny_dropped)} cells "
            f"(sample: {sample})")
    # R26 P1-6 — honor _R25_VARIANT_BUDGETS at submit time. K11 ($1.50
    # cap), K33/K34/K35 caps were declared in R26 prep but the filter
    # function _r25_should_skip_for_budget was never called. Apply it
    # NOW so K11 cells stop accruing once variant cumulative spend
    # exceeds cap (R24 K11 spent $4.39 across 18 cells; cap should
    # have stopped at ~6 cells).
    layer_results: list[dict] = []
    skipped_for_budget = []
    filtered_specs = []
    for s in cell_specs:
        if _r25_should_skip_for_budget(s, all_cells):
            skipped_for_budget.append(s)
        else:
            filtered_specs.append(s)
    if skipped_for_budget:
        log(f"  R26-budget: skipped {len(skipped_for_budget)} cells over variant cap "
            f"({sorted({s[1][0] for s in skipped_for_budget})})")
    cell_specs = filtered_specs
    # R26 P1-7 (S1): collect hedged-escalation specs from low-primary
    # cells finishing in this layer, then run them as a small fallback
    # batch after the main pool drains. Bounded depth: a fallback cell
    # that ALSO scores low does not re-escalate (no recursion).
    escalation_specs: list = []
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = {}
        for s in cell_specs:
            # R26 P1-11 — re-check on every submit so a mid-layer trip
            # halts further work even after the layer started.
            _check_cost_circuit_breaker(layer_name)
            futures[pool.submit(execute_cell, *s)] = s
        for f in as_completed(futures):
            spec = futures[f]
            try:
                cell = f.result()
            except Exception as exc:
                _, variant, dial, layer_label = spec
                log(f"  ! pool worker died on {variant[0]}: {exc}")
                # R26 P1-13 — record exception cells so the daemon can
                # see what didn't complete cleanly.
                try:
                    _append_cell_replay({
                        "variant": variant[0],
                        "task_id": spec[0].get("id", "?"),
                        "dial_label": dial.label() if hasattr(dial, "label") else "",
                        "error": f"pool_worker_exception:{exc}",
                        "layer": layer_label,
                    }, layer_label)
                except Exception:
                    pass
                continue
            with state_lock:
                layer_results.append(cell)
                all_cells.append(cell)
                s = score_cell(cell)
                prog = PROGRESS.cell_done(cell)
                log(f"    {cell.get('variant','?'):>16} score={s.get('score',0):.1f} "
                    f"primary={s.get('primary',0)} fab={s.get('fab',0)} {prog}")
                update_live_state(all_cells)
                # R26 P1-13 — append per-cell replay line for crash recovery.
                _append_cell_replay(cell, spec[3] if len(spec) > 3 else layer_name)
                if len(all_cells) % 5 == 0:
                    print_leaderboard(all_cells)
                # S1 hedged-strong escalation
                fallback_name = _should_escalate(cell)
                if fallback_name:
                    esc_spec = _build_escalation_spec(spec, fallback_name)
                    if esc_spec is not None:
                        escalation_specs.append(esc_spec)
                        log(f"    S1 escalating {cell.get('variant')} → "
                            f"{fallback_name} on {spec[0].get('id','?')} "
                            f"(primary={s.get('primary',0)} < {HEDGED_QUALITY_THRESHOLD})")
    # S1: run escalation fallbacks as a sub-batch (no further escalation)
    if escalation_specs:
        log(f"  S1 hedged-strong: running {len(escalation_specs)} fallback cells")
        with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
            efutures = {pool.submit(execute_cell, *s): s for s in escalation_specs}
            for f in as_completed(efutures):
                spec = efutures[f]
                try:
                    cell = f.result()
                except Exception as exc:
                    _, variant, _, _ = spec
                    log(f"  ! S1 fallback worker died on {variant[0]}: {exc}")
                    continue
                with state_lock:
                    cell["hedged_escalation"] = True
                    layer_results.append(cell)
                    all_cells.append(cell)
                    s = score_cell(cell)
                    prog = PROGRESS.cell_done(cell)
                    log(f"    [S1] {cell.get('variant','?'):>16} "
                        f"score={s.get('score',0):.1f} "
                        f"primary={s.get('primary',0)} {prog}")
                    update_live_state(all_cells)
                    # R26 P1-13 — append S1 fallback cells to replay too
                    _append_cell_replay(cell, spec[3] if len(spec) > 3 else layer_name)
    return layer_results


def main():
    global PROGRESS, state_lock, worktree_lock
    import threading
    state_lock = threading.Lock()
    worktree_lock = threading.Lock()   # R18 — fix git worktree concurrency race
    log(f"Round-26 — overnight scope + maximalist strategies + Modal-Kimi")
    log(f"  trunk HEAD: " + subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "trunk"],
        capture_output=True, text=True).stdout.strip())
    log(f"  variants: {len(VARIANTS)} (13+5 FOSS + 1 Claude); "
        f"tasks: {len(TASKS)}; per-cell budget: ${PER_CELL_BUDGET_USD}; "
        f"parallelism={PARALLELISM}")
    # Best-effort: R17 doesn't use Agor REST (uses git worktree add directly),
    # but try to relogin in case downstream tooling needs the token.
    try: relogin()
    except Exception as exc: log(f"  agor relogin skipped: {exc}")

    # R17 fix B1 — eager-compute every task's prefetch ONCE up front.
    # Saves repeated work across cells + surfaces prefetch errors before
    # any cell spends API budget.
    log("eager-prefetching all tasks...")
    for t in TASKS:
        try:
            pf = get_prefetch(t)
            log(f"  {t['id']} prefetch: {len(json.dumps(pf))} chars")
        except Exception as exc:
            log(f"  {t['id']} prefetch FAILED: {exc}")
            raise

    # R17 fix B4 — per-provider warmup ping
    if os.environ.get("R17_SKIP_WARMUP") != "1":
        warmup_providers()

    # R26 P1-10 — resume K20 endpoint if K20 is in active VARIANTS.
    # The Together dedicated endpoint costs $7.98/hr while running;
    # resume at round start, pause at round end (atexit hook below).
    if any(v[0].startswith("K20-") for v in VARIANTS):
        log("K20 in active variants — resuming Together endpoint")
        try:
            rc = subprocess.run(
                ["bash", str(REPO / "scripts/_k20_endpoint_resume.sh")],
                capture_output=True, text=True, timeout=600,
            )
            if rc.returncode == 0:
                log(f"  K20 endpoint resumed: {rc.stdout.strip()}")
                # Register pause helper so endpoint stops on round exit
                import atexit
                def _pause_k20_on_exit():
                    try:
                        subprocess.run(
                            ["bash", str(REPO / "scripts/_k20_endpoint_pause.sh")],
                            capture_output=True, timeout=30,
                        )
                        log("[exit] K20 endpoint paused")
                    except Exception:
                        pass
                atexit.register(_pause_k20_on_exit)
            else:
                log(f"  K20 resume FAILED: {rc.stderr.strip()[:200]}")
                log("  removing K20 from active VARIANTS for this round")
                VARIANTS[:] = [v for v in VARIANTS if not v[0].startswith("K20-")]
        except Exception as exc:
            log(f"  K20 resume exception: {exc} — removing K20 from VARIANTS")
            VARIANTS[:] = [v for v in VARIANTS if not v[0].startswith("K20-")]

    # Cell counts:
    #   Layer 1: 9 variants × 1 task (B1) × 1 config = 9
    # Cell counts at PARALLELISM=8:
    #   Layer 1: 14 variants × B1 = 14 cells (~3 min wall)
    #   Layer 2: top-3 FOSS + Claude × remaining 7 tasks = 28 cells (~7 min)
    #   Layer 3: top-2 FOSS × 6 ablation dials × B1 = 12 cells (~3 min)
    #   Total: ~54 cells, ~15-20 min wall (vs sequential ~2-3h)
    EXPECTED_CELLS = len(VARIANTS) + 28 + 12
    PROGRESS = Progress(EXPECTED_CELLS, budget_usd=100.0)   # R17: $25 → $100

    all_cells: list[dict] = []

    # ── Layer 1: tournament on B1 with BEST_CONFIG ───────────────────────
    # R17 synthesis #4 — promote K8-V3 + K13-V4-pro with n=3 to validate
    # stability claim (hidden-capability agent: K8 6/6 across R15+R16; n=1
    # too thin to crown). Other variants stay at n=1 for L1 budget.
    # R18 all-night ambition — n=10-15 default for top-tier; n=5 for probes.
    # Per spend-headroom analysis: 23-33× cheaper than Claude per qpt;
    # plenty of room to bump samples for variance estimation.
    N_REPLICATES_L1 = {
        "K8-deepseekV3":     25,  # validated workhorse (R17 stability)
        "K1-qwen30":         50,  # R25_DESIGN: K1+K2 n=50 sample-up  # answers R26-Q3
        "K2-kimi-k2.6":      50,  # R25_DESIGN: K1+K2 n=50 sample-up  # quality leader; underweighted in R17
        "K11-qwen3coder":    10,  # premium reference
        "K7-qwen72b":        10,  # post-repin probe
        "K6-llama70b":       15,  # R26 P1-9: alt-broker probe (Together/Parasail) at n=15
        "K17-glm46":         10,  # n=1 score 8 (R17) — promising
        "K9-mixtral":         5,  # carry-over probe
        "K15-kimi-thinking":  5,  # side-pool (long pole)
        "K5-claude-solo":     5,  # sentinel only
    }
    b1 = TASKS[0]
    cell_specs = []
    for variant in VARIANTS:
        n = N_REPLICATES_L1.get(variant[0], 1)
        for sample in range(n):
            cell_specs.append((b1, variant, BEST_CONFIG, "layer1"))
    layer1_cells = run_layer_parallel("LAYER 1 — variant tournament on B1",
                                          cell_specs, all_cells)

    # R26 P0-1 fix — pick top-3 distinct variants by per-cell-mean score.
    # The previous version (=foss_cells[:3]= → take .variant of top 3 cells)
    # picked individual cells, which collapsed to the same variant when
    # one variant had all 3 highest cells (R25: top3_foss = [K1, K1, K1]
    # → L2/L3/BK ran K1-only). Fix: aggregate by variant name, rank by
    # mean score per cell (not sum, so cells the variant didn't run
    # don't dilute), tiebreak by cost-per-unit ascending.
    foss_cells = [c for c in layer1_cells if not c.get("is_external_baseline")]
    _by_variant: dict[str, list[float]] = {}
    _cost_by_variant: dict[str, float] = {}
    for c in foss_cells:
        v = c["variant"]
        _by_variant.setdefault(v, []).append(score_cell(c).get("score", 0))
        # Track cumulative cost so cost-per-unit tiebreak works
        _cost_by_variant[v] = _cost_by_variant.get(v, 0.0) + (
            c.get("cost_usd") or
            (c.get("phase1_cost_usd", 0) + c.get("specialist_cost_usd", 0))
        )
    def _variant_rank_key(v: str) -> tuple[float, float]:
        scores = _by_variant[v]
        mean_score = sum(scores) / max(len(scores), 1)
        # Tiebreak: lower $/qpt = preferred. Need primary count; approx
        # via cost / max(mean_score, 1) to avoid div-zero.
        cpu_proxy = _cost_by_variant.get(v, 0) / max(mean_score, 0.001)
        return (-mean_score, cpu_proxy)
    ranked_variants = sorted(_by_variant.keys(), key=_variant_rank_key)
    top3_foss = ranked_variants[:3]
    log(f"\nLayer 1 → top-3 distinct FOSS variants: {top3_foss}")
    # Drop dead variants — variants whose mean score is 0 get excluded
    # from L2 even if they're top-3 (R17 carry-forward).
    top3_foss = [v for v in top3_foss
                  if (sum(_by_variant[v]) / max(len(_by_variant[v]), 1)) > 0]
    log(f"Layer 1 → advancing (after dead-variant filter): {top3_foss}")

    # ── Layer 2: top-3 FOSS + Claude × remaining tasks ───────────────────
    # R25_DESIGN: K8 in Layer-2 advancing pool
    # R26-Q1: K8 must run outside B1 to confirm recovery is broad
    # R26 P1-3: K8 + K11 added to force-list. K8 needs B5/B7/B11/B25 + B11+B26
    # mini-probe to confirm cross-task generalization (R26 force-listed
    # but selector bug excluded; the 4 forced cells silent_noop'd — R26
    # will retry with selector dedupe in P0-1 + dedicated probe at n=5).
    # K11 added so it gets exercised on B25 elisp (its native task) at
    # higher n than the layer_k11b25 mini stage.
    _R25_FORCE_LAYER2 = {"K8-deepseekV3", "K1-qwen30", "K11-qwen3coder"}
    advancing = [v for v in VARIANTS if v[0] in top3_foss
                  or v[0] in _R25_FORCE_LAYER2
                  or v[2] == "claude-solo"]
    # R24_SYNTHESIS: drop floor/broken tasks
    _R24_DROP_TASK_IDS = {"B13", "B20", "B26"}
    cell_specs = [(task, variant, BEST_CONFIG, "layer2")
                   for task in TASKS[1:]
                   if task.get("id") not in _R24_DROP_TASK_IDS
                   and not task.get("id", "").startswith("BK")
                   for variant in advancing]
    # R26 P1-7 (S4) — specialty routing: keep only variants tagged for
    # the task's family (when both task and family entry are present).
    _pre_s4_count = len(cell_specs)
    cell_specs = _filter_specs_by_family(cell_specs)
    log(f"  S4 specialty routing: {_pre_s4_count} → {len(cell_specs)} cells")
    # R26 P1-7 (S2) — best-of-N voting: multiply BEST_OF_N_VARIANT cells
    # on BEST_OF_N_TASKS by BEST_OF_N_SAMPLES.
    _pre_s2_count = len(cell_specs)
    cell_specs = _expand_best_of_n(cell_specs)
    log(f"  S2 best-of-N: {_pre_s2_count} → {len(cell_specs)} cells")
    run_layer_parallel("LAYER 2 — top-3 FOSS + Claude × remaining tasks",
                          cell_specs, all_cells)

    # R26 P1-7 (S3) — critique-revise: K1 draft → K2 critique → K1 revise
    # for tasks in CRITIQUE_REVISE_TASKS where K1 + K2 both advance.
    if os.environ.get("R26_DISABLE_S3") != "1":
        run_layer_critique_revise(advancing, all_cells)

    # R24_SYNTHESIS: K11-B25 sample-up
    k11_var = next((v for v in VARIANTS if v[0] == "K11-qwen3coder"), None)
    b25_task = next((t for t in TASKS if t.get("id") == "B25"), None)
    if k11_var and b25_task:
        log("\nLayer K11-B25 sample-up — n=8 verification of elisp lead")
        k11b25_specs = [(b25_task, k11_var, BEST_CONFIG, "layer_k11b25")
                          for _ in range(8)]
        run_layer_parallel("LAYER K11-B25 (n=8)", k11b25_specs, all_cells)

    # ── Layer 3: dial ablation on B1 + top-2 FOSS ────────────────────────
    top2_foss = [v for v in VARIANTS if v[0] in top3_foss[:2]]
    cell_specs = [(b1, variant, dial, f"layer3_{dial_label}")
                   for variant in top2_foss
                   for dial_label, dial in ABLATION_DIALS]
    run_layer_parallel("LAYER 3 — dial ablation on B1 (top-2 FOSS)",
                          cell_specs, all_cells)

    # ── Final summary ────────────────────────────────────────────────────
    log("")

    # R26 P1-8: Layer-BK on FULL advancing pool, not just top-3.
    # R26 ran 10 BK cells (5 BK tasks × top-3 FOSS=K1+K5). With selector
    # dedupe (P0-1) + force-list (P1-3) ALL surviving FOSS variants
    # plus Claude get to attempt long-horizon tasks. Adds ~$2-4/round
    # (per r26-quality-judge §R-4) but produces K8/K11 long-horizon data
    # for the first time.
    bk_tasks = [t for t in TASKS if t.get("id", "").startswith("BK")]
    if bk_tasks and top3_foss:
        log(f"\nLayer BK — long-horizon tasks: {[t['id'] for t in bk_tasks]}")
        # Full advancing pool: every active VARIANT that scored > 0 in L1
        # OR is on the force-list (K1, K8, K11) OR is Claude.
        scored_variants = {v for v, scores in _by_variant.items()
                            if (sum(scores) / max(len(scores), 1)) > 0}
        bk_advancing = [v for v in VARIANTS
                          if v[0] in scored_variants
                          or v[0] in _R25_FORCE_LAYER2
                          or v[2] == "claude-solo"]
        log(f"Layer BK pool: {[v[0] for v in bk_advancing]}")
        bk_specs = [(task, variant, BEST_CONFIG, "layer_bk")
                       for task in bk_tasks
                       for variant in bk_advancing]
        run_layer_parallel("LAYER BK — BK1-BK5 long-horizon",
                              bk_specs, all_cells)
    else:
        log("\nLayer BK skipped — no BK tasks loaded or no top-3 FOSS yet")

        log("=" * 80)
    log(f"R26 COMPLETE — {len(all_cells)} cells, ${PROGRESS.spent:.3f} spent")
    log("=" * 80)
    print_leaderboard(all_cells)

    summary = {
        "epoch": EPOCH,
        "total_cells": len(all_cells),
        "total_cost_usd": round(PROGRESS.spent, 4),
        "total_wall_seconds": round(time.time() - PROGRESS.t0, 1),
        "best_config": asdict(BEST_CONFIG),
        "top3_foss_after_layer1": top3_foss,
        "cells": [{k: v for k, v in c.items()
                    if k not in ("specialists", "analysis", "prefetch")}
                   | {"score": score_cell(c)}
                  for c in all_cells],
    }
    summary_p = ARTIFACTS / f"summary-{EPOCH}.json"
    summary_p.write_text(json.dumps(summary, indent=2, default=str))
    log(f"summary: {summary_p}")
    log(f"live state: {LIVE}")


# ── R26 P1-7 acceptance probes (no API; mocks only) ──────────────────
def _r26_p1_7_acceptance_probes():
    """Smoke test S1-S4 wiring without spawning real cells. Run via:
        python scripts/_round26_dials.py --test-s1234
    Asserts each strategy's helper does what the launch checklist
    promised. Returns (passed, failed) counts."""
    passed = []
    failed = []

    def check(name, cond, detail=""):
        if cond:
            passed.append(name)
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name}: {detail}")

    # S1: low primary on K8-deepseekV3 → escalate to K11-qwen3coder
    fake_cell = {
        "variant": "K8-deepseekV3",
        "task_id": "B5",
        "analysis": {
            "diff_in_scope": {"insertions": 4, "deletions": 4},
            "diff_out_of_scope": {"insertions": 0, "deletions": 0},
            "wrap_categories": {},
            "id_fabrication_count": 0,
        },
    }
    s1_target = _should_escalate(fake_cell)
    check("S1: low-primary K8 escalates to K11",
          s1_target == "K11-qwen3coder",
          f"got {s1_target!r}, expected K11-qwen3coder "
          f"(primary={score_cell(fake_cell).get('primary')})")

    # S1 negative: high primary should NOT escalate
    fake_cell_high = {
        "variant": "K8-deepseekV3",
        "task_id": "B5",
        "analysis": {
            "diff_in_scope": {"insertions": 30, "deletions": 0},
            "diff_out_of_scope": {"insertions": 0, "deletions": 0},
            "wrap_categories": {},
            "id_fabrication_count": 0,
        },
    }
    check("S1: high-primary K8 does NOT escalate",
          _should_escalate(fake_cell_high) is None,
          "expected None when primary >= threshold")

    # S2: BEST_OF_N_VARIANT × BEST_OF_N task expands to BEST_OF_N_SAMPLES
    fake_dial = type("D", (), {"label": lambda self: "best"})()
    fake_task = {"id": "B5", "label": "wiki edit"}
    fake_variant = (BEST_OF_N_VARIANT, "model/whatever", "specialist")
    specs = [(fake_task, fake_variant, fake_dial, "layer2")]
    expanded = _expand_best_of_n(specs)
    check("S2: B5 + BEST_OF_N_VARIANT expands to N copies",
          len(expanded) == BEST_OF_N_SAMPLES,
          f"got {len(expanded)} cells, expected {BEST_OF_N_SAMPLES}")
    check("S2: all expanded specs are identical (suffix logic in execute_cell)",
          all(s == specs[0] for s in expanded),
          "expansion produced non-identical specs")

    # S2 negative: non-BEST_OF_N task is unaffected
    other_task = {"id": "B7", "label": "code edit"}
    other_specs = [(other_task, fake_variant, fake_dial, "layer2")]
    check("S2: non-BEST_OF_N task is not multiplied",
          len(_expand_best_of_n(other_specs)) == 1,
          "non-BEST_OF_N task got multiplied")

    # S3: revise brief contains "Reviewer feedback:" + critique text
    revise_brief = _build_revise_brief(
        "Original task brief.",
        "- Missing Summary section\n- Bracket count wrong",
    )
    check("S3: revise brief contains 'Reviewer feedback:'",
          "Reviewer feedback:" in revise_brief, revise_brief[:200])
    check("S3: revise brief contains critic text",
          "Missing Summary section" in revise_brief, revise_brief[:200])
    check("S3: revise brief preserves original brief",
          "Original task brief." in revise_brief, revise_brief[:200])

    # S3: critique prompt names task + draft diff
    cprompt = _build_critique_prompt(
        {"id": "B11", "label": "DEC", "goal": "Author DEC-XXX."},
        "diff --git a/foo b/foo\n+hello\n",
    )
    check("S3: critique prompt names task id",
          "B11" in cprompt, cprompt[:200])
    check("S3: critique prompt embeds draft diff",
          "+hello" in cprompt, cprompt[:200])

    # S4: B25 (elisp) only allows K2-kimi-k2.6, filters out K1-qwen30
    k1_t = ("K1-qwen30", "qwen/qwen3-30b-a3b", "specialist")
    k2_t = ("K2-kimi-k2.6", "moonshot/kimi-k2", "specialist")
    k8_t = ("K8-deepseekV3", "deepseek/deepseek-chat", "specialist")
    b25_task = {"id": "B25", "label": "elisp"}
    layer2_specs = [
        (b25_task, k1_t, fake_dial, "layer2"),
        (b25_task, k2_t, fake_dial, "layer2"),
        (b25_task, k8_t, fake_dial, "layer2"),
    ]
    filtered = _filter_specs_by_family(layer2_specs)
    filtered_names = {s[1][0] for s in filtered}
    check("S4: B25 (elisp) keeps K2-kimi-k2.6",
          "K2-kimi-k2.6" in filtered_names, str(filtered_names))
    check("S4: B25 (elisp) drops K1-qwen30 (wiki_edit-only)",
          "K1-qwen30" not in filtered_names, str(filtered_names))
    check("S4: B25 (elisp) drops K8-deepseekV3 (not in elisp family)",
          "K8-deepseekV3" not in filtered_names, str(filtered_names))

    # S4 negative: task with no family entry passes through
    bk_task = {"id": "BK1", "label": "long horizon"}
    bk_specs = [(bk_task, k1_t, fake_dial, "layer_bk")]
    check("S4: tasks without family entry pass through",
          len(_filter_specs_by_family(bk_specs)) == 1,
          "BK1 without family was filtered out")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return passed, failed


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-s1234":
        _, failures = _r26_p1_7_acceptance_probes()
        sys.exit(0 if not failures else 1)
    main()
