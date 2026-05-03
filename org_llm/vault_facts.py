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
