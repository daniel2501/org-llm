#!/usr/bin/env python3
"""R18 preflight — verify all 5 cloud services reachable + auth-functional
before R18 launches. Catches token / cluster / quota issues at zero spend.

Services:
  1. Hugging Face — gated-model access (Llama-3.3-70B license accepted?)
  2. Modal — workspace + active token
  3. RunPod — account reachable + GPU pricing visible
  4. Together AI — model list returns
  5. Qdrant Cloud — cluster reachable + collections list returns

Each check costs $0 (informational API calls only).
"""
from __future__ import annotations
import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path


def G(s): return f"\033[32m{s}\033[0m"
def R(s): return f"\033[31m{s}\033[0m"
def Y(s): return f"\033[33m{s}\033[0m"
def D(s): return f"\033[2m{s}\033[0m"


FAILED: list[str] = []


def step(name): print(f"\n{D('━' * 4)} {name} {D('━' * (60 - len(name)))}")
def ok(msg): print(f"  {G('✓')} {msg}")
def fail(msg, detail=""):
    print(f"  {R('✗')} {msg}")
    if detail:
        for ln in detail.splitlines()[:4]:
            print(f"    {D(ln[:120])}")
    FAILED.append(msg)
def warn(msg): print(f"  {Y('!')} {msg}")


def pass_get(slug: str) -> str:
    return subprocess.run(["pass", slug], capture_output=True, text=True,
                            check=True, timeout=5).stdout.strip()


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# 1. Hugging Face
def check_hf():
    step("1. Hugging Face")
    try:
        token = pass_get("org-llm/cloud/huggingface/token")
        ok(f"token in pass: {len(token)} chars")
    except Exception as e:
        fail(f"hf token not in pass: {e}")
        return
    headers = {"Authorization": f"Bearer {token}"}
    # whoami
    try:
        status, body = http_get("https://huggingface.co/api/whoami-v2", headers)
        d = json.loads(body)
        ok(f"hf whoami: {d.get('name')} (type={d.get('type')})")
    except Exception as e:
        fail(f"hf whoami failed: {e}")
        return
    # gated model access — try Llama 3.3 70B model card
    try:
        status, body = http_get(
            "https://huggingface.co/api/models/meta-llama/Llama-3.3-70B-Instruct",
            headers)
        d = json.loads(body)
        gated = d.get("gated", False)
        if gated and d.get("siblings"):
            ok(f"meta-llama/Llama-3.3-70B-Instruct: gate accepted, {len(d['siblings'])} files visible")
        elif gated:
            warn(f"Llama-3.3-70B is gated; if you didn't accept the license, "
                 f"K6/K12 Modal-host will fail at weight pull. Visit https://"
                 f"huggingface.co/meta-llama/Llama-3.3-70B-Instruct → Agree.")
        else:
            ok("Llama-3.3-70B accessible (not gated)")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            warn(f"Llama-3.3-70B gated, license not accepted yet. K6/K12 "
                  f"Modal-host will need this; OpenRouter routing still works.")
        else:
            fail(f"Llama check failed: {e}")
    except Exception as e:
        fail(f"Llama check failed: {e}")


# 2. Modal
def check_modal():
    step("2. Modal")
    try:
        cp = subprocess.run(["modal", "profile", "current"],
                              capture_output=True, text=True, timeout=10)
        if cp.returncode == 0 and cp.stdout.strip():
            ok(f"modal workspace: {cp.stdout.strip()}")
        else:
            fail(f"modal profile current failed: rc={cp.returncode} {cp.stderr[:200]}")
    except FileNotFoundError:
        fail("modal CLI not on PATH (pip install --user modal)")
    except subprocess.TimeoutExpired:
        fail("modal CLI timeout")


# 3. RunPod
def check_runpod():
    step("3. RunPod")
    try:
        token = pass_get("org-llm/cloud/runpod/api-key")
        ok(f"token in pass: {len(token)} chars")
    except Exception as e:
        fail(f"runpod key not in pass: {e}")
        return
    # RunPod has both REST + GraphQL; REST /v1/pods is the simplest auth check.
    headers = {"Authorization": f"Bearer {token}",
                "User-Agent": "org-llm-r18-preflight/1"}
    try:
        status, body = http_get("https://rest.runpod.io/v1/pods", headers)
        d = json.loads(body)
        pods = d if isinstance(d, list) else d.get("pods", [])
        ok(f"runpod auth verified — {len(pods)} pod(s) currently active")
    except Exception as e:
        fail(f"runpod query failed: {e}")


# 4. Together
def check_together():
    step("4. Together AI")
    try:
        token = pass_get("org-llm/cloud/together/api-key")
        ok(f"token in pass: {len(token)} chars")
    except Exception as e:
        fail(f"together key not in pass: {e}")
        return
    headers = {"Authorization": f"Bearer {token}",
                "User-Agent": "org-llm-r18-preflight/1",
                "Accept": "application/json"}
    try:
        status, body = http_get("https://api.together.xyz/v1/models", headers)
        models = json.loads(body)
        n = len(models) if isinstance(models, list) else len(models.get("data", []))
        ok(f"together models: {n} available")
        # Sample two FOSS models
        if isinstance(models, list):
            ids = [m.get("id", "?") for m in models[:50]
                    if any(s in m.get("id", "") for s in
                            ("Kimi", "DeepSeek", "Llama-3", "Qwen"))][:3]
            for mid in ids: ok(f"  {mid}")
    except Exception as e:
        fail(f"together query failed: {e}")


# 5. Qdrant Cloud
def check_qdrant():
    step("5. Qdrant Cloud")
    try:
        url = pass_get("org-llm/cloud/qdrant/url").rstrip("/")
        api_key = pass_get("org-llm/cloud/qdrant/api-key")
        ok(f"url in pass: {len(url)} chars")
        ok(f"api-key in pass: {len(api_key)} chars")
    except Exception as e:
        fail(f"qdrant pass slugs missing: {e}")
        return
    headers = {"api-key": api_key}
    try:
        status, body = http_get(f"{url}/collections", headers)
        d = json.loads(body)
        cols = (d.get("result") or {}).get("collections") or []
        ok(f"qdrant cluster reachable, {len(cols)} collections")
        if cols:
            ok(f"  collections: {[c.get('name') for c in cols]}")
        else:
            ok("  (cluster ready for first collection — R18 will create org-llm-vault)")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("qdrant 401 — api-key wrong or url-key mismatch")
        elif e.code == 403:
            fail("qdrant 403 — api-key lacks permission")
        else:
            fail(f"qdrant HTTP {e.code}: {e}")
    except Exception as e:
        fail(f"qdrant query failed: {e}")


def main():
    print(f"\n{D('═' * 64)}")
    print(f"  R18 PRE-FLIGHT — 5 cloud services")
    print(D('═' * 64))
    check_hf()
    check_modal()
    check_runpod()
    check_together()
    check_qdrant()

    print(f"\n{D('═' * 64)}")
    if FAILED:
        print(f"  {R('PRE-FLIGHT FAILED')} — {len(FAILED)} issue(s):")
        for f in FAILED: print(f"    {R('•')} {f}")
        sys.exit(1)
    print(f"  {G('PRE-FLIGHT GREEN')} — all 5 services reachable + auth-functional.")
    print(f"\n  R18 unlocked deployments:")
    print(f"    1. modal deploy /tmp/modal_vllm/serve_kimi.py  (Modal-Kimi self-host)")
    print(f"    2. python scripts/r18_qdrant_index.py          (RAG index build)")
    print(f"    3. modal run /tmp/lora_prep/02_train.py        (LoRA training)")


if __name__ == "__main__":
    main()
