"""AR6 — LoRA deploy auto-heal carryover catalog.

Parallel to scripts/_lora_progress_logger.sh's training-side recipes,
but for the /serve/ phase. Given a deploy attempt's HTTP status +
error JSON, returns a structured suggestion: (recipe_id, fix_action,
retry_with_changes). Used by smoke / probe / production deploy
flows to make the next attempt smarter.

Pure data + classifier; no side effects. Apply the suggestion
yourself via your existing launcher.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class HealRecipe:
    id: str
    pattern: re.Pattern
    fix_action: str
    retry_with: dict[str, Any]
    rationale: str

    def matches(self, error_text: str) -> bool:
        return bool(self.pattern.search(error_text))


# Each recipe maps a known failure to a concrete next-attempt change.
# Order matters — first match wins, so put more-specific recipes first.
RECIPES: list[HealRecipe] = [
    # ── Together-side ────────────────────────────────────────────────
    HealRecipe(
        id="together_non_serverless_model",
        pattern=re.compile(r"Unable to access non-serverless model", re.I),
        fix_action="Together account tier doesn't host this fine-tune. "
                   "Pull adapter via `together fine-tuning download -c adapter` "
                   "and serve on RunPod vLLM with `--enable-lora`.",
        retry_with={"provider": "runpod", "serve_strategy": "vllm_lora_modules"},
        rationale="K20 v1 pre-flight 2026-05-08: Together returned this 14× "
                  "across all hardware × shape combos. Account-tier gate, not "
                  "hardware shortage. Adapter weights are downloadable; "
                  "Together's serverless tier is the dead-end.",
    ),
    HealRecipe(
        id="together_hardware_unavailable",
        pattern=re.compile(r"hardware request not available", re.I),
        fix_action="Together pool exhausted for this hardware tier. "
                   "Bump to next-larger SKU OR switch provider to RunPod / Modal.",
        retry_with={"hardware_bump": True, "fallback_provider": "runpod"},
        rationale="Together's capacity pool varies by region/hour; if a tier "
                  "shows unavailable for >2 attempts, switching providers "
                  "is faster than waiting.",
    ),
    HealRecipe(
        id="together_endpoint_parse",
        pattern=re.compile(r"Error parsing endpoint request", re.I),
        fix_action="Together API quirk: this happens on the `flat` deploy "
                   "shape (top-level min/max_replicas). Switch to "
                   "`autoscaling` shape (nested under `autoscaling.{min,max}_replicas`).",
        retry_with={"shape": "autoscaling"},
        rationale="Observed 8× in K20 v1 deploy attempts — only on `flat` shape, "
                  "never on `autoscaling` or `sdk`.",
    ),

    # ── RunPod REST API ──────────────────────────────────────────────
    HealRecipe(
        id="runpod_post_pods_201_not_200",
        pattern=re.compile(r"launch failed: code=201", re.I),
        fix_action="RunPod REST returns HTTP 201 Created (not 200) on success. "
                   "Update launcher to accept (200, 201). The pod IS running — "
                   "do NOT relaunch; attach to the returned `id` and proceed.",
        retry_with={"action": "attach_to_existing", "do_not_relaunch": True},
        rationale="2026-05-08 — wasted ~$0.05 on a pod that 'failed' but was "
                  "actually running. Treat 201 as success; teardown if abandoning.",
    ),
    HealRecipe(
        id="runpod_post_pods_empty_body_launches",
        pattern=re.compile(r"runpod.*empty.*body|defaults.*launched", re.I),
        fix_action="RunPod REST `POST /v1/pods` with `{}` LAUNCHES a default pod "
                   "($1.89/hr). No dry-run mode. Always include explicit "
                   "gpuTypeIds + imageName even when probing schema.",
        retry_with={"requires": ["gpuTypeIds", "imageName"]},
        rationale="2026-05-08 — schema probe accidentally launched a pytorch pod. "
                  "Terminated within 30s. There is no safe way to validate "
                  "schema against the live API.",
    ),

    # ── vLLM serve issues ────────────────────────────────────────────
    HealRecipe(
        id="vllm_lora_rank_too_low",
        pattern=re.compile(r"max_lora_rank.*\d+.*does not match|lora rank.*exceeds", re.I),
        fix_action="vLLM `--max-lora-rank` is below the adapter's r value. "
                   "Re-launch with `--max-lora-rank` >= adapter `r` (16 for K20 v1).",
        retry_with={"max_lora_rank": 32},
        rationale="vLLM rejects loading at serve time, not launch — silent burn "
                  "of pod boot time if not caught.",
    ),
    HealRecipe(
        id="vllm_chat_template_mismatch",
        pattern=re.compile(r"chat_template.*not.*found|tokenizer.*template.*missing", re.I),
        fix_action="vLLM couldn't find chat_template. Either inject it into "
                   "tokenizer_config.json on the HF repo, OR pass "
                   "--chat-template /path/to/chat_template.jinja at serve time.",
        retry_with={"chat_template_arg": True},
        rationale="K20 v1 had this — Together's adapter ships chat_template as a "
                  "separate file, not embedded in tokenizer_config.json. vLLM "
                  "auto-loads from tokenizer_config only.",
    ),
    HealRecipe(
        id="runpod_proxy_403_means_container_alive_but_no_app",
        pattern=re.compile(
            r"proxy\.runpod\.net.*\b403\b|/v1/models\s+403", re.I
        ),
        fix_action="RunPod proxy 403 (not 5xx) usually means container is "
                   "alive and CPU-busy but the listening port hasn't been "
                   "claimed yet — typically HF download retry-loop, OR app "
                   "crashed at boot. Check GraphQL runtime: cpu=99% gpu=0% "
                   "for >5 min = stuck (download crash-loop or ENOSPC).",
        retry_with={"diagnostic": "graphql_runtime_stats"},
        rationale="2026-05-08 — vllm:v0.20.1 pod stayed at 403 for 13+ min. "
                  "Runtime showed cpu=99% gpu=0% memory=2% — confirmed not "
                  "actually serving. Root cause: HF_HUB_ENABLE_HF_TRANSFER=1 "
                  "without hf_transfer pkg installed in image.",
    ),
    HealRecipe(
        id="hf_transfer_missing",
        pattern=re.compile(
            r"hf_transfer.*not (installed|available)|Fast download.*disabled",
            re.I,
        ),
        fix_action="HF_HUB_ENABLE_HF_TRANSFER=1 was set but hf_transfer "
                   "package is not in the image. Either: (a) drop the env var "
                   "(falls back to slower stdlib downloads), OR (b) bake "
                   "`pip install hf_transfer` into a custom image.",
        retry_with={"env_remove": ["HF_HUB_ENABLE_HF_TRANSFER"]},
        rationale="2026-05-08 — vllm/vllm-openai stock image does NOT ship "
                  "hf_transfer. Setting the env without the package causes "
                  "huggingface_hub to error in download retry-loops, "
                  "burning pod time at 99% CPU with no progress.",
    ),
    HealRecipe(
        id="runpod_container_disk_too_small",
        pattern=re.compile(
            r"No space left on device|ENOSPC|disk.*full|errno 28", re.I
        ),
        fix_action="containerDiskInGb too small for model + image + HF tmp "
                   "cache. Rule of thumb: image_gb + 2 × model_gb + 20GB "
                   "buffer. For 30B BF16 (~60GB) on vllm image (~10GB): "
                   "use containerDiskInGb >= 150, OR mount a network volume "
                   "at /root/.cache/huggingface.",
        retry_with={"containerDiskInGb": 150},
        rationale="2026-05-08 docs — RunPod recommends 200GB container disk "
                  "for ~27B models. HF caches in tmp dir during download, "
                  "doubling peak space need.",
    ),
    HealRecipe(
        id="vllm_image_too_old_for_qwen3",
        pattern=re.compile(
            r"qwen3.*not recognized|qwen3_moe.*does not.*recognize|model type.*qwen3"
            r"|runtime.*null.*5.*min|container init stuck",
            re.I,
        ),
        fix_action="vLLM image transformers version predates Qwen3MoE support. "
                   "Bump to vllm/vllm-openai:v0.20.1 or later (transformers >= 4.51).",
        retry_with={"image": "vllm/vllm-openai:v0.20.1"},
        rationale="2026-05-08 — vllm:v0.6.5 (Dec 2024, transformers 4.46.3) "
                  "stuck on from_pretrained for Qwen3-Coder-30B-A3B-Instruct. "
                  "Pod showed RUNNING but runtime stayed null (no container "
                  "process). Same symptom as R19 training auto-heal recipe.",
    ),
    HealRecipe(
        id="vllm_oom",
        pattern=re.compile(r"CUDA out of memory|OutOfMemoryError|torch.*OOM", re.I),
        fix_action="Reduce --max-model-len OR drop --gpu-memory-utilization "
                   "OR bump to next GPU tier (80GB → 2x80GB or H100).",
        retry_with={"max_model_len": 4096, "gpu_memory_utilization": 0.85},
        rationale="vLLM pre-allocates KV cache per max_model_len. For 30B BF16 "
                  "on a single 80GB GPU, max_model_len=8192 is the safe ceiling.",
    ),

    # ── HF Hub / network ─────────────────────────────────────────────
    HealRecipe(
        id="cloudflare_403_python_urllib_ua",
        pattern=re.compile(
            r"Python-urllib.*forbidden|cloudflare.*403|/v1/chat/completions.*403",
            re.I,
        ),
        fix_action="Cloudflare in front of RunPod proxy blocks the default "
                   "`Python-urllib/3.x` User-Agent with 403. Set a generic "
                   "User-Agent header (e.g. 'g5-endpoint-preflight/1.0') on "
                   "every POST. GET requests pass without this — only POST "
                   "is gated.",
        retry_with={"add_header": {"User-Agent": "g5-endpoint-preflight/1.0"}},
        rationale="2026-05-08 — confirmed: GET /v1/models returns 200 from "
                  "urllib, but POST /v1/chat/completions returns 403. Same "
                  "POST via curl with default UA works fine. Cloudflare "
                  "bot-detection on POST writes only.",
    ),
    HealRecipe(
        id="hf_401_invalid_token",
        pattern=re.compile(r"401 (Client )?Error|InvalidUserToken|HfHubHTTPError.*401", re.I),
        fix_action="HF token invalid or missing in pod env. Re-fetch from "
                   "`pass org-llm/cloud/huggingface/token` and pass via "
                   "env={'HF_TOKEN': tok} in launch body.",
        retry_with={"refresh_hf_token": True},
        rationale="HF rotates tokens periodically; private adapter repos fail "
                  "without a current one.",
    ),
    HealRecipe(
        id="hf_repo_not_found",
        pattern=re.compile(r"Repository.*not found|404.*huggingface", re.I),
        fix_action="HF adapter repo doesn't exist or is private and pod has no "
                   "token. Verify with `hf model-info <repo>` from your shell, "
                   "make public if private, OR ensure HF_TOKEN propagates.",
        retry_with={},
        rationale="Easy mistake when adapter repo name has a typo or hasn't been "
                  "pushed yet.",
    ),
]


def diagnose(error_text: str) -> dict:
    """Match error text against catalog; return first hit + suggestions."""
    for r in RECIPES:
        if r.matches(error_text):
            return {
                "matched": True,
                "recipe_id": r.id,
                "fix_action": r.fix_action,
                "retry_with": r.retry_with,
                "rationale": r.rationale,
            }
    return {
        "matched": False,
        "recipe_id": None,
        "fix_action": "No known recipe matched. Manual triage needed.",
        "retry_with": {},
        "rationale": "Unrecognized failure pattern — consider adding a recipe.",
    }


def main() -> int:
    args = sys.argv[1:]
    if args:
        text = " ".join(args)
    else:
        text = sys.stdin.read()
    if not text.strip():
        print(
            "usage: echo '<error text>' | python3 _lora_deploy_autoheal.py\n"
            "   or: python3 _lora_deploy_autoheal.py '<error text>'\n"
            "\n"
            f"Catalog has {len(RECIPES)} recipes:",
            file=sys.stderr,
        )
        for r in RECIPES:
            print(f"  - {r.id}", file=sys.stderr)
        return 2
    print(json.dumps(diagnose(text), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
