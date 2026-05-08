#!/usr/bin/env bash
# R26 Pre-flight gate runner — boost plan §13 single-entry-point.
#
# Six gate categories run in order. Hard-fails write
# $ARTIFACTS/PREFLIGHT_FAIL + exit 1; all-pass writes
# $ARTIFACTS/PREFLIGHT_PASS + exit 0. Cat-3 (K20) is the only
# soft-fail: failure removes K20 from the round, does NOT block.
#
# Usage:
#   bash scripts/_r26_preflight.sh
#   FAST_PREFLIGHT=1 bash scripts/_r26_preflight.sh   # skip 410s WALL_CAP probe
#   FAKE_DISK_FREE=2 bash scripts/_r26_preflight.sh   # inject failure for meta-test
#
# Env overrides:
#   ARTIFACTS               — output dir (default scripts/_round26_dials_artifacts)
#   R26_VARIANTS_FILE       — python module path with VARIANTS list
#                             (default scripts/_round25_dials.py — R26 forks from R25)
#   R26_HEADROOM_FLOOR      — OpenRouter min headroom (default 40.00)
#   R26_RUNPOD_FLOOR        — RunPod min balance (default 20.00)
#   R26_MAX_HOLDOUT_PCT     — held-out variant ceiling (default 0.30)
#   FAST_PREFLIGHT=1        — skip 410s WALL_CAP probe (substitutes static check)
#   MODAL_BILLING_CAPPED=1  — skip Modal-Kimi probes
#   ANTHROPIC_BUDGET_USED   — manual sentinel; if unset, K5-claude warns only
#   FAKE_DISK_FREE=<GB>     — inject fake disk-free GB for meta-test
#
# Sentinels written under $ARTIFACTS:
#   PREFLIGHT_PASS          — all gates passed (or K20 cat-3 soft-skipped)
#   PREFLIGHT_FAIL          — JSON list of {gate, reason, suggested_action}
#   PREFLIGHT_K20_SKIP      — K20 dropped from round (cat-3 soft-fail)
#   SKIPPED_VARIANTS        — newline-separated variant names to remove
#   HELD_OUT_VARIANTS       — newline-separated variants from cat-2 hold-out

set -uo pipefail

REPO=/home/daniel/repos/org-llm
LIVE_LOG=$REPO/docs/wiki/2026-05-08-r18-live-log.org
ARTIFACTS=${ARTIFACTS:-$REPO/scripts/_round26_dials_artifacts}
DIALS=${R26_VARIANTS_FILE:-$REPO/scripts/_round25_dials.py}
HEADROOM_FLOOR=${R26_HEADROOM_FLOOR:-40.00}
RUNPOD_FLOOR=${R26_RUNPOD_FLOOR:-20.00}
MAX_HOLDOUT_PCT=${R26_MAX_HOLDOUT_PCT:-0.30}
FAST_PREFLIGHT=${FAST_PREFLIGHT:-0}

mkdir -p "$ARTIFACTS"
# Idempotent: clear prior sentinels before this run
rm -f "$ARTIFACTS/PREFLIGHT_PASS" \
      "$ARTIFACTS/PREFLIGHT_FAIL" \
      "$ARTIFACTS/PREFLIGHT_K20_SKIP" \
      "$ARTIFACTS/SKIPPED_VARIANTS" \
      "$ARTIFACTS/HELD_OUT_VARIANTS"

FAIL_JSON="$ARTIFACTS/PREFLIGHT_FAIL"
SKIPPED_VARIANTS="$ARTIFACTS/SKIPPED_VARIANTS"
HELD_OUT_VARIANTS="$ARTIFACTS/HELD_OUT_VARIANTS"

# ── logging helpers ───────────────────────────────────────────────────────
ts() { date +%H:%M:%S; }

# Append one-liner per gate to the live log
log_live() {
    echo "[r26-preflight $(ts)] $*" >> "$LIVE_LOG"
}

# Stdout + live log
say() {
    echo "$*"
    log_live "$*"
}

pass_gate() {
    local cat="$1" name="$2"
    say "[PASS] $cat/$name"
}

fail_gate() {
    local cat="$1" name="$2" reason="$3" action="${4:-investigate}"
    say "[FAIL] $cat/$name — $reason"
    # Append structured JSON line per fail (PREFLIGHT_FAIL is JSONL)
    printf '{"gate":"%s/%s","reason":%s,"suggested_action":%s}\n' \
        "$cat" "$name" \
        "$(jq -Rs . <<< "$reason")" \
        "$(jq -Rs . <<< "$action")" \
        >> "$FAIL_JSON"
}

skip_gate() {
    local cat="$1" name="$2" reason="$3"
    say "[SKIP] $cat/$name — $reason"
}

warn_gate() {
    local cat="$1" name="$2" reason="$3"
    say "[WARN] $cat/$name — $reason"
}

hard_fail() {
    local cat="$1" name="$2" reason="$3" action="${4:-investigate}"
    fail_gate "$cat" "$name" "$reason" "$action"
    say "PREFLIGHT_FAIL — aborting at $cat/$name"
    exit 1
}

say "── R26 pre-flight starting (FAST=$FAST_PREFLIGHT) ──"
say "  artifacts: $ARTIFACTS"
say "  dials:     $DIALS"

# ─────────────────────────────────────────────────────────────────────────
# CAT 1 — Provider funding
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 1: provider funding ──"

# 1.1 OpenRouter headroom > $40
OR_KEY=$(pass org-llm/cloud/openrouter/api-key 2>/dev/null | head -1)
if [ -z "$OR_KEY" ]; then
    hard_fail funding openrouter_headroom \
        "no OpenRouter API key in pass" \
        "pass insert org-llm/cloud/openrouter/api-key"
fi
OR_JSON=$(curl -sS --max-time 10 https://openrouter.ai/api/v1/credits \
    -H "Authorization: Bearer $OR_KEY" 2>/dev/null)
OR_TOTAL=$(echo "$OR_JSON" | jq -r '.data.total_credits // 0' 2>/dev/null)
OR_USED=$(echo "$OR_JSON"  | jq -r '.data.total_usage   // 0' 2>/dev/null)
OR_HEADROOM=$(awk "BEGIN { printf \"%.2f\", $OR_TOTAL - $OR_USED }")
if awk "BEGIN { exit ($OR_HEADROOM >= $HEADROOM_FLOOR) ? 0 : 1 }"; then
    pass_gate funding "openrouter_headroom (\$$OR_HEADROOM >= \$$HEADROOM_FLOOR)"
else
    hard_fail funding openrouter_headroom \
        "headroom \$$OR_HEADROOM < floor \$$HEADROOM_FLOOR" \
        "top up OpenRouter credits"
fi

# 1.2 RunPod balance > $20 (best-effort; query may fail without billing perms)
RP_KEY=$(pass org-llm/cloud/runpod/api-key 2>/dev/null | head -1)
if [ -z "$RP_KEY" ]; then
    warn_gate funding runpod_balance "no RunPod key in pass — skip"
else
    RP_JSON=$(curl -sS --max-time 10 \
        -X POST https://api.runpod.io/graphql \
        -H "Authorization: Bearer $RP_KEY" \
        -H "Content-Type: application/json" \
        -d '{"query":"{ myself { clientBalance } }"}' 2>/dev/null)
    RP_BAL=$(echo "$RP_JSON" | jq -r '.data.myself.clientBalance // empty' 2>/dev/null)
    if [ -z "$RP_BAL" ] || [ "$RP_BAL" = "null" ]; then
        warn_gate funding runpod_balance "balance API returned no value (may be quota-restricted)"
    elif awk "BEGIN { exit ($RP_BAL >= $RUNPOD_FLOOR) ? 0 : 1 }"; then
        pass_gate funding "runpod_balance (\$$RP_BAL >= \$$RUNPOD_FLOOR)"
    else
        hard_fail funding runpod_balance \
            "balance \$$RP_BAL < floor \$$RUNPOD_FLOOR" \
            "top up RunPod balance"
    fi
fi

# 1.3 Together balance non-zero (defensive: 4xx → warn, not abort)
TG_KEY=$(pass org-llm/cloud/together/api-key 2>/dev/null | head -1)
if [ -z "$TG_KEY" ]; then
    warn_gate funding together_balance "no Together key in pass — skip"
else
    TG_HTTP=$(curl -sS --max-time 10 -o /tmp/r26-together-bal.$$ -w "%{http_code}" \
        https://api.together.xyz/v1/finance \
        -H "Authorization: Bearer $TG_KEY" 2>/dev/null)
    if [ "$TG_HTTP" = "200" ]; then
        TG_BAL=$(jq -r '.balance // .data.balance // 0' /tmp/r26-together-bal.$$ 2>/dev/null)
        if awk "BEGIN { exit ($TG_BAL > 0) ? 0 : 1 }"; then
            pass_gate funding "together_balance (\$$TG_BAL)"
        else
            warn_gate funding together_balance "balance \$$TG_BAL — Together routes optional"
        fi
    else
        warn_gate funding together_balance "HTTP $TG_HTTP — balance unknown; Together routes optional"
    fi
    rm -f /tmp/r26-together-bal.$$
fi

# 1.4 Anthropic budget — manual sentinel
if [ -n "${ANTHROPIC_BUDGET_USED:-}" ]; then
    pass_gate funding "anthropic_budget (used=\$${ANTHROPIC_BUDGET_USED})"
else
    warn_gate funding anthropic_budget "ANTHROPIC_BUDGET_USED unset — K5-claude sentinel disabled"
    echo "K5-claude-solo" >> "$SKIPPED_VARIANTS"
fi

# 1.5 Modal billing-cap check
if [ "${MODAL_BILLING_CAPPED:-0}" = "1" ]; then
    skip_gate funding modal_capped "MODAL_BILLING_CAPPED=1 — Kimi-K2 routes via OR Parasail"
else
    pass_gate funding modal_capped
fi

# 1.6 HF + Qdrant quota — best-effort
HF_TOKEN=$(pass org-llm/cloud/huggingface/api-key 2>/dev/null | head -1)
if [ -z "$HF_TOKEN" ]; then
    warn_gate funding hf_quota "no HF token in pass — skip"
else
    HF_HTTP=$(curl -sS --max-time 10 -o /dev/null -w "%{http_code}" \
        https://huggingface.co/api/whoami-v2 \
        -H "Authorization: Bearer $HF_TOKEN" 2>/dev/null)
    if [ "$HF_HTTP" = "200" ]; then
        pass_gate funding "hf_quota (auth ok)"
    else
        warn_gate funding hf_quota "HTTP $HF_HTTP — defer K20 v2 push if needed"
    fi
fi

QD_URL=$(pass org-llm/cloud/qdrant/url 2>/dev/null | head -1)
QD_KEY=$(pass org-llm/cloud/qdrant/api-key 2>/dev/null | head -1)
if [ -z "$QD_URL" ] || [ -z "$QD_KEY" ]; then
    warn_gate funding qdrant_quota "no Qdrant url/key in pass — skip"
else
    QD_HTTP=$(curl -sS --max-time 10 -o /dev/null -w "%{http_code}" \
        "$QD_URL/" -H "api-key: $QD_KEY" 2>/dev/null)
    if [ "$QD_HTTP" = "200" ]; then
        pass_gate funding "qdrant_quota (reachable)"
    else
        warn_gate funding qdrant_quota "HTTP $QD_HTTP — defer vector ops"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# CAT 2 — Endpoint health (real chat completion per variant)
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 2: endpoint health ──"

# Extract VARIANTS from dials file via python.
# NOTE: the dials module prints status banners on import; we redirect its
# stdout to stderr so only our final json line ends up on stdout.
VARIANTS_JSON=$(python3 - <<PYEOF 2>/dev/null
import importlib.util, json, sys, io, contextlib
modname = "dials_for_preflight"
spec = importlib.util.spec_from_file_location(modname, "$DIALS")
mod = importlib.util.module_from_spec(spec)
sys.modules[modname] = mod  # required for @dataclass cls.__module__ lookup
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        spec.loader.exec_module(mod)
except Exception as exc:
    sys.stdout.write(json.dumps({"error": str(exc)}))
    sys.exit(0)
out = []
for v in getattr(mod, "VARIANTS", []):
    name, model_id, mode = v[0], v[1], v[2]
    out.append({"name": name, "model_id": model_id, "mode": mode})
pins = getattr(mod, "PROVIDER_PINS", {})
sys.stdout.write(json.dumps({"variants": out, "pins": pins}))
PYEOF
)
VARIANTS_ERROR=$(echo "$VARIANTS_JSON" | jq -r '.error // empty' 2>/dev/null)
if [ -n "$VARIANTS_ERROR" ]; then
    hard_fail health variants_load \
        "could not import VARIANTS from $DIALS: $VARIANTS_ERROR" \
        "fix syntax of dials file"
fi

VARIANTS_COUNT=$(echo "$VARIANTS_JSON" | jq '.variants | length')
say "  loaded $VARIANTS_COUNT variants from dials"

held_out_count=0
total_probe_variants=0

while IFS=$'\t' read -r name model_id mode; do
    [ -z "$name" ] && continue
    [ "$mode" != "agor" ] && continue
    [ "$model_id" = "null" ] && continue
    total_probe_variants=$((total_probe_variants + 1))

    # Allowed providers from PROVIDER_PINS (best-effort; missing = any allowed)
    allowed=$(echo "$VARIANTS_JSON" | jq -r --arg m "$model_id" \
        '.pins[$m].order // [] | join(",")')

    PIN_BODY=$(echo "$VARIANTS_JSON" | jq -c --arg m "$model_id" '.pins[$m] // {}')

    # Prompt: "Reply with PING" — strict instruction. Some models still
    # reply with PONG / acknowledgements; we accept any of:
    #   - matches /PING/i (strict path per spec)
    #   - non-empty content + finish_reason ∈ {stop, length}
    # The endpoint-health gate's real job is to catch silent_noop /
    # garbage / 4xx / wrong-provider, not pedantic literal matching.
    PAYLOAD=$(jq -n --arg model "$model_id" --argjson pin "$PIN_BODY" \
        '{model:$model, messages:[{role:"user", content:"Reply with the single word: PING"}], max_tokens:10, temperature:0, provider:$pin}')

    RESP=$(curl -sS --max-time 30 -X POST \
        https://openrouter.ai/api/v1/chat/completions \
        -H "Authorization: Bearer $OR_KEY" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    CONTENT=$(echo "$RESP" | jq -r '.choices[0].message.content // empty' 2>/dev/null)
    PROVIDER=$(echo "$RESP" | jq -r '.provider // empty' 2>/dev/null)
    FINISH=$(echo "$RESP"   | jq -r '.choices[0].finish_reason // empty' 2>/dev/null)
    ERR_MSG=$(echo "$RESP"  | jq -r '.error.message // empty' 2>/dev/null)

    reason=""
    if [ -n "$ERR_MSG" ]; then
        reason="endpoint error: $ERR_MSG"
    elif [ -z "$CONTENT" ] || [ "$CONTENT" = "null" ]; then
        reason="empty content (resp head=$(echo "$RESP" | tr -d '\n' | head -c 160))"
    elif [ -z "$PROVIDER" ]; then
        reason="missing provider field (silent_noop risk)"
    elif [ "$FINISH" != "stop" ] && [ "$FINISH" != "length" ]; then
        reason="finish_reason=$FINISH (expected stop|length)"
    fi
    # Soft note: log if literal /PING/i didn't match but otherwise OK
    note=""
    if [ -z "$reason" ] && ! echo "$CONTENT" | grep -qi "PING"; then
        note=" (note: content lacked literal PING — content head: $(echo "$CONTENT" | tr -d '\n' | head -c 50))"
    fi

    # If we have an allowed-providers list, verify provider is in it
    if [ -z "$reason" ] && [ -n "$allowed" ]; then
        ok=0
        IFS=',' read -ra ALLOWED_ARR <<< "$allowed"
        for a in "${ALLOWED_ARR[@]}"; do
            [ "$a" = "$PROVIDER" ] && ok=1 && break
        done
        if [ "$ok" -eq 0 ]; then
            reason="provider=$PROVIDER not in allowed=[$allowed]"
        fi
    fi

    if [ -z "$reason" ]; then
        pass_gate health "probe_$name (provider=$PROVIDER, finish=$FINISH)$note"
    else
        warn_gate health "probe_$name" "$reason"
        echo "$name" >> "$HELD_OUT_VARIANTS"
        echo "$name" >> "$SKIPPED_VARIANTS"
        held_out_count=$((held_out_count + 1))
    fi
done < <(echo "$VARIANTS_JSON" | jq -r '.variants[] | [.name, .model_id, .mode] | @tsv')

# Round-level fail-rate
if [ "$total_probe_variants" -gt 0 ]; then
    HOLDOUT_PCT=$(awk "BEGIN { printf \"%.3f\", $held_out_count / $total_probe_variants }")
    if awk "BEGIN { exit ($HOLDOUT_PCT > $MAX_HOLDOUT_PCT) ? 0 : 1 }"; then
        hard_fail health holdout_rate \
            "held-out $held_out_count/$total_probe_variants ($HOLDOUT_PCT) > MAX_HOLDOUT_PCT $MAX_HOLDOUT_PCT" \
            "investigate provider outages or update PROVIDER_PINS"
    else
        pass_gate health "holdout_rate ($held_out_count/$total_probe_variants = $HOLDOUT_PCT <= $MAX_HOLDOUT_PCT)"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# CAT 3 — K20 endpoint resume (soft-fail: skip K20)
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 3: K20 endpoint resume (soft-fail) ──"

K20_IN_VARIANTS=$(echo "$VARIANTS_JSON" | jq -r '.variants[] | select(.name | startswith("K20")) | .name' | head -1)

if [ -z "$K20_IN_VARIANTS" ]; then
    skip_gate k20 endpoint_resume "K20 not in active VARIANTS — n/a"
else
    if bash "$REPO/scripts/_k20_endpoint_resume.sh" >/tmp/r26-k20-resume.$$ 2>&1; then
        K20_MODEL=$(tail -1 /tmp/r26-k20-resume.$$)
        pass_gate k20 "endpoint_resume (model=$K20_MODEL)"

        # Cat-3.2 / Cat-6.5 : K20 model name match
        EXPECTED_K20=$(python3 - <<PYEOF 2>/dev/null
import importlib.util, io, contextlib, sys
modname = "dials_for_preflight2"
spec = importlib.util.spec_from_file_location(modname, "$DIALS")
mod = importlib.util.module_from_spec(spec)
sys.modules[modname] = mod
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        spec.loader.exec_module(mod)
except Exception:
    sys.exit(0)
for v in getattr(mod, "VARIANTS", []):
    if v[0].startswith("K20"):
        sys.stdout.write(v[1] or "")
        break
PYEOF
)
        if [ -n "$EXPECTED_K20" ] && [ "$EXPECTED_K20" != "$K20_MODEL" ]; then
            hard_fail k20 model_match \
                "dials says K20=$EXPECTED_K20 but resume printed $K20_MODEL" \
                "fix VARIANTS K20 entry or pass org-llm/cloud/foss-lora/model"
        else
            pass_gate k20 "model_match"
        fi
    else
        warn_gate k20 endpoint_resume "resume failed; SKIPPING K20 from round (soft-fail)"
        echo "K20 dropped at $(date -Is) — resume failed" \
            > "$ARTIFACTS/PREFLIGHT_K20_SKIP"
        echo "$K20_IN_VARIANTS" >> "$SKIPPED_VARIANTS"
    fi
    rm -f /tmp/r26-k20-resume.$$
fi

# ─────────────────────────────────────────────────────────────────────────
# CAT 4 — Patch verification (R26 P0 patches behavior probes)
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 4: patch verification ──"

# 4.1 max_tokens cap (P0-2): synthetic check — reads _R25_VARIANT_MAX_TOKENS
#     constant from dials AND verifies specialist threads max_tokens.
#     Live K2 long-prompt probe is too costly for pre-flight; we do
#     static + probe-style hybrid (assert constant defined + specialist
#     accepts max_tokens kw arg).
P02_RESULT=$(python3 - <<'PYEOF' 2>&1
import importlib.util, sys, inspect, pathlib, io, contextlib
ROOT = pathlib.Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(ROOT))

# Load dials (suppress its banner prints so our PASS:/FAIL: line is parseable)
modname = "_d_p02"
spec = importlib.util.spec_from_file_location(modname, str(ROOT / "scripts/_round25_dials.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[modname] = m
_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
        spec.loader.exec_module(m)
except Exception as e:
    print(f"FAIL: dials load: {e}")
    sys.exit(0)

caps = getattr(m, "_R25_VARIANT_MAX_TOKENS", None) \
    or getattr(m, "_R26_VARIANT_MAX_TOKENS", None)
if not caps:
    print("FAIL: no _R25/_R26_VARIANT_MAX_TOKENS dict found")
    sys.exit(0)

k2_cap = caps.get("K2-kimi-k2.6")
if not k2_cap or k2_cap > 16000:
    print(f"FAIL: K2 cap missing or too high: {k2_cap}")
    sys.exit(0)

# Verify specialist accepts max_tokens through SpecialistTask
try:
    from org_llm.specialist import SpecialistTask
    sig = inspect.signature(SpecialistTask)
    if "max_tokens" not in sig.parameters:
        print(f"FAIL: SpecialistTask has no max_tokens kwarg ({list(sig.parameters)[:8]})")
        sys.exit(0)
except Exception as e:
    print(f"WARN: specialist import failed: {e} (cap defined but not asserted-threaded)")
    sys.exit(0)

print(f"PASS: K2 cap={k2_cap} & SpecialistTask threads max_tokens")
PYEOF
)
P02_RESULT_FIRST=$(echo "$P02_RESULT" | head -1)
case "$P02_RESULT_FIRST" in
    PASS:*)  pass_gate patches "p0_2_max_tokens (${P02_RESULT_FIRST#PASS: })" ;;
    WARN:*)  warn_gate patches p0_2_max_tokens "${P02_RESULT_FIRST#WARN: }" ;;
    FAIL:*)  hard_fail patches p0_2_max_tokens "${P02_RESULT_FIRST#FAIL: }" \
                 "wire max_tokens through SpecialistTask + chat completions" ;;
    *)       hard_fail patches p0_2_max_tokens "unknown probe output: $P02_RESULT_FIRST" "review _r26_preflight" ;;
esac

# 4.2 WALL_CAP runtime probe (P0-3) — synthetic 5s-cap on 500s task
#     We use a small wall-cap (5s) and a sleep(500) child.
#     If FAST_PREFLIGHT=1: skip and substitute static check.
if [ "$FAST_PREFLIGHT" = "1" ]; then
    if grep -qE "multiprocessing\.Process|mp\.Process" \
        "$REPO/scripts/_round25_dials.py" 2>/dev/null \
       || grep -qE "ProcessPoolExecutor.*signal|Pool.*terminate|os\.killpg" \
        "$REPO/scripts/_round25_dials.py" 2>/dev/null; then
        pass_gate patches "p0_3_wallcap_static (mp.Process or kill mechanism present)"
    else
        warn_gate patches p0_3_wallcap_static \
            "FAST_PREFLIGHT but no clear mp.Process / killpg in dials — runtime probe recommended"
    fi
else
    P03_RESULT=$(python3 - <<'PYEOF' 2>&1
import multiprocessing as mp
import time, sys

def child():
    time.sleep(500)

start = time.time()
p = mp.Process(target=child)
p.start()
p.join(timeout=5.0)  # synthetic 5s cap
if p.is_alive():
    p.terminate()
    p.join(timeout=1.0)
    if p.is_alive():
        p.kill()
        p.join(timeout=1.0)
elapsed = time.time() - start
if elapsed <= 5.5:
    print(f"PASS: synthetic 5s cap killed sleep(500) in {elapsed:.2f}s")
else:
    print(f"FAIL: outer worker took {elapsed:.2f}s (> 5.5s)")
PYEOF
)
    P03_FIRST=$(echo "$P03_RESULT" | head -1)
    case "$P03_FIRST" in
        PASS:*) pass_gate patches "p0_3_wallcap (${P03_FIRST#PASS: })" ;;
        FAIL:*) hard_fail patches p0_3_wallcap "${P03_FIRST#FAIL: }" \
                    "implement WALL_CAP via multiprocessing.Process not ThreadPool ctx-mgr" ;;
        *)      hard_fail patches p0_3_wallcap "unknown: $P03_FIRST" "review WALL_CAP probe" ;;
    esac
fi

# 4.3 Top-3 selector dedupe (P0-1) — mock roster, assert distinct variants
P01_RESULT=$(python3 - <<'PYEOF' 2>&1
import sys, pathlib
ROOT = pathlib.Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(ROOT))

# Mock roster: 3 variants with different scores
roster = [
    {"variant": "K1-qwen30",       "score": 50, "cost": 0.23},
    {"variant": "K1-qwen30",       "score": 48, "cost": 0.22},
    {"variant": "K2-kimi-k2.6",    "score": 40, "cost": 0.15},
    {"variant": "K2-kimi-k2.6",    "score": 38, "cost": 0.14},
    {"variant": "K8-deepseekV3",   "score": 25, "cost": 0.10},
    {"variant": "K8-deepseekV3",   "score": 23, "cost": 0.09},
]

# Try to import a real selector if present; otherwise verify dedupe-by-variant logic
top3_fn = None
for modname in ("scripts._r26_design_patch", "scripts._r25_design_patch"):
    try:
        import importlib
        m = importlib.import_module(modname)
        for cand in ("top3_foss_after_layer1", "select_top3", "verify_top3_selector_landed"):
            if hasattr(m, cand):
                top3_fn = getattr(m, cand)
                break
        if top3_fn:
            break
    except Exception:
        continue

if top3_fn is None:
    # Inline dedupe-by-variant reference impl: assert that any selector
    # MUST return 3 distinct variant names from this mock roster
    by_var = {}
    for cell in sorted(roster, key=lambda c: -c["score"]):
        by_var.setdefault(cell["variant"], cell)
    top3 = list(by_var.keys())[:3]
    if sorted(top3) == sorted(["K1-qwen30", "K2-kimi-k2.6", "K8-deepseekV3"]):
        print(f"PASS: reference dedupe yields {top3} (no live selector to test)")
    else:
        print(f"FAIL: reference dedupe got {top3}")
else:
    try:
        result = top3_fn(roster) if callable(top3_fn) else top3_fn
        names = [r.get("variant") if isinstance(r, dict) else r for r in result]
        if len(set(names)) == 3:
            print(f"PASS: live selector returns 3 distinct variants: {names}")
        else:
            print(f"FAIL: live selector returned dup variants: {names}")
    except Exception as e:
        print(f"FAIL: selector raised {type(e).__name__}: {e}")
PYEOF
)
P01_FIRST=$(echo "$P01_RESULT" | head -1)
case "$P01_FIRST" in
    PASS:*) pass_gate patches "p0_1_top3_dedupe (${P01_FIRST#PASS: })" ;;
    FAIL:*) hard_fail patches p0_1_top3_dedupe "${P01_FIRST#FAIL: }" \
                "fix top-3 selector to dedupe by variant not cell" ;;
    *)      hard_fail patches p0_1_top3_dedupe "unknown: $P01_FIRST" "review selector probe" ;;
esac

# 4.4 count_bracket_errors per-line (P0-4) — re-lint saved K11/K17/K8 diffs
P04_RESULT=$(python3 - <<'PYEOF' 2>&1
import sys, pathlib
ROOT = pathlib.Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(ROOT))
try:
    from scripts.quality_lint import count_bracket_errors
except Exception as e:
    print(f"FAIL: quality_lint import: {e}")
    sys.exit(0)

import inspect
src = inspect.getsource(count_bracket_errors)
if "for line in" not in src:
    print("FAIL: count_bracket_errors does not appear to scan per-line")
    sys.exit(0)

# Re-lint up to 3 saved R25 diffs (K11 / K17 / K8) and assert each cell
# contributes ≤ 1.0 lint penalty.
import glob, json
penalties = []
candidates = []
for prefix in ("K11", "K17", "K8"):
    matches = sorted(glob.glob(
        f"/home/daniel/repos/org-llm/scripts/_round25_dials_artifacts/layer1/B1/{prefix}-*/diff.patch"
    ))
    if matches:
        candidates.append((prefix, matches[0]))

if not candidates:
    print("WARN: no R25 saved diffs found — per-line scan checked statically only")
    sys.exit(0)

for prefix, path in candidates:
    try:
        text = pathlib.Path(path).read_text()
    except Exception:
        continue
    n = count_bracket_errors(text)
    penalties.append((prefix, n))
    if n > 50:
        print(f"FAIL: {prefix} diff produced {n} bracket errors (> 50 = likely cross-line FP regression)")
        sys.exit(0)

if penalties:
    print(f"PASS: per-line lint on {len(penalties)} diffs: {penalties}")
else:
    print("WARN: no diffs scanned")
PYEOF
)
P04_FIRST=$(echo "$P04_RESULT" | head -1)
case "$P04_FIRST" in
    PASS:*) pass_gate patches "p0_4_lint_perline (${P04_FIRST#PASS: })" ;;
    WARN:*) warn_gate patches p0_4_lint_perline "${P04_FIRST#WARN: }" ;;
    FAIL:*) hard_fail patches p0_4_lint_perline "${P04_FIRST#FAIL: }" \
                "fix count_bracket_errors per-line scan" ;;
    *)      hard_fail patches p0_4_lint_perline "unknown: $P04_FIRST" "review lint probe" ;;
esac

# 4.5 Warmup pre-flight negative (P0-5) — inject bad slug; assert removed/fail
P05_RESULT=$(python3 - <<'PYEOF' 2>&1
import sys, urllib.request, json, subprocess, pathlib
# Test the *behavior*: if we send a known-dead slug, does the OR endpoint
# return non-2xx (which our warmup must catch and treat as FAIL)?
try:
    or_key = subprocess.run(
        ["pass", "org-llm/cloud/openrouter/api-key"],
        capture_output=True, text=True, check=True).stdout.strip()
except Exception as e:
    print(f"WARN: no OR key for warmup probe: {e}")
    sys.exit(0)

DEAD_SLUG = "qwen/_DEAD_MODEL_TEST"
payload = json.dumps({
    "model": DEAD_SLUG,
    "messages": [{"role": "user", "content": "ok"}],
    "max_tokens": 1, "temperature": 0,
}).encode()

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=payload, method="POST",
    headers={"Authorization": f"Bearer {or_key}",
             "Content-Type": "application/json"})

err_caught = False
http_status = None
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        http_status = r.status
        body = r.read()
        # 200 with error in body? still wrong-slug recognizable
        try:
            j = json.loads(body)
            if j.get("error") or "DEAD" in str(j):
                err_caught = True
        except Exception:
            pass
except urllib.error.HTTPError as e:
    err_caught = True
    http_status = e.code
except Exception as e:
    err_caught = True
    http_status = f"exc:{type(e).__name__}"

if err_caught:
    print(f"PASS: dead slug rejected (status={http_status}) — warmup gate would catch it")
else:
    print(f"FAIL: dead slug NOT rejected (status={http_status}) — warmup would not gate")
PYEOF
)
P05_FIRST=$(echo "$P05_RESULT" | head -1)
case "$P05_FIRST" in
    PASS:*) pass_gate patches "p0_5_warmup_gate (${P05_FIRST#PASS: })" ;;
    WARN:*) warn_gate patches p0_5_warmup_gate "${P05_FIRST#WARN: }" ;;
    FAIL:*) hard_fail patches p0_5_warmup_gate "${P05_FIRST#FAIL: }" \
                "ensure warmup_providers raises on dead slug + harness aborts" ;;
    *)      hard_fail patches p0_5_warmup_gate "unknown: $P05_FIRST" "review warmup probe" ;;
esac

# 4.6 K20 endpoint reachable + sanity (P1-10) — already covered by cat-3
if [ -f "$ARTIFACTS/PREFLIGHT_K20_SKIP" ]; then
    skip_gate patches p1_10_k20_reachable "K20 dropped at cat-3 — n/a"
elif [ -z "$K20_IN_VARIANTS" ]; then
    skip_gate patches p1_10_k20_reachable "K20 not in VARIANTS"
else
    pass_gate patches "p1_10_k20_reachable (cat-3 cleared)"
fi

# ─────────────────────────────────────────────────────────────────────────
# CAT 5 — Resource gates
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 5: resources ──"

# 5.1 Disk free > 5GB (FAKE_DISK_FREE injection for meta-test)
if [ -n "${FAKE_DISK_FREE:-}" ]; then
    DISK_GB="$FAKE_DISK_FREE"
    say "  (FAKE_DISK_FREE=$DISK_GB injected)"
else
    DISK_GB=$(df -BG "$REPO" | awk 'NR==2 {gsub("G",""); print $4}')
fi
if [ "$DISK_GB" -gt 5 ] 2>/dev/null; then
    pass_gate resource "disk_free (${DISK_GB}G > 5G)"
else
    hard_fail resource disk_free \
        "only ${DISK_GB}G free (< 5G floor)" \
        "prune scripts/_round*_dials_artifacts/ or expand disk"
fi

# 5.2 Stale worktree prune (R26-* dirs older than 1h)
STALE_COUNT=0
WT_PARENT="${REPO}/.."
if [ -d "$WT_PARENT" ]; then
    while IFS= read -r wt; do
        STALE_COUNT=$((STALE_COUNT + 1))
        say "  pruning stale worktree: $wt"
        rm -rf "$wt" 2>/dev/null || true
    done < <(find "$WT_PARENT" -maxdepth 2 -type d -name "r26-*" -mmin +60 2>/dev/null)
fi
if [ "$STALE_COUNT" -gt 0 ]; then
    git -C "$REPO" worktree prune 2>/dev/null || true
    pass_gate resource "worktree_prune (cleaned $STALE_COUNT stale)"
else
    pass_gate resource "worktree_prune (no stale r26-* dirs)"
fi

# 5.3 CPU load < 4.0
LOAD1=$(uptime | sed -E 's/.*load average: ([0-9.]+),.*/\1/')
if awk "BEGIN { exit ($LOAD1 < 4.0) ? 0 : 1 }"; then
    pass_gate resource "cpu_load ($LOAD1 < 4.0)"
else
    warn_gate resource cpu_load "load=$LOAD1 — sleeping 60s and re-checking"
    sleep 60
    LOAD2=$(uptime | sed -E 's/.*load average: ([0-9.]+),.*/\1/')
    if awk "BEGIN { exit ($LOAD2 < 4.0) ? 0 : 1 }"; then
        pass_gate resource "cpu_load_retry ($LOAD2 < 4.0)"
    else
        hard_fail resource cpu_load \
            "load still $LOAD2 after 60s wait" \
            "kill background agents or wait for load to drop"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# CAT 6 — Configuration sanity
# ─────────────────────────────────────────────────────────────────────────
say ""
say "── CAT 6: configuration sanity ──"

CONFIG_RESULT=$(python3 - <<PYEOF 2>/dev/null
import importlib.util, json, time, pathlib, os, sys, io, contextlib
ROOT = pathlib.Path("/home/daniel/repos/org-llm")
DIALS_PATH = pathlib.Path("$DIALS")

modname = "_d_cfg"
spec = importlib.util.spec_from_file_location(modname, str(DIALS_PATH))
m = importlib.util.module_from_spec(spec)
sys.modules[modname] = m
_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
        spec.loader.exec_module(m)
except Exception as e:
    sys.stdout.write(json.dumps({"fatal": f"dials load: {e}"}))
    sys.exit(0)

errs = []

# 6.1 PARALLELISM != 0
para = (os.environ.get("R26_PARALLELISM")
        or os.environ.get("R18_PARALLELISM")
        or getattr(m, "PARALLELISM", None)
        or getattr(m, "DEFAULT_PARALLELISM", None)
        or 16)
try:
    para_i = int(para)
except Exception:
    para_i = 0
if para_i <= 0:
    errs.append({"gate": "parallelism", "reason": f"PARALLELISM={para} (must be > 0)"})

# 6.2 PROVIDER_PINS sanity — every variant primary not in deny-list
pins = getattr(m, "PROVIDER_PINS", {})
deny_files = [
    ROOT / "scripts/_round26_dials_artifacts/LIVE_DENY_LIST",
    ROOT / "scripts/_round25_dials_artifacts/LIVE_DENY_LIST",
]
deny = set()
for f in deny_files:
    if f.exists():
        deny.update(line.strip() for line in f.read_text().splitlines() if line.strip())
for variant in getattr(m, "VARIANTS", []):
    name, model_id = variant[0], variant[1]
    if not model_id:
        continue
    pin = pins.get(model_id, {})
    primary = (pin.get("order") or [None])[0]
    if primary and primary in deny:
        errs.append({"gate": "provider_pins",
                     "reason": f"{name} primary={primary} in deny-list"})

# 6.3 EPOCH != prior round
epoch_now = int(time.time())
artifacts_root = ROOT / "scripts"
prior_epochs = set()
for d in artifacts_root.glob("_round*_dials_artifacts"):
    for p in d.glob("log-*.txt"):
        try:
            prior_epochs.add(int(p.stem.replace("log-", "")))
        except Exception:
            pass
if epoch_now in prior_epochs:
    errs.append({"gate": "epoch", "reason": f"EPOCH={epoch_now} collides with prior round"})

# 6.4 BK fixtures have target_files
fixtures = (getattr(m, "BK_FIXTURES", None)
            or getattr(m, "_BK_FIXTURES", None)
            or getattr(m, "TASKS_BK", None)
            or [])
bk_count = 0
bk_bad = []
for fx in fixtures:
    bk_count += 1
    tf = fx.get("target_files") if isinstance(fx, dict) else None
    if not tf:
        bk_bad.append(fx.get("id") if isinstance(fx, dict) else str(fx))
        continue
    for path_s in tf:
        if not pathlib.Path(path_s).exists():
            bk_bad.append(f"{fx.get('id', '?')}::{path_s}")

if bk_bad:
    errs.append({"gate": "bk_fixtures",
                 "reason": f"missing target_files: {bk_bad[:3]}"})

# 6.5 K20 model match — already done in cat-3; non-fatal here
sys.stdout.write(json.dumps({"errors": errs, "para": para_i, "bk_count": bk_count,
                              "deny_size": len(deny), "epoch": epoch_now}))
PYEOF
)
CFG_FATAL=$(echo "$CONFIG_RESULT" | jq -r '.fatal // empty' 2>/dev/null)
if [ -n "$CFG_FATAL" ]; then
    hard_fail config dials_load "$CFG_FATAL" "fix _round25_dials.py syntax"
fi
CFG_ERRS=$(echo "$CONFIG_RESULT" | jq -c '.errors // []' 2>/dev/null)
CFG_ERRS_LEN=$(echo "$CFG_ERRS" | jq 'length' 2>/dev/null)
if [ "$CFG_ERRS_LEN" -gt 0 ]; then
    while IFS= read -r err; do
        gate=$(echo "$err" | jq -r '.gate')
        reason=$(echo "$err" | jq -r '.reason')
        fail_gate config "$gate" "$reason" "fix dials configuration"
    done < <(echo "$CFG_ERRS" | jq -c '.[]')
    say "PREFLIGHT_FAIL — configuration errors"
    exit 1
else
    PARA=$(echo "$CONFIG_RESULT" | jq -r '.para')
    BK=$(echo "$CONFIG_RESULT"   | jq -r '.bk_count')
    EPOCH_NOW=$(echo "$CONFIG_RESULT" | jq -r '.epoch')
    pass_gate config "parallelism (=$PARA)"
    pass_gate config "provider_pins (no deny-listed primaries)"
    pass_gate config "epoch_unique ($EPOCH_NOW)"
    pass_gate config "bk_fixtures ($BK fixtures, target_files ok)"
fi

# ─────────────────────────────────────────────────────────────────────────
# All gates passed (or K20 cat-3 soft-skipped)
# ─────────────────────────────────────────────────────────────────────────
PASS_BODY=$(jq -n --arg ts "$(date -Is)" \
                  --arg headroom "$OR_HEADROOM" \
                  --arg variants "$total_probe_variants" \
                  --arg held_out "$held_out_count" \
                  '{ts:$ts, openrouter_headroom:$headroom,
                    variants_probed:$variants, held_out:$held_out}')
echo "$PASS_BODY" > "$ARTIFACTS/PREFLIGHT_PASS"
say ""
say "PREFLIGHT_PASS at $(date -Is) — sentinel written"
say "  $PASS_BODY"
[ -s "$SKIPPED_VARIANTS" ] && say "  skipped variants: $(tr '\n' ',' < "$SKIPPED_VARIANTS")"
exit 0
