"""Phase 21 — deterministic vault inferrers + DB-backed fact cache.

Every "thing the LLM tends to guess wrong about the user's vault"
that can be answered with a regex sweep + counts gets an inferrer
here. Each inferrer:

  - takes no arguments (vault_dir is resolved internally)
  - returns a JSON-serialisable dict
  - is registered with `register_inferrer(name, ttl_secs=...)`

Results are cached in the `vault_fact` DB table keyed by inferrer
name. Cache invalidation:

  1. mtime mismatch — newest mtime among sampled files differs
     from `inputs_mtime` → recompute.
  2. ttl exceeded — `computed_at + ttl_secs < now` → recompute.

Both are checked on every `get_fact(name)` call. Lookup is a
single keyed query plus an O(N) directory scan to compute the
freshness mtime; sub-millisecond at this vault size.

Adding a new inferrer is three lines:

    @register_inferrer("my_fact", ttl_secs=3600)
    def _infer_my_fact() -> dict:
        return {"some": "data"}

Agents read facts via the `vault_facts` MCP tool, which calls
`get_all_facts()` (refreshes stale entries lazily). Pre-fetch
blocks for orchestration recipes can include specific facts
inline — see `format_prefetch_block` in `recipe_runner.py`.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

# ── registry ─────────────────────────────────────────────────────────────────

_INFERRERS: dict[str, tuple[Callable[[], dict], int]] = {}


def register_inferrer(name: str, *, ttl_secs: int = 86400):
    """Decorator: registers `fn` as an inferrer named `name` with
    a hard TTL ceiling of `ttl_secs` seconds (default 24h)."""
    def deco(fn: Callable[[], dict]) -> Callable[[], dict]:
        _INFERRERS[name] = (fn, ttl_secs)
        return fn
    return deco


# ── public API ───────────────────────────────────────────────────────────────

def get_fact(name: str, *, force: bool = False) -> Optional[dict]:
    """Return the cached fact named `name`, recomputing if stale
    (mtime changed OR ttl expired) or missing.

    `force=True` recomputes unconditionally. Returns None if the
    name isn't registered."""
    entry = _INFERRERS.get(name)
    if entry is None:
        return None
    fn, ttl_secs = entry
    if force:
        return _compute_and_store(name, fn, ttl_secs)
    cached = _read_cached(name)
    if cached is None:
        return _compute_and_store(name, fn, ttl_secs)
    if _stale(cached, ttl_secs):
        return _compute_and_store(name, fn, ttl_secs)
    return cached["value"]


def get_all_facts(*, force: bool = False) -> dict[str, dict]:
    """Return {name: value} for every registered inferrer,
    recomputing stale entries lazily. `force=True` recomputes
    everything."""
    out: dict[str, dict] = {}
    for name in _INFERRERS:
        v = get_fact(name, force=force)
        if v is not None:
            out[name] = v
    return out


def list_inferrers() -> list[dict]:
    """Return [{name, ttl_secs}, …] for diagnostics + the CLI
    `inferrer-cache` verb."""
    return [{"name": n, "ttl_secs": ttl}
            for n, (_, ttl) in _INFERRERS.items()]


# ── persistence layer ────────────────────────────────────────────────────────

def _read_cached(name: str) -> Optional[dict]:
    """Pull the cached row for `name` and parse the JSON value.
    Returns {value, computed_at, inputs_mtime, inputs_count} or
    None when the row is missing / unparseable."""
    try:
        from .db import VaultFact, make_engine
        from sqlalchemy.orm import Session
    except Exception:
        return None
    try:
        with Session(make_engine()) as s:
            row = s.get(VaultFact, name)
            if row is None:
                return None
            try:
                value = json.loads(row.value or "{}")
            except Exception:
                return None
            return {
                "value":         value,
                "computed_at":   row.computed_at,
                "inputs_mtime":  float(row.inputs_mtime or 0.0),
                "inputs_count":  int(row.inputs_count or 0),
            }
    except Exception:
        return None


def _store(name: str, value: dict, mtime: float, count: int,
            ttl_secs: int) -> None:
    """Upsert the cached row."""
    try:
        from .db import VaultFact, make_engine
        from sqlalchemy.orm import Session
    except Exception:
        return
    try:
        with Session(make_engine()) as s:
            row = s.get(VaultFact, name)
            now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            if row is None:
                row = VaultFact(name=name, value=json.dumps(value),
                                  computed_at=now,
                                  inputs_mtime=mtime,
                                  inputs_count=count,
                                  ttl_secs=ttl_secs)
                s.add(row)
            else:
                row.value = json.dumps(value)
                row.computed_at = now
                row.inputs_mtime = mtime
                row.inputs_count = count
                row.ttl_secs = ttl_secs
            s.commit()
    except Exception:
        pass


def _stale(cached: dict, ttl_secs: int) -> bool:
    """True iff the cached row should be recomputed.

    Two conditions:
      1. computed_at + ttl_secs < now
      2. newest mtime among the vault files DIFFERS from inputs_mtime
    """
    try:
        ts = datetime.strptime(
            cached["computed_at"].rstrip("Z"),
            "%Y-%m-%dT%H:%M:%S",
        )
        if datetime.utcnow() > ts + timedelta(seconds=int(ttl_secs)):
            return True
    except Exception:
        return True
    # Cheap mtime probe — newest mtime in `<org>/`. We don't walk
    # subdirs deeply for the freshness check; a stale .org touch
    # at the top level is enough signal that something changed.
    try:
        org = _org_dir()
        newest = max(
            (p.stat().st_mtime
              for p in org.glob("**/*.org")
              if p.is_file()),
            default=0.0,
        )
        if abs(newest - cached["inputs_mtime"]) > 1.0:   # 1s tolerance
            return True
    except Exception:
        return False
    return False


def _compute_and_store(name: str, fn: Callable[[], dict],
                        ttl_secs: int) -> Optional[dict]:
    """Run the inferrer, persist the result, return its value."""
    t0 = time.time()
    try:
        result = fn()
    except Exception as err:
        result = {"error": str(err)[:200]}
    mtime, count = _vault_freshness()
    _store(name, result, mtime, count, ttl_secs)
    # Annotate elapsed for observability — not persisted (it would
    # invalidate the mtime-based dedup) but returned to callers
    # that care.
    if isinstance(result, dict):
        result["_elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


def _org_dir() -> Path:
    """Resolve the user's org_dir from env or DB config."""
    import os
    env = os.environ.get("ORG_LLM_ORG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
        with Session(make_engine()) as s:
            row = s.get(Config, "org_dir")
            if row and row.value:
                return Path(row.value).expanduser().resolve()
    except Exception:
        pass
    return Path("~/org").expanduser().resolve()


def _vault_freshness() -> tuple[float, int]:
    """Return (newest_mtime, file_count) across `<org>/`."""
    org = _org_dir()
    newest = 0.0
    count = 0
    try:
        for p in org.glob("**/*.org"):
            try:
                if p.is_file():
                    count += 1
                    m = p.stat().st_mtime
                    if m > newest:
                        newest = m
            except OSError:
                continue
    except Exception:
        pass
    return newest, count


# ── inferrers ────────────────────────────────────────────────────────────────

@register_inferrer("vault_stats", ttl_secs=3600)
def _infer_vault_stats() -> dict:
    """High-level vault shape: file count, dailies count, oldest /
    newest dailies, total nodes (from DB if available)."""
    org = _org_dir()
    files = list(org.glob("**/*.org"))
    dailies = list(org.glob("daily/**/*.org"))
    daily_dates: list[str] = []
    for d in dailies:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", d.stem)
        if m:
            daily_dates.append(m.group(1))
    daily_dates.sort()
    out: dict = {
        "files":            len(files),
        "dailies":          len(dailies),
        "oldest_daily":     daily_dates[0]  if daily_dates else "",
        "newest_daily":     daily_dates[-1] if daily_dates else "",
        "daily_span_days":  (
            (datetime.fromisoformat(daily_dates[-1])
              - datetime.fromisoformat(daily_dates[0])).days
            if len(daily_dates) >= 2 else 0
        ),
    }
    # Try to get node count from the indexed DB.
    try:
        from .db import Node, make_engine
        from sqlalchemy.orm import Session
        with Session(make_engine()) as s:
            out["nodes"] = s.query(Node).count()
    except Exception:
        out["nodes"] = -1
    return out


@register_inferrer("tag_taxonomy", ttl_secs=3600)
def _infer_tag_taxonomy() -> dict:
    """Top tags by frequency across the whole vault. Reuses the
    existing org_tools.org_tag_index helper. Capped at the top 30
    so the agent's pre-fetch context stays compact."""
    try:
        from . import org_tools as _ot
    except Exception:
        return {"top_tags": []}
    rows = _ot.org_tag_index(limit=30) or []
    return {
        "top_tags":   [[tag, n] for tag, n in rows],
        "total_tags": len(rows),
    }


_DAILY_CHECKBOX_RE = re.compile(
    r"^[\s*\-+]*\[X\]\s+(.+?)(?:\s+:[A-Za-z][\w@:-]*:)*\s*$",
    re.MULTILINE,
)
# Patterns that signal a checkbox item is one-off / specific
# rather than a recurring chore. The Phase 18.4 lesson: many
# users TitleCase routine items ("Make bed", "Trash", "Vacuum"),
# so a bare "any capitalised word" rule rejects everything. Look
# for stronger proper-noun signals instead.
#
# - Multi-capital sequence (Google Drive, John Smith)
# - Acronym (3+ all-caps letters, RMA / VPN / SSL)
# - Mid-sentence capitalised word (not the leading word)
_MULTI_CAP_RE = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_ACRONYM_RE   = re.compile(r"\b[A-Z]{3,}\b")
_MID_CAP_RE   = re.compile(r"(?<=\S\s)[A-Z][a-z]+\b")
_STOP_VERBS = {
    "post", "follow", "send", "meeting", "finish", "complete",
    "review", "respond", "reply", "schedule", "submit", "research",
    "study", "learn", "watch", "listen", "buy", "order", "book",
}


@register_inferrer("routine_chores", ttl_secs=43200)
def _infer_routine_chores() -> dict:
    """Identify recurring chore checkboxes from recent dailies.

    Heuristic (REJECT-first to match the dailies_routine_filter
    recipe contract):
      - Look at the last 30 daily files.
      - Extract `[X] <text>` checkboxes.
      - Reject items whose text contains a capitalised proper
        noun, a stop-verb, or a date/time token.
      - Bucket the remaining items by lowercase phrase, count
        occurrences, return the top 12 with their frequency.

    Output:
      {
        "chores": [{"phrase": "make bed", "count": 17, "first_seen": "...",
                    "last_seen": "..."}],
        "samples_total":    int,
        "samples_rejected": int,
        "dailies_scanned":  int,
      }
    """
    org = _org_dir()
    dailies = sorted(org.glob("daily/**/*.org"), reverse=True)[:30]
    counts: Counter = Counter()
    first_seen: dict[str, str] = {}
    last_seen:  dict[str, str] = {}
    samples_total = 0
    samples_rejected = 0
    for d in dailies:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", d.stem)
        date_iso = m.group(1) if m else ""
        try:
            text = d.read_text(errors="replace")
        except Exception:
            continue
        for cm in _DAILY_CHECKBOX_RE.finditer(text):
            phrase = cm.group(1).strip()
            samples_total += 1
            if _is_chore_reject(phrase):
                samples_rejected += 1
                continue
            key = re.sub(r"\s+", " ", phrase.lower()).strip()
            if not key:
                continue
            counts[key] += 1
            if date_iso:
                last_seen[key] = max(last_seen.get(key, ""), date_iso)
                if key not in first_seen:
                    first_seen[key] = date_iso
                else:
                    first_seen[key] = min(first_seen[key], date_iso)
    chores = []
    for phrase, n in counts.most_common(12):
        chores.append({
            "phrase":     phrase,
            "count":      n,
            "first_seen": first_seen.get(phrase, ""),
            "last_seen":  last_seen.get(phrase, ""),
        })
    return {
        "chores":            chores,
        "samples_total":     samples_total,
        "samples_rejected":  samples_rejected,
        "dailies_scanned":   len(dailies),
    }


def _is_chore_reject(phrase: str) -> bool:
    """Return True iff this phrase looks like a one-off / specific
    item rather than a recurring chore. Mirrors the
    dailies_routine_filter recipe's reject rules.

    The bar is REJECT-FIRST — but we only fire on STRONG
    proper-noun signals (multi-cap sequence, acronym, mid-sentence
    cap), not on any single capitalised word, because users
    routinely TitleCase routine items ("Make bed", "Vacuum")."""
    if not phrase or len(phrase) < 3:
        return True
    if _MULTI_CAP_RE.search(phrase):
        return True
    if _ACRONYM_RE.search(phrase):
        return True
    if _MID_CAP_RE.search(phrase):
        return True
    if re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|@\d", phrase):
        return True
    first = re.match(r"\s*(\w+)", phrase.lower())
    if first and first.group(1) in _STOP_VERBS:
        return True
    if "[[" in phrase or phrase.startswith("(?"):
        return True
    return False


# ── priority_usage ───────────────────────────────────────────────────────────

_PRIORITY_RE = re.compile(r"^\*+\s+(?:[A-Z]+\s+)?\[#([A-Z])\]", re.MULTILINE)


@register_inferrer("priority_usage", ttl_secs=21600)
def _infer_priority_usage() -> dict:
    """Detect whether the user uses org-mode priority cookies
    (`[#A]` / `[#B]` / `[#C]`) and the distribution if so. Lets
    agents skip suggesting priorities to users who don't use them
    (and lets them honour the user's actual distribution when they
    do)."""
    org = _org_dir()
    counts: Counter = Counter()
    sample_files: list[str] = []
    files_with_priorities: set[str] = set()
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        hits = _PRIORITY_RE.findall(text)
        if hits:
            files_with_priorities.add(str(f))
            counts.update(hits)
            if len(sample_files) < 5:
                sample_files.append(str(f))
    total = sum(counts.values())
    dominant = counts.most_common(1)[0][0] if counts else None
    return {
        "used":           total > 0,
        "total":          total,
        "by_priority":    dict(counts),
        "dominant":       dominant,
        "files_with_priorities": len(files_with_priorities),
        "sample_files":   sample_files,
    }


# ── date_format ──────────────────────────────────────────────────────────────

_FNAME_DATE_PATTERNS = {
    "YYYY-MM-DD":              re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "YYYY-MM-DD-slug":         re.compile(r"^\d{4}-\d{2}-\d{2}-[\w-]+$"),
    "YYYYMMDDHHMMSS-slug":     re.compile(r"^\d{14}-[\w-]+$"),
    "YYYYMMDD-slug":           re.compile(r"^\d{8}-[\w-]+$"),
}
_HEADLINE_ACTIVE_DATE  = re.compile(r"<\d{4}-\d{2}-\d{2}\b")
_HEADLINE_INACTIVE_DATE = re.compile(r"\[\d{4}-\d{2}-\d{2}\b")


@register_inferrer("date_format", ttl_secs=86400)
def _infer_date_format() -> dict:
    """Detect the user's filename + headline date conventions.

    Helps agents that mint new node files (e.g. via capture) pick
    a filename that matches the user's existing taxonomy rather
    than guessing YYYYMMDDHHMMSS when the user prefers
    YYYY-MM-DD-slug.
    """
    org = _org_dir()
    fname_counts: Counter = Counter()
    active_count   = 0
    inactive_count = 0
    sampled = 0
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        sampled += 1
        stem = f.stem
        matched_fname = False
        for label, pat in _FNAME_DATE_PATTERNS.items():
            if pat.match(stem):
                fname_counts[label] += 1
                matched_fname = True
                break
        if not matched_fname:
            fname_counts["other"] += 1
        # Sample headline date markers from a subset (cap at 200
        # files for speed — date convention is stable across the
        # vault).
        if sampled <= 200:
            try:
                text = f.read_text(errors="replace")[:8000]
            except Exception:
                continue
            active_count   += len(_HEADLINE_ACTIVE_DATE.findall(text))
            inactive_count += len(_HEADLINE_INACTIVE_DATE.findall(text))
    fname_top = fname_counts.most_common(1)[0][0] if fname_counts else None
    if active_count == 0 and inactive_count == 0:
        headline = "rare"
    elif active_count > 5 * inactive_count:
        headline = "active"   # <YYYY-MM-DD>
    elif inactive_count > 5 * active_count:
        headline = "inactive" # [YYYY-MM-DD]
    else:
        headline = "both"
    return {
        "filename_format":     fname_top,
        "filename_breakdown":  dict(fname_counts.most_common(5)),
        "headline_dates":      headline,
        "active_count":        active_count,
        "inactive_count":      inactive_count,
        "files_sampled":       sampled,
    }


# ── scheduling_pattern ───────────────────────────────────────────────────────

_SCHEDULED_RE_FACTS = re.compile(
    r"SCHEDULED:\s*<(\d{4}-\d{2}-\d{2})", re.IGNORECASE,
)
_DEADLINE_RE_FACTS = re.compile(
    r"DEADLINE:\s*<(\d{4}-\d{2}-\d{2})", re.IGNORECASE,
)
_REPEATER_RE = re.compile(r"\+(\d+)([dwmy])")


@register_inferrer("scheduling_pattern", ttl_secs=21600)
def _infer_scheduling_pattern() -> dict:
    """Detect SCHEDULED vs DEADLINE preference + common date
    offsets relative to the file's mtime. Lets agents that draft
    TODOs honour the user's actual scheduling habits."""
    org = _org_dir()
    sched_count = 0
    dead_count  = 0
    repeater_units: Counter = Counter()
    today = datetime.now().date()
    offset_buckets: Counter = Counter()
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        sched_dates = _SCHEDULED_RE_FACTS.findall(text)
        dead_dates  = _DEADLINE_RE_FACTS.findall(text)
        sched_count += len(sched_dates)
        dead_count  += len(dead_dates)
        for d in sched_dates[:50]:
            try:
                target = datetime.strptime(d, "%Y-%m-%d").date()
                delta = (target - today).days
                if   delta == 0:           offset_buckets["today"]    += 1
                elif delta == 1:           offset_buckets["tomorrow"] += 1
                elif 2 <= delta <= 7:      offset_buckets["this_week"] += 1
                elif 8 <= delta <= 30:     offset_buckets["this_month"] += 1
                elif delta < 0:            offset_buckets["overdue"]  += 1
                else:                       offset_buckets["future"]   += 1
            except ValueError:
                continue
        for n, unit in _REPEATER_RE.findall(text):
            repeater_units[unit] += 1
    total = sched_count + dead_count
    pref = (
        "scheduled" if sched_count > 2 * dead_count and total > 0
        else "deadline" if dead_count > 2 * sched_count and total > 0
        else "mixed"   if total > 0
        else "neither"
    )
    return {
        "preference":        pref,
        "scheduled_count":   sched_count,
        "deadline_count":    dead_count,
        "ratio_sched_to_dead": (
            round(sched_count / max(1, dead_count), 2) if dead_count else None
        ),
        "common_offsets":    dict(offset_buckets.most_common(6)),
        "repeater_units":    dict(repeater_units),
    }


# ── capture_section_titles ───────────────────────────────────────────────────

_LEVEL_1_HEADING_RE = re.compile(r"^\*\s+(?:\[[ X-]\]\s+)?(.+?)\s*$",
                                    re.MULTILINE)
# Strip trailing :tag1:tag2: from the captured heading text.
_TRAILING_TAGS_RE = re.compile(r"\s+:[A-Za-z][\w@:-]*(?::[A-Za-z][\w@:-]*)*:\s*$")
# Strip a leading TODO/DONE/NEXT keyword.
_LEADING_TODO_RE = re.compile(r"^(?:TODO|DONE|NEXT|WAITING|HOLD|CANCELLED)\s+")


@register_inferrer("capture_section_titles", ttl_secs=43200)
def _infer_capture_section_titles() -> dict:
    """Find the user's most-frequent level-1 (=*=) heading titles
    across the vault. These are the natural top-level sections the
    user organises around (Chores, Work, Reading, Inbox, etc). An
    agent drafting a capture can offer to file under one of these
    instead of inventing new section names."""
    org = _org_dir()
    counts: Counter = Counter()
    files_sampled = 0
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        files_sampled += 1
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for m in _LEVEL_1_HEADING_RE.finditer(text):
            title = m.group(1).strip()
            title = _TRAILING_TAGS_RE.sub("", title)
            title = _LEADING_TODO_RE.sub("", title)
            # Drop link syntax — `[[id:...][label]]` → label.
            title = re.sub(r"\[\[(?:id:)?[^\]]+\]\[([^\]]+)\]\]",
                             r"\1", title)
            title = title.strip()
            if not title or len(title) > 60:
                continue
            counts[title.lower()] += 1
    return {
        "top_titles":     [[t, n] for t, n in counts.most_common(20)],
        "total_distinct": len(counts),
        "files_sampled":  files_sampled,
    }


# ── voice_register ───────────────────────────────────────────────────────────

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+\s+")
_CONTRACTION_RE = re.compile(
    r"\b(?:don't|won't|can't|isn't|aren't|it's|that's|i'm|i've|you're|"
    r"we're|they're|there's|here's|what's|who's|how's|let's|"
    r"i'll|you'll|we'll|they'll|i'd|you'd|we'd|they'd|"
    r"shouldn't|couldn't|wouldn't|hasn't|haven't|hadn't|wasn't|weren't)\b",
    re.IGNORECASE,
)
_FORMAL_MARKERS = {
    "therefore", "however", "furthermore", "moreover", "nevertheless",
    "consequently", "accordingly", "subsequently", "additionally",
    "specifically", "particularly", "notably", "regarding",
}
_CASUAL_MARKERS = {
    "yeah", "ok", "okay", "gonna", "wanna", "gotta", "kinda", "sorta",
    "lol", "lmao", "haha", "ugh", "meh", "nah", "yep", "yup", "anyway",
}


@register_inferrer("voice_register", ttl_secs=86400)
def _infer_voice_register() -> dict:
    """Sample paragraph-length text from notes and characterise
    the user's voice: avg sentence length, contraction usage, and
    formal vs casual markers. Helps drafting agents (scribe,
    writer) match the user's natural register."""
    org = _org_dir()
    files_sampled = 0
    sentence_word_counts: list[int] = []
    contraction_count = 0
    formal_hits = 0
    casual_hits = 0
    total_words = 0
    for f in org.glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        files_sampled += 1
        # Skip headlines, lists, code blocks, property drawers.
        body_lines: list[str] = []
        in_block = False
        for line in text.splitlines():
            if line.startswith("#+begin") or line.startswith("#+BEGIN"):
                in_block = True; continue
            if line.startswith("#+end") or line.startswith("#+END"):
                in_block = False; continue
            if in_block:
                continue
            stripped = line.strip()
            if (not stripped or stripped.startswith("*")
                    or stripped.startswith("- ")
                    or stripped.startswith("+ ")
                    or stripped.startswith("#+")
                    or stripped.startswith(":")):
                continue
            body_lines.append(stripped)
        if not body_lines:
            continue
        prose = " ".join(body_lines)
        for sentence in _SENTENCE_SPLIT_RE.split(prose):
            words = sentence.split()
            if 3 <= len(words) <= 60:
                sentence_word_counts.append(len(words))
                total_words += len(words)
        contraction_count += len(_CONTRACTION_RE.findall(prose))
        prose_lower = prose.lower()
        for marker in _FORMAL_MARKERS:
            if marker in prose_lower:
                formal_hits += prose_lower.count(marker)
        for marker in _CASUAL_MARKERS:
            casual_hits += prose_lower.count(marker)
        if files_sampled >= 200:   # cap — voice is stable
            break
    if not sentence_word_counts:
        return {
            "register":        "unknown",
            "avg_sentence_words": 0.0,
            "files_sampled":   files_sampled,
        }
    avg_words = round(sum(sentence_word_counts) /
                       len(sentence_word_counts), 1)
    contractions_ratio = round(contraction_count / max(1, total_words), 4)
    if casual_hits > 3 * formal_hits and contractions_ratio > 0.005:
        register = "casual"
    elif formal_hits > 3 * casual_hits and contractions_ratio < 0.002:
        register = "formal"
    else:
        register = "mixed"
    return {
        "register":             register,
        "avg_sentence_words":   avg_words,
        "sentence_count":       len(sentence_word_counts),
        "contractions_ratio":   contractions_ratio,
        "formal_hits":          formal_hits,
        "casual_hits":          casual_hits,
        "files_sampled":        files_sampled,
    }


# ── vault_profile digest ─────────────────────────────────────────────────────

def vault_profile_digest(*, force: bool = False) -> str:
    """Synthesise a compact human-readable digest of the user's
    vault from all registered inferrers. Designed as the org-llm
    primary agent's startup primer — one tool call returns enough
    context for the crew to answer "who is this user, how do they
    organise" without re-reading the vault.
    """
    facts = get_all_facts(force=force)
    lines = ["USER VAULT PROFILE:"]
    vs = facts.get("vault_stats") or {}
    if vs:
        lines.append(f"- shape: {vs.get('files', '?')} files, "
                      f"{vs.get('dailies', 0)} dailies "
                      f"(span {vs.get('daily_span_days', 0)} days), "
                      f"{vs.get('nodes', '?')} nodes indexed")
    tt = facts.get("tag_taxonomy") or {}
    top_tags = tt.get("top_tags") or []
    if top_tags:
        head = ", ".join(f"{t} ({n})" for t, n in top_tags[:5])
        lines.append(f"- top tags: {head}")
    rc = facts.get("routine_chores") or {}
    chores = rc.get("chores") or []
    if chores:
        head = ", ".join(f"{c['phrase']} ({c['count']}×)"
                          for c in chores[:5])
        lines.append(f"- routines: {head}")
    pu = facts.get("priority_usage") or {}
    if pu.get("used"):
        bp = pu.get("by_priority") or {}
        breakdown = ", ".join(f"#{k}={v}"
                                for k, v in sorted(bp.items()))
        lines.append(f"- priority cookies: USED ({breakdown}; "
                      f"dominant #{pu.get('dominant', '?')})")
    else:
        lines.append("- priority cookies: not used")
    df = facts.get("date_format") or {}
    if df.get("filename_format"):
        lines.append(f"- filenames: {df.get('filename_format')}; "
                      f"headline dates: {df.get('headline_dates','?')}")
    sp = facts.get("scheduling_pattern") or {}
    if sp.get("preference") and sp.get("preference") != "neither":
        sched = sp.get('scheduled_count', 0)
        dead  = sp.get('deadline_count', 0)
        lines.append(f"- scheduling: prefers {sp.get('preference')} "
                      f"(SCHEDULED={sched}, DEADLINE={dead})")
    cs = facts.get("capture_section_titles") or {}
    titles = cs.get("top_titles") or []
    if titles:
        head = ", ".join(t for t, _ in titles[:6])
        lines.append(f"- capture sections: {head}")
    vr = facts.get("voice_register") or {}
    if vr.get("register") and vr.get("register") != "unknown":
        lines.append(f"- voice: {vr.get('register')} "
                      f"(~{vr.get('avg_sentence_words', 0)} words/sentence)")
    return "\n".join(lines)
