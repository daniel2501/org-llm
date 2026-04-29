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

from dataclasses import dataclass, field, replace
from typing import Any
import os
import re
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


def _gen_stale_candidates(session: Session, since_ts: int) -> list[InsightCard]:
    """Nodes already tagged :stale: or :drift: (set by previous
    stale-detection runs or by the user). Surfaces them as a single
    "you have N stale notes" card so the user can decide whether to
    revisit. Stays passive — doesn't run the stale detector itself.
    """
    rows = session.execute(text("""
        SELECT n.title, n.tags, f.path
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.tags LIKE '%stale%' OR n.tags LIKE '%drift%'
           OR n.auto_tags LIKE '%stale%' OR n.auto_tags LIKE '%drift%'
        LIMIT 20
    """)).fetchall()
    if not rows:
        return []
    # Sparse evidence on purpose — narration will fill in the why.
    titles = [r.title for r in rows[:5]]
    return [InsightCard(
        kind="stale_candidates",
        title=f"{len(rows)} stale-tagged note(s) pending review",
        body=(f"{len(rows)} note(s) carry :stale: or :drift: tags. "
               f"Sample: {', '.join(titles[:3])}."),
        evidence={"count": len(rows), "first_titles": titles,
                   "tag_categories": ["stale", "drift"]},
        suggested_command="/stale review",
        score=0.6,
    )]


def _gen_topic_cluster(session: Session, since_ts: int) -> list[InsightCard]:
    """Tags with disproportionate recent activity vs all-time.
    A tag with 5 captures in the last week and 5 captures all-time
    is "emerging"; a tag with 5 in the last week and 200 all-time
    is "ongoing" (less interesting). Surfaces only the emerging ones.
    """
    rows = session.execute(text("""
        SELECT n.tags, n.mtime, n.title
        FROM nodes n
        WHERE n.tags IS NOT NULL AND n.tags != ''
    """)).fetchall()
    if not rows:
        return []

    all_count: dict[str, int] = {}
    recent_count: dict[str, int] = {}
    for r in rows:
        for tag in (r.tags or "").split():
            all_count[tag] = all_count.get(tag, 0) + 1
            if r.mtime and r.mtime >= since_ts:
                recent_count[tag] = recent_count.get(tag, 0) + 1

    cards: list[InsightCard] = []
    # Tag is "emerging" when ≥3 captures since since_ts AND
    # recent_share ≥ 0.5 (at least half of all uses are recent)
    for tag, rcount in sorted(recent_count.items(),
                                  key=lambda kv: -kv[1]):
        if rcount < 3:
            break          # rest will be smaller; sorted desc
        share = rcount / max(1, all_count.get(tag, 1))
        if share < 0.5:
            continue
        cards.append(InsightCard(
            kind="topic_cluster",
            title=f"Emerging topic :{tag}: ({rcount} recent / {all_count[tag]} total)",
            body=(f"Tag :{tag}: has {rcount} captures since the "
                   f"window started, out of {all_count[tag]} all-time "
                   f"({share:.0%} recent). Likely an active focus."),
            evidence={"tag": tag, "recent_count": rcount,
                       "all_count": all_count[tag], "share": share},
            suggested_command=f"/explore {tag}",
            score=min(0.85, 0.4 + 0.1 * rcount + 0.2 * share),
        ))
        if len(cards) >= 2:           # cap to top 2 emerging clusters
            break
    return cards


def _gen_orphan_growth(session: Session, since_ts: int) -> list[InsightCard]:
    """Files where new headings have no incoming or outgoing links.
    Catches the case where a user is dumping captures into inbox.org
    without linking them into the broader graph yet.
    """
    rows = session.execute(text("""
        SELECT f.path, COUNT(n.id) AS new_count
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.mtime >= :since
          AND (n.body IS NULL OR n.body NOT LIKE '%[[id:%')
        GROUP BY f.path
        HAVING new_count >= 3
        ORDER BY new_count DESC
        LIMIT 3
    """), {"since": float(since_ts)}).fetchall()
    if not rows:
        return []
    # Take the worst offender
    top = rows[0]
    from pathlib import Path as _P
    fname = _P(top.path).name
    return [InsightCard(
        kind="orphan_growth",
        title=f"{top.new_count} new headings in {fname} are unlinked",
        body=(f"{top.new_count} recent capture(s) in {fname} contain "
               f"no [[id:...]] links to other notes. Stitching them "
               f"into the graph makes them retrievable from related "
               f"queries."),
        evidence={"file": fname, "new_count": top.new_count,
                   "path": top.path},
        suggested_command=f"/stitch {fname}",
        score=min(0.7, 0.35 + 0.05 * top.new_count),
    )]


def _gen_doctor_warnings(session: Session, since_ts: int) -> list[InsightCard]:
    """Most recent doctor invocation's warnings, surfaced as a single
    card. Pulls from history; does not run doctor.
    """
    rows = session.execute(text("""
        SELECT timestamp, response, outcome
        FROM history
        WHERE kind = 'cli' AND command LIKE 'doctor%'
        ORDER BY timestamp DESC
        LIMIT 1
    """)).fetchall()
    if not rows:
        return []
    last = rows[0]
    resp = (last.response or "")[:500]
    # Count "warning" or "⚠" occurrences in the response as a rough
    # signal. Don't try to re-parse the panel here — narration step
    # will summarize.
    n_warn = resp.lower().count("warn") + resp.count("⚠")
    if n_warn == 0:
        return []                     # nothing to surface
    return [InsightCard(
        kind="doctor_warnings",
        title=f"{n_warn} unresolved doctor warning(s) since last run",
        body=(f"Last `org-llm doctor` produced {n_warn} warning marker"
               f"{'s' if n_warn != 1 else ''}. Re-run doctor for the "
               f"current state, or fold the fixes via `doctor --fix`."),
        evidence={"warning_count": n_warn,
                   "last_run": last.timestamp,
                   "response_preview": resp[:200]},
        suggested_command="/doctor --fix",
        score=min(0.75, 0.4 + 0.04 * n_warn),
    )]


def _gen_sensor_attention(session: Session, since_ts: int) -> list[InsightCard]:
    """Life-support probes that have tripped non-nominal status
    recently. Quiet when everything's fine.
    """
    rows = session.execute(text("""
        SELECT probe, status, label, ts
        FROM sensor_log
        WHERE ts >= :since AND status != 'nominal'
        ORDER BY ts DESC
        LIMIT 20
    """), {"since": float(since_ts)}).fetchall()
    if not rows:
        return []
    by_probe: dict[str, dict] = {}
    for r in rows:
        rec = by_probe.setdefault(r.probe, {"count": 0, "status": r.status,
                                              "latest_label": r.label})
        rec["count"] += 1
    if not by_probe:
        return []
    probes_text = ", ".join(
        f"{p} ({rec['count']}× {rec['status']})"
        for p, rec in sorted(by_probe.items(),
                                key=lambda kv: -kv[1]["count"])
    )
    return [InsightCard(
        kind="sensor_attention",
        title=f"{sum(r['count'] for r in by_probe.values())} non-nominal sensor reading(s)",
        body=(f"Life-support probes flagged in the last window: "
               f"{probes_text}. Run `org-llm life-support` for current "
               f"vitals or `org-llm sensors --drill <probe>` to "
               f"investigate."),
        evidence={"probes": dict(by_probe),
                   "total_events": sum(r['count'] for r in by_probe.values())},
        suggested_command="/life-support",
        score=min(0.9, 0.5 + 0.05 * len(by_probe)),
    )]


# ── LLM narration (Phase 12.2) ────────────────────────────────────────────
#
# Takes deterministic cards + re-narrates the bodies via a local LLM.
# Cloud-first / local-fallback (decision #4) is deferred to 12.3 — for
# now narration is local-only so we tune the prompt against a single
# backend without cloud cost noise.

_NARRATION_SYS_PLAIN = (
    "You re-narrate observation cards from a CLI tool that opens "
    "an LLM workspace over the user's org-roam vault.\n"
    "\n"
    "Each card has structured data attached. Your job: write a "
    "2-4 sentence body in plain English that names SPECIFIC real "
    "things from the data — note titles, tag names, file paths, "
    "counts. Refer to them as the user does, not as internal labels.\n"
    "\n"
    "STRICT RULES:\n"
    "  - When `first_titles` has values, weave 2-3 of those titles "
    "INTO the body literally. Don't paraphrase them.\n"
    "  - NEVER use the words 'evidence', 'node_count', "
    "'first_titles', 'spread_hours', 'card', 'data block', "
    "'observation' — those are internal labels, never user-facing.\n"
    "  - Use the EXACT counts from the data. If `node_count: 5`, "
    "write 'five' or '5', NOT 'six' or 'about five'.\n"
    "  - NEVER invent titles, tags, IDs, or counts that aren't in "
    "the data.\n"
    "  - NO generic flourish ('this could be useful', 'consider "
    "reviewing'). Just the substantive observation.\n"
    "  - NO markdown. Plain text only.\n"
    "  - NO preamble like 'Here's a summary'. Just the body.\n"
    "\n"
    "Output exactly one numbered line per input card, in order:\n"
    "  1. <body for card 1, 2-4 sentences>\n"
    "  2. <body for card 2>\n"
    "  ...\n"
)

_NARRATION_SYS_TREK = (
    "You re-narrate observation cards as a Starfleet operations "
    "officer would relay incoming items to the bridge — brief, "
    "professional, slightly anachronistic. The user is the captain "
    "returning to their workspace.\n"
    "\n"
    "Each card has a deterministic anchor (the EVIDENCE block). "
    "Your job: write a 2-4 sentence body that names SPECIFIC "
    "files, tags, IDs, or counts from the evidence in that voice.\n"
    "\n"
    "STRICT RULES (same as plain mode):\n"
    "  - NEVER invent files, tags, IDs, or counts.\n"
    "  - NEVER editorialize beyond what the evidence supports.\n"
    "  - NO markdown. Plain text.\n"
    "\n"
    "Output exactly one numbered line per input card, in order:\n"
    "  1. <body for card 1>\n"
    "  2. <body for card 2>\n"
)


def _build_narration_prompt(cards: list[InsightCard]) -> str:
    """Compact text representation of all cards for one LLM call."""
    import json as _json
    parts = []
    for i, c in enumerate(cards, 1):
        # Compact evidence — keep specifics, drop large nested arrays
        ev = dict(c.evidence)
        for k, v in list(ev.items()):
            if isinstance(v, list) and len(v) > 5:
                ev[k] = v[:5] + [f"…and {len(v) - 5} more"]
        parts.append(
            f"Card {i} ({c.kind}):\n"
            f"  Title:    {c.title}\n"
            f"  Evidence: {_json.dumps(ev, ensure_ascii=False)}\n"
        )
    return "\n".join(parts)


def _parse_narrated_lines(raw: str, n_expected: int) -> list[str]:
    """Pull `1. body / 2. body / ...` lines out of the LLM response.

    Returns a list of length n_expected with empty strings for any
    line that didn't match. Caller falls back to deterministic body
    for empty slots.
    """
    out = [""] * n_expected
    if not raw:
        return out
    line_re = re.compile(r"^\s*(\d+)[.):]?\s+(.+)$")
    # Multi-line bodies: collect continuation lines that don't start
    # with a new "N." marker.
    cur_idx: int | None = None
    cur_buf: list[str] = []

    def _flush():
        nonlocal cur_idx, cur_buf
        if cur_idx is not None and 0 <= cur_idx < n_expected:
            out[cur_idx] = " ".join(cur_buf).strip()
        cur_idx, cur_buf = None, []

    for line in raw.splitlines():
        m = line_re.match(line)
        if m:
            _flush()
            cur_idx = int(m.group(1)) - 1
            cur_buf = [m.group(2).strip()]
        elif line.strip() and cur_idx is not None:
            cur_buf.append(line.strip())
    _flush()
    return out


def _evidence_terms(evidence: dict) -> set[str]:
    """Pull out concrete strings from evidence — the validator
    checks any quoted-looking name in the narrated body against
    this set. Strings short enough to be filler ('1', '2h') get
    skipped."""
    terms: set[str] = set()

    def _walk(v):
        if isinstance(v, str):
            if len(v) >= 4:
                terms.add(v.lower())
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                _walk(x)

    _walk(evidence)
    return terms


# Words that look "specific" (paths, tags, code-fence-y) and so
# should be checked against evidence — not generic English. We use
# a coarse regex: anything ending in .org, anything :tag-shaped:,
# UUID-shaped chunks, or filename-shaped tokens with hyphens or
# underscores.
_SPECIFIC_RE = re.compile(
    r"""(
        [A-Za-z0-9_\-./]+\.org      # .org filenames
      | :[a-z][a-z0-9_-]+:          # :tag: form
      | [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}   # UUID
    )""",
    re.VERBOSE,
)


_METALANGUAGE_TOKENS = {
    "evidence", "node_count", "node counts", "first_titles",
    "spread_hours", "card", "cards", "data block", "observation",
    "observations", "data structure", "structured data",
    "metadata block",
}


def _validation_safe(narrated: str, evidence: dict) -> bool:
    """Reject narrations that:
       (a) name specific files / tags / UUIDs not in evidence, OR
       (b) contain internal-label metalanguage that the prompt
           forbids ('evidence', 'node count', etc.) — small models
           leak prompt vocabulary into output.
    Generic English is fine; specific tokens get cross-checked.
    """
    if not narrated:
        return False
    low = narrated.lower()
    for token in _METALANGUAGE_TOKENS:
        if token in low:
            return False                # leaked the prompt's labels
    terms = _evidence_terms(evidence)
    for tok in _SPECIFIC_RE.findall(narrated):
        t = tok.strip(":").lower()
        if t in terms:
            continue
        bare = re.sub(r"\W", " ", t).split()
        if bare and any(b in terms or any(b in et for et in terms)
                          for b in bare if len(b) >= 4):
            continue
        return False
    return True


def _term_count(body: str, terms: set[str]) -> int:
    """How many evidence-anchored terms appear in `body`."""
    if not body or not terms:
        return 0
    low = body.lower()
    return sum(1 for t in terms if t and t in low)


def _pick_better_body(
    deterministic_body: str,
    narrated:           str,
    evidence:           dict,
) -> tuple[str, str]:
    """Score deterministic vs narrated body and return (winner, source).

    `source` is one of 'deterministic' or 'narrated' for downstream
    attribution (insight_engagement table in Phase 12.5).

    Heuristic:
      - Empty narration → deterministic wins.
      - Narration that fails validation (hallucinated specifics or
        leaked metalanguage) → deterministic wins.
      - Otherwise compare evidence-term density. Narrated wins when:
          (a) it cites at least as many specific terms, AND
          (b) it's not radically shorter (≥70% of det length) — this
              rules out the "LLM compressed away the substance" case
              we saw in 12.2 testing.
      - If narrated wins on terms-cited even at shorter length, take
        it (the model picked the BEST anchors and dropped the rest).

    Cheap: one substring sweep per term, no extra LLM call.
    """
    if not narrated:
        return deterministic_body, "deterministic"
    if not _validation_safe(narrated, evidence):
        return deterministic_body, "deterministic"

    terms = _evidence_terms(evidence)
    det_count = _term_count(deterministic_body, terms)
    nar_count = _term_count(narrated, terms)

    # Strong narrated win: more evidence terms cited.
    if nar_count > det_count:
        return narrated, "narrated"

    # Tie on terms-cited: prefer narrated only if it's not a
    # compression. This is the case Phase 12.2 testing kept hitting:
    # llama3.2:1b takes a 25-word deterministic line that names 3
    # titles + a count and rewrites it as a 12-word abstraction.
    # Stay deterministic in that case.
    if nar_count == det_count:
        if len(narrated) >= 0.7 * len(deterministic_body):
            return narrated, "narrated"
        return deterministic_body, "deterministic"

    # Fewer terms cited — only take narrated if it's MUCH longer
    # (added explanation/context that the deterministic version
    # lacked). Generators with sparse evidence anchors benefit from
    # this path: e.g. stale_candidates with just IDs.
    if len(narrated) >= 1.5 * len(deterministic_body):
        return narrated, "narrated"
    return deterministic_body, "deterministic"


def narrate_via_llm(
    cards:    list[InsightCard],
    *,
    model:    str,
    base_url: str,
    voice:    str = "plain",
) -> list[InsightCard]:
    """Re-narrate card bodies via local LLM, then SCORE the LLM
    output against the deterministic baseline. Picks whichever has
    more evidence-anchored specifics + reasonable length.

    Returns a NEW list. Each card's `.narration_model` is set to
    the model name when the LLM body won, or stays
    "deterministic" when the heuristic kept the baseline. That lets
    Phase 12.5's engagement table attribute card-quality reactions
    to the path that produced the body.

    `voice` ∈ {"plain", "trek"} maps to playful_level 0 / 2.
    """
    if not cards:
        return cards
    sys_msg = _NARRATION_SYS_TREK if voice == "trek" else _NARRATION_SYS_PLAIN
    user_msg = _build_narration_prompt(cards)
    try:
        from .llm import chat as _chat
        raw = _chat(user_msg, model=model, base_url=base_url,
                     system=sys_msg, timeout=30.0) or ""
    except Exception:
        return cards                # network / model error — keep deterministic

    parsed = _parse_narrated_lines(raw, len(cards))
    out: list[InsightCard] = []
    for card, narrated in zip(cards, parsed):
        chosen, source = _pick_better_body(card.body, narrated, card.evidence)
        if source == "narrated":
            out.append(replace(card, body=chosen, narration_model=model))
        else:
            out.append(card)        # deterministic body, model unchanged
    return out


# ── pipeline entry ────────────────────────────────────────────────────────

def gather_insights(
    session:        Session,
    *,
    since_ts:       int | None = None,
    max_cards:      int        = 5,
    narrate:        bool       = False,
    narration_model:str        = "",
    narration_url:  str        = "",
    voice:          str        = "plain",
) -> list[InsightCard]:
    """Run all card generators, score, dedupe, return top-N.

    Phase 12.1: only `new_captures` is wired. Phase 12.2 adds the
    optional `narrate=True` flag that re-writes bodies via LLM.
    Phase 12.3 wires the rest of the generators + cloud-first
    narration switching.

    `since_ts` defaults to "24 hours ago" — meant to anchor on
    "since the user was last active" but absent that signal, 24h
    is a reasonable default.
    """
    if since_ts is None:
        since_ts = int(time.time()) - 24 * 3600

    raw: list[InsightCard] = []
    # Each generator is best-effort — a failing query (missing
    # table, schema drift) shouldn't take the whole pre-mount down.
    for gen in (_gen_new_captures,
                  _gen_stale_candidates,
                  _gen_topic_cluster,
                  _gen_orphan_growth,
                  _gen_doctor_warnings,
                  _gen_sensor_attention):
        try:
            raw.extend(gen(session, since_ts))
        except Exception:
            session.rollback()
            continue

    # Sort by score desc, cap at max_cards. Empty list is a valid
    # response — caller should mount opencode normally without a
    # welcome message in that case.
    raw.sort(key=lambda c: -c.score)
    raw = raw[:max_cards]

    if narrate and raw and narration_model and narration_url:
        raw = narrate_via_llm(raw,
                                model=narration_model,
                                base_url=narration_url,
                                voice=voice)
    return raw


# ── caching (Phase 12.6 will add real persistence; stub for now) ──────────

_CACHE: dict[str, tuple[float, list[InsightCard]]] = {}
_CACHE_TTL = 30 * 60          # 30 minutes per design decision

def cached_gather(session: Session, *, cache_key: str,
                   **kwargs) -> list[InsightCard]:    # noqa: D401
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
