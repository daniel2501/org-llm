"""K20 v1 LoRA RunPod smoke serve + G5 Mode B probe + teardown.

Uses RunPod REST API (not deprecated GraphQL). Steps:
  1. POST /v1/pods to launch 1xA100-80GB w/ vLLM image + LoRA args
  2. Poll GET /v1/pods/{id} until RUNNING + proxy URL responsive
  3. Run scripts/_endpoint_preflight.py endpoint mode (N=5 prompts)
  4. DELETE /v1/pods/{id} regardless of probe result
  5. Hard kill switch: 35 min wall cap
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

REST = "https://rest.runpod.io/v1"
GPU_TYPE_ID = "NVIDIA A100 80GB PCIe"
IMAGE = "vllm/vllm-openai:v0.20.1"
HF_REPO = "daniel2501/k20-foss-distill-v1-adapter"
BASE_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
LORA_NAME = "k20-v1"
HTTP_PORT = 8000
WALL_CAP_S = 35 * 60
POLL_S = 20


def pass_show(path: str) -> str:
    return subprocess.run(
        ["pass", "show", path], capture_output=True, text=True, check=True
    ).stdout.split("\n", 1)[0].strip()


def rest(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    req = urllib.request.Request(
        f"{REST}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {RP_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def launch_pod() -> str:
    body = {
        "name": f"k20-v1-smoke-{int(time.time())}",
        "imageName": IMAGE,
        "gpuTypeIds": [GPU_TYPE_ID],
        "gpuCount": 1,
        "containerDiskInGb": 150,
        "minRAMPerGPU": 64,
        "minVCPUPerGPU": 8,
        "cloudType": "SECURE",
        "supportPublicIp": True,
        "ports": [f"{HTTP_PORT}/http"],
        "env": {
            "HF_TOKEN": HF_TOK,
        },
        "dockerStartCmd": [
            "--model", BASE_MODEL,
            "--enable-lora",
            "--lora-modules", f"{LORA_NAME}={HF_REPO}",
            "--max-loras", "1",
            "--max-lora-rank", "16",
            "--port", str(HTTP_PORT),
            "--gpu-memory-utilization", "0.92",
            "--max-model-len", "8192",
            "--download-dir", "/root/.cache/huggingface",
        ],
    }
    print(f"[launch] body={json.dumps(body)[:200]}...", flush=True)
    code, res = rest("POST", "/pods", body)
    print(f"[launch] HTTP {code}", flush=True)
    if code not in (200, 201) or not res:
        raise RuntimeError(f"launch failed: code={code} res={res}")
    pod_id = res["id"]
    print(f"[launch] pod_id={pod_id} cost=${res.get('costPerHr')}/hr image={res.get('imageName')}", flush=True)
    return pod_id


def poll_pod(pod_id: str) -> dict | None:
    code, res = rest("GET", f"/pods/{pod_id}")
    if code != 200:
        print(f"[poll] HTTP {code}", flush=True)
        return None
    return res


def proxy_url(pod_id: str) -> str:
    return f"https://{pod_id}-{HTTP_PORT}.proxy.runpod.net"


def runtime_stats(pod_id: str) -> dict | None:
    """GraphQL runtime view (uptime + GPU/container util) — REST omits these."""
    body = {
        "query": (
            "query { pod(input: {podId: \"" + pod_id + "\"}) { "
            "runtime { uptimeInSeconds "
            "gpus { gpuUtilPercent memoryUtilPercent } "
            "container { cpuPercent memoryPercent } } } }"
        )
    }
    req = urllib.request.Request(
        f"https://api.runpod.io/graphql?api_key={RP_KEY}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            return ((d.get("data") or {}).get("pod") or {}).get("runtime")
    except Exception:
        return None


def wait_ready(pod_id: str, deadline: float) -> str | None:
    """Wait for pod RUNNING + vLLM /v1/models 200.

    Logs runtime stats each poll so we can see whether the container is
    actually downloading (CPU 99% / GPU 0%) vs serving (CPU low / GPU >0%).
    """
    last_status = None
    while time.time() < deadline:
        pod = poll_pod(pod_id)
        if pod:
            status = pod.get("desiredStatus")
            if status != last_status:
                print(f"[poll] status={status}", flush=True)
                last_status = status
            if status == "RUNNING":
                rt = runtime_stats(pod_id) or {}
                ut = rt.get("uptimeInSeconds")
                gpu = (rt.get("gpus") or [{}])[0]
                ctr = rt.get("container") or {}
                print(
                    f"[stats] uptime={ut}s "
                    f"gpu_util={gpu.get('gpuUtilPercent')}% "
                    f"vram={gpu.get('memoryUtilPercent')}% "
                    f"cpu={ctr.get('cpuPercent')}% "
                    f"mem={ctr.get('memoryPercent')}%",
                    flush=True,
                )
                # Try /v1/models on the proxy URL
                url = proxy_url(pod_id)
                try:
                    req = urllib.request.Request(f"{url}/v1/models")
                    with urllib.request.urlopen(req, timeout=10) as r:
                        if r.status == 200:
                            body = json.loads(r.read())
                            ids = [m.get("id") for m in body.get("data", [])]
                            print(f"[ready] /v1/models → {ids}", flush=True)
                            return url
                except urllib.error.HTTPError as e:
                    print(f"[wait] /v1/models {e.code}", flush=True)
                except Exception as e:
                    print(f"[wait] /v1/models {type(e).__name__}: {str(e)[:80]}", flush=True)
        time.sleep(POLL_S)
    return None


def stop_pod(pod_id: str) -> None:
    print(f"[teardown] DELETE /pods/{pod_id}", flush=True)
    code, res = rest("DELETE", f"/pods/{pod_id}")
    print(f"[teardown] HTTP {code} res={res}", flush=True)


def run_probe(url: str) -> dict:
    cmd = [
        "python3", "scripts/_endpoint_preflight.py", "endpoint",
        "--url", f"{url}/v1/chat/completions",
        "--base-model", BASE_MODEL,
        "--lora-model", LORA_NAME,
        "--n-prompts", "5",
    ]
    print(f"[probe] {' '.join(cmd)}", flush=True)
    cp = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[probe stdout] {cp.stdout.strip()}", flush=True)
    if cp.stderr.strip():
        print(f"[probe stderr] {cp.stderr.strip()[:500]}", flush=True)
    try:
        return json.loads(cp.stdout.strip().splitlines()[-1])
    except Exception:
        return {"status": "fail", "fail_reasons": ["probe_no_json_output"]}


def main() -> int:
    global RP_KEY, HF_TOK
    RP_KEY = pass_show("org-llm/cloud/runpod/api-key")
    HF_TOK = pass_show("org-llm/cloud/huggingface/token")

    started = time.time()
    deadline = started + WALL_CAP_S
    pod_id = None
    try:
        pod_id = launch_pod()
        url = wait_ready(pod_id, deadline)
        if not url:
            raise TimeoutError("pod never became serve-ready in WALL_CAP_S")
        result = run_probe(url)
        print(f"\n=== PROBE RESULT ===\n{json.dumps(result, indent=2)}", flush=True)
        return 0 if result.get("status") == "pass" else 2
    finally:
        if pod_id:
            try:
                stop_pod(pod_id)
            except Exception as e:
                print(f"[teardown ERROR] {e}", flush=True)
        elapsed = time.time() - started
        cost = elapsed / 3600 * 1.19
        print(f"\n[done] wall={elapsed:.0f}s estimated_spend=${cost:.2f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
