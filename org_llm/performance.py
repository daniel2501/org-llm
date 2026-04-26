# [[file:../../../org/20260425230731-org_llm.org::*performance.py][performance.py:1]]
"""Hardware probe + per-model benchmarks for the `org-llm performance` command.

Pure logic lives here so it can be unit-tested without spawning Ollama.
The CLI command in cli.py wraps these helpers in panels and progress bars.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple


class HardwareSnapshot(NamedTuple):
    cpu_model:        str
    cpu_count:        int
    ram_total_gb:     float
    ram_free_gb:      float
    vram_total_gb:    float | None
    vram_free_gb:     float | None
    disk_free_gb:     float


def probe_hardware() -> HardwareSnapshot:
    """Read /proc, /sys, and nvidia-smi (if present) — never raises."""
    cpu_model = "unknown"
    cpu_count = os.cpu_count() or 1
    ram_total = ram_free = 0.0
    vram_total = vram_free = None
    disk_free = 0.0

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except Exception:
        cpu_model = platform.processor() or platform.machine() or "unknown"

    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
            ram_total = mem.get("MemTotal", 0) / 1_048_576
            avail = mem.get("MemAvailable") or mem.get("MemFree", 0)
            ram_free = avail / 1_048_576
    except Exception:
        pass

    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5, text=True,
            )
            tot = fre = 0.0
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    tot += float(parts[0]) / 1024
                    fre += float(parts[1]) / 1024
            if tot > 0:
                vram_total = tot
                vram_free = fre
        except Exception:
            pass

    try:
        usage = shutil.disk_usage(str(Path.home()))
        disk_free = usage.free / 1_073_741_824
    except Exception:
        pass

    return HardwareSnapshot(
        cpu_model=cpu_model, cpu_count=cpu_count,
        ram_total_gb=ram_total, ram_free_gb=ram_free,
        vram_total_gb=vram_total, vram_free_gb=vram_free,
        disk_free_gb=disk_free,
    )


# ── Per-model benchmarking ────────────────────────────────────────────────────

class BenchmarkResult(NamedTuple):
    model:          str
    role:           str          # which role this benchmark exercises
    latency_ms:     float        # time-to-first-token (best-effort)
    tokens_per_sec: float
    output_tokens:  int
    error:          str          # "" on success, otherwise short message


_BENCH_PROMPT = "Reply with one short sentence about the color blue."
_BENCH_SYSTEM = "Be concise."


def benchmark_chat_model(model: str, base_url: str,
                         timeout: int = 60) -> BenchmarkResult:
    """Run one short chat call and time it. Returns BenchmarkResult."""
    try:
        import ollama
        client = ollama.Client(host=base_url)
        t0 = time.monotonic()
        resp = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _BENCH_SYSTEM},
                {"role": "user",   "content": _BENCH_PROMPT},
            ],
            options={"num_predict": 80},
        )
        elapsed = time.monotonic() - t0
        content = resp.message.content or ""
        out_tokens = max(len(content.split()), 1)   # word≈token approximation
        return BenchmarkResult(
            model=model, role="chat",
            latency_ms=elapsed * 1000,
            tokens_per_sec=out_tokens / elapsed if elapsed > 0 else 0.0,
            output_tokens=out_tokens, error="",
        )
    except Exception as e:
        msg = str(e)
        if len(msg) > 120:
            msg = msg[:120] + "…"
        return BenchmarkResult(
            model=model, role="chat", latency_ms=0.0,
            tokens_per_sec=0.0, output_tokens=0, error=msg,
        )


def benchmark_embed_model(model: str, base_url: str) -> BenchmarkResult:
    try:
        import ollama
        client = ollama.Client(host=base_url)
        text = "The quick brown fox jumps over the lazy dog."
        t0 = time.monotonic()
        resp = client.embed(model=model, input=text)
        elapsed = time.monotonic() - t0
        ok = bool(resp.embeddings)
        return BenchmarkResult(
            model=model, role="embed",
            latency_ms=elapsed * 1000,
            tokens_per_sec=(len(text.split()) / elapsed) if elapsed > 0 and ok else 0.0,
            output_tokens=len(text.split()),
            error="" if ok else "no embeddings returned",
        )
    except Exception as e:
        msg = str(e)
        if len(msg) > 120:
            msg = msg[:120] + "…"
        return BenchmarkResult(
            model=model, role="embed", latency_ms=0.0,
            tokens_per_sec=0.0, output_tokens=0, error=msg,
        )


# ── Recommendation engine ────────────────────────────────────────────────────

class RoleRecommendation(NamedTuple):
    role:        str
    current:     str
    suggested:   str
    reason:      str
    severity:    str    # "fit" | "upgrade" | "downgrade" | "missing"
    measured_tps: float | None   # None if we didn't benchmark


def recommend(
    hw: HardwareSnapshot,
    role_assignments: dict[str, str],   # role -> model_tag
    benchmarks: dict[str, BenchmarkResult],   # model_tag -> result
    pulled: set[str],
    cloud_configured: bool,
) -> list[RoleRecommendation]:
    """Pick role assignments that respect FREE memory and measured throughput.

    Differs from `models.recommendations()` in three ways:
      - uses ram_free_gb (not total × 0.55) because the user has other apps open
      - prefers measured tokens/sec over catalog quality scores when we have data
      - emits explicit DOWNGRADE recommendations for oversized current models
    """
    from .models import CATALOG, ROLE_KEYS, _quality

    budget = hw.vram_free_gb if hw.vram_free_gb is not None else hw.ram_free_gb * 0.6
    out: list[RoleRecommendation] = []

    def _vram_for(tag: str) -> float:
        norm = (tag or "").lower().rstrip(":latest").strip(":")
        for m in CATALOG:
            if m.tag.lower() == norm or m.tag.lower().split(":")[0] == norm.split(":")[0]:
                return m.vram_gb
        return 0.0

    for role in ROLE_KEYS:
        cur = role_assignments.get(role, "")
        cur_vram = _vram_for(cur)
        cur_bench = benchmarks.get(cur)

        # Find the highest-quality catalog entry with this role, fitting budget
        candidates = [m for m in CATALOG if role in m.roles and m.vram_gb <= budget]
        if not candidates:
            # Nothing fits locally — but we still owe the user a verdict.
            if cur:
                # If they have something configured, flag it: oversize iff really too big,
                # otherwise mark as "cloud-only" so the table still shows a row.
                is_oversized = cur_vram > budget + 0.5
                suggested = "(use --cloud)" if cloud_configured else "(no fit; pull more RAM or use cloud)"
                out.append(RoleRecommendation(
                    role=role, current=cur, suggested=suggested,
                    reason=(f"{cur} needs ~{cur_vram:.1f} GB but only {budget:.1f} GB free"
                            if is_oversized
                            else f"role doesn't fit in {budget:.1f} GB; route via --cloud"),
                    severity="downgrade" if is_oversized else "fit",
                    measured_tps=cur_bench.tokens_per_sec if cur_bench else None,
                ))
            continue

        candidates.sort(
            key=lambda m: (
                _quality(m.tag),
                # Prefer pulled models (avoid downloads)
                any(m.tag.split(":")[0] == p.split(":")[0] for p in pulled),
            ),
            reverse=True,
        )
        best = candidates[0]

        if not cur:
            out.append(RoleRecommendation(
                role=role, current="—", suggested=best.tag,
                reason="not assigned",
                severity="missing", measured_tps=None,
            ))
        elif cur_vram > budget + 0.5:
            out.append(RoleRecommendation(
                role=role, current=cur, suggested=best.tag,
                reason=f"{cur} needs ~{cur_vram:.1f} GB but only {budget:.1f} GB free",
                severity="downgrade",
                measured_tps=cur_bench.tokens_per_sec if cur_bench else None,
            ))
        elif cur_bench and cur_bench.error:
            # The model is configured but broken at runtime — definitely swap
            out.append(RoleRecommendation(
                role=role, current=cur, suggested=best.tag,
                reason=f"{cur} errored during benchmark: {cur_bench.error[:60]}",
                severity="downgrade",
                measured_tps=cur_bench.tokens_per_sec if cur_bench else None,
            ))
        elif _quality(best.tag) > _quality(cur.split(":")[0]) + 5:
            out.append(RoleRecommendation(
                role=role, current=cur, suggested=best.tag,
                reason=f"higher quality available ({best.params}, {best.license})",
                severity="upgrade",
                measured_tps=cur_bench.tokens_per_sec if cur_bench else None,
            ))
        else:
            # No change needed — but include for the table
            out.append(RoleRecommendation(
                role=role, current=cur, suggested=cur,
                reason="optimal for this hardware",
                severity="fit",
                measured_tps=cur_bench.tokens_per_sec if cur_bench else None,
            ))

    return out
# performance.py:1 ends here
