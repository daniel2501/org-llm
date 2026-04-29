"""Walk + Teach — Phase 13.

Interactive LLM-led tour of the user's vault. The LLM picks
notes; the user explains them; the LLM extracts atomic facts;
those facts persist as context the LLM uses on every future RAG
call.

Phase 13.1 (this file) ships:
- WalkSession dataclass (in-memory; persistence in 13.3)
- Three node-selection strategies for the `notes` track
- LLM extraction of atomic facts from user responses
- One-pass confirmation → save to context.add_fact()

Phase 13.2-5 layer on:
- 13.2: dailies track (cluster daily notes by week-theme)
- 13.3: persistence (walks table, --resume)
- 13.4: per-slug context files (~/org/llm-context/<slug>.org)
- 13.5: insight-card integration (/walk slash command)

Design intent — see ~/org/org-llm-test-session/phase-13-walk-and-teach.org

The LLM's job here:
- Extract ATOMIC factual claims the user MADE (not restated from
  the note body, not generalities, not questions).
- Do NOT ask follow-up questions; that's a Phase 13.5 idea.
- Do NOT re-narrate the note; the user already saw it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
import time

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class WalkNode:
    """One node selected for the walk."""
    node_id:   str
    title:     str
    body:      str
    tags:      str
    file_path: str
    mtime:     float
    score:     float                 # selection score; higher = more attention-worthy
    reason:    str                   # why this node was picked (orphan? recent? neglected?)


@dataclass
class WalkSession:
    """In-memory state for a single walk. Persistence lands in 13.3."""
    track:        str            # "notes" | "dailies" | "projects"
    started_at:   int
    selected:     list[WalkNode]            = field(default_factory=list)
    nodes_walked: list[str]                 = field(default_factory=list)
    facts_saved: list[tuple[str, str]]      = field(default_factory=list)
    skipped:      list[tuple[str, str]]     = field(default_factory=list)


# ── selection (Phase 13.1: notes track only) ─────────────────────────────

def _select_notes_track(session: Session, *, window_days: int,
                          k: int) -> list[WalkNode]:
    """Pick K nodes for `walk notes`. Bias toward signal-rich-but-
    context-poor: orphans, newly-captured, never-been-asked-about.
    Randomization is deterministic per-day so consecutive walks the
    same day surface the same nodes (resumability hint).
    """
    since = int(time.time()) - max(1, window_days) * 86400
    rows = session.execute(text("""
        SELECT n.node_id, n.title, n.body, n.tags, f.path, n.mtime
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.title IS NOT NULL AND n.title != ''
          AND n.body  IS NOT NULL AND length(n.body) >= 30
        ORDER BY n.mtime DESC
        LIMIT 500
    """)).fetchall()
    if not rows:
        return []

    candidates: list[WalkNode] = []
    for r in rows:
        body = r.body or ""
        score = 0.0
        reasons: list[str] = []

        # Recent → higher score (recency-weighted).
        if r.mtime and r.mtime >= since:
            age_h = (time.time() - r.mtime) / 3600.0
            recency_bonus = max(0.0, 1.0 - (age_h / 168.0))    # 1 week half-life
            score += 0.5 * recency_bonus
            if age_h <= 48:
                reasons.append("recent")

        # Orphan → no [[id:...]] outgoing links → user hasn't
        # stitched it into the graph yet.
        if "[[id:" not in body and "[[file:" not in body:
            score += 0.3
            reasons.append("orphan")

        # Substantial body without tags → likely needs context.
        n_tags = len((r.tags or "").split())
        if len(body) >= 100 and n_tags <= 1:
            score += 0.2
            reasons.append("untagged-substantive")

        # Has an :ID: but title is just a URL → captured-but-not-summarized
        if r.node_id and (r.title or "").startswith(("http://", "https://")):
            score += 0.15
            reasons.append("url-titled")

        if score < 0.15:
            continue

        candidates.append(WalkNode(
            node_id=r.node_id or "",
            title=r.title or "(untitled)",
            body=body,
            tags=r.tags or "",
            file_path=r.path,
            mtime=float(r.mtime or 0),
            score=score,
            reason=", ".join(reasons),
        ))

    candidates.sort(key=lambda c: -c.score)
    return candidates[:k]


def select_walk_nodes(session: Session, track: str = "notes",
                        *, window_days: int = 7,
                        k: int = 5) -> list[WalkNode]:
    """Top-level selector. Phase 13.1: only `notes` is wired."""
    if track == "notes":
        return _select_notes_track(session, window_days=window_days, k=k)
    raise ValueError(f"track {track!r} not yet supported "
                       f"(Phase 13.2 adds 'dailies'; 13+ adds 'projects')")


def select_picked_nodes(session: Session,
                          picks: list[str]) -> list[WalkNode]:
    """User-chosen walk targets. Each `pick` is a node_id, a
    title-substring, or a file path. Returns matching nodes in
    pick-order. Picks that match nothing are silently skipped
    (caller surfaces unmatched ones).
    """
    out: list[WalkNode] = []
    for pick in picks:
        rows = session.execute(text("""
            SELECT n.node_id, n.title, n.body, n.tags, f.path, n.mtime
            FROM nodes n
            JOIN files f ON f.id = n.file_id
            WHERE n.node_id = :pick
               OR LOWER(n.title) LIKE LOWER(:pat)
               OR f.path = :pick
               OR f.path LIKE LOWER(:pat)
            LIMIT 5
        """), {"pick": pick, "pat": f"%{pick}%"}).fetchall()
        for r in rows:
            out.append(WalkNode(
                node_id=r.node_id or "",
                title=r.title or "(untitled)",
                body=r.body or "",
                tags=r.tags or "",
                file_path=r.path,
                mtime=float(r.mtime or 0),
                score=1.0,                       # user said so → max
                reason=f"user-picked: {pick}",
            ))
    # Dedupe by (node_id, title, path) — same node may match multiple
    # patterns
    seen: set[tuple] = set()
    deduped: list[WalkNode] = []
    for n in out:
        key = (n.node_id, n.title, n.file_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    return deduped


# ── review-mode selection (Phase 13.1+: re-walk saved context facts) ─────

@dataclass
class ContextFactCard:
    """One saved context fact + provenance for review-mode."""
    line:        str          # the raw fact line (without leading "-")
    file_path:   str          # which context file it came from
    history_ts:  str          # date the fact was added (from history)
    source:     str           # e.g. "walk:abdd2584" or "cli"


def select_review_facts(*, max_facts: int = 5) -> list[ContextFactCard]:
    """Pull saved context facts that haven't been reviewed recently.
    Reads from the context file's history block to score by age.
    Older, never-reviewed facts surface first.
    """
    from . import context as _ctx
    p = _ctx.context_file_path()
    try:
        text_blob = p.read_text(errors="replace")
    except (FileNotFoundError, OSError):
        return []

    # The active-facts block is the source of truth. The history
    # block tells us WHEN each fact was added.
    block_re = re.compile(
        r"#\+name:\s*active-facts\s*\n#\+begin_src[^\n]*\n([\s\S]*?)\n#\+end_src",
        re.M)
    m = block_re.search(text_blob)
    if not m:
        return []
    facts_lines = [ln.strip().lstrip("-•").strip()
                     for ln in m.group(1).splitlines()
                     if ln.strip().lstrip("-•").strip()
                     and "no facts yet" not in ln.lower()]

    # History block (if present) — extract per-fact timestamps.
    history_lines = re.findall(
        r"^- \[([0-9-]+)\]\s+(.+?)(?:\s+\(([^)]+)\))?$",
        text_blob, re.M)
    fact_to_history: dict[str, tuple[str, str]] = {}
    for ts, fact, source in history_lines:
        # Match by leading words — history line ends with the fact body
        fact_to_history[fact.strip()] = (ts, source or "cli")

    cards: list[ContextFactCard] = []
    for line in facts_lines:
        ts, source = fact_to_history.get(line, ("", "unknown"))
        cards.append(ContextFactCard(
            line=line, file_path=str(p),
            history_ts=ts, source=source,
        ))

    # Older first (so review prioritises old assertions)
    cards.sort(key=lambda c: c.history_ts or "0000")
    return cards[:max_facts]


# ── LLM extraction ────────────────────────────────────────────────────────

_EXTRACTION_SYS = (
    "You read a user's response to 'tell me about this note' and "
    "extract ATOMIC factual claims they made. Output STRICT format, "
    "no preamble, no markdown:\n"
    "\n"
    "  FACT: <one sentence, atomic claim>\n"
    "  FACT: <one sentence, atomic claim>\n"
    "  ...\n"
    "  SLUG: <kebab-case 2-4 word topic, all lowercase>\n"
    "\n"
    "Rules:\n"
    "  - SKIP anything that just restates the note's body — we want "
    "what the user ADDED, not what they paraphrased.\n"
    "  - SKIP generalities ('I should be more organized') and "
    "anything the user marked as a question or doubt.\n"
    "  - SKIP anything not directly tied to the note's topic.\n"
    "  - Each FACT line is one sentence, atomic, present tense.\n"
    "  - The SLUG names a CONTEXT TOPIC (e.g. 'gardening-2026', "
    "'org-llm-phase11', 'work-q2-2026') so future facts about the "
    "same topic land in the same context file.\n"
    "  - If the response contains zero useful facts, output a "
    "single line 'NO_FACTS' and stop."
)


def _parse_extraction(raw: str) -> tuple[list[str], str]:
    """Pull (facts, slug) out of the LLM extraction response."""
    if not raw:
        return ([], "")
    lower = raw.lower()
    if "no_facts" in lower or "no facts" in lower:
        return ([], "")
    facts: list[str] = []
    slug = ""
    for line in raw.splitlines():
        s = line.strip()
        if s.upper().startswith("FACT:"):
            f = s[5:].strip().lstrip("-•").strip()
            if f and len(f) >= 5:
                facts.append(f)
        elif s.upper().startswith("SLUG:"):
            slug = s[5:].strip().lower()
            slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")[:48]
    return (facts, slug)


def extract_facts_from_response(
    node:      WalkNode,
    response:  str,
    *,
    model:     str,
    base_url:  str,
) -> tuple[list[str], str]:
    """Run the LLM extractor. Returns (facts, slug). Empty facts
    means the user's response had nothing context-worthy."""
    if not response or not response.strip():
        return ([], "")
    user_msg = (
        f"Note title: {node.title}\n"
        f"Note body:  {node.body[:600]}\n"
        f"\n"
        f"User's response:\n{response.strip()}"
    )
    try:
        from .llm import chat as _chat
        raw = _chat(user_msg, model=model, base_url=base_url,
                     system=_EXTRACTION_SYS, timeout=30.0) or ""
    except Exception:
        return ([], "")
    return _parse_extraction(raw)
