# [[file:../../../org/20260425230731-org_llm.org::*cloud.py][cloud.py:1]]
"""Cloud GPU provider registry — automatic compute expansion beyond local Ollama."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import NamedTuple


# ── Provider registry ──────────────────────────────────────────────────────────

class ProviderInfo(NamedTuple):
    slug:           str          # config key prefix / ID
    name:           str          # display name
    signup_url:     str          # referral/signup link
    console_url:    str          # manage instances
    docs_url:       str          # Ollama / API setup docs
    api_compat:     str          # "ollama" | "openai" | "both"
    endpoint_hint:  str          # template for endpoint URL
    gpu_costs:      dict         # GPU name → $/hr approximate spot
    description:    str          # one-line summary


PROVIDERS: list[ProviderInfo] = [
    ProviderInfo(
        slug        = "runpod",
        name        = "RunPod",
        signup_url  = "https://www.runpod.io/?ref=org-llm-cli",
        console_url = "https://www.runpod.io/console/pods",
        docs_url    = "https://www.runpod.io/console/explore",
        api_compat  = "both",
        endpoint_hint = "https://{pod_id}-11434.proxy.runpod.net",
        gpu_costs   = {
            "RTX 3090":      0.37,
            "RTX 4090":      0.74,
            "RTX A6000":     0.79,
            "A100 40GB":     1.64,
            "A100 80GB":     1.89,
            "H100 PCIe":     2.49,
            "H100 80GB SXM": 2.79,
        },
        description = "Popular GPU cloud; Ollama templates; pay-per-second billing",
    ),
    ProviderInfo(
        slug        = "vast",
        name        = "Vast.ai",
        signup_url  = "https://cloud.vast.ai/",
        console_url = "https://cloud.vast.ai/",
        docs_url    = "https://vast.ai/docs/",
        api_compat  = "ollama",
        endpoint_hint = "http://{host}:{port}",
        gpu_costs   = {
            "RTX 3080":      0.14,
            "RTX 3090":      0.18,
            "RTX 4090":      0.35,
            "A100 80GB":     1.20,
            "H100 80GB SXM": 2.10,
        },
        description = "GPU marketplace — bid on spot; often 40–60% cheaper than RunPod",
    ),
    ProviderInfo(
        slug        = "lambda",
        name        = "Lambda Labs",
        signup_url  = "https://lambdalabs.com/service/gpu-cloud",
        console_url = "https://cloud.lambdalabs.com/instances",
        docs_url    = "https://docs.lambdalabs.com/on-demand-cloud/",
        api_compat  = "openai",
        endpoint_hint = "https://api.lambdalabs.com/v1",
        gpu_costs   = {
            "A10":           0.75,
            "A100 40GB SXM": 1.29,
            "A100 80GB SXM": 1.99,
            "H100 80GB SXM": 2.49,
        },
        description = "Reliable on-demand GPU cloud; strong SLA; OpenAI-compatible inference API",
    ),
    ProviderInfo(
        slug        = "tensordock",
        name        = "TensorDock",
        signup_url  = "https://tensordock.com/",
        console_url = "https://marketplace.tensordock.com/",
        docs_url    = "https://tensordock.com/docs/",
        api_compat  = "ollama",
        endpoint_hint = "http://{host}:{port}",
        gpu_costs   = {
            "RTX 3090":      0.22,
            "RTX 4090":      0.40,
            "A100 80GB":     1.35,
            "H100 80GB SXM": 2.20,
        },
        description = "Low-cost GPU marketplace; deploy Ollama containers; spot instances",
    ),
    ProviderInfo(
        slug        = "salad",
        name        = "Salad Cloud",
        signup_url  = "https://salad.com/",
        console_url = "https://portal.salad.com/",
        docs_url    = "https://docs.salad.com/",
        api_compat  = "openai",
        endpoint_hint = "https://{container_id}.salad.cloud",
        gpu_costs   = {
            "RTX 3080":      0.08,
            "RTX 3090":      0.12,
            "RTX 4090":      0.28,
            "A100 80GB":     0.80,
        },
        description = "Distributed consumer GPU network — lowest rates; best for batch inference",
    ),
    ProviderInfo(
        slug        = "paperspace",
        name        = "Paperspace (DigitalOcean GPU)",
        signup_url  = "https://www.paperspace.com/gpu-cloud",
        console_url = "https://console.paperspace.com/",
        docs_url    = "https://docs.paperspace.com/",
        api_compat  = "both",
        endpoint_hint = "https://{deployment_url}",
        gpu_costs   = {
            "A100 80GB":     3.09,
            "H100 80GB":     4.50,
        },
        description = "Managed GPU cloud; Gradient notebooks + deployments; DigitalOcean-backed",
    ),
    ProviderInfo(
        slug        = "coreweave",
        name        = "CoreWeave",
        signup_url  = "https://www.coreweave.com/",
        console_url = "https://cloud.coreweave.com/",
        docs_url    = "https://docs.coreweave.com/",
        api_compat  = "openai",
        endpoint_hint = "https://{service}.coreweave.cloud",
        gpu_costs   = {
            "RTX A6000":     0.80,
            "A100 80GB SXM": 2.06,
            "H100 80GB SXM": 2.99,
        },
        description = "Enterprise-grade GPU cloud; Kubernetes-native; highest uptime SLA",
    ),
]

PROVIDER_MAP: dict[str, ProviderInfo] = {p.slug: p for p in PROVIDERS}


def get_provider(slug: str) -> ProviderInfo | None:
    return PROVIDER_MAP.get(slug)


# ── Approximate model VRAM requirements (GB) ──────────────────────────────────
MODEL_VRAM: list[tuple[str, float]] = [
    ("1b",              1.5),
    ("3b",              3.0),
    ("7b",              5.5),
    ("8b",              6.0),
    ("phi4",            9.0),
    ("13b",            10.0),
    ("mistral",         5.5),
    ("gemma3",          6.0),
    ("llama3.2",        3.5),
    ("llama3.3",       48.0),
    ("llama3.1",        5.5),
    ("qwen2.5-coder",   5.5),
    ("deepseek-r1",    48.0),
    ("nomic-embed",     0.5),
    ("phi3",            4.0),
    ("mistral-nemo",    8.5),
    ("qwq",            22.0),
]


class CloudStatus(NamedTuple):
    provider:   str
    endpoint:   str
    model:      str
    reachable:  bool
    auth_ok:    bool
    latency_ms: float | None


# ── Hardware assessment ────────────────────────────────────────────────────────

def local_vram_gb() -> float | None:
    """Return available GPU VRAM in GB, or None if no GPU / nvidia-smi unavailable."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        return sum(float(x.strip()) for x in out.strip().splitlines() if x.strip()) / 1024
    except Exception:
        return None


def local_ram_gb() -> float:
    """Return total system RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1_048_576
    except Exception:
        pass
    import shutil
    return shutil.disk_usage("/").total / 1e9


def model_needs_vram(model_name: str) -> float:
    name = model_name.lower()
    for fragment, vram in MODEL_VRAM:
        if fragment in name:
            return vram
    return 5.5  # assume 7B-class


def assess_local_capability(models: list[str]) -> list[dict]:
    vram = local_vram_gb()
    ram  = local_ram_gb()
    results = []
    for model in models:
        needed = model_needs_vram(model)
        if vram is not None:
            can_local = vram >= needed
            resource  = f"{vram:.0f} GB VRAM available"
        else:
            can_local = (ram * 0.6) >= needed
            resource  = f"{ram:.0f} GB RAM (CPU-only)"
        results.append({
            "model":       model,
            "vram_needed": needed,
            "can_local":   can_local,
            "resource":    resource,
            "reason":      "✓ fits locally" if can_local else f"needs {needed:.0f}GB, recommend cloud",
        })
    return results


# ── Connection check ───────────────────────────────────────────────────────────

def check_connection(endpoint_url: str, api_key: str = "", model: str = "") -> CloudStatus:
    """Ping a cloud Ollama/OpenAI-compatible endpoint and return status."""
    import time
    url = endpoint_url.rstrip("/")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for path in ("/api/tags", "/v1/models"):
        try:
            req = urllib.request.Request(f"{url}{path}", headers=headers, method="GET")
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=8) as resp:
                latency = (time.monotonic() - t0) * 1000
                json.loads(resp.read())
                return CloudStatus("configured", endpoint_url, model, True, True, latency)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return CloudStatus("configured", endpoint_url, model, True, False, None)
            continue
        except Exception:
            continue

    return CloudStatus("configured", endpoint_url, model, False, False, None)


# ── Chat / embed via cloud ─────────────────────────────────────────────────────

def cloud_chat(
    prompt: str, model: str, endpoint_url: str,
    api_key: str = "", system: str = "",
) -> str:
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode()

    for path in ("/api/chat", "/v1/chat/completions"):
        try:
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                if "message" in data:
                    return data["message"]["content"]
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
        except Exception:
            continue
    raise RuntimeError(f"Cloud chat failed — endpoint {endpoint_url} unreachable")


def cloud_embed(text: str, model: str, endpoint_url: str, api_key: str = "") -> list[float]:
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for path, payload_fn in [
        ("/api/embed",     lambda: {"model": model, "input": text}),
        ("/v1/embeddings", lambda: {"model": model, "input": text}),
    ]:
        try:
            payload = json.dumps(payload_fn()).encode()
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if "embeddings" in data:
                    return data["embeddings"][0]
                if "data" in data:
                    return data["data"][0]["embedding"]
        except Exception:
            continue
    raise RuntimeError(f"Cloud embed failed — endpoint {endpoint_url}")


def cost_per_1k_tokens(
    gpu_name: str,
    tokens_per_sec: float = 30.0,
    provider_slug: str = "runpod",
) -> float:
    """Estimate cost per 1000 tokens given GPU type, throughput, and provider."""
    provider = PROVIDER_MAP.get(provider_slug)
    costs = provider.gpu_costs if provider else PROVIDERS[0].gpu_costs
    hourly = costs.get(gpu_name, list(costs.values())[1] if len(costs) > 1 else 0.74)
    tokens_per_hour = tokens_per_sec * 3600
    return (hourly / tokens_per_hour) * 1000


def open_url(url: str) -> None:
    import webbrowser
    webbrowser.open(url)

# cloud.py:1 ends here
