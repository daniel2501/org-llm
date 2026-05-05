"""@tracker (Boothby) — self-hosted dev/project tracking.

Phase 29.0 — self-coded tools seed. The verbs `tracker init`, `tracker
review`, and `tracker pace` flip this very dev-tracker.org from a
file-in-the-repo to a vault-resident, vault-synced surface that
@tracker (the in-app agent persona modeled on TNG/VOY's Boothby) can
own deterministically.

Per project_tracker_self_hosting_goal: the long-running goal is to
have org-llm itself be the surface that opens, summarises, and paces
the tracker — not a wiki page the user manually edits. These three
verbs are the deterministic skeleton; the @tracker LLM persona will
narrate on top of them.

Per DEC-011 (vault-and-DB symmetry, with files canonical): the
canonical location is `~/org/org-llm-dev-tracker.org`. The wiki copy
becomes a redirect stub so cross-refs by `:ID:` keep working.

Module surface (kept small + pure where possible so testing is
table-stakes):

  - `init_tracker(target, source, force=False) -> InitResult` —
    copies source → target; refuses if target exists unless force.
  - `review_tracker(tracker_path, claims_path, repo_root) -> str` —
    builds the read-only "what's in flight" report.
  - `pace_tracker(tracker_path) -> str` — builds the EFFORT vs.
    actual report.
  - Internal helpers (`_parse_org_entries`, `_recent_commits`, etc.)
    are exposed for tests but underscored to signal "use the verb".

CLI wiring lives in `cli.py` under the `tracker_app` Typer sub-app.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ── Canonical paths ──────────────────────────────────────────────────────────

WIKI_DEV_TRACKER = Path(__file__).resolve().parent.parent / "docs" / "wiki" / "dev-tracker.org"
WIKI_ACTIVE_CLAIMS = Path(__file__).resolve().parent.parent / "docs" / "wiki" / "active-claims.org"
DEFAULT_TRACKER_FILENAME = "org-llm-dev-tracker.org"


# ── init verb ────────────────────────────────────────────────────────────────

@dataclass
class InitResult:
    target: Path
    source: Path
    created: bool          # did we actually write?
    skipped_reason: str    # "" if created; otherwise human-readable
    bytes_written: int = 0


_INIT_HEADER_TEMPLATE = """# Managed by @tracker (Boothby) — Phase 29.0 self-hosted tracker.
# Canonical location: this file (vault-resident, syncs via git per DEC-011).
# Archived original: docs/wiki/dev-tracker.org in the org-llm repo.
#
# DO NOT edit the wiki stub directly — it's a redirect now. Edit
# this file instead. `org-llm tracker review` summarises what's in
# flight; `org-llm tracker pace` flags EFFORT drift.

"""


def init_tracker(target: Path, source: Path, *, force: bool = False) -> InitResult:
    """Copy source dev-tracker.org → target with a managed-by header.

    Idempotent: if target already exists, returns a result with
    `created=False` + a `skipped_reason` unless `force=True`. Never
    raises for normal "file already there" — that's a control-flow
    signal the CLI should surface, not a stack trace.
    """
    target = Path(target).expanduser()
    source = Path(source).expanduser()
    if not source.exists():
        return InitResult(target=target, source=source, created=False,
                          skipped_reason=f"source missing: {source}")
    if target.exists() and not force:
        return InitResult(target=target, source=source, created=False,
                          skipped_reason=f"target exists: {target} (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    body = source.read_text()
    written = _INIT_HEADER_TEMPLATE + body
    target.write_text(written)
    return InitResult(target=target, source=source, created=True,
                      skipped_reason="", bytes_written=len(written.encode("utf-8")))


# ── review verb ──────────────────────────────────────────────────────────────

@dataclass
class OrgEntry:
    """One TODO heading parsed from a tracker / active-claims org file."""
    state:    str        # TODO | NEXT | STARTED | HOLD | WAITING | DONE | CANCELLED
    title:    str        # heading text after the state
    tags:     list[str]  # e.g. ["@active", "bug", "load-bearing"]
    section:  str        # nearest top-level "* …" heading text
    line_no:  int        # 1-indexed line number of the heading
    properties: dict     # PROPERTIES drawer key → value (uppercase keys)
    body:     str        # body text up to next heading (for CLOCK lines etc.)


_HEADING_RE = re.compile(
    r"^(\*+)\s+"
    r"(?:(TODO|NEXT|STARTED|HOLD|WAITING|DONE|CANCELLED)\s+)?"
    r"(.*?)"
    r"(?:\s+(:[^\s]+:))?\s*$"
)


def _parse_tags(tag_str: str) -> list[str]:
    if not tag_str:
        return []
    return [t for t in tag_str.strip(":").split(":") if t]


def _parse_org_entries(text: str) -> list[OrgEntry]:
    """Walk an org-mode file. Best-effort, deliberately small.

    Captures: TODO state, title (with tags stripped off the right),
    tag list, the most recent top-level "* …" heading as `section`,
    PROPERTIES drawer values, and body text up to the next heading.

    Skips the file's pre-amble (anything before the first heading).
    Doesn't try to be a full org parser — we only need enough to
    surface STARTED claims, :@active: / :@plan: sections, EFFORT
    properties, and CLOCK lines. Anything more belongs upstream of
    org-roam.
    """
    lines = text.splitlines()
    entries: list[OrgEntry] = []
    current_top_section = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HEADING_RE.match(line)
        if not m:
            i += 1
            continue
        stars, state, title, tag_str = m.group(1), m.group(2) or "", m.group(3) or "", m.group(4) or ""
        # Top-level heading sets section context
        if len(stars) == 1:
            current_top_section = title.strip() if title else ""
            # Tags on the section heading itself can include :@active:
            section_tags = _parse_tags(tag_str)
            # Encode tags into the section name in their colon-wrapped
            # form (matches the literal `:@active:` substring search
            # the filters use). Leading + trailing colons preserved.
            if section_tags:
                colon_tags = ":" + ":".join(section_tags) + ":"
                current_top_section = f"{current_top_section} {colon_tags}"
        # Body capture: from i+1 until next heading
        body_start = i + 1
        j = body_start
        while j < n and not _HEADING_RE.match(lines[j]):
            j += 1
        body_lines = lines[body_start:j]
        properties = _parse_properties_drawer(body_lines)
        body_text = "\n".join(body_lines)
        if state:
            entries.append(OrgEntry(
                state=state,
                title=title.strip(),
                tags=_parse_tags(tag_str),
                section=current_top_section,
                line_no=i + 1,
                properties=properties,
                body=body_text,
            ))
        i = j
    return entries


_PROP_DRAWER_OPEN  = re.compile(r"^\s*:PROPERTIES:\s*$", re.IGNORECASE)
_PROP_DRAWER_CLOSE = re.compile(r"^\s*:END:\s*$",        re.IGNORECASE)
_PROP_LINE         = re.compile(r"^\s*:([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _parse_properties_drawer(body_lines: Iterable[str]) -> dict:
    """Pull the first PROPERTIES drawer of a heading's body. Keys uppercased."""
    in_drawer = False
    out: dict = {}
    for raw in body_lines:
        if not in_drawer:
            if _PROP_DRAWER_OPEN.match(raw):
                in_drawer = True
            continue
        if _PROP_DRAWER_CLOSE.match(raw):
            break
        m = _PROP_LINE.match(raw)
        if m:
            out[m.group(1).upper()] = m.group(2).strip()
    return out


def _recent_commits(repo_root: Path, n: int = 20) -> list[str]:
    """`git log --oneline -n N`, best-effort. Returns [] if not a repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"-n{n}", "--oneline"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return []
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _started_claims(claims_text: str) -> list[OrgEntry]:
    """Filter active-claims entries to STARTED ones (ignores DONE/etc.)."""
    return [e for e in _parse_org_entries(claims_text) if e.state == "STARTED"]


def _entries_in_section(entries: list[OrgEntry], section_tag: str) -> list[OrgEntry]:
    """Keep entries whose nearest top-level section header is tagged
    `:@<section_tag>:`. We encode top-level tags into the `section`
    field (see _parse_org_entries) so we can filter by string match."""
    needle = f":@{section_tag}"
    return [e for e in entries if needle in (e.section or "")]


def _entries_with_any_tag(entries: list[OrgEntry], tags: set[str]) -> list[OrgEntry]:
    return [e for e in entries if set(e.tags) & tags]


def review_tracker(
    tracker_path: Path,
    claims_path: Path,
    repo_root: Path,
    *,
    commits_n: int = 20,
    max_lines: int = 80,
) -> str:
    """Render the read-only "what's in flight" report. ASCII-clean,
    bounded to ~80 lines per feedback_condense_long_outputs.
    """
    out: list[str] = []
    push = out.append

    # Section 1: STARTED claims (live coordination)
    push("== STARTED claims ==")
    if claims_path.exists():
        claims = _started_claims(claims_path.read_text())
        if not claims:
            push("  (none — active-claims clean)")
        else:
            for c in claims[:10]:
                started = c.properties.get("STARTED", "")
                branch = c.properties.get("BRANCH", "")
                push(f"  {c.title}")
                if started or branch:
                    push(f"    started={started}  branch={branch}")
    else:
        push(f"  (no active-claims file at {claims_path})")
    push("")

    # Section 2: in-flight TODOs from :@active: + :@plan:
    push("== In-flight (@active + @plan) ==")
    if tracker_path.exists():
        entries = _parse_org_entries(tracker_path.read_text())
        active = _entries_in_section(entries, "active")
        plan = _entries_in_section(entries, "plan")
        live = [e for e in active + plan if e.state in ("TODO", "NEXT", "STARTED")]
        if not live:
            push("  (no active/plan TODOs)")
        else:
            for e in live[:15]:
                tag_str = ":".join(e.tags) if e.tags else ""
                push(f"  [{e.state}] {e.title}"
                     + (f"  ({tag_str})" if tag_str else ""))
    else:
        push(f"  (tracker missing at {tracker_path})")
    push("")

    # Section 3: shipped today (recent commits)
    push(f"== Shipped (last {commits_n} commits) ==")
    commits = _recent_commits(repo_root, n=commits_n)
    if not commits:
        push("  (no commits or not a git repo)")
    else:
        for c in commits:
            push(f"  {c}")
    push("")

    # Section 4: blockers / surprises
    push("== Blockers / surprises ==")
    if tracker_path.exists():
        entries = _parse_org_entries(tracker_path.read_text())
        flagged = _entries_with_any_tag(entries, {"blocker", "surprise"})
        flagged = [e for e in flagged if e.state in ("TODO", "NEXT", "STARTED", "HOLD", "WAITING")]
        if not flagged:
            push("  (none)")
        else:
            for e in flagged[:10]:
                push(f"  [{e.state}] {e.title}")
    else:
        push("  (tracker missing — skip)")

    # Trim to max_lines if we overflowed (safety belt)
    if len(out) > max_lines:
        out = out[:max_lines - 1] + [f"  … ({len(out) - max_lines + 1} more lines truncated)"]
    return "\n".join(out)


# ── pace verb ────────────────────────────────────────────────────────────────

_EFFORT_PAT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([dhm])?\s*$", re.IGNORECASE)
_CLOCK_PAT = re.compile(
    r"^\s*CLOCK:\s*\[[^\]]+\](?:--\[[^\]]+\])?\s*=>\s*(\d+):(\d{2})\s*$"
)


def _parse_effort(s: str) -> float | None:
    """Parse an org-mode :EFFORT: value into hours.

    Accepts: "30m", "1h", "2h", "1d" (=8h), "1.5h", "1:30" (= 1h30m),
    bare numbers (treated as hours). Returns None for anything we
    can't confidently parse — the pace report just notes those as
    unparseable.
    """
    if not s:
        return None
    s = s.strip()
    # H:MM form
    if ":" in s:
        try:
            h, m = s.split(":", 1)
            return int(h) + int(m) / 60.0
        except ValueError:
            return None
    m = _EFFORT_PAT.match(s)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "h").lower()
    if unit == "m":
        return val / 60.0
    if unit == "h":
        return val
    if unit == "d":
        return val * 8.0     # org-mode default: 1 day = 8 hours
    return None


def _logged_hours_from_body(body: str) -> float:
    """Sum CLOCK: lines in a heading body. Returns hours as float.

    Format: `CLOCK: [2026-05-04 ...]--[...] =>  1:30`. We only need
    the trailing `=> H:MM` since that's what org-clock writes.
    """
    total = 0.0
    for line in body.splitlines():
        m = _CLOCK_PAT.match(line)
        if m:
            total += int(m.group(1)) + int(m.group(2)) / 60.0
    return total


@dataclass
class PaceRow:
    title: str
    state: str
    effort_h: float | None
    actual_h: float
    ratio: float | None       # actual / effort; None if effort missing/zero


def _pace_rows(entries: list[OrgEntry]) -> list[PaceRow]:
    rows: list[PaceRow] = []
    for e in entries:
        eff_str = e.properties.get("EFFORT", "")
        effort = _parse_effort(eff_str) if eff_str else None
        actual = _logged_hours_from_body(e.body)
        # Also accept :ACTUAL: property as a fallback (the dev-tracker
        # uses this freeform — e.g. ":ACTUAL: ~3h").
        if actual == 0.0:
            actual_prop = e.properties.get("ACTUAL", "")
            if actual_prop:
                # strip leading "~", "approx", etc.
                cleaned = re.sub(r"[~≈]\s*", "", actual_prop).split()
                if cleaned:
                    parsed = _parse_effort(cleaned[0])
                    if parsed is not None:
                        actual = parsed
        ratio = (actual / effort) if (effort and effort > 0) else None
        rows.append(PaceRow(
            title=e.title, state=e.state,
            effort_h=effort, actual_h=actual, ratio=ratio,
        ))
    return rows


def pace_tracker(tracker_path: Path, *, max_lines: int = 80) -> str:
    """Render EFFORT vs. actual report. Tight: header + summary +
    top-3 worst overruns."""
    out: list[str] = []
    push = out.append

    if not tracker_path.exists():
        return f"== Pace ==\n  (tracker missing at {tracker_path})\n"

    entries = _parse_org_entries(tracker_path.read_text())
    # Only look at items with a parseable EFFORT — items without one
    # have nothing to compare against.
    rows = [r for r in _pace_rows(entries) if r.effort_h is not None]

    push("== Pace (EFFORT vs. actual) ==")
    if not rows:
        push("  (no items with parseable :EFFORT: properties)")
        return "\n".join(out)

    n_total   = len(rows)
    n_with_actual = sum(1 for r in rows if r.actual_h > 0)
    n_overrun = sum(1 for r in rows if r.ratio and r.ratio > 1.25)
    n_onpace  = sum(1 for r in rows if r.ratio and 0.75 <= r.ratio <= 1.25)
    n_ahead   = sum(1 for r in rows if r.ratio and r.ratio < 0.75)

    push(f"  items with EFFORT:        {n_total}")
    push(f"  items with actual logged: {n_with_actual}")
    push(f"  on-pace  (0.75–1.25x):    {n_onpace}")
    push(f"  ahead    (<0.75x):        {n_ahead}")
    push(f"  overrun  (>1.25x):        {n_overrun}")
    push("")

    # Top-3 worst overruns by ratio
    overruns = sorted(
        [r for r in rows if r.ratio and r.ratio > 1.25],
        key=lambda r: r.ratio or 0.0, reverse=True,
    )[:3]
    if overruns:
        push("  worst overruns:")
        for r in overruns:
            push(f"    {r.ratio:.2f}x  [{r.state}] {r.title}"
                 f"  ({r.actual_h:.1f}h / {r.effort_h:.1f}h)")
    else:
        push("  (no significant overruns)")

    if len(out) > max_lines:
        out = out[:max_lines - 1] + [f"  … ({len(out) - max_lines + 1} more lines truncated)"]
    return "\n".join(out)
