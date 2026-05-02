#!/usr/bin/env python3
"""Audit whether Phase 17.1 defaults (=sidebar_slow_llm_threshold_ms=,
=sidebar_auto_session=, the proxy interceptor chain) are pulling their
weight ~2 weeks after launch (committed ed3079e on 2026-05-02). Run
locally near 2026-05-16; if the data suggests defaults need
adjustment, the script writes a flag file the remote tuning agent
will pick up.

Usage::

    uv run python scripts/audit_phase17.py
    uv run python scripts/audit_phase17.py --cutoff 2026-05-02 --window 14
    uv run python scripts/audit_phase17.py --branch audit/phase17-2026-05-16
    uv run python scripts/audit_phase17.py --no-flag       # report only

Why it lives here (not the remote agent):
    The signal is local-only. ~/.local/share/org-llm/llm-audit.jsonl
    (proxy interceptor + latency log) and the SQLite History table
    are the load-bearing data; a remote agent in Anthropic's cloud
    has no path to either. Same hybrid pattern as
    scripts/audit_agents_md.py — local audit pushes a flag-file
    branch; the remote routine reads the flag and opens a tuning PR.

What we're checking:
    1. Interceptor distribution — which short-circuits actually fire?
    2. Forward-path latency — p50/p95/p99 of non-intercepted requests.
       The 25s slow-LLM threshold is meaningful only if real latency
       has interesting density around that line.
    3. Slow-LLM trip rate — fraction of forwards that exceed the
       threshold. Too high → users routinely hit the auto-doctor
       (annoying); too low (zero hits in 2 weeks) → threshold may be
       too generous to be useful.
    4. Cache potential — repeated prompt hashes that bypass
       intercept_response_cache (i.e. duplicates landing on the
       forward path). High duplicate count = prefix cache (Phase 18)
       is worth prioritising.
    5. Doctor / cloud-failover invocation counts — sanity check that
       slow-llm-watch.tsx is actually wiring through.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Phase 17.1 commit landed on this date (ed3079e). The audit window
# defaults to 14 days starting from the cutoff — by 2026-05-16 there
# should be enough JSONL entries for a verdict.
DEFAULT_CUTOFF = "2026-05-02"
DEFAULT_WINDOW_DAYS = 14

# Default values shipped in db.py MODEL_DEFAULTS — what we're auditing
# AGAINST. The flag-file directions reference these so the remote
# agent knows which knob to bump.
SHIPPED_DEFAULTS = {
    "sidebar_slow_llm_threshold_ms":  25000,
    "sidebar_auto_session":           True,
    "sidebar_auto_session_use_cloud": False,
    "sidebar_slow_llm_confirm":       True,
    "sidebar_slow_llm_auto_relaunch": True,
    "proxy_local_only":               False,
}

# Verdict thresholds. Tuned conservatively — we'd rather report
# "looks fine" than open a PR over noise.

# If <2% of forward requests trip the slow-LLM threshold, the watcher
# is essentially dormant. Either UX is bad enough nobody triggers
# slow flows, or the threshold is too high to be useful.
SLOW_TRIP_FLOOR_PCT     = 2.0
# Above this trip rate, the watcher is firing too often — every 25th
# turn or so users are getting an auto-doctor injection. Lower the
# threshold or raise auto-confirm.
SLOW_TRIP_CEILING_PCT   = 8.0
# If repeated prompt-hash on the forward path exceeds this, prefix
# cache from Phase 18 is high-value.
CACHE_DUPE_FLAG_PCT     = 15.0
# Minimum forward-path sample size for any verdict. Below this the
# audit is "inconclusive" — wait for more data.
MIN_FORWARD_SAMPLE      = 50

FLAG_PATH = Path(".audit/phase17-tuning-needed.md")
JSONL_PATH = (Path(os.environ.get("XDG_DATA_HOME") or
                    (Path.home() / ".local" / "share"))
                / "org-llm" / "llm-audit.jsonl")


@dataclass
class ProxyStats:
    """Summary of llm-audit.jsonl entries within the window."""
    label:           str
    start_ts:        float
    end_ts:          float
    total:           int = 0
    by_interceptor:  Counter = field(default_factory=Counter)
    forward_count:   int = 0
    forward_durations_ms: list[float] = field(default_factory=list)
    forward_hashes:  Counter = field(default_factory=Counter)
    error_count:     int = 0
    by_status:       Counter = field(default_factory=Counter)

    @property
    def intercepted_total(self) -> int:
        return sum(c for k, c in self.by_interceptor.items() if k != "forward")

    @property
    def intercept_pct(self) -> float:
        return (self.intercepted_total / self.total * 100) if self.total else 0.0

    @property
    def slow_trip_pct(self) -> float:
        """Fraction of forwards whose duration exceeded the shipped
        slow-LLM threshold. Proxy of how often the watcher would
        fire if the user kept the default."""
        if not self.forward_durations_ms:
            return 0.0
        thr = SHIPPED_DEFAULTS["sidebar_slow_llm_threshold_ms"]
        slow = sum(1 for d in self.forward_durations_ms if d > thr)
        return slow / len(self.forward_durations_ms) * 100

    @property
    def cache_dupe_pct(self) -> float:
        """Fraction of forward requests sharing a prompt_hash with
        another forward request — i.e. a perfect prefix-cache hit."""
        if not self.forward_count:
            return 0.0
        # Count requests beyond the first occurrence of each hash.
        dupes = sum(c - 1 for c in self.forward_hashes.values() if c > 1)
        return dupes / self.forward_count * 100


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def collect_proxy_stats(label: str, start_ts: float, end_ts: float) -> ProxyStats:
    """Stream the JSONL log; collect entries in [start_ts, end_ts).
    Best-effort: bad lines are skipped."""
    stats = ProxyStats(label=label, start_ts=start_ts, end_ts=end_ts)
    if not JSONL_PATH.exists():
        return stats
    try:
        with JSONL_PATH.open() as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                ts = e.get("ts")
                if ts is None:
                    continue
                try:
                    ts_f = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts_f < start_ts or ts_f >= end_ts:
                    continue
                stats.total += 1
                interceptor = e.get("intercepted_by") or "forward"
                stats.by_interceptor[interceptor] += 1
                status = e.get("status")
                if status is not None:
                    stats.by_status[status] += 1
                if e.get("error"):
                    stats.error_count += 1
                if interceptor == "forward":
                    stats.forward_count += 1
                    dur = e.get("duration_ms")
                    if isinstance(dur, (int, float)):
                        stats.forward_durations_ms.append(float(dur))
                    h = e.get("prompt_hash")
                    if h:
                        stats.forward_hashes[h] += 1
    except Exception as exc:
        print(f"warning: failed to read {JSONL_PATH} ({exc}). "
              f"Audit will report what we got.", file=sys.stderr)
    return stats


def collect_doctor_invocations(start_ts: float, end_ts: float) -> Counter:
    """Captain's-log entries with kind='doctor' or kind='proxy' inside
    the window — slow-llm-watch.tsx runs `org-llm doctor --power-boost`
    as a subprocess; the cli wires write_event() into doctor flows."""
    counts: Counter = Counter()
    try:
        from org_llm.db import History, DB_PATH, make_engine
        from sqlalchemy.orm import Session
    except Exception:
        return counts
    db_path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not db_path.exists():
        return counts
    start_iso = datetime.utcfromtimestamp(start_ts).isoformat()
    end_iso   = datetime.utcfromtimestamp(end_ts).isoformat()
    try:
        engine = make_engine(db_path)
        with Session(engine) as s:
            rows = (
                s.query(History)
                .filter(History.kind.in_(["doctor", "proxy", "cli"]))
                .filter(History.timestamp >= start_iso)
                .filter(History.timestamp < end_iso)
                .all()
            )
        for r in rows:
            cmd = (r.command or "").strip()
            if not cmd:
                continue
            # Crude but useful: bucket by leading verb of the command.
            head = cmd.split()[0] if cmd else "?"
            counts[head] += 1
    except Exception:
        pass
    return counts


def render_report(s: ProxyStats, doctor_calls: Counter,
                    cutoff: str, window_days: int) -> str:
    lines = [
        f"# Phase 17.1 audit ({cutoff}, +{window_days}d)",
        "",
        f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"Cutoff: Phase 17.1 committed in ed3079e on {cutoff}.",
        f"Window: {datetime.utcfromtimestamp(s.start_ts).date().isoformat()}"
        f" → {datetime.utcfromtimestamp(s.end_ts).date().isoformat()}.",
        f"Source: {JSONL_PATH}",
        "",
        "## Volume",
        "",
        f"- Total proxy requests: **{s.total}**",
        f"- Forwarded (no interceptor): **{s.forward_count}**"
        f" ({(s.forward_count / s.total * 100) if s.total else 0:.1f}%)",
        f"- Intercepted: **{s.intercepted_total}**"
        f" ({s.intercept_pct:.1f}%)",
        f"- Errors: {s.error_count}",
        "",
        "## Interceptor distribution",
        "",
    ]
    if s.by_interceptor:
        for name, count in s.by_interceptor.most_common():
            pct = count / s.total * 100 if s.total else 0
            lines.append(f"- `{name}`: {count} ({pct:.1f}%)")
    else:
        lines.append("- (no entries in window)")
    lines += [
        "",
        "## Forward-path latency (ms)",
        "",
    ]
    if s.forward_durations_ms:
        durs = s.forward_durations_ms
        lines += [
            f"- Sample: {len(durs)} forwarded request(s)",
            f"- p50: {_percentile(durs, 50):.0f}",
            f"- p95: {_percentile(durs, 95):.0f}",
            f"- p99: {_percentile(durs, 99):.0f}",
            f"- mean: {statistics.fmean(durs):.0f}",
            f"- max:  {max(durs):.0f}",
            "",
            f"- % of forwards exceeding slow-LLM threshold "
            f"({SHIPPED_DEFAULTS['sidebar_slow_llm_threshold_ms']}ms):"
            f" **{s.slow_trip_pct:.1f}%**",
        ]
    else:
        lines.append("- (no forwarded requests in window)")
    lines += [
        "",
        "## Cache potential (forward-path duplicate prompts)",
        "",
        f"- Unique prompt hashes seen on forward: {len(s.forward_hashes)}",
        f"- % of forwards that are exact-prompt duplicates of an earlier"
        f" forward: **{s.cache_dupe_pct:.1f}%**",
        "",
        "> Phase 18's prefix cache only hits on identical-prefix system"
        " prompts; this is the upper bound, not the realistic rate.",
        "",
        "## Doctor / cloud-failover invocations (captain's log)",
        "",
    ]
    if doctor_calls:
        for verb, count in doctor_calls.most_common(10):
            lines.append(f"- `{verb}`: {count}")
    else:
        lines.append("- (no relevant captain's-log rows in window)")
    return "\n".join(lines)


def render_tuning_directions(s: ProxyStats) -> tuple[str, list[str]]:
    """Return (markdown body, list of suggested knob changes for the
    remote PR). Empty list = nothing actionable, no flag needed."""
    lines = ["## Suggested tuning directions", ""]
    suggestions: list[str] = []

    # Slow-LLM threshold tuning.
    if s.forward_durations_ms:
        p95 = _percentile(s.forward_durations_ms, 95)
        thr = SHIPPED_DEFAULTS["sidebar_slow_llm_threshold_ms"]
        if s.slow_trip_pct < SLOW_TRIP_FLOOR_PCT and p95 < thr * 0.5:
            suggestions.append(
                f"Lower `sidebar_slow_llm_threshold_ms` from {thr} to "
                f"{int(p95 * 1.5)} (1.5× p95). Current threshold trips "
                f"on {s.slow_trip_pct:.1f}% of forwards — the watcher "
                f"is essentially dormant; users never get the "
                f"diagnostic when local LLM is sluggish-but-not-dead."
            )
        elif s.slow_trip_pct > SLOW_TRIP_CEILING_PCT:
            suggestions.append(
                f"Raise `sidebar_slow_llm_threshold_ms` from {thr} to "
                f"{int(p95 * 1.1)} (1.1× p95). Current threshold trips "
                f"on {s.slow_trip_pct:.1f}% of forwards — that's "
                f"every ~{int(100 / s.slow_trip_pct)}th turn injecting "
                f"a doctor banner. Users will tune it out."
            )

    # Cache-potential signal.
    if s.cache_dupe_pct >= CACHE_DUPE_FLAG_PCT:
        suggestions.append(
            f"Phase 18 priority-bump: prefix cache. "
            f"{s.cache_dupe_pct:.1f}% of forwards are exact-prompt "
            f"duplicates — that's the easy upper bound. Even a "
            f"system-prompt-only prefix cache should land >5% of "
            f"that. The next-up Phase 18 doc lists this 4th; consider "
            f"promoting to 1st."
        )

    # Interceptor-mix signal.
    if s.total and s.intercept_pct < 5.0:
        suggestions.append(
            f"Proxy interceptor chain firing on only {s.intercept_pct:.1f}% "
            f"of requests. /sys* family may not be discoverable enough — "
            f"audit AGENTS.md primer + sidebar engage card for whether "
            f"users see the slash menu."
        )

    if not suggestions:
        lines.append(
            "- No clear tuning signal. Defaults look workable in the "
            "current usage pattern. Re-run after another window if "
            "user behavior changes."
        )
        return "\n".join(lines), []

    for sg in suggestions:
        lines.append(f"- {sg}")

    lines += [
        "",
        "### Mechanical pointers",
        "",
        "- Defaults: `org_llm/db.py` `MODEL_DEFAULTS` (search "
        "`sidebar_slow_llm`).",
        "- Plugin watcher: "
        "`extensions/opencode/src/slow-llm-watch.tsx`.",
        "- Proxy interceptor chain: "
        "`org_llm/llm_proxy.py` (`start_proxy` builds the default chain).",
        "- Roadmap: `docs/wiki/roadmap.org` § Phase 18 lists the queued "
        "interceptor work.",
    ]
    return "\n".join(lines), suggestions


def write_flag(report: str, directions: str, branch: str) -> Path:
    flag_dir = REPO_ROOT / FLAG_PATH.parent
    flag_dir.mkdir(parents=True, exist_ok=True)
    body = report + "\n\n" + directions + "\n"
    flag_full = REPO_ROOT / FLAG_PATH
    flag_full.write_text(body)

    subprocess.run(["git", "checkout", "-b", branch],
                    cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "add", str(FLAG_PATH)],
                    cwd=REPO_ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m",
         f"audit: flag Phase 17.1 defaults for tuning "
         f"({datetime.utcnow().date().isoformat()})"],
        cwd=REPO_ROOT, check=True,
    )
    return flag_full


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                          help="ISO date Phase 17.1 landed")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                          help="Days after cutoff to inspect")
    parser.add_argument("--branch", default=None,
                          help="Branch name for the flag commit "
                               "(default: audit/phase17-<today>)")
    parser.add_argument("--no-flag", action="store_true",
                          help="Print the report; never write/commit a flag")
    args = parser.parse_args()

    cutoff_dt = datetime.fromisoformat(args.cutoff)
    cutoff_ts = cutoff_dt.timestamp()
    window_secs = args.window * 86400

    branch = (args.branch
              or f"audit/phase17-{datetime.utcnow().date().isoformat()}")

    s = collect_proxy_stats("after", cutoff_ts, cutoff_ts + window_secs)
    doctor_calls = collect_doctor_invocations(cutoff_ts, cutoff_ts + window_secs)

    report = render_report(s, doctor_calls, args.cutoff, args.window)
    print(report)

    if s.forward_count < MIN_FORWARD_SAMPLE:
        print(f"\nVerdict: INCONCLUSIVE (forwarded={s.forward_count}, "
              f"need ≥{MIN_FORWARD_SAMPLE}). No flag written. "
              f"Re-run when more data is available.")
        return 0

    directions, suggestions = render_tuning_directions(s)
    print()
    print(directions)
    print()

    if not suggestions:
        print("Verdict: DEFAULTS LOOK FINE. No flag written.")
        return 0

    print(f"Verdict: TUNING NEEDED ({len(suggestions)} suggestion(s)).")
    if args.no_flag:
        print("--no-flag set; not writing flag file.")
        return 0

    flag_full = write_flag(report, directions, branch)
    print(f"\nFlag committed on branch `{branch}` at {flag_full}.")
    print("Next:")
    print(f"  git push -u origin {branch}")
    print("Then the scheduled remote agent (firing 2026-05-16) will")
    print("read the flag and open a tuning PR against trunk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
