"""R26 P2-7 probe: does Together Serverless Multi-LoRA support Qwen3-Coder-30B-A3B-Instruct?

Reviewer caveat in the boost plan: Together's blog only names Llama-3.1
and Qwen-2.5 as supported bases for serverless multi-LoRA. The K20
fine-tune (ft-985a8266) targets Qwen/Qwen3-Coder-30B-A3B-Instruct, which
is NOT in the named families. This probe answers — before we migrate K20
from the dedicated endpoint ($7.98/hr) to serverless (per-token base) —
whether the base model is actually in the multi-LoRA roster.

Three checks:
1. GET /v1/models, find the entry for Qwen/Qwen3-Coder-30B-A3B-Instruct,
   inspect serverless / supports_lora_adapters / config.fine_tuning flags.
2. GET /v1/models for the K20 output model; confirm it exists + check
   its type/parent_model fields.
3. POST /v1/chat/completions with model=K20 output name (stripped of
   any dedicated-endpoint routing). If Together routes it through the
   serverless multi-LoRA path it succeeds + bills at base rate; if not,
   it errors with a clear "endpoint required" / "model not supported" /
   "404" message.

Verdict: SUPPORTED / NOT_SUPPORTED / UNKNOWN.

Cost: at most one short chat completion (~$0.001 if it works, $0 on
404). Total budget < $0.05.

Outputs:
- stdout: human-readable summary
- /home/daniel/repos/org-llm/docs/notes/2026-05-08-together-slm-probe.org
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO = Path("/home/daniel/repos/org-llm")
NOTE_PATH = REPO / "docs/notes/2026-05-08-together-slm-probe.org"

BASE_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
TOGETHER_BASE = "https://api.together.xyz"


def _pass_lookup(slug: str) -> str:
    res = subprocess.run(
        ["pass", "show", slug],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"pass lookup failed for {slug}: {res.stderr.strip()}"
        )
    return res.stdout.strip().splitlines()[0]


def _http(
    method: str,
    url: str,
    api_key: str,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> tuple[int, dict | str]:
    """Return (status, parsed_body_or_text). Never raises on HTTP errors."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Together is fronted by Cloudflare, which 403s the default
        # Python-urllib UA with "error code: 1010" — set a real UA.
        "User-Agent": "org-llm-probe/1.0 (+https://github.com/local)",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if hasattr(e, "read") else str(e)
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        return 0, f"URLError: {e}"


def check_base_model(api_key: str) -> dict[str, Any]:
    """Check the base model's metadata for serverless/multi-LoRA flags."""
    print(f"[1/3] GET /v1/models — looking for {BASE_MODEL}")
    status, body = _http("GET", f"{TOGETHER_BASE}/v1/models", api_key)
    out: dict[str, Any] = {
        "status": status,
        "found": False,
        "entry": None,
        "all_serverless_lora_models": [],
    }
    if status != 200 or not isinstance(body, list):
        out["error"] = f"models list returned {status}: {str(body)[:200]}"
        return out

    for m in body:
        if not isinstance(m, dict):
            continue
        # Together's /v1/models schema (verified 2026-05-08) does NOT
        # expose explicit serverless/lora flags. The proxy signals are:
        #   - pricing.input/output > 0  → serverless tier exists
        #   - pricing.hourly > 0        → dedicated-endpoint pricing
        # Even with input/output > 0, multi-LoRA support is a separate
        # roster managed server-side and is not in the model listing.
        # We collect the serverless-tier ids here for context only.
        pricing = m.get("pricing") or {}
        if (
            m.get("type") == "chat"
            and isinstance(pricing, dict)
            and (pricing.get("input") or 0) > 0
            and (pricing.get("output") or 0) > 0
        ):
            out["all_serverless_lora_models"].append(m.get("id"))

        if (m.get("id") or "").lower() == BASE_MODEL.lower():
            out["found"] = True
            out["entry"] = m

    return out


def check_k20_model(api_key: str, k20_name: str) -> dict[str, Any]:
    """Check that the K20 output model is registered + inspect type."""
    print(f"[2/3] GET /v1/models — looking for K20 output {k20_name}")
    status, body = _http("GET", f"{TOGETHER_BASE}/v1/models", api_key)
    out: dict[str, Any] = {"status": status, "found": False, "entry": None}
    if status != 200 or not isinstance(body, list):
        out["error"] = f"models list returned {status}: {str(body)[:200]}"
        return out
    for m in body:
        if not isinstance(m, dict):
            continue
        if (m.get("id") or "") == k20_name:
            out["found"] = True
            out["entry"] = m
            break
    return out


def attempt_serverless_inference(
    api_key: str, k20_name: str
) -> dict[str, Any]:
    """Attempt a real chat completion with model=K20 output name.

    If Together routes via serverless multi-LoRA, it succeeds and
    bills at base rate. Otherwise we get an error indicating either
    "needs dedicated endpoint" or "model not supported".
    """
    print(f"[3/3] POST /v1/chat/completions — model={k20_name}")
    body = {
        "model": k20_name,
        "messages": [
            {
                "role": "user",
                "content": "Reply with the single word PROBE and nothing else.",
            }
        ],
        "max_tokens": 10,
        "temperature": 0.0,
    }
    t0 = time.time()
    status, response = _http(
        "POST",
        f"{TOGETHER_BASE}/v1/chat/completions",
        api_key,
        body=body,
        timeout=60,
    )
    elapsed = time.time() - t0
    out: dict[str, Any] = {
        "status": status,
        "elapsed_s": round(elapsed, 2),
        "response": response,
    }
    if status == 200 and isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            out["content"] = choices[0].get("message", {}).get("content")
        out["usage"] = response.get("usage")
    return out


def classify(
    base_check: dict, k20_check: dict, infer: dict
) -> tuple[str, list[str]]:
    """Return (verdict, reasons[]).

    Verdict in {"SUPPORTED", "NOT_SUPPORTED", "UNKNOWN"}.
    """
    reasons: list[str] = []

    # Real inference call is the strongest signal
    inf_status = infer.get("status")
    if inf_status == 200 and infer.get("content"):
        reasons.append(
            f"chat-completions HTTP 200 in {infer['elapsed_s']}s with "
            f"non-empty content — Together routed the K20 adapter call "
            f"successfully without a dedicated endpoint"
        )
        return "SUPPORTED", reasons

    if isinstance(inf_status, int) and inf_status in (400, 403, 404, 422):
        # Inspect error message for known signals
        err_msg = ""
        if isinstance(infer.get("response"), dict):
            err_msg = (
                str(
                    infer["response"].get("error", {}).get("message")
                    or infer["response"].get("message")
                    or infer["response"]
                )
            ).lower()
        elif isinstance(infer.get("response"), str):
            err_msg = infer["response"].lower()
        reasons.append(
            f"chat-completions HTTP {inf_status}; error excerpt: "
            f"{err_msg[:240]!r}"
        )
        # Heuristics for "not supported" vs other failure modes
        if any(
            tok in err_msg
            for tok in (
                "not supported",
                "non-serverless",
                "dedicated",
                "endpoint required",
                "no available",
                "no serverless",
                "not deployed",
                "must be deployed",
                "serverless lora",
                "fine-tune is not",
                "lora not available",
            )
        ):
            return "NOT_SUPPORTED", reasons
        if "not found" in err_msg or inf_status == 404:
            reasons.append(
                "404 may mean either 'serverless multi-LoRA not enabled "
                "for this base' OR transient routing — fall back to "
                "metadata signal"
            )

    # Fall back to base-model metadata. Together's /v1/models schema
    # has no explicit serverless/lora flags; the strongest proxy is
    # pricing.input/output > 0 (public per-token rate exists).
    entry = base_check.get("entry")
    if entry:
        pricing = entry.get("pricing") or {}
        in_p = pricing.get("input") or 0
        out_p = pricing.get("output") or 0
        hourly = pricing.get("hourly") or 0
        is_serverless = in_p > 0 and out_p > 0
        reasons.append(
            f"base-model pricing: input={in_p} output={out_p} "
            f"hourly={hourly} → serverless_tier={is_serverless}"
        )
        if not is_serverless:
            return "NOT_SUPPORTED", reasons + [
                "base has no public per-token serverless pricing — "
                "multi-LoRA can't ride a tier that doesn't exist"
            ]
        # serverless tier exists but multi-LoRA roster membership is
        # not visible from metadata alone
        return "UNKNOWN", reasons + [
            "base is on serverless tier but multi-LoRA roster "
            "membership is not exposed in /v1/models — needs the "
            "real inference probe to confirm"
        ]
    else:
        reasons.append("base-model entry NOT FOUND in /v1/models response")
        return "NOT_SUPPORTED", reasons

    return "UNKNOWN", reasons


def write_note(
    verdict: str,
    reasons: list[str],
    base_check: dict,
    k20_check: dict,
    infer: dict,
    k20_name: str,
) -> None:
    serverless_lora_ids = base_check.get("all_serverless_lora_models") or []

    base_entry = base_check.get("entry")
    if base_entry:
        pricing = base_entry.get("pricing") or {}
        base_summary_lines = [
            f"- id: ={base_entry.get('id')}=",
            f"- type: ={base_entry.get('type')}=",
            f"- display_name: ={base_entry.get('display_name')}=",
            f"- context_length: ={base_entry.get('context_length')}=",
            f"- running: ={base_entry.get('running')}=",
            f"- uuid (endpoint id, if any): ={base_entry.get('uuid')}=",
            f"- pricing.input: ={pricing.get('input')}= (>0 = public serverless inference)",
            f"- pricing.output: ={pricing.get('output')}=",
            f"- pricing.hourly: ={pricing.get('hourly')}= (>0 = dedicated only)",
            f"- pricing.finetune: ={pricing.get('finetune')}=",
        ]
    else:
        base_summary_lines = ["- /not found in /v1/models response/"]

    k20_entry = k20_check.get("entry")
    if k20_entry:
        k20_pricing = k20_entry.get("pricing") or {}
        k20_summary_lines = [
            f"- id: ={k20_entry.get('id')}=",
            f"- type: ={k20_entry.get('type')}=",
            f"- display_name: ={k20_entry.get('display_name')}=",
            f"- uuid (endpoint id, if any): ={k20_entry.get('uuid')}=",
            f"- pricing.input: ={k20_pricing.get('input')}=",
            f"- pricing.output: ={k20_pricing.get('output')}=",
            f"- pricing.hourly: ={k20_pricing.get('hourly')}=",
        ]
    else:
        k20_summary_lines = [
            "- /not found in /v1/models response/ — likely scoped to "
            "the dedicated endpoint only"
        ]

    if verdict == "SUPPORTED":
        recommendation = (
            "*MIGRATE.* K20 can stop using the dedicated endpoint. "
            "Update =scripts/_round26_dials.py= to drop the dedicated "
            "endpoint resume + pause hooks; route K20 through Together "
            "serverless. The pause helper continues to work but cost "
            "drops to per-token base (~$0.30 / 50 cells vs. ~$30 / 5h "
            "reserve)."
        )
    elif verdict == "NOT_SUPPORTED":
        recommendation = (
            "*KEEP DEDICATED ENDPOINT.* Qwen3-Coder-30B-A3B-Instruct is "
            "NOT in Together's serverless multi-LoRA roster. The R25 "
            "boost-plan §3 fallback is now load-bearing: stay on the "
            "dedicated H100 endpoint for R26. R27 K20 v2 should retrain "
            "on a confirmed-supported base — Llama-3.1-8B-Instruct or "
            "Qwen-2.5-32B-Instruct. Cross-reference: blog only names "
            "Llama 3.1 + Qwen 2.5 families."
        )
    else:
        recommendation = (
            "*INVESTIGATE.* Probe was inconclusive. Re-run after the "
            "dedicated endpoint is paused (so the inference path can't "
            "be served by it), and inspect the precise error from "
            "/v1/chat/completions. Pending resolution: keep dedicated "
            "endpoint; do NOT migrate."
        )

    inf_status = infer.get("status")
    inf_resp = infer.get("response")
    if isinstance(inf_resp, (dict, list)):
        inf_resp_str = json.dumps(inf_resp, indent=2)[:1500]
    else:
        inf_resp_str = str(inf_resp)[:1500]

    content = f"""\
:PROPERTIES:
:ID:       2026-05-08-together-slm-probe
:CREATED:  [2026-05-08]
:END:
#+TITLE: P2-7 probe — Together Serverless Multi-LoRA support for Qwen3-Coder-30B-A3B-Instruct
#+FILETAGS: :notes:bench:r26:lora:p2-7:
#+OPTIONS: toc:1 num:nil
#+CATEGORY: bench

#+STARTUP: overview content

* Summary

*Summary.* Verdict: */{verdict}/*. Probe of Together's API confirms
whether =Qwen/Qwen3-Coder-30B-A3B-Instruct= (the K20 base) is in the
serverless multi-LoRA roster. Reviewer caveat in the boost plan
(=docs/wiki/2026-05-08-r26-boost-plan.org= §3) flagged that the
official blog only names Llama-3.1 and Qwen-2.5 — this probe answers
the question for our specific base.

*Expanded.* Three checks: (1) base-model metadata via =GET /v1/models=
(Together's schema exposes =pricing.input/output/hourly= but no
explicit serverless or multi-LoRA flags, so we read pricing as the
proxy); (2) K20 output-model presence in the same listing; (3) a real
inference call to =POST /v1/chat/completions= with =model={k20_name}=
— Together's reply (success vs. concrete error) is the strongest
signal. Note: Together is fronted by Cloudflare and 403s the default
=Python-urllib= UA with "error code: 1010"; the probe sets a
=User-Agent= header explicitly.

* Verdict

*{verdict}.*

Reasons:
{chr(10).join(f"- {r}" for r in reasons)}

* Recommendation

{recommendation}

* Probe details

** Base model — ={BASE_MODEL}=

{chr(10).join(base_summary_lines)}

** K20 output model — ={k20_name}=

{chr(10).join(k20_summary_lines)}

** Inference attempt — POST /v1/chat/completions

- HTTP status: ={inf_status}=
- Elapsed: ={infer.get("elapsed_s")}s=
- Response (truncated):

#+begin_src json
{inf_resp_str}
#+end_src

** Other Together chat models on the public serverless tier

({len(serverless_lora_ids)} found via pricing heuristic — heuristic
matches =type=chat= AND =pricing.input>0= AND =pricing.output>0=.
Note: serverless tier does NOT imply multi-LoRA roster membership;
that's a separate server-side allow-list not exposed in /v1/models.)

{chr(10).join(f"- ={mid}=" for mid in serverless_lora_ids[:30]) or "- /none — heuristic returned empty list (Together may use different field names)/"}
{"- ... and " + str(len(serverless_lora_ids) - 30) + " more" if len(serverless_lora_ids) > 30 else ""}

* Cross-references

- =docs/wiki/2026-05-08-r26-boost-plan.org= §3 — Together serverless
  multi-LoRA pivot (with reviewer caveat naming only Llama-3.1 + Qwen-2.5)
- =docs/wiki/2026-05-08-r26-launch-checklist.org= §P2-7 — checklist item
- =scripts/_p2_7_probe_together_slm.py= — this probe
- =scripts/_k20_endpoint_resume.sh= + =_k20_endpoint_pause.sh= — dedicated
  endpoint helpers (kept regardless of verdict)
- K20 fine-tune job: =ft-985a8266-3945= → output model
  ={k20_name}=
"""
    NOTE_PATH.write_text(content)
    print(f"  wrote {NOTE_PATH}")


def main() -> int:
    print("== P2-7 probe: Together Serverless Multi-LoRA for Qwen3-Coder-30B ==")
    api_key = _pass_lookup("org-llm/cloud/together/api-key")
    k20_name = _pass_lookup("org-llm/cloud/together/k20-output-model")
    print(f"  K20 output model: {k20_name}")

    base_check = check_base_model(api_key)
    print(f"  base found: {base_check.get('found')}; "
          f"serverless+lora model count via heuristic: "
          f"{len(base_check.get('all_serverless_lora_models') or [])}")

    k20_check = check_k20_model(api_key, k20_name)
    print(f"  K20 found in /v1/models: {k20_check.get('found')}")

    infer = attempt_serverless_inference(api_key, k20_name)
    print(f"  inference HTTP {infer.get('status')} in "
          f"{infer.get('elapsed_s')}s")

    verdict, reasons = classify(base_check, k20_check, infer)
    print()
    print(f"VERDICT: {verdict}")
    for r in reasons:
        print(f"  - {r}")

    write_note(verdict, reasons, base_check, k20_check, infer, k20_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
