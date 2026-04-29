"""Insight pre-mount — Phase 12.

Generates 1-5 attention-worthy cards from the current vault state
and packs them into the workspace's first message. The user opens
opencode and the LLM greets them with `here's what I noticed`
instead of an empty prompt.

Design decisions (see ~/org/org-llm-test-session/phase-12-insight-pre-mount.org):
- 1-5 cards, dynamic — only emit cards that actually deserve attention
- Voice matches playful_level (0 plain → 3 full Trek persona)
- 30-min cache so serial relaunches don't re-narrate
- Cloud-first narration (Phase 12.2); falls back to local on failure
- Card-engagement table tracks user reactions for `doctor
  --diagnose-cards`

Phase 12.1 (this file) ships:
- InsightCard dataclass + the gather_insights() entry point
- One deterministic generator: new_captures (no LLM yet)
- A no-op cache stub (real cache lands in 12.6)

Phase 12.2-12.6 (next commits) layer on:
- LLM narration (cloud-first w/ local fallback)
- 4 more generators (stale, topic_cluster, orphan_growth,
  doctor_warnings, sensor_attention)
- Workspace injection (greeting field or sentinel)
- insight_engagement table + /card-bad command
- doctor --diagnose-cards path
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import time

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class InsightCard:
    """One attention-worthy observation about the vault state.

    Generators emit raw deterministic anchors; a later LLM step
    (Phase 12.2) re-narrates body. Until then, body comes
    straight from the generator.
    """
    kind:              str            # generator name: "new_captures", "stale", etc.
    title:             str            # one-line, ≤ 60 chars (truncate when rendering)
    body:              str            # 2-4 sentences of detail
    evidence:          dict[str, Any] = field(default_factory=dict)
    suggested_command: str | None     = None
    score:             float          = 0.5      # 0..1, higher = more attention
    narration_model:   str            = "deterministic"


# ── card generators ────────────────────────────────────────────────────────

def _gen_new_captures(session: Session, since_ts: int) -> list[InsightCard]:
    """Notes captured since `since_ts`, clustered by tag.

    Single SQL query against the nodes table (filtered by mtime).
    Output is a single card unless captures span multiple distinct
    tag-clusters, in which case one card per cluster (capped at 2).
    """
    rows = session.execute(text("""
        SELECT n.title, n.tags, f.path, n.mtime
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.mtime >= :since
        ORDER BY n.mtime DESC
        LIMIT 50
    """), {"since": float(since_ts)}).fetchall()
    if not rows:
        return []

    # Cluster by primary tag (first tag in the space-separated list)
    by_tag: dict[str, list] = {}
    untagged: list = []
    for r in rows:
        tags = (r.tags or "").split()
        if tags:
            by_tag.setdefault(tags[0], []).append(r)
        else:
            untagged.append(r)

    cards: list[InsightCard] = []

    # Cluster cards — one per tag-bucket with ≥2 captures
    for tag, items in sorted(by_tag.items(), key=lambda kv: -len(kv[1])):
        if len(items) < 2:
            continue
        # Score: more captures + more recent → higher
        spread_h = max(1, (items[0].mtime - items[-1].mtime) / 3600)
        score = min(1.0, 0.4 + 0.1 * len(items) - 0.02 * spread_h)
        title_preview = ", ".join(i.title[:30] for i in items[:3])
        cards.append(InsightCard(
            kind="new_captures",
            title=f"{len(items)} new captures under :{tag}:",
            body=(f"{len(items)} notes captured under :{tag}: in the "
                   f"last {int(spread_h) or 1}h: {title_preview}"
                   f"{'…' if len(items) > 3 else ''}. "
                   f"They may form a coherent retrospective or note-set."),
            evidence={"tag": tag,
                       "node_count": len(items),
                       "spread_hours": spread_h,
                       "first_titles": [i.title for i in items[:5]]},
            suggested_command=f"/synth {tag}",
            score=score,
        ))
        if len(cards) >= 2:                 # cap to top-2 tag clusters
            break

    # Untagged-burst card — when many captures share no tag
    if len(untagged) >= 3 and not cards:
        cards.append(InsightCard(
            kind="new_captures",
            title=f"{len(untagged)} fresh captures, untagged",
            body=(f"{len(untagged)} notes captured recently haven't "
                   f"been tagged yet. Run `org-llm tag --apply` to "
                   f"surface them in topic-clustered queries."),
            evidence={"untagged_count": len(untagged),
                       "first_titles": [r.title for r in untagged[:5]]},
            suggested_command="/tag-untagged",
            score=0.55,
        ))

    return cards


# ── pipeline entry ────────────────────────────────────────────────────────

def gather_insights(
    session:    Session,
    *,
    since_ts:   int | None = None,
    max_cards:  int        = 5,
) -> list[InsightCard]:
    """Run all card generators, score, dedupe, return top-N.

    Phase 12.1: only `new_captures` is wired. Phase 12.2 adds LLM
    narration on top; Phase 12.3 wires the rest of the generators.

    `since_ts` defaults to "24 hours ago" — meant to anchor on
    "since the user was last active" but absent that signal, 24h
    is a reasonable default.
    """
    if since_ts is None:
        since_ts = int(time.time()) - 24 * 3600

    raw: list[InsightCard] = []
    raw.extend(_gen_new_captures(session, since_ts))

    # Sort by score desc, cap at max_cards. Empty list is a valid
    # response — caller should mount opencode normally without a
    # welcome message in that case.
    raw.sort(key=lambda c: -c.score)
    return raw[:max_cards]


# ── caching (Phase 12.6 will add real persistence; stub for now) ──────────

_CACHE: dict[str, tuple[float, list[InsightCard]]] = {}
_CACHE_TTL = 30 * 60          # 30 minutes per design decision

def cached_gather(session: Session, *, cache_key: str,
                   **kwargs) -> list[InsightCard]:
    """Wrapper around gather_insights that respects a 30-min TTL.

    Phase 12.1 uses an in-memory dict — fine for one-shot
    `org-llm launch` runs but resets across processes. Phase 12.6
    persists this to a tiny JSON file in
    ~/.local/share/org-llm/insights-cache.json.
    """
    now = time.time()
    if cache_key in _CACHE:
        ts, cards = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return cards
    cards = gather_insights(session, **kwargs)
    _CACHE[cache_key] = (now, cards)
    return cards
