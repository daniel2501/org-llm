# [[file:../../../org/20260425230731-org_llm.org::*life_support.py][life_support.py:1]]
"""Life-support — host-system health probes for a self-hosted second brain.

Trek reference plus literal: this app runs locally on the user's machine,
so its substrate IS the life-support system. When battery dies, when
RAM is exhausted, when the disk fills, when Ollama wedges, when the
auto-embedder daemon falls over — those are the failure modes that
turn a great tool into a frustrating one.

This module provides deterministic probes for the moving parts:

  • Battery (level + state)              — laptop survival
  • CPU                                  — load + per-core idle
  • RAM                                  — free / total / swap
  • Disk                                 — vault, DB, snapshots roots
  • Thermals                             — coolant levels, in TNG terms
  • Network                              — link state for cloud routing
  • Ollama                               — daemon reachable + models pulled
  • Auto-embedder                        — watcher daemon status

Each probe returns a `Reading` namedtuple with:

  • value        — the raw measurement (float / int / bool)
  • normalized   — 0..1 health score where 1.0 = nominal, 0 = critical
  • label        — short human display ("87%", "0.4 load", "12 GB free")
  • status       — "nominal" | "watch" | "alert" | "critical"
  • message      — one-line message (themable; trek-flavoured by default)

Prefer stdlib paths (/proc, /sys, os.getloadavg) for performance and
to avoid the psutil import on the hot path. Fall back to psutil for
the cross-platform corners (macOS battery, Windows once we get there).
psutil is a soft dependency — every probe degrades gracefully when it
isn't importable.
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path
from typing import NamedTuple


# ── primitives ───────────────────────────────────────────────────────────────

class Reading(NamedTuple):
    name:       str
    value:      float | int | bool | None
    normalized: float          # 0..1; 1.0 = nominal, 0 = critical
    label:      str
    status:     str            # "nominal" | "watch" | "alert" | "critical"
    message:    str            # one-line, trek-flavoured by default


# Status thresholds — used by probes that produce a 0..1 normalized score
# to bucket into a status label.
def _status_from_norm(norm: float) -> str:
    if   norm >= 0.7: return "nominal"
    elif norm >= 0.4: return "watch"
    elif norm >= 0.2: return "alert"
    else:             return "critical"


# ── battery ──────────────────────────────────────────────────────────────────

def probe_battery() -> Reading:
    """Read battery percentage + plugged state from /sys/class/power_supply.

    Falls back to psutil.sensors_battery() on non-Linux. Returns a
    name="battery" Reading where value is the percent (0-100), or None
    when no battery is present (desktop / VM).
    """
    # Linux /sys path first — no dep, very fast.
    try:
        ps_root = Path("/sys/class/power_supply")
        for entry in sorted(ps_root.iterdir()):
            if not entry.name.startswith("BAT"):
                continue
            cap = (entry / "capacity").read_text().strip()
            status = (entry / "status").read_text().strip()
            pct = int(cap)
            plugged = status.lower() in ("charging", "full", "not charging")
            norm = pct / 100.0
            # Ignore the floor when plugged in — battery can be 5% but
            # the host is fine because it's on AC.
            if plugged:
                norm = max(norm, 0.95)
            stat = _status_from_norm(norm)
            label = (f"{pct}% {'⚡' if plugged else '🔋'}")
            msg = _battery_message(pct, plugged)
            return Reading("battery", pct, norm, label, stat, msg)
    except (FileNotFoundError, OSError, ValueError):
        pass
    # Non-Linux / fallback via psutil
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return Reading("battery", None, 1.0, "AC only",
                            "nominal", "Auxiliary power not required.")
        pct = int(b.percent)
        norm = pct / 100.0
        if b.power_plugged:
            norm = max(norm, 0.95)
        stat = _status_from_norm(norm)
        label = f"{pct}% {'⚡' if b.power_plugged else '🔋'}"
        return Reading("battery", pct, norm, label, stat,
                        _battery_message(pct, b.power_plugged))
    except Exception:
        return Reading("battery", None, 1.0, "n/a",
                        "nominal", "Battery sensor unreachable.")


def _battery_message(pct: int, plugged: bool) -> str:
    if plugged:
        return "External power source engaged. Reserves recharging."
    if pct < 10:
        return "RED ALERT — emergency power cells failing. Plug in NOW."
    if pct < 20:
        return "Auxiliary power dropping. Recommend connecting to mains."
    if pct < 40:
        return "Reserves at half. Cloud routing advised for heavy work."
    return "Auxiliary power nominal."


# ── CPU + memory ─────────────────────────────────────────────────────────────

def probe_cpu() -> Reading:
    """1-minute load average normalised by core count.

    A load of 1.0 per core is "fully busy"; we cap norm at 1.0 there
    and call >2.0/core "alert".
    """
    try:
        load1, _, _ = os.getloadavg()
    except OSError:
        return Reading("cpu", None, 1.0, "n/a",
                        "nominal", "Load probe unavailable.")
    try:
        n_cores = os.cpu_count() or 1
    except Exception:
        n_cores = 1
    per_core = load1 / max(1, n_cores)
    # Inverse: low per-core = high norm (healthy)
    norm = max(0.0, 1.0 - per_core / 2.0)
    norm = min(1.0, max(0.0, norm))
    stat = _status_from_norm(norm)
    label = f"load {load1:.2f} / {n_cores}c"
    if   per_core < 0.5: msg = "Sublight engines idle — capacity to spare."
    elif per_core < 1.0: msg = "Impulse drive engaged. Steady cruise."
    elif per_core < 1.5: msg = "Warp coils approaching nominal load."
    elif per_core < 2.0: msg = "Power conduits saturated — limit new tasks."
    else:                msg = "RED ALERT — primary systems overloaded."
    return Reading("cpu", load1, norm, label, stat, msg)


def probe_memory() -> Reading:
    """Free RAM as percent of total (read from /proc/meminfo, falls back
    to psutil)."""
    free_gb = total_gb = None
    # Linux /proc path
    try:
        meminfo = Path("/proc/meminfo").read_text()
        kv: dict[str, int] = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                try:
                    kv[parts[0][:-1]] = int(parts[1])
                except ValueError:
                    continue
        if "MemTotal" in kv and "MemAvailable" in kv:
            total_gb = kv["MemTotal"] / 2**20
            free_gb  = kv["MemAvailable"] / 2**20
    except (FileNotFoundError, OSError):
        pass
    if free_gb is None:
        try:
            import psutil
            vm = psutil.virtual_memory()
            total_gb = vm.total / 2**30
            free_gb  = vm.available / 2**30
        except Exception:
            return Reading("memory", None, 1.0, "n/a",
                            "nominal", "Memory probe unavailable.")
    pct_free = free_gb / total_gb if total_gb else 0.0
    norm = pct_free
    label = f"{free_gb:.1f} / {total_gb:.1f} GB free"
    # Memory-specific bands aligned to the message thresholds below.
    # The generic _status_from_norm cliff at 0.7 (== 70% free) was way
    # too tight — 51% free RAM is plenty on a 16 GB laptop and should
    # not be tagged "watch" with a message saying "plenty".
    if   pct_free >= 0.30:
        stat, msg = "nominal", "Holodecks online. Plenty of working memory."
    elif pct_free >= 0.15:
        stat = "watch"
        msg  = "Recommend purging unused models or routing to cloud."
    elif pct_free >= 0.07:
        stat = "alert"
        msg  = "Memory tight — local LLM may not load. Use --cloud."
    else:
        stat, msg = "critical", "RED ALERT — memory exhaustion imminent."
    return Reading("memory", free_gb, norm, label, stat, msg)


# ── disk ─────────────────────────────────────────────────────────────────────

def probe_disk() -> Reading:
    """Free disk on the partition holding ~/.local/share/org-llm/.

    Picks the most user-relevant partition for the app: where the DB,
    snapshots, and (typically) the vault all live.
    """
    target = Path(os.environ.get("ORG_LLM_DB")
                   or os.path.expanduser("~/.local/share/org-llm")).parent
    try:
        usage = shutil.disk_usage(target)
    except (FileNotFoundError, OSError):
        return Reading("disk", None, 1.0, "n/a",
                        "nominal", "Disk probe unavailable.")
    free_gb  = usage.free  / 2**30
    total_gb = usage.total / 2**30
    pct_free = usage.free / max(1, usage.total)
    # Disk-specific bands: 25%+ free is normal; below 5% is dire.
    # The generic _status_from_norm cliff at 0.7 was way too tight —
    # it called 60% free "watch" while the message read "well-stocked".
    if   pct_free >= 0.25:
        stat, msg = "nominal", "Cargo bays well-stocked."
    elif pct_free >= 0.10:
        stat = "watch"
        msg  = "Recommend `org-llm db --vacuum` and snapshot pruning."
    elif pct_free >= 0.05:
        stat = "alert"
        msg  = "Stowage critical — clear cache or move data."
    else:
        stat, msg = "critical", "RED ALERT — disk exhaustion imminent."
    label = f"{free_gb:.1f} / {total_gb:.1f} GB"
    # Keep `normalized` proportional for trend analysis — sensor_log
    # consumers (LLM advice, sparklines) read it.
    return Reading("disk", free_gb, pct_free, label, stat, msg)


# ── thermals ─────────────────────────────────────────────────────────────────

def probe_thermal() -> Reading:
    """CPU package / coretemp temperature, normalised against the
    sensor's REPORTED critical threshold (typically 100°C for Intel,
    similar for AMD). NOT the highest sensor across all zones — that
    falsely flagged routine 77°C CPU loads as "critical" because the
    old hand-coded curve assumed 95°C critical, ignoring the real
    sensor metadata.

    Pick order:
      1. psutil coretemp `Package id 0` (or first Core entry)
      2. /sys/class/thermal zone whose `type` looks CPU-ish
      3. psutil any `*temp*` entry as last resort

    Skip non-CPU thermals (wifi, nvme, gpu) — those have very
    different design envelopes and reading the max across them
    surfaces the wrong warnings (NVMe at 32°C is the COLD one).
    """
    cpu_temp: float | None = None
    crit_temp: float = 100.0   # safe Intel/AMD assumption when no sensor reports

    # First choice — psutil with proper labels + critical threshold.
    try:
        import psutil
        data = (psutil.sensors_temperatures()
                  if hasattr(psutil, "sensors_temperatures") else {})
        # Prefer coretemp Package; fall back to first Core; then anything else.
        ranked: list[tuple[int, float, float | None]] = []
        for chip, entries in data.items():
            for t in entries:
                if not t.current or t.current <= 0:
                    continue
                lbl = (t.label or "").lower()
                # Skip clearly non-CPU sensors
                if any(x in chip.lower()
                        for x in ("nvme", "wifi", "wlan", "iwlwifi",
                                   "battery", "gpu")):
                    continue
                if "gpu" in lbl:
                    continue
                # Score: package > core 0 > any core > anything labelled cpu
                score = 0
                if "package" in lbl:                   score = 100
                elif lbl == "core 0":                  score = 80
                elif lbl.startswith("core"):           score = 70
                elif "cpu" in lbl or chip.lower() == "coretemp":
                                                        score = 50
                else:
                    continue
                ranked.append((score, t.current, t.critical))
        if ranked:
            ranked.sort(reverse=True)
            cpu_temp = ranked[0][1]
            if ranked[0][2]:
                crit_temp = float(ranked[0][2])
    except Exception:
        pass

    # Second choice — /sys/class/thermal cpu-ish zone.
    if cpu_temp is None:
        try:
            for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
                try:
                    zt = (zone / "type").read_text().strip().lower()
                except FileNotFoundError:
                    continue
                # CPU package types across distros
                if not any(s in zt for s in ("x86_pkg_temp", "cpu",
                                                "coretemp", "k10temp",
                                                "zen", "soc")):
                    continue
                try:
                    t_milli = int((zone / "temp").read_text().strip())
                    cpu_temp = t_milli / 1000.0
                    break
                except (FileNotFoundError, ValueError):
                    continue
        except (FileNotFoundError, OSError):
            pass

    if cpu_temp is None:
        return Reading("thermal", None, 1.0, "n/a", "nominal",
                        "Thermal sensors unreachable.")

    # Normalise against the sensor's reported critical threshold.
    # Treat 50°C as the "totally idle, room-temp baseline" and the
    # sensor's reported `critical` as the floor (norm=0). 80% of the
    # way to critical → norm=0.2 (alert), 60% → 0.4 (watch),
    # below 40% → nominal. This puts a 77°C reading on a
    # critical=100°C CPU at norm=0.46 (watch) instead of "critical",
    # matching what the silicon's actually doing.
    floor = 50.0
    crit  = max(crit_temp, floor + 10.0)   # avoid div-by-zero
    norm = (crit - cpu_temp) / (crit - floor)
    norm = max(0.0, min(1.0, norm))
    headroom = crit - cpu_temp
    label = f"{cpu_temp:.0f}°C / crit {crit:.0f}°C"
    # Thermal-specific bands by °C of headroom — generic norm cliff
    # at 0.7 was too tight (30°C of headroom → norm=0.6 → "watch"
    # contradicting the "plenty of headroom" message). Status now
    # follows headroom directly so the message and status agree.
    if   headroom > 20:
        stat, msg = "nominal", "Coolant flow nominal — plenty of headroom."
    elif headroom > 12:
        stat, msg = "watch",   "Temperatures elevated under load. Vents OK."
    elif headroom >  6:
        stat, msg = "alert",   "Plasma manifolds running hot — close to throttle."
    else:
        stat, msg = "critical", "RED ALERT — thermal critical. Throttle imminent."
    return Reading("thermal", cpu_temp, norm, label, stat, msg)


# ── network ──────────────────────────────────────────────────────────────────

def probe_network(*, target: str = "1.1.1.1", port: int = 53,
                    timeout: float = 1.0) -> Reading:
    """TCP-connect probe — lightweight ICMP-free liveness check.

    Defaults to Cloudflare DNS (1.1.1.1:53). Override per call when
    offline-first deployments need a specific check (LAN endpoint,
    self-hosted Ollama on another host, etc.).
    """
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return Reading("network", True, 1.0,
                            f"online → {target}",
                            "nominal", "Subspace link nominal.")
    except (socket.timeout, OSError) as e:
        return Reading("network", False, 0.0,
                        f"offline ({type(e).__name__})",
                        "alert",
                        "Subspace link silent. Cloud routes unavailable.")


# ── Ollama daemon ────────────────────────────────────────────────────────────

def probe_ollama() -> Reading:
    """Probe the configured Ollama URL for /api/tags."""
    import urllib.request, json
    # Resolve URL the same way the rest of the app does.
    url = os.environ.get("ORG_LLM_OLLAMA_URL", "")
    if not url:
        try:
            from .db import DB_PATH, Config, make_engine
            from sqlalchemy.orm import Session
            if DB_PATH.exists():
                eng = make_engine(DB_PATH)
                with Session(eng) as s:
                    row = s.get(Config, "ollama_url")
                    if row and row.value:
                        url = row.value
        except Exception:
            pass
    url = (url or "http://localhost:11434").rstrip("/")
    try:
        req = urllib.request.Request(f"{url}/api/tags",
                                       headers={"User-Agent": "org-llm/life-support"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        n_models = len(data.get("models") or [])
        return Reading("ollama", n_models, 1.0,
                        f"online · {n_models} model{'s' if n_models != 1 else ''}",
                        "nominal", "Local LLM bays online.")
    except Exception as e:
        return Reading("ollama", 0, 0.0,
                        f"unreachable ({type(e).__name__})",
                        "alert",
                        "Local LLM bays offline. Run `ollama serve`.")


# ── auto-embedder ────────────────────────────────────────────────────────────

def probe_auto_embedder() -> Reading:
    """Read auto-embedder state file written by org_llm.auto_embedder."""
    state_path = Path(os.environ.get("ORG_LLM_AUTO_EMBED_STATE", "")
                       or os.path.expanduser(
                           "~/.local/share/org-llm/auto-embed.state.json"))
    if not state_path.exists():
        return Reading("auto_embedder", False, 1.0, "not running",
                        "nominal", "Auto-embedder dormant (foreground only).")
    try:
        import json
        s = json.loads(state_path.read_text())
        last_run = s.get("last_run_ts", 0)
        age = time.time() - last_run
        # Healthy: ran within 5× its own interval.
        interval = s.get("interval_secs", 60)
        norm = max(0.0, 1.0 - age / (interval * 5)) if interval else 1.0
        if age > interval * 10:
            stat, msg = "alert", (f"Auto-embedder hasn't run in "
                                    f"{age/60:.0f} min — daemon stuck?")
        elif age > interval * 5:
            stat, msg = "watch", (f"Auto-embedder lagging behind its "
                                    f"interval ({age/60:.0f} min).")
        else:
            stat, msg = "nominal", "Auto-embedder ticking on schedule."
        return Reading("auto_embedder", round(age, 1), norm,
                        f"{age/60:.1f} min since last run", stat, msg)
    except Exception:
        return Reading("auto_embedder", False, 1.0, "state unreadable",
                        "watch", "Auto-embedder state file malformed.")


# ── orchestration ────────────────────────────────────────────────────────────

# Order matters — readout panels render top-to-bottom.
ALL_PROBES = (
    probe_battery,
    probe_cpu,
    probe_memory,
    probe_disk,
    probe_thermal,
    probe_network,
    probe_ollama,
    probe_auto_embedder,
)


def probe_all() -> list[Reading]:
    """Run every probe once. Probes are independent and side-effect-
    free; an exception in one never blocks another."""
    out: list[Reading] = []
    for fn in ALL_PROBES:
        try:
            out.append(fn())
        except Exception as e:
            out.append(Reading(
                fn.__name__.replace("probe_", ""),
                None, 1.0, "probe error",
                "watch", f"Probe failed: {type(e).__name__}: {str(e)[:80]}",
            ))
    return out


def overall_status(readings: list[Reading]) -> str:
    """Aggregate status — the worst of any individual probe."""
    rank = {"nominal": 0, "watch": 1, "alert": 2, "critical": 3}
    worst = max(readings, key=lambda r: rank.get(r.status, 0))
    return worst.status


# ── timeseries persistence ────────────────────────────────────────────────────

def _activity_context(secs: int = 30) -> str:
    """Summarise the user's last few CLI / LLM / MCP events from
    History so the sensor_log row carries 'what was happening' at
    probe time. Used to correlate resource spikes with the activity
    that caused them.

    Returns a short single-line string like:
      `cli:ask|llm:chat:gemma3(45s)|llm:chat:gemma3(38s)`
    or "" when there's no recent activity.
    """
    try:
        from .db import DB_PATH, History, make_engine, get_session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return ""
        engine = make_engine(path)
        cutoff = int(time.time()) - max(5, secs)
        with get_session(engine) as s:
            rows = (s.query(History)
                      .order_by(History.id.desc())
                      .limit(8).all())
        from datetime import datetime as _dt
        bits: list[str] = []
        for r in rows:
            try:
                ts_dt = _dt.fromisoformat(r.timestamp.replace(" ", "T"))
                if ts_dt.timestamp() < cutoff:
                    continue
            except Exception:
                pass
            kind = (r.kind or "").lower()
            cmd  = (r.command or "?")[:24]
            mdl  = (r.model or "")[:20]
            dur  = (f"{r.duration_ms / 1000:.0f}s"
                     if r.duration_ms else "")
            bit  = f"{kind}:{cmd}"
            if mdl:
                bit += f":{mdl}"
            if dur:
                bit += f"({dur})"
            bits.append(bit)
        return "|".join(bits[:6])
    except Exception:
        return ""


def record_readings(readings: list[Reading]) -> int:
    """Persist a probe snapshot to the sensor_log table.

    Returns the number of rows written. Best-effort: a DB error never
    fails the probe call. Polling loops stay running even when the DB
    is locked or temporarily missing.

    Each row carries an `activity context` snapshot — the last few
    History events in the seconds before the reading. The LLM advice
    path uses this column to correlate resource spikes with the
    activity that caused them ("CPU pegged at 8.0 while ask --reason
    deepseek-r1:7b ran 240s") instead of just describing the spike.
    """
    try:
        from .db import DB_PATH, SensorLog, make_engine, get_session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return 0
        engine = make_engine(path)
        ts = int(time.time())
        ctx = _activity_context()    # captured ONCE per snapshot
        n = 0
        with get_session(engine) as s:
            for r in readings:
                s.add(SensorLog(
                    ts=ts, probe=r.name,
                    value=str(r.value) if r.value is not None else "",
                    normalized=f"{r.normalized:.4f}",
                    status=r.status,
                    label=r.label,
                    message=r.message,
                    context=ctx,
                ))
                n += 1
            s.commit()
        return n
    except Exception:
        return 0


def recent_readings(probe: str | None = None, *,
                      since_secs: int = 3600,
                      limit: int = 1000) -> list[dict]:
    """Read recent rows from sensor_log. Returns newest-first.

    `probe=None` returns all probes; pass a probe name (battery / cpu /
    mem / disk / thermal / network / ollama / auto_embedder) to scope.
    `since_secs` defaults to one hour; `limit` caps row count to keep
    LLM context windows manageable.
    """
    try:
        from .db import DB_PATH, SensorLog, make_engine, get_session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return []
        engine = make_engine(path)
        cutoff = int(time.time()) - max(60, since_secs)
        with get_session(engine) as s:
            q = (s.query(SensorLog)
                   .filter(SensorLog.ts >= cutoff)
                   .order_by(SensorLog.ts.desc()))
            if probe:
                q = q.filter(SensorLog.probe == probe)
            rows = q.limit(limit).all()
        return [{"ts": r.ts, "probe": r.probe, "value": r.value,
                  "normalized": r.normalized, "status": r.status,
                  "label": r.label, "message": r.message,
                  "context": getattr(r, "context", "") or ""}
                for r in rows]
    except Exception:
        return []


# ── trend analysis: deterministic floor + LLM advice ────────────────────────

# Below this many SAMPLES PER PROBE in the window the LLM doesn't
# have enough signal to detect real trends and confabulates ("battery
# is rapidly draining" from 2 readings 30 seconds apart). Falls back
# to a flat status-summary listing. Same lesson as cloud --refresh-
# catalog / log --reflect / history build; codified at the boundary.
_MIN_SAMPLES_PER_PROBE = 12


def deterministic_summary(readings: list[Reading]) -> str:
    """Plain-text status block of the current readings — no LLM call,
    no synthesis, just the labels + statuses + messages. Always safe.
    """
    lines = []
    for r in readings:
        sym = {"nominal": "✓", "watch": "•",
                "alert": "⚠", "critical": "✗"}.get(r.status, "?")
        lines.append(f"  {sym} {r.name:<14} {r.label:<24} {r.message}")
    return "\n".join(lines)


def llm_optimization_advice(window_secs: int = 3600) -> str:
    """Ask the chat model for concrete model + config optimisation
    suggestions based on the recent sensor_log window.

    Subject to the same small-sample-size guard as other LLM-synthesis
    paths: when fewer than `_MIN_SAMPLES_PER_PROBE` samples per probe
    are present in the window, returns a deterministic message
    instead. Output gets the rescue.sanitize_llm_advice pass before
    being returned, so banned shell patterns / unrecognised commands
    surface as flagged advice.

    Returns "" when the analysis genuinely had nothing to say (steady
    state, no rows, etc.) so the caller can decide whether to render
    the section.
    """
    rows = recent_readings(since_secs=window_secs, limit=2000)
    if not rows:
        return ""
    by_probe: dict[str, list[dict]] = {}
    for r in rows:
        by_probe.setdefault(r["probe"], []).append(r)
    sparse = {p: len(v) for p, v in by_probe.items()
              if len(v) < _MIN_SAMPLES_PER_PROBE}
    if sparse:
        return (f"(deterministic mode — only "
                f"{min(sparse.values())} sample(s) for "
                f"{', '.join(sorted(sparse))}; need "
                f"≥{_MIN_SAMPLES_PER_PROBE} per probe for trend "
                f"analysis. Keep `org-llm life-support --interval 30` "
                f"running and check back in a few minutes.)")

    # Short-circuit: if NOTHING in the window tripped a non-nominal
    # status, don't call the LLM. Small chat models hallucinate
    # problems out of steady-state telemetry (treating `normalized`
    # as utilization, treating labels as config args, inventing
    # "battery is steadily draining" from a flat 96% line). Same
    # lesson as the small-sample floor, applied to the trivial-
    # input case. This is by far the common path on a healthy host.
    non_nominal = [v for v in rows if v["status"] != "nominal"]
    if not non_nominal:
        n_total = sum(len(v) for v in by_probe.values())
        return (f"All systems nominal across {len(by_probe)} "
                f"probe(s), {n_total} sample(s) over the last "
                f"{window_secs // 60} minute(s). No optimisations "
                f"to recommend. Advice surfaces automatically when "
                f"a probe trips watch / alert / critical.")

    # Compose a tight prompt: latest reading + status COUNTS per
    # probe. Deliberately NOT exposing the 0..1 `normalized` health
    # score — small chat models keep inverting it (treating norm-mean
    # as "% utilization") even when the prompt defines it. Status
    # enums are unambiguous; the probe has already done the health
    # calculation, so the LLM never needs to recompute.
    summary_lines: list[str] = []
    for probe, vs in sorted(by_probe.items()):
        latest = vs[0]
        counts: dict[str, int] = {}
        for v in vs:
            k = (v.get("status") or "nominal")
            counts[k] = counts.get(k, 0) + 1
        counts_text = " ".join(
            f"{k}={counts.get(k, 0)}"
            for k in ("nominal", "watch", "alert", "critical")
        )
        summary_lines.append(
            f"  {probe:<14} latest={latest['label']:<22} "
            f"status_now={latest['status']:<8} "
            f"window_counts: {counts_text}  n={len(vs)}"
        )

    # Activity correlation — pair readings where status was alert/
    # critical with the user-activity context recorded at that moment.
    # Lets the model say "CPU pegged WHILE ask --reason was running"
    # instead of just "CPU is high".
    activity_correlations: list[str] = []
    seen_ctx: set[str] = set()
    for v in rows:
        if v["status"] not in ("alert", "critical"):
            continue
        ctx = (v.get("context") or "").strip()
        if not ctx or ctx in seen_ctx:
            continue
        seen_ctx.add(ctx)
        activity_correlations.append(
            f"  {v['probe']:<10} {v['status']:<8} during: {ctx}"
        )
        if len(activity_correlations) >= 8:
            break

    sys_msg = (
        "You read host-system probe readings from a self-hosted "
        "second-brain CLI tool. The user has at least ONE probe "
        "in watch / alert / critical state — surface concrete "
        "optimisation suggestions for THOSE probes only, not the "
        "nominal ones.\n"
        "\n"
        "How to read the data:\n"
        "  • `status_now` is the current health enum: nominal / "
        "watch / alert / critical. The probe has already done the "
        "health calculation. Trust it.\n"
        "  • `window_counts` shows how many readings landed in each "
        "status enum during the window. e.g. "
        "`nominal=80 watch=2 alert=0 critical=0` means the probe "
        "was mostly fine with two brief blips.\n"
        "  • `label` is a human-readable display string (e.g. "
        "'12 models', 'not running', '76°C / crit 100°C'). It is "
        "NOT a config value. Do NOT propose 'swap to latest=<label>' "
        "or treat label fragments as command arguments.\n"
        "\n"
        "Output STRICTLY 1-3 bullet lines starting with `• `, no "
        "preamble. Each bullet: ONE actionable suggestion targeting "
        "a non-nominal probe. Reference the probe name and the "
        "activity correlation when present (e.g. 'CPU pegged WHILE "
        "`ask --reason` running'). If the flagged probes look like "
        "normal load with no clear lever, say so in ONE bullet — "
        "do not invent problems. Never propose installing packages "
        "outside the org-llm verb surface; never propose "
        "`pip install`, `curl | sh`, or any open-shell command."
    )
    user_msg = (
        f"Probe summary over the last "
        f"{window_secs // 60} minute(s):\n\n"
        + "\n".join(summary_lines)
    )
    if activity_correlations:
        user_msg += (
            "\n\nActivity correlations — what the user was doing when "
            "the probes flagged alert/critical (most-recent first):\n"
            + "\n".join(activity_correlations)
        )
    user_msg += (
        "\n\nWhat ONE OR TWO concrete optimisations should the user "
        "consider? Reference real probe values AND the activity "
        "correlations when present."
    )

    advice = ""
    try:
        from .llm import chat as _chat
        from .db import DB_PATH, Config, get_session, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return deterministic_summary([])
        engine = make_engine(path)
        with Session(engine) as s:
            mdl_row = (s.get(Config, "fast_model")
                         or s.get(Config, "chat_model"))
            mdl = mdl_row.value if mdl_row else "llama3.2"
            url_row = s.get(Config, "ollama_url")
            url = (url_row.value if url_row
                    else "http://localhost:11434")
        advice = _chat(user_msg, model=mdl, base_url=url,
                        system=sys_msg, timeout=45.0) or ""
    except Exception as e:
        return f"(LLM advice unavailable: {type(e).__name__}: {e})"

    if not advice.strip():
        return ""
    try:
        from .rescue import sanitize_llm_advice as _san
        checked = _san(advice)
        if not checked.safe:
            flag = "; ".join(checked.flagged)
            return (f"[FLAGGED: {flag}] "
                    f"VERIFY BEFORE ACTING ON:\n{advice.strip()}")
    except Exception:
        pass
    return advice.strip()
# life_support.py:1 ends here
