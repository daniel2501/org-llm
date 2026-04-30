#!/usr/bin/env python3
"""Audit whether the .opencode/AGENTS.md primer (committed 2026-04-29
in commit a1f0ff9) actually moved engagement metrics. Run it locally
near 2026-05-13; if results indicate the primer is flat or down, the
script writes a flag file the remote tuning agent will pick up.

Usage:
    uv run python scripts/audit_agents_md.py
    uv run python scripts/audit_agents_md.py --cutoff 2026-04-29 --window 14
    uv run python scripts/audit_agents_md.py --branch audit/agents-md-2026-05-13

Why it lives here (not the remote agent):
    The data is local-only. insight_engagement rows live in the user's
    SQLite DB; opencode session traces live under
    ~/.local/share/opencode/storage/. A remote agent in Anthropic's
    cloud has no path to either. The hybrid design (option C from the
    2026-04-29 schedule conversation) is: this script does the audit
    locally and pushes a flag-file branch; the remote routine reads
    the flag and opens a tuning PR.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Default audit window: 14 days each side of the AGENTS.md commit.
DEFAULT_CUTOFF = "2026-04-29"
DEFAULT_WINDOW_DAYS = 14

# Tools the AGENTS.md primer pushes hardest. If these aren't getting
# called more after the primer landed, the primer isn't moving the
# behavior we wanted.
PRIMER_TOOLS = ("search_notes", "ask_notes", "proactive_doctor")

# Threshold: engagement-after / engagement-before. Below this means
# "flat or down" and triggers the tuning flag. Above means "working".
ENGAGEMENT_RATIO_THRESHOLD = 1.05

# Minimum total sample size (before + after) for a verdict. Below this
# the audit is "inconclusive" — no flag is written, the user is told
# to wait for more data.
MIN_SAMPLE_SIZE = 6

FLAG_PATH = Path(".audit/agents-md-tuning-needed.md")


@dataclass
class WindowStats:
    """Engagement + tool-call metrics for a single time window."""
    label: str
    start_ts: int
    end_ts: int
    engagement_total: int = 0
    engagement_good: int = 0          # reaction == "good" or "clicked"
    engagement_bad: int = 0           # reaction == "bad"
    by_kind: Counter = None
    tool_calls_total: int = 0
    tool_calls_primer: Counter = None  # search_notes, ask_notes, proactive_doctor

    def __post_init__(self):
        if self.by_kind is None:
            self.by_kind = Counter()
        if self.tool_calls_primer is None:
            self.tool_calls_primer = Counter()

    @property
    def engagement_rate(self) -> float:
        """good / total. 0.0 when total == 0 — caller checks sample size."""
        return (self.engagement_good / self.engagement_total
                if self.engagement_total else 0.0)


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _fmt_delta(before: float, after: float) -> str:
    if before == 0:
        return f"0 → {after} (∞ — no baseline)"
    delta = (after - before) / before * 100
    sign = "+" if delta >= 0 else ""
    return f"{before} → {after} ({sign}{delta:.0f}%)"


def collect_window(session, label: str, start_ts: int, end_ts: int) -> WindowStats:
    """Pull the engagement + tool-call rows for one window. Imports
    SQLAlchemy models lazily so the script can fail fast with a clear
    error if the user runs it without the venv configured."""
    from org_llm.db import History, InsightEngagement

    stats = WindowStats(label=label, start_ts=start_ts, end_ts=end_ts)

    # Engagement: insight_engagement.shown_at is unix epoch seconds.
    rows = (
        session.query(InsightEngagement)
        .filter(InsightEngagement.shown_at >= start_ts)
        .filter(InsightEngagement.shown_at < end_ts)
        .all()
    )
    for r in rows:
        stats.engagement_total += 1
        if r.reaction in ("good", "clicked"):
            stats.engagement_good += 1
        elif r.reaction == "bad":
            stats.engagement_bad += 1
        stats.by_kind[r.card_kind or "(unknown)"] += 1

    # Tool calls: history.kind == 'mcp', timestamp is ISO string.
    # NOTE: the codebase emits this kind sparsely (one explicit
    # write_event("mcp", ...) at last audit); this signal is best-
    # effort. If the count is implausibly low, fall back to "primer
    # signal is engagement-only" in the report.
    start_iso = datetime.utcfromtimestamp(start_ts).isoformat()
    end_iso = datetime.utcfromtimestamp(end_ts).isoformat()
    mcp_rows = (
        session.query(History)
        .filter(History.kind == "mcp")
        .filter(History.timestamp >= start_iso)
        .filter(History.timestamp < end_iso)
        .all()
    )
    for r in mcp_rows:
        stats.tool_calls_total += 1
        if r.command in PRIMER_TOOLS:
            stats.tool_calls_primer[r.command] += 1

    return stats


def render_report(before: WindowStats, after: WindowStats,
                    cutoff: str, window_days: int) -> str:
    """Build the markdown report. Used both for stdout and for the
    flag file body — same content, the flag's the trigger."""
    lines = [
        f"# AGENTS.md primer audit ({cutoff}, ±{window_days}d)",
        "",
        f"Generated: {datetime.utcnow().isoformat()}Z",
        f"Cutoff: AGENTS.md committed in a1f0ff9 on {cutoff}.",
        "",
        "## Engagement deltas",
        "",
        f"- Total cards shown: {_fmt_delta(before.engagement_total, after.engagement_total)}",
        f"- Engagement rate (good+clicked / total):"
            f" before={_fmt_pct(before.engagement_rate)} ({before.engagement_good}/{before.engagement_total}),"
            f" after={_fmt_pct(after.engagement_rate)} ({after.engagement_good}/{after.engagement_total})",
        f"- Bad reactions: {_fmt_delta(before.engagement_bad, after.engagement_bad)}",
        "",
        "## Per-card-kind breakdown (after window)",
        "",
    ]
    if after.by_kind:
        for kind, count in after.by_kind.most_common():
            before_count = before.by_kind.get(kind, 0)
            lines.append(f"- `{kind}`: {_fmt_delta(before_count, count)}")
    else:
        lines.append("- (no engagement events recorded after the cutoff)")
    lines += [
        "",
        "## Primer-tool calls (search_notes / ask_notes / proactive_doctor)",
        "",
        f"- All MCP-logged tool calls: {_fmt_delta(before.tool_calls_total, after.tool_calls_total)}",
    ]
    for tool in PRIMER_TOOLS:
        lines.append(
            f"- `{tool}`: "
            f"{_fmt_delta(before.tool_calls_primer.get(tool, 0), after.tool_calls_primer.get(tool, 0))}"
        )
    lines.append("")
    lines.append("> Note: tool-call frequency is best-effort — the "
                 "codebase logs `kind='mcp'` sparsely. If the totals "
                 "look implausibly low, treat engagement as the "
                 "load-bearing signal.")
    return "\n".join(lines)


def render_tuning_directions(before: WindowStats, after: WindowStats) -> str:
    """If the audit decides tuning is needed, build a section
    suggesting WHERE in `_opencode_agents_md()` (org_llm/cli.py) to
    sharpen — based on which card kinds and tools underperformed.
    The remote routine reads this to draft a tuning PR."""
    lines = [
        "## Suggested tuning directions",
        "",
        "Engagement is flat or down post-primer. Candidates for the "
        "tuning PR:",
        "",
    ]

    # Card kinds that LOST engagement most.
    kind_drops = []
    for kind in set(list(before.by_kind) + list(after.by_kind)):
        b = before.by_kind.get(kind, 0)
        a = after.by_kind.get(kind, 0)
        if b > 0 and a < b:
            kind_drops.append((kind, b, a, (b - a) / b))
    kind_drops.sort(key=lambda x: -x[3])

    if kind_drops:
        lines.append("### Card kinds losing engagement")
        for kind, b, a, drop in kind_drops[:5]:
            lines.append(
                f"- `{kind}`: {b} → {a} (-{drop * 100:.0f}%). The "
                f"primer's MCP cheatsheet may not surface the right "
                f"tool for this kind. Consider adding a row in the "
                f"AGENTS.md table that names the action a `{kind}` "
                f"card invites."
            )
        lines.append("")

    # Primer tools getting fewer calls than baseline.
    tool_drops = []
    for tool in PRIMER_TOOLS:
        b = before.tool_calls_primer.get(tool, 0)
        a = after.tool_calls_primer.get(tool, 0)
        if b > a:
            tool_drops.append((tool, b, a))
    if tool_drops:
        lines.append("### Primer tools called less often")
        for tool, b, a in tool_drops:
            lines.append(
                f"- `{tool}`: {b} → {a}. The primer's 'search before "
                f"you speculate' rule may need to call out `{tool}` "
                f"by name in the BEFORE column of the cheatsheet, "
                f"not just the AFTER."
            )
        lines.append("")

    if not kind_drops and not tool_drops:
        lines.append(
            "- Sample sizes are above threshold but no clear "
            "degradation per kind or per tool. The primer may just be "
            "verbose — consider a tightening pass that strips the "
            "lowest-traction sections (whichever ones the engagement "
            "report shows weren't useful)."
        )
        lines.append("")

    lines += [
        "### Mechanical pointer",
        "",
        "Helper: `_opencode_agents_md()` in `org_llm/cli.py` (~line "
        "12460). The MCP cheatsheet table is the highest-leverage "
        "section to edit — it's what the agent reads first.",
    ]
    return "\n".join(lines)


def write_flag(report: str, directions: str, branch: str) -> Path:
    """Write the flag file the remote routine looks for, then commit
    it on a fresh branch and (if a remote is configured) push."""
    flag_dir = REPO_ROOT / FLAG_PATH.parent
    flag_dir.mkdir(parents=True, exist_ok=True)

    body = report + "\n\n" + directions + "\n"
    flag_full = REPO_ROOT / FLAG_PATH
    flag_full.write_text(body)

    # Branch + commit. Don't push automatically — the user may want to
    # review first. Print the next-step commands instead.
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=REPO_ROOT, check=True,
    )
    subprocess.run(
        ["git", "add", str(FLAG_PATH)],
        cwd=REPO_ROOT, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m",
         f"audit: flag AGENTS.md primer for tuning ({datetime.utcnow().date().isoformat()})"],
        cwd=REPO_ROOT, check=True,
    )
    return flag_full


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                          help="ISO date the AGENTS.md primer landed")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                          help="Days each side of cutoff to compare")
    parser.add_argument("--branch", default=None,
                          help="Branch name for the flag commit "
                               f"(default: audit/agents-md-<today>)")
    parser.add_argument("--no-flag", action="store_true",
                          help="Print report, never write/commit the flag")
    args = parser.parse_args()

    cutoff_dt = datetime.fromisoformat(args.cutoff)
    cutoff_ts = int(cutoff_dt.timestamp())
    window_secs = args.window * 86400

    branch = args.branch or f"audit/agents-md-{datetime.utcnow().date().isoformat()}"

    try:
        from org_llm.db import DB_PATH, make_engine
        from sqlalchemy.orm import Session
    except ImportError as e:
        print(f"error: org_llm import failed ({e}). "
              f"Run via `uv run python scripts/audit_agents_md.py`.",
              file=sys.stderr)
        return 1

    db_path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not db_path.exists():
        print(f"error: DB not found at {db_path}. "
              f"Run `org-llm setup` or set ORG_LLM_DB.", file=sys.stderr)
        return 1

    engine = make_engine(db_path)
    with Session(engine) as session:
        before = collect_window(
            session, "before",
            cutoff_ts - window_secs, cutoff_ts,
        )
        after = collect_window(
            session, "after",
            cutoff_ts, cutoff_ts + window_secs,
        )

    report = render_report(before, after, args.cutoff, args.window)
    print(report)

    total_sample = before.engagement_total + after.engagement_total
    if total_sample < MIN_SAMPLE_SIZE:
        print(f"\nVerdict: INCONCLUSIVE (total engagement events="
              f"{total_sample}, need ≥{MIN_SAMPLE_SIZE}). "
              f"No flag written. Re-run when more data is available.")
        return 0

    if before.engagement_total == 0:
        ratio = float("inf") if after.engagement_total else 0.0
    else:
        ratio = after.engagement_total / before.engagement_total

    print(f"\nEngagement-volume ratio (after/before): {ratio:.2f}x "
          f"(threshold: ≥{ENGAGEMENT_RATIO_THRESHOLD:.2f}x)")

    if ratio >= ENGAGEMENT_RATIO_THRESHOLD:
        print("Verdict: PRIMER IS WORKING. No flag written.")
        return 0

    # Tuning needed.
    directions = render_tuning_directions(before, after)
    print()
    print(directions)
    print()
    print("Verdict: TUNING NEEDED. Engagement is flat or down.")

    if args.no_flag:
        print("--no-flag set; not writing flag file.")
        return 0

    flag_full = write_flag(report, directions, branch)
    print(f"\nFlag committed on branch `{branch}` at {flag_full}.")
    print("Next:")
    print(f"  git push -u origin {branch}")
    print("Then the scheduled remote agent (firing 2026-05-13) will")
    print("read the flag and open a tuning PR against trunk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
