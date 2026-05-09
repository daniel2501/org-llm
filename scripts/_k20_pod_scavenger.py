#!/usr/bin/env python3
"""K20 pod scavenger — productionalized harness primitive (R28 P1-9 FOLD G8+F7).

Spawned alongside an in-flight K20 sweep. Tries to grow the pool toward
TARGET_POOL_SIZE whenever RunPod capacity opens. Each successful pod
gets appended to the pool-state JSON for the next sweep to pick up
(the current in-flight sweep won't use them).

R28 changes vs /tmp/r27_5_pod_scavenger.py source:
  - Lives in scripts/ as a first-class harness primitive
    (per [Productionalize harness into app] memory rule).
  - POOL_STATE path is configurable via K20_POOL_STATE env var; default
    is round-aware (=scripts/_round28_dials_artifacts/...=).
  - F7 (FOLD): pool-state JSON gets a 24h TTL. On startup, if
    POOL_STATE mtime > TTL, file is gc'd before scavenging starts —
    prevents stale pod_ids leaking across days.
  - Adds atexit + SIGTERM/SIGINT handlers that DO NOT tear down
    scavenged pods (those belong to the next sweep). Per
    [`kill -KILL` not `-TERM` with EXIT trap] memory rule, the operator
    must use =kill -KILL= if they want the scavenger gone WITHOUT
    leaving its child pods alive (rare; usually you WANT pods to outlive
    the scavenger).

Usage:
    # Scavenge into R28's pool state (default)
    python3 scripts/_k20_pod_scavenger.py

    # Scavenge into a custom pool
    K20_POOL_STATE=/path/to/pool.json python3 scripts/_k20_pod_scavenger.py

Env:
    K20_POOL_STATE       — pool-state JSON path (default R28 artifacts)
    K20_SCAV_TARGET      — target pool size (default 4)
    K20_SCAV_WALL_S      — wall-clock cap in seconds (default 720 = 12 min)
    K20_SCAV_RETRY_S     — gap between launch attempts (default 30)
    K20_SCAV_TTL_HOURS   — pool-state TTL for F7 gc (default 24)
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(REPO / "scripts"))
import _k20_runpod_resume as resume_helper

DEFAULT_POOL_STATE = (REPO / "scripts/_round28_dials_artifacts/"
                          "k20_pod_scavenger_state.json")
POOL_STATE = Path(os.environ.get("K20_POOL_STATE") or DEFAULT_POOL_STATE)
TARGET_POOL_SIZE = int(os.environ.get("K20_SCAV_TARGET", "4"))
WALL_CAP_S = int(os.environ.get("K20_SCAV_WALL_S", str(12 * 60)))
RETRY_GAP_S = int(os.environ.get("K20_SCAV_RETRY_S", "30"))
TTL_HOURS = float(os.environ.get("K20_SCAV_TTL_HOURS", "24"))

_SHUTDOWN_REQUESTED = False


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[scavenger {ts}] {msg}", file=sys.stderr, flush=True)


def _gc_stale_state() -> None:
    """F7 — drop pool-state if it's older than TTL_HOURS. Prevents stale
    pod_ids (deleted/dead) leaking across days into the next round's
    scavenger run."""
    if not POOL_STATE.exists():
        return
    age_s = time.time() - POOL_STATE.stat().st_mtime
    age_h = age_s / 3600
    if age_h > TTL_HOURS:
        log(f"F7 gc — pool-state {age_h:.1f}h old > {TTL_HOURS}h TTL; clearing")
        try:
            POOL_STATE.unlink()
        except Exception as e:
            log(f"  gc failed: {e}")


def current_pool() -> list[dict]:
    if not POOL_STATE.exists():
        return []
    try:
        return json.loads(POOL_STATE.read_text()).get("pods", [])
    except Exception:
        return []


def append_pod(pod_id: str, url: str) -> None:
    pods = current_pool()
    pods.append({"id": pod_id, "url": url, "ready": False, "scavenged": True})
    POOL_STATE.parent.mkdir(parents=True, exist_ok=True)
    POOL_STATE.write_text(
        json.dumps({"pods": pods, "ts": time.time()}, indent=2)
    )
    log(f"appended {pod_id} to pool state — {len(pods)} pods total")


def _on_signal(sig, _frame) -> None:
    """SIGTERM/SIGINT handler. Does NOT tear down child pods — scavenged
    pods are owned by the NEXT sweep, not this scavenger. Operator who
    wants pods+scavenger gone together must call teardown separately."""
    global _SHUTDOWN_REQUESTED
    log(f"received signal {sig}; will exit after current attempt")
    _SHUTDOWN_REQUESTED = True


def _on_exit() -> None:
    """atexit — log final pool size. Scavenged pods are intentionally
    NOT torn down (next sweep owns them)."""
    pods = current_pool()
    log(f"scavenger exit — final pool size {len(pods)}")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    atexit.register(_on_exit)

    _gc_stale_state()  # F7

    start = time.time()
    log(f"scavenger started; target={TARGET_POOL_SIZE}; "
        f"cap={WALL_CAP_S}s; pool_state={POOL_STATE}")

    while time.time() - start < WALL_CAP_S and not _SHUTDOWN_REQUESTED:
        existing = current_pool()
        if len(existing) >= TARGET_POOL_SIZE:
            log(f"target reached ({len(existing)}); exiting")
            return 0
        log(f"existing pool size: {len(existing)}; attempting +1")
        try:
            pod_id = resume_helper.launch_pod()
            if pod_id:
                url = resume_helper.proxy_url(pod_id)
                append_pod(pod_id, url)
            else:
                log("  launch returned None; retrying")
        except SystemExit:
            log("  all GPU types declined; sleeping")
        except Exception as e:
            log(f"  exception: {type(e).__name__}: {e}")
        # Sleep in 1s slices so signal handlers can interrupt promptly
        slept = 0
        while slept < RETRY_GAP_S and not _SHUTDOWN_REQUESTED:
            time.sleep(1)
            slept += 1

    if _SHUTDOWN_REQUESTED:
        log("shutdown requested; exiting")
    else:
        log("WALL_CAP hit; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
