# [[file:../../../org/20260425230731-org_llm.org::*cloud.py][cloud.py:1]]
"""Cloud GPU provider registry — automatic compute expansion beyond local Ollama."""
from __future__ import annotations

import json
import os
import ssl
import urllib.request
from pathlib import Path
from typing import NamedTuple


# ── SSL CA bundle resolution ──────────────────────────────────────────────────
# On Guix and minimal containers Python's compiled-in openssl defaults often
# point at a /gnu/store path that doesn't contain certs, breaking HTTPS to
# every cloud provider. Probe the common system locations and build a context
# that works in any environment.

def _ssl_context() -> ssl.SSLContext | None:
    """Return an SSL context with a working CA bundle, or None to use the default."""
    env_file = os.environ.get("SSL_CERT_FILE")
    env_dir  = os.environ.get("SSL_CERT_DIR")
    if env_file or env_dir:
        return ssl.create_default_context(cafile=env_file, capath=env_dir)
    # Common bundle locations across distros
    for cafile in (
        "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu/Arch/Guix System
        "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora/RHEL
        "/etc/ssl/cert.pem",                    # BSD/macOS
        str(Path.home() / ".guix-profile/etc/ssl/certs/ca-certificates.crt"),
        str(Path.home() / ".guix-home/profile/etc/ssl/certs/ca-certificates.crt"),
    ):
        if Path(cafile).exists():
            return ssl.create_default_context(cafile=cafile)
    # Last resort — try certifi if it's importable
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_SSL_CONTEXT = _ssl_context()


def _urlopen(req, timeout: float = 30):
    """urlopen wrapper that injects our resolved SSL context."""
    if _SSL_CONTEXT is not None and req.full_url.startswith("https://"):
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
    return urllib.request.urlopen(req, timeout=timeout)


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
    pricing_url:    str = ""     # billing / paid-tier upgrade page
    paid_examples:  tuple = ()   # representative paid models for upgrade pitch


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
    # ── Free-tier hosted inference (no GPU rental, just a metered API) ───────
    ProviderInfo(
        slug        = "openrouter",
        name        = "OpenRouter",
        signup_url  = "https://openrouter.ai/",
        console_url = "https://openrouter.ai/keys",
        docs_url    = "https://openrouter.ai/docs",
        api_compat  = "openai",
        endpoint_hint = "https://openrouter.ai/api/v1",
        gpu_costs   = {
            "free tier (Llama 3.1 8B)":    0.00,
            "Llama 3.3 70B":               0.40,
            "DeepSeek R1":                 0.55,
            "Claude Sonnet 4.6":           3.00,
        },
        description = "Hosted multi-model gateway; FREE tier (Llama 3.1 8B); per-token billing",
        pricing_url = "https://openrouter.ai/credits",
        # FOSS / open-weights first; closed APIs last. All are fully self-hostable
        # except the trailing two.
        paid_examples = ("deepseek/deepseek-r1",                   # MIT, open weights
                          "meta-llama/llama-3.3-70b-instruct",       # Meta Llama community, open weights
                          "qwen/qwen-2.5-72b-instruct",              # Apache 2.0, open weights
                          "openai/gpt-oss-120b",                     # Apache 2.0 (OpenAI's open release)
                          "anthropic/claude-sonnet-4.6",             # closed API; long-context tool use
                          "openai/gpt-5.5"),                         # closed API; structured-JSON specialist
    ),
    ProviderInfo(
        slug        = "groq",
        name        = "Groq",
        signup_url  = "https://console.groq.com/",
        console_url = "https://console.groq.com/keys",
        docs_url    = "https://console.groq.com/docs",
        api_compat  = "openai",
        endpoint_hint = "https://api.groq.com/openai/v1",
        gpu_costs   = {
            "free tier (Llama 3.1 8B)":  0.00,
            "Llama 3.3 70B":             0.79,
            "Llama 3.1 8B paid":         0.05,
        },
        description = "Ultra-fast LPU inference; FREE tier with rate limits; very low latency",
        pricing_url = "https://groq.com/pricing/",
        # All Groq paid models happen to be FOSS-friendly (Meta Llama, Qwen,
        # DeepSeek) — that's part of why we ship them.
        paid_examples = ("llama-3.3-70b-versatile",
                          "deepseek-r1-distill-llama-70b",
                          "qwen-2.5-32b"),
    ),
    ProviderInfo(
        slug        = "huggingface",
        name        = "Hugging Face Inference",
        signup_url  = "https://huggingface.co/join",
        console_url = "https://huggingface.co/settings/tokens",
        docs_url    = "https://huggingface.co/docs/api-inference",
        api_compat  = "openai",
        endpoint_hint = "https://router.huggingface.co/v1",
        gpu_costs   = {
            "free tier (rate-limited)":  0.00,
            "Llama 3.3 70B":             0.50,
        },
        description = "Hosted inference for any HF model; FREE tier with rate limits",
        pricing_url = "https://huggingface.co/pricing",
        paid_examples = ("meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-R1"),
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

def _candidate_paths(endpoint_url: str, kind: str) -> list[str]:
    """Return the list of probe paths to try for a given URL.

    OpenAI-compatible base URLs frequently already include `/v1` (OpenRouter,
    Lambda, Groq, …). Naively appending `/v1/...` would yield `/v1/v1/...`
    which 404s. We probe both `/v1/<thing>` and `/<thing>` so a single helper
    works for plain Ollama, hosted OpenAI gateways, and bare OpenAI APIs.
    """
    url = endpoint_url.rstrip("/")
    has_v1 = url.endswith("/v1")
    if kind == "tags":
        # Listing models — Ollama uses /api/tags, OpenAI uses /models or /v1/models
        return ["/models", "/api/tags"] if has_v1 else ["/api/tags", "/v1/models"]
    if kind == "chat":
        return ["/chat/completions", "/api/chat"] if has_v1 else ["/api/chat", "/v1/chat/completions"]
    if kind == "embed":
        return ["/embeddings", "/api/embed"] if has_v1 else ["/api/embed", "/v1/embeddings"]
    return []


def check_connection(endpoint_url: str, api_key: str = "", model: str = "") -> CloudStatus:
    """Ping a cloud Ollama/OpenAI-compatible endpoint and return status."""
    import time
    url = endpoint_url.rstrip("/")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_status: CloudStatus | None = None
    for path in _candidate_paths(endpoint_url, "tags"):
        try:
            req = urllib.request.Request(f"{url}{path}", headers=headers, method="GET")
            t0 = time.monotonic()
            with _urlopen(req, timeout=8) as resp:
                latency = (time.monotonic() - t0) * 1000
                json.loads(resp.read())
                return CloudStatus("configured", endpoint_url, model, True, True, latency)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                last_status = CloudStatus("configured", endpoint_url, model, True, False, None)
                continue
            # Non-401 HTTP error (e.g. 404 because we picked the wrong path) — try next
            continue
        except Exception:
            continue

    return last_status or CloudStatus("configured", endpoint_url, model, False, False, None)


# ── Chat / embed via cloud ─────────────────────────────────────────────────────

def cloud_chat(
    prompt: str, model: str, endpoint_url: str,
    api_key: str = "", system: str = "",
) -> str:
    """Cloud chat round-trip.

    Logs every successful call to the logbook (kind=llm,
    command=cloud-chat) so dbt + Captain's Log see it the same way as
    local Ollama calls. Failures are also logged with outcome=error.
    Conversation history is preserved in BOTH the History.response
    column AND the org file.
    """
    from .logbook import track_event as _track
    with _track("llm", "cloud-chat", model=model,
                  args=f"prompt_chars={len(prompt)} "
                       f"system_chars={len(system or '')} "
                       f"endpoint={endpoint_url}") as ev:
        out = _cloud_chat_core(prompt, model, endpoint_url, api_key, system)
        ev["response"] = out or ""
        return out


def _cloud_chat_core(
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

    import time as _time
    last_err: Exception | None = None
    # Detect provider slug from endpoint for telemetry
    provider_slug = ""
    for p in PROVIDERS:
        if p.endpoint_hint == endpoint_url or endpoint_url.startswith(
                p.endpoint_hint.split("{")[0] if "{" in p.endpoint_hint else p.endpoint_hint):
            provider_slug = p.slug
            break

    for path in _candidate_paths(endpoint_url, "chat"):
        t0 = _time.monotonic()
        try:
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with _urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                latency_ms = (_time.monotonic() - t0) * 1000
                if "message" in data:
                    record_event(provider_slug, model, "ok", latency_ms=latency_ms)
                    return data["message"]["content"]
                if "choices" in data:
                    record_event(provider_slug, model, "ok", latency_ms=latency_ms)
                    return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            outcome, code = _classify_error(e)
            record_event(provider_slug, model, outcome, status_code=code,
                          detail=str(e)[:160])
            continue
    raise RuntimeError(f"Cloud chat failed — endpoint {endpoint_url} unreachable ({last_err!r})")


def cloud_embed(text: str, model: str, endpoint_url: str, api_key: str = "") -> list[float]:
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_err: Exception | None = None
    for path in _candidate_paths(endpoint_url, "embed"):
        try:
            payload = json.dumps({"model": model, "input": text}).encode()
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with _urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if "embeddings" in data:
                    return data["embeddings"][0]
                if "data" in data:
                    return data["data"][0]["embedding"]
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Cloud embed failed — endpoint {endpoint_url} ({last_err!r})")


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


# ── Usage telemetry: detect when a paid upgrade is justified ──────────────────
#
# We log per-call outcomes (success / rate_limit / error / latency_ms) into a
# `cloud_usage` config row as a JSON list, capped at 200 entries. The
# `recommend_upgrade()` helper reasons over that history to decide whether
# the user would benefit from a paid tier — and which model to upgrade to.

_USAGE_KEY = "cloud_usage"
_USAGE_CAP = 200


def _read_usage() -> list[dict]:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return []
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, _USAGE_KEY)
            if not row or not row.value:
                return []
            data = json.loads(row.value)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_usage(events: list[dict]) -> None:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, _USAGE_KEY)
            payload = json.dumps(events[-_USAGE_CAP:])
            if row:
                row.value = payload
            else:
                s.add(Config(key=_USAGE_KEY, value=payload))
            s.commit()
    except Exception:
        pass


def record_event(provider: str, model: str, outcome: str,
                  latency_ms: float | None = None,
                  status_code: int | None = None,
                  detail: str = "") -> None:
    """Append a usage event. outcome ∈ {ok, rate_limit, auth_error, server_error,
    timeout, network, other}. Bounded; oldest events drop off."""
    import time
    events = _read_usage()
    events.append({
        "ts":       time.time(),
        "provider": provider, "model": model, "outcome": outcome,
        "latency_ms": float(latency_ms) if latency_ms is not None else None,
        "status_code": status_code,
        "detail":   detail[:160],
    })
    _write_usage(events)


def _classify_error(exc: Exception) -> tuple[str, int | None]:
    """Map a cloud-call exception to (outcome, status_code)."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 401 or code == 403:
            return ("auth_error", code)
        if code == 429:
            return ("rate_limit", code)
        if 500 <= code < 600:
            return ("server_error", code)
        return ("other", code)
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return ("timeout", None)
    if "connect" in msg or "refused" in msg or "name or service not known" in msg:
        return ("network", None)
    return ("other", None)


# ── Recommendation engine ────────────────────────────────────────────────────

class UpgradeRecommendation(NamedTuple):
    should_upgrade: bool
    severity:       str          # "ok" | "consider" | "recommend" | "strongly"
    reasons:        list[str]
    suggested_provider: str       # slug of provider whose paid tier to use
    suggested_models:   list[str] # paid_examples from that provider
    metrics:        dict         # raw stats for the report


def recommend_upgrade(
    fixer_top_accuracy: float | None = None,
    window_seconds: float = 7 * 24 * 3600,
) -> UpgradeRecommendation:
    """Decide if the user should upgrade to a paid cloud tier.

    Considers:
      • Recent rate-limit / server-error frequency in cloud_usage events
        (within window_seconds; default 7 days)
      • Median cloud latency (slow tiers might justify a faster paid plan)
      • Best fixer benchmark accuracy across free models (if known)

    Returns an UpgradeRecommendation with reasons + a suggested provider
    (defaults to whichever the user already configured) + that provider's
    paid_examples for use in `cloud --upgrade`.
    """
    import time
    now = time.time()
    events = [e for e in _read_usage()
              if now - float(e.get("ts", 0)) <= window_seconds]

    n = len(events)
    rate_limits  = [e for e in events if e.get("outcome") == "rate_limit"]
    server_errs  = [e for e in events if e.get("outcome") == "server_error"]
    timeouts     = [e for e in events if e.get("outcome") == "timeout"]
    successes    = [e for e in events if e.get("outcome") == "ok"]
    latencies    = [e["latency_ms"] for e in successes
                    if isinstance(e.get("latency_ms"), (int, float))]
    latencies.sort()
    median_latency = latencies[len(latencies)//2] if latencies else None

    # Read configured provider for the suggestion default
    suggested_provider = ""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if path.exists():
            engine = make_engine(path)
            with Session(engine) as s:
                row = s.get(Config, "cloud_provider")
                if row: suggested_provider = row.value or ""
    except Exception:
        pass

    if not suggested_provider:
        suggested_provider = "openrouter"   # most flexible default

    suggested_models: list[str] = []
    p = PROVIDER_MAP.get(suggested_provider)
    if p:
        suggested_models = list(p.paid_examples)

    reasons: list[str] = []
    score = 0   # higher = more urgent

    # Rate-limit pressure: any non-trivial ratio is worth flagging
    if n >= 5 and rate_limits:
        rl_ratio = len(rate_limits) / n
        if rl_ratio >= 0.20:
            reasons.append(f"Rate-limited on {len(rate_limits)}/{n} cloud calls "
                            f"in the last {int(window_seconds/86400)}d "
                            f"({rl_ratio*100:.0f}%) — paid tier removes the cap.")
            score += 3
        elif rl_ratio >= 0.05:
            reasons.append(f"Some rate limits ({len(rate_limits)}/{n}, "
                            f"{rl_ratio*100:.0f}%) — borderline; paid would be faster.")
            score += 1

    # Server / availability noise
    avail_failures = len(server_errs) + len(timeouts)
    if n >= 5 and avail_failures / n >= 0.10:
        reasons.append(f"{avail_failures}/{n} cloud calls hit server errors or "
                        "timeouts — free tiers de-prioritise during congestion.")
        score += 2

    # Latency
    if median_latency is not None and median_latency > 4000:
        reasons.append(f"Median cloud latency is {median_latency:.0f} ms — paid "
                        "endpoints (Groq, Anthropic Sonnet) typically <1 s.")
        score += 1

    # Fixer benchmark
    if fixer_top_accuracy is not None and fixer_top_accuracy < 0.70:
        reasons.append(f"Best free-tier fixer model scores only "
                        f"{fixer_top_accuracy*100:.0f}% on canonical fix scenarios — "
                        "paid models (Claude / GPT-5) would clear 90%+.")
        score += 3

    if score >= 5:
        severity = "strongly"
    elif score >= 3:
        severity = "recommend"
    elif score >= 1:
        severity = "consider"
    else:
        severity = "ok"

    return UpgradeRecommendation(
        should_upgrade=(score >= 1),
        severity=severity,
        reasons=reasons,
        suggested_provider=suggested_provider,
        suggested_models=suggested_models,
        metrics={
            "events":        n,
            "rate_limits":   len(rate_limits),
            "server_errors": len(server_errs),
            "timeouts":      len(timeouts),
            "successes":     len(successes),
            "median_latency_ms": median_latency,
            "score":         score,
        },
    )

# cloud.py:1 ends here
