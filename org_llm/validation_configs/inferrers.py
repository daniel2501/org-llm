"""Validation config — vault_facts inferrers (deterministic correctness).

Each check returns (passed, message). Reuses the manual checks from the
2026-05-03 vault_facts validation pass, packaged for repeatable runs.

Run via `org-llm validate inferrers`.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from org_llm import validation


def _org_dir() -> Path:
    return Path(os.environ.get("ORG_LLM_ORG_DIR")
                 or os.path.expanduser("~/org"))


# ── vault_stats checks ───────────────────────────────────────────────────────

def _check_vault_stats_files() -> tuple[bool, str]:
    from org_llm.vault_facts import get_fact
    vs = get_fact("vault_stats", force=True)
    if vs is None:
        return False, "vault_stats returned None"
    disk_count = sum(1 for f in _org_dir().glob("**/*.org") if f.is_file())
    inf = vs.get("files", -1)
    ok = inf == disk_count
    return ok, (f"inferrer={inf}, disk={disk_count}"
                + ("" if ok else " — MISMATCH"))


def _check_vault_stats_dailies() -> tuple[bool, str]:
    from org_llm.vault_facts import get_fact
    vs = get_fact("vault_stats", force=True)
    if vs is None:
        return False, "vault_stats returned None"
    daily_dir = _org_dir() / "daily"
    disk = sum(1 for f in daily_dir.glob("*.org") if f.is_file()) \
        if daily_dir.exists() else 0
    inf = vs.get("dailies", -1)
    ok = inf == disk
    return ok, (f"inferrer={inf}, disk={disk}"
                + ("" if ok else " — MISMATCH"))


def _check_vault_stats_nodes() -> tuple[bool, str]:
    from org_llm.vault_facts import get_fact
    vs = get_fact("vault_stats", force=True)
    if vs is None:
        return False, "vault_stats returned None"
    try:
        from sqlalchemy.orm import Session
        from org_llm.db import Node, make_engine
        with Session(make_engine()) as s:
            db_nodes = s.query(Node).count()
    except Exception as e:
        return False, f"DB query failed: {type(e).__name__}: {e}"
    inf = vs.get("nodes", -1)
    ok = inf == db_nodes
    return ok, (f"inferrer={inf}, db={db_nodes}"
                + ("" if ok else " — MISMATCH"))


# ── tag_taxonomy check ───────────────────────────────────────────────────────

def _check_tag_taxonomy_overlap() -> tuple[bool, str]:
    """Inferrer's top-15 tags must have >= 12/15 set overlap with a
    fresh disk-grep ground truth."""
    from org_llm.vault_facts import get_fact
    tt = get_fact("tag_taxonomy", force=True)
    if tt is None:
        return False, "tag_taxonomy returned None"
    inf = {t for t, _ in (tt.get("top_tags") or [])[:15]}
    # GT — replicate org_tag_index logic minimally.
    DRAWER_KW = {"PROPERTIES", "LOGBOOK", "CLOCK", "END", "CLOSED"}
    file_tags_re = re.compile(r"^#\+(?:FILE)?TAGS:\s*(.+)$",
                               re.MULTILINE | re.IGNORECASE)
    headline_tags_re = re.compile(
        r"^\*+\s+.*?\s+(:[\w@:-]+:)\s*$", re.MULTILINE)
    tag_token_re = re.compile(r":([\w@-]+):")
    counts: Counter = Counter()
    for f in _org_dir().glob("**/*.org"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        ft = file_tags_re.search(text)
        if ft:
            raw = ft.group(1).strip()
            tokens = raw.split(":") if ":" in raw else raw.split()
            for t in tokens:
                if t and t not in DRAWER_KW:
                    counts[t] += 1
        for tag_block in headline_tags_re.findall(text):
            for t in tag_token_re.findall(tag_block):
                if t not in DRAWER_KW:
                    counts[t] += 1
    gt = {t for t, _ in counts.most_common(15)}
    overlap = len(inf & gt)
    ok = overlap >= 12
    return ok, f"top-15 set overlap: {overlap}/15 (need >= 12)"


# ── routine_chores checks ────────────────────────────────────────────────────

REJECT_PHRASES = [
    "Send Brian email",
    "Schedule oil change",
    "Meeting with John Smith",
    "Submit RMA form",
    "Buy groceries",
    "Review the design doc",
    "Post to Mastodon",
]

KEEP_PHRASES = [
    "make bed",
    "clean bathroom",
    "exercise",
    "trash",
    "juice",
    "fire wood",
    "vacuum",
]


def _check_chores_reject_filter() -> tuple[bool, str]:
    from org_llm.vault_facts import _is_chore_reject
    fails = [p for p in REJECT_PHRASES if not _is_chore_reject(p)]
    if fails:
        return False, f"{len(fails)}/{len(REJECT_PHRASES)} false negatives: {fails}"
    return True, f"{len(REJECT_PHRASES)}/{len(REJECT_PHRASES)} reject cases caught"


def _check_chores_keep_filter() -> tuple[bool, str]:
    from org_llm.vault_facts import _is_chore_reject
    fails = [p for p in KEEP_PHRASES if _is_chore_reject(p)]
    if fails:
        return False, f"{len(fails)}/{len(KEEP_PHRASES)} false positives: {fails}"
    return True, f"{len(KEEP_PHRASES)}/{len(KEEP_PHRASES)} chore cases kept"


def _check_chores_nonempty() -> tuple[bool, str]:
    from org_llm.vault_facts import get_fact
    rc = get_fact("routine_chores", force=True)
    if rc is None:
        return False, "routine_chores returned None"
    n = len(rc.get("chores") or [])
    return n >= 5, f"{n} chores returned (need >= 5)"


# ── TTL invalidation checks ──────────────────────────────────────────────────

def _check_ttl_fresh_not_stale() -> tuple[bool, str]:
    from org_llm.vault_facts import _stale, _vault_freshness
    mtime, _ = _vault_freshness()
    cached = {
        "value": {},
        "computed_at": (datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        "inputs_mtime": mtime,
        "inputs_count": 0,
    }
    is_stale = _stale(cached, ttl_secs=3600)
    return (not is_stale), ("fresh entry correctly marked NOT stale"
                             if not is_stale
                             else "fresh entry incorrectly marked stale")


def _check_ttl_expired_is_stale() -> tuple[bool, str]:
    from org_llm.vault_facts import _stale, _vault_freshness
    mtime, _ = _vault_freshness()
    old_ts = (datetime.utcnow() - timedelta(hours=2)
              ).isoformat(timespec="seconds") + "Z"
    cached = {"value": {}, "computed_at": old_ts,
              "inputs_mtime": mtime, "inputs_count": 0}
    is_stale = _stale(cached, ttl_secs=3600)   # 1h TTL, 2h old
    return is_stale, ("ttl-expired correctly marked stale"
                       if is_stale
                       else "ttl-expired NOT marked stale — bug")


def _check_ttl_mtime_changed_is_stale() -> tuple[bool, str]:
    from org_llm.vault_facts import _stale, _vault_freshness
    mtime, _ = _vault_freshness()
    cached = {
        "value": {},
        "computed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "inputs_mtime": mtime - 100,    # claim disk was older — should now appear changed
        "inputs_count": 0,
    }
    is_stale = _stale(cached, ttl_secs=3600)
    return is_stale, ("mtime-divergent correctly marked stale"
                       if is_stale
                       else "mtime-divergent NOT marked stale — bug")


def _check_ttl_malformed_is_stale() -> tuple[bool, str]:
    from org_llm.vault_facts import _stale
    cached = {"value": {}, "computed_at": "not-a-date",
              "inputs_mtime": 0.0, "inputs_count": 0}
    is_stale = _stale(cached, ttl_secs=3600)
    return is_stale, ("malformed-timestamp correctly marked stale "
                       "(safe-default behaviour)"
                       if is_stale
                       else "malformed-timestamp NOT stale — bug")


# ── register ─────────────────────────────────────────────────────────────────

validation.register(validation.ValidationConfig(
    name="inferrers",
    description=(
        "vault_facts inferrers — deterministic correctness checks for "
        "vault_stats, tag_taxonomy, routine_chores filtering, and TTL "
        "invalidation logic."
    ),
    kind="deterministic",
    checks=[
        ("vault_stats",     "files matches disk count",   _check_vault_stats_files),
        ("vault_stats",     "dailies matches disk count", _check_vault_stats_dailies),
        ("vault_stats",     "nodes matches DB count",     _check_vault_stats_nodes),
        ("tag_taxonomy",    "top-15 set overlap >= 12/15", _check_tag_taxonomy_overlap),
        ("routine_chores",  "REJECT filter — 7 known proper-nouns/stop-verbs",
         _check_chores_reject_filter),
        ("routine_chores",  "KEEP filter — 7 known chores not falsely rejected",
         _check_chores_keep_filter),
        ("routine_chores",  "non-empty output (>= 5 chores)",
         _check_chores_nonempty),
        ("ttl_invalidation", "fresh entry not stale",
         _check_ttl_fresh_not_stale),
        ("ttl_invalidation", "ttl-expired marked stale",
         _check_ttl_expired_is_stale),
        ("ttl_invalidation", "mtime-changed marked stale",
         _check_ttl_mtime_changed_is_stale),
        ("ttl_invalidation", "malformed timestamp safely marked stale",
         _check_ttl_malformed_is_stale),
    ],
))
