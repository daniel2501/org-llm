# [[file:../../../org/20260425230731-org_llm.org::*cloud.py][cloud.py:1]]
"""RunPod cloud LLM backend — automatic compute expansion beyond local Ollama."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import NamedTuple

RUNPOD_SIGNUP_URL  = "https://www.runpod.io/?ref=org-llm-cli"
RUNPOD_CONSOLE_URL = "https://www.runpod.io/console/pods"
RUNPOD_TEMPLATES_URL = "https://www.runpod.io/console/explore"

# ── GPU cost table ($/hr, approximate spot rates) ─────────────────────────────
RUNPOD_GPU_COSTS: dict[str, float] = {
    "RTX 3090":      0.37,
    "RTX 4090":      0.74,
    "RTX A6000":     0.79,
    "A100 40GB":     1.64,
    "A100 80GB":     1.89,
    "H100 PCIe":     2.49,
    "H100 80GB SXM": 2.79,
}

# ── Approximate model VRAM requirements (GB) ──────────────────────────────────
# model name fragment → VRAM needed (fp16)
MODEL_VRAM: list[tuple[str, float]] = [
    ("1b",          1.5),
    ("3b",          3.0),
    ("7b",          5.5),
    ("8b",          6.0),
    ("phi4",        9.0),
    ("13b",        10.0),
    ("mistral",     5.5),
    ("gemma3",      6.0),
    ("llama3.2",    3.5),
    ("llama3.3",   48.0),
    ("llama3.1",    5.5),
    ("qwen2.5-coder", 5.5),
    ("deepseek-r1",48.0),
    ("nomic-embed", 0.5),
    ("phi3",        4.0),
]


class CloudStatus(NamedTuple):
    provider:   str          # "runpod" | "none"
    endpoint:   str          # URL
    model:      str          # model name
    reachable:  bool
    auth_ok:    bool
    latency_ms: float | None


# ── VRAM / hardware assessment ─────────────────────────────────────────────────

def local_vram_gb() -> float | None:
    """Return available GPU VRAM in GB, or None if no GPU / nvidia-smi unavailable."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        # Sum across all GPUs
        return sum(float(x.strip()) for x in out.strip().splitlines() if x.strip()) / 1024
    except Exception:
        return None


def local_ram_gb() -> float:
    """Return total system RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / 1_048_576
    except Exception:
        pass
    import shutil
    return shutil.disk_usage("/").total / 1e9  # fallback: disk as proxy


def model_needs_vram(model_name: str) -> float:
    """Estimate VRAM needed (GB) for a model by name."""
    name = model_name.lower()
    for fragment, vram in MODEL_VRAM:
        if fragment in name:
            return vram
    # fallback: assume medium (7B-class)
    return 5.5


def assess_local_capability(models: list[str]) -> list[dict]:
    """
    For each model, decide if it can run locally or needs cloud.
    Returns list of {model, vram_needed, can_run_local, reason}.
    """
    vram = local_vram_gb()
    ram  = local_ram_gb()
    results = []
    for model in models:
        needed = model_needs_vram(model)
        if vram is not None:
            can_local = vram >= needed
            resource  = f"{vram:.0f} GB VRAM available"
        else:
            # CPU-only: models up to ~8GB can run via RAM (ollama does 4-bit quant)
            can_local = (ram * 0.6) >= needed
            resource  = f"{ram:.0f} GB RAM (CPU-only)"
        results.append({
            "model":      model,
            "vram_needed": needed,
            "can_local":  can_local,
            "resource":   resource,
            "reason":     "✓ fits locally" if can_local else f"needs {needed:.0f}GB, recommend cloud",
        })
    return results


# ── Cloud connection ───────────────────────────────────────────────────────────

def check_connection(endpoint_url: str, api_key: str = "", model: str = "") -> CloudStatus:
    """Ping a cloud Ollama/OpenAI-compatible endpoint and return status."""
    import time
    url = endpoint_url.rstrip("/")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Try Ollama /api/tags first, then OpenAI /v1/models
    for path in ("/api/tags", "/v1/models"):
        try:
            req = urllib.request.Request(
                f"{url}{path}", headers=headers, method="GET"
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=8) as resp:
                latency = (time.monotonic() - t0) * 1000
                data = json.loads(resp.read())
                return CloudStatus(
                    provider="runpod",
                    endpoint=endpoint_url,
                    model=model,
                    reachable=True,
                    auth_ok=True,
                    latency_ms=latency,
                )
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return CloudStatus("runpod", endpoint_url, model, True, False, None)
            continue
        except Exception:
            continue

    return CloudStatus("runpod", endpoint_url, model, False, False, None)


def cloud_chat(
    prompt: str,
    model: str,
    endpoint_url: str,
    api_key: str = "",
    system: str = "",
) -> str:
    """Send a chat request to a cloud Ollama-compatible or OpenAI-compatible endpoint."""
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Try Ollama /api/chat format first
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode()

    for path in ("/api/chat", "/v1/chat/completions"):
        try:
            req = urllib.request.Request(
                f"{url}{path}",
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                # Ollama format
                if "message" in data:
                    return data["message"]["content"]
                # OpenAI format
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
        except Exception:
            continue

    raise RuntimeError(f"Cloud chat failed — endpoint {endpoint_url} unreachable")


def cloud_embed(text: str, model: str, endpoint_url: str, api_key: str = "") -> list[float]:
    """Get embeddings from a cloud endpoint."""
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for path, payload_fn in [
        ("/api/embed", lambda: {"model": model, "input": text}),
        ("/v1/embeddings", lambda: {"model": model, "input": text}),
    ]:
        try:
            payload = json.dumps(payload_fn()).encode()
            req = urllib.request.Request(
                f"{url}{path}", data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if "embeddings" in data:
                    return data["embeddings"][0]
                if "data" in data:
                    return data["data"][0]["embedding"]
        except Exception:
            continue
    raise RuntimeError(f"Cloud embed failed — endpoint {endpoint_url}")


def cost_per_1k_tokens(gpu_name: str = "RTX 4090", tokens_per_sec: float = 30.0) -> float:
    """Estimate cost per 1000 tokens given GPU type and throughput."""
    hourly = RUNPOD_GPU_COSTS.get(gpu_name, 0.74)
    tokens_per_hour = tokens_per_sec * 3600
    return (hourly / tokens_per_hour) * 1000


def open_signup() -> None:
    """Open the RunPod signup page in the default browser."""
    import webbrowser
    webbrowser.open(RUNPOD_SIGNUP_URL)


def open_console() -> None:
    """Open the RunPod console in the default browser."""
    import webbrowser
    webbrowser.open(RUNPOD_CONSOLE_URL)
# cloud.py:1 ends here
