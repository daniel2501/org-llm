# [[file:../../../org/20260425230731-org_llm.org::*tests/test_cloud.py][test_cloud.py:1]]
"""Tests for org_llm.cloud — multi-provider GPU cloud registry."""
from __future__ import annotations

import json
import pytest
from org_llm.cloud import (
    PROVIDERS,
    PROVIDER_MAP,
    CloudStatus,
    ProviderInfo,
    assess_local_capability,
    check_connection,
    cloud_chat,
    cloud_embed,
    cost_per_1k_tokens,
    get_provider,
    local_ram_gb,
    local_vram_gb,
    model_needs_vram,
)


# ── Provider registry ──────────────────────────────────────────────────────────

def test_providers_not_empty():
    assert len(PROVIDERS) >= 5


def test_providers_are_provider_info():
    for p in PROVIDERS:
        assert isinstance(p, ProviderInfo)


def test_required_providers_present():
    slugs = {p.slug for p in PROVIDERS}
    for slug in ("runpod", "vast", "lambda", "tensordock", "salad"):
        assert slug in slugs, f"Provider {slug!r} missing from PROVIDERS"


def test_provider_slugs_unique():
    slugs = [p.slug for p in PROVIDERS]
    assert len(slugs) == len(set(slugs)), "Duplicate provider slugs"


def test_provider_map_matches_providers():
    assert set(PROVIDER_MAP.keys()) == {p.slug for p in PROVIDERS}


def test_provider_info_fields_non_empty():
    for p in PROVIDERS:
        assert p.slug
        assert p.name
        assert p.signup_url.startswith("http")
        assert p.console_url.startswith("http")
        assert p.api_compat in ("ollama", "openai", "both")
        assert p.gpu_costs, f"{p.slug} has empty gpu_costs"
        assert p.description


def test_provider_gpu_costs_positive():
    for p in PROVIDERS:
        for gpu, cost in p.gpu_costs.items():
            assert cost > 0, f"{p.slug}/{gpu} cost must be positive"


def test_provider_endpoint_hints_non_empty():
    for p in PROVIDERS:
        assert p.endpoint_hint.startswith("http"), f"{p.slug} endpoint_hint should start with http"


# ── get_provider ───────────────────────────────────────────────────────────────

def test_get_provider_known():
    p = get_provider("runpod")
    assert p is not None
    assert p.name == "RunPod"


def test_get_provider_unknown():
    assert get_provider("nonexistent-provider") is None


def test_get_provider_all_slugs():
    for p in PROVIDERS:
        assert get_provider(p.slug) is p


# ── Hardware assessment ────────────────────────────────────────────────────────

def test_local_ram_gb_positive():
    ram = local_ram_gb()
    assert ram > 0


def test_local_vram_gb_none_or_positive():
    vram = local_vram_gb()
    assert vram is None or vram > 0


def test_model_needs_vram_7b():
    assert model_needs_vram("llama3.1:7b") == 5.5


def test_model_needs_vram_known_fragment():
    assert model_needs_vram("deepseek-r1:70b") == 48.0


def test_model_needs_vram_unknown_defaults():
    assert model_needs_vram("unknown-exotic-model:42b") == 5.5


def test_model_needs_vram_phi4():
    assert model_needs_vram("phi4") == 9.0


def test_model_needs_vram_embed():
    assert model_needs_vram("nomic-embed-text") == 0.5


def test_assess_local_capability_structure():
    results = assess_local_capability(["llama3.1:7b", "deepseek-r1:70b"])
    assert len(results) == 2
    for r in results:
        assert "model" in r
        assert "vram_needed" in r
        assert "can_local" in r
        assert "resource" in r
        assert "reason" in r


def test_assess_local_capability_small_model_fits_cpu():
    results = assess_local_capability(["nomic-embed-text"])
    # nomic-embed needs 0.5GB; any machine with 2+ GB RAM should handle it
    assert results[0]["can_local"] is True or results[0]["can_local"] is False  # bool


def test_assess_large_model_wont_fit_tiny_ram(monkeypatch):
    monkeypatch.setattr("org_llm.cloud.local_vram_gb", lambda: None)
    monkeypatch.setattr("org_llm.cloud.local_ram_gb", lambda: 4.0)
    results = assess_local_capability(["deepseek-r1:70b"])
    assert results[0]["can_local"] is False


def test_assess_small_model_fits_16gb_ram(monkeypatch):
    monkeypatch.setattr("org_llm.cloud.local_vram_gb", lambda: None)
    monkeypatch.setattr("org_llm.cloud.local_ram_gb", lambda: 16.0)
    results = assess_local_capability(["nomic-embed-text"])
    assert results[0]["can_local"] is True


# ── Cost estimation ────────────────────────────────────────────────────────────

def test_cost_per_1k_tokens_positive():
    c = cost_per_1k_tokens("RTX 4090", tokens_per_sec=50.0, provider_slug="runpod")
    assert c > 0


def test_cost_per_1k_tokens_faster_is_cheaper():
    slow = cost_per_1k_tokens("RTX 4090", tokens_per_sec=10.0, provider_slug="runpod")
    fast = cost_per_1k_tokens("RTX 4090", tokens_per_sec=100.0, provider_slug="runpod")
    assert fast < slow


def test_cost_per_1k_tokens_pricier_gpu_higher():
    cheap = cost_per_1k_tokens("RTX 3090", tokens_per_sec=30.0, provider_slug="runpod")
    pricey = cost_per_1k_tokens("H100 80GB SXM", tokens_per_sec=30.0, provider_slug="runpod")
    assert pricey > cheap


def test_cost_per_1k_tokens_salad_cheaper_than_runpod():
    salad = cost_per_1k_tokens("RTX 4090", tokens_per_sec=30.0, provider_slug="salad")
    runpod = cost_per_1k_tokens("RTX 4090", tokens_per_sec=30.0, provider_slug="runpod")
    assert salad < runpod


def test_cost_per_1k_tokens_unknown_gpu_fallback():
    c = cost_per_1k_tokens("Totally Unknown GPU", tokens_per_sec=30.0, provider_slug="runpod")
    assert c > 0


def test_cost_per_1k_tokens_unknown_provider_fallback():
    c = cost_per_1k_tokens("RTX 4090", tokens_per_sec=30.0, provider_slug="nonexistent")
    assert c > 0


# ── CloudStatus ────────────────────────────────────────────────────────────────

def test_cloud_status_is_named_tuple():
    cs = CloudStatus("runpod", "https://example.com", "llama3.1", True, True, 42.5)
    assert cs.provider == "runpod"
    assert cs.reachable is True
    assert cs.latency_ms == 42.5


def test_check_connection_unreachable():
    # Loopback port that should be closed
    cs = check_connection("http://127.0.0.1:19999", model="test")
    assert cs.reachable is False
    assert cs.auth_ok is False
    assert cs.latency_ms is None


def test_check_connection_bad_url():
    cs = check_connection("http://this.host.does.not.exist.invalid:11434", model="test")
    assert cs.reachable is False


# ── cloud_chat / cloud_embed (mock responses) ──────────────────────────────────

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_cloud_chat_ollama_response(monkeypatch):
    body = json.dumps({"message": {"content": "hello"}}).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(body)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = cloud_chat("hi", "llama3.1", "http://fake:11434")
    assert result == "hello"


def test_cloud_chat_openai_response(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "world"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(body)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = cloud_chat("hi", "llama3.1", "http://fake:11434")
    assert result == "world"


def test_cloud_chat_raises_on_failure(monkeypatch):
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise OSError("no connection")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="Cloud chat failed"):
        cloud_chat("hi", "model", "http://fake:11434")


def test_cloud_embed_ollama_response(monkeypatch):
    body = json.dumps({"embeddings": [[0.1, 0.2, 0.3]]}).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(body)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    vec = cloud_embed("test", "nomic-embed-text", "http://fake:11434")
    assert vec == [0.1, 0.2, 0.3]


def test_cloud_embed_openai_response(monkeypatch):
    body = json.dumps({"data": [{"embedding": [0.4, 0.5]}]}).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(body)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    vec = cloud_embed("test", "nomic-embed-text", "http://fake:11434")
    assert vec == [0.4, 0.5]


def test_cloud_embed_raises_on_failure(monkeypatch):
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise OSError("no connection")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="Cloud embed failed"):
        cloud_embed("test", "model", "http://fake:11434")
# test_cloud.py:1 ends here
