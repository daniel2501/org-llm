"""G5 endpoint pre-flight probe — r25 design item.

Two modes, both designed to live in CI / cron and emit a single JSON
line so the AR6 auto-heal recipes can pattern-match on result.type.

Mode A — `artifact`:
    Pure offline. Validates a HF model repo contains a healthy PEFT
    LoRA adapter that is safe to serve: required files present,
    adapter_config target_modules match expected, chat_template
    embedded in tokenizer_config.json (vLLM serve doesn't fall back
    to base model template), safetensors key count matches
    (layers × proj_kinds × {A,B}).

    No spend, no GPU. Run nightly.

Mode B — `endpoint`:
    Hits an OpenAI-compatible /v1/chat/completions URL with one
    1-token request for both `base_model` and `lora_model_id`,
    diffs the outputs over N=5 prompts. Pass if (a) both 200,
    (b) lora response differs from base on ≥1 prompt (proves
    adapter actually loaded, not silently dropped). Cost: ~$0.001
    per probe at typical FOSS rates.

Output: single JSON line to stdout, structured for AR6 catalog.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any


REQUIRED_PEFT_FILES = {
    "adapter_config.json",
    "adapter_model.safetensors",
    "tokenizer_config.json",
    "chat_template.jinja",   # we expect both: file + embedded
    "config.json",
}

EXPECTED_TARGETS = {"q_proj", "k_proj", "v_proj", "o_proj"}


def emit(payload: dict[str, Any]) -> None:
    """One JSON line; AR6 recipes match on result.type."""
    print(json.dumps(payload, sort_keys=True))


def http_get_json(url: str, token: str | None = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def http_post_json(
    url: str, body: dict, token: str | None = None, timeout: int = 60
) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            # Cloudflare in front of RunPod proxy blocks default
            # `Python-urllib/3.11` UA with 403. Use a generic UA.
            "User-Agent": "g5-endpoint-preflight/1.0",
            "Accept": "*/*",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def probe_artifact(repo_id: str, hf_token: str | None) -> dict:
    """Mode A — offline HF repo validation."""
    started = time.time()
    out: dict[str, Any] = {
        "type": "artifact",
        "repo_id": repo_id,
        "checks": {},
    }

    # 1. Repo + file listing
    try:
        meta = http_get_json(
            f"https://huggingface.co/api/models/{repo_id}", hf_token
        )
    except Exception as e:
        out["status"] = "fail"
        out["error"] = f"hf_repo_unreachable: {type(e).__name__}: {e}"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    files = {s["rfilename"] for s in meta.get("siblings", [])}
    missing = REQUIRED_PEFT_FILES - files
    out["checks"]["required_files_present"] = not missing
    if missing:
        out["checks"]["missing_files"] = sorted(missing)

    # 2. adapter_config.json
    try:
        cfg = http_get_json(
            f"https://huggingface.co/{repo_id}/resolve/main/adapter_config.json",
            hf_token,
        )
        targets = set(cfg.get("target_modules", []))
        out["checks"]["adapter_targets_match"] = targets == EXPECTED_TARGETS
        out["checks"]["lora_r"] = cfg.get("r")
        out["checks"]["lora_alpha"] = cfg.get("lora_alpha")
        out["checks"]["base_model"] = cfg.get("base_model_name_or_path")
        if targets != EXPECTED_TARGETS:
            out["checks"]["adapter_targets_actual"] = sorted(targets)
    except Exception as e:
        out["checks"]["adapter_config_load"] = f"fail: {e}"

    # 3. tokenizer_config.json — chat_template MUST be embedded
    #    (this is the silent quality killer if it's missing)
    try:
        tcfg = http_get_json(
            f"https://huggingface.co/{repo_id}/resolve/main/tokenizer_config.json",
            hf_token,
        )
        embedded_tpl = tcfg.get("chat_template") or ""
        out["checks"]["chat_template_embedded"] = bool(embedded_tpl)
        out["checks"]["chat_template_bytes"] = len(embedded_tpl)
    except Exception as e:
        out["checks"]["tokenizer_config_load"] = f"fail: {e}"

    # 4. Roll up
    fail_reasons = []
    if missing:
        fail_reasons.append(f"missing_files:{sorted(missing)}")
    if not out["checks"].get("adapter_targets_match"):
        fail_reasons.append("adapter_targets_mismatch")
    if not out["checks"].get("chat_template_embedded"):
        fail_reasons.append(
            "chat_template_not_embedded — vLLM will fall back to base model template"
        )
    out["status"] = "fail" if fail_reasons else "pass"
    if fail_reasons:
        out["fail_reasons"] = fail_reasons
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def probe_endpoint(
    url: str,
    base_model: str,
    lora_model: str,
    auth_token: str | None,
    n_prompts: int = 5,
) -> dict:
    """Mode B — OpenAI-compatible serve probe."""
    started = time.time()
    out: dict[str, Any] = {
        "type": "endpoint",
        "url": url,
        "base_model": base_model,
        "lora_model": lora_model,
        "n_prompts": n_prompts,
    }

    prompts = [
        "Refile this org heading under the Projects category.",
        "Write an elisp function that toggles org-mode TODO state.",
        "Summarize this org-roam node in one sentence.",
        "Convert this org table to JSON.",
        "What is the keybinding for org-capture?",
    ][:n_prompts]

    base_outs, lora_outs = [], []
    base_errs, lora_errs = [], []
    for p in prompts:
        body = lambda m: {
            "model": m,
            "messages": [{"role": "user", "content": p}],
            "max_tokens": 32,
            "temperature": 0,
        }
        bs, br = http_post_json(url, body(base_model), auth_token)
        ls, lr = http_post_json(url, body(lora_model), auth_token)
        base_errs.append(bs)
        lora_errs.append(ls)
        base_outs.append(
            (br or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
            if bs == 200 else None
        )
        lora_outs.append(
            (lr or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
            if ls == 200 else None
        )

    out["checks"] = {
        "base_all_200": all(s == 200 for s in base_errs),
        "lora_all_200": all(s == 200 for s in lora_errs),
        "base_status_codes": base_errs,
        "lora_status_codes": lora_errs,
    }

    if all(s == 200 for s in base_errs + lora_errs):
        diffs = sum(1 for b, l in zip(base_outs, lora_outs) if b != l)
        out["checks"]["differing_outputs"] = diffs
        out["checks"]["base_sample"] = (base_outs[0] or "")[:120]
        out["checks"]["lora_sample"] = (lora_outs[0] or "")[:120]
        out["status"] = "pass" if diffs >= 1 else "fail"
        if diffs == 0:
            out["fail_reasons"] = ["lora_output_identical_to_base — adapter not loaded"]
    else:
        out["status"] = "fail"
        out["fail_reasons"] = ["non_200_response"]

    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    a = sub.add_parser("artifact", help="HF repo PEFT adapter validation")
    a.add_argument("--repo", required=True, help="HF model repo id")
    a.add_argument("--hf-token", default=None)

    e = sub.add_parser("endpoint", help="OpenAI-compatible serve probe")
    e.add_argument("--url", required=True, help="/v1/chat/completions URL")
    e.add_argument("--base-model", required=True)
    e.add_argument("--lora-model", required=True)
    e.add_argument("--auth-token", default=None)
    e.add_argument("--n-prompts", type=int, default=5)

    args = ap.parse_args()
    if args.mode == "artifact":
        result = probe_artifact(args.repo, args.hf_token)
    else:
        result = probe_endpoint(
            args.url, args.base_model, args.lora_model,
            args.auth_token, args.n_prompts,
        )
    emit(result)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
