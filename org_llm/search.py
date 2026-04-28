# [[file:../../../org/20260425230731-org_llm.org::*search.py][search.py:1]]
from __future__ import annotations

import struct
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session


class SearchResult(NamedTuple):
    node_id:  str | None
    title:    str
    body:     str
    tags:     str
    file_path: str
    score:    float          # cosine distance (lower = closer)


def to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def from_blob(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _extract_title_phrases(query: str) -> list[str]:
    """Pull candidate phrases from a free-text query that might exactly
    match a note title.

    Heuristics, in order of specificity:
      - Quoted runs ('...' or "...")
      - Capitalised multi-word runs (`Sun Microsystems`)
      - Stop-word-stripped query — remove leading WH-/auxiliary words
        AND trailing verb-words like "say", "do", "says". What's left
        tends to be the noun phrase the user actually means.
      - Length-2..N substrings of the cleaned query

    The caller checks each phrase as a case-insensitive substring of
    candidate titles. False positives are cheap (just a small ranking
    bonus); missing the right phrase costs precision.
    """
    import re as _re

    LEADING = (
        r"what|why|how|where|when|which|who|tell|show|summari[sz]e|"
        r"explain|find|give|list|describe|can you|please|do(?:es)?|"
        r"is|are|was|were|the|a|an"
    )
    TRAILING = (
        r"say|says|said|do|does|did|mean|means|is about|are about|"
        r"contain|contains|cover|covers"
    )

    phrases: list[str] = []
    seen: set[str] = set()

    def _push(p: str):
        p = p.strip(" ?.!,'\"")
        if not p or len(p) < 4:
            return
        key = p.lower()
        if key in seen:
            return
        seen.add(key)
        phrases.append(p)

    # Quoted strings
    for m in _re.finditer(r"['\"]([^'\"]{3,80})['\"]", query):
        _push(m.group(1))

    # Capitalised multi-word runs
    for m in _re.finditer(r"\b((?:[A-Z][a-zA-Z0-9]+\s+){1,5}[A-Za-z0-9]+)\b",
                           query):
        _push(m.group(1).strip())

    # Stop-word-stripped query: drop leading interrogatives + trailing verbs
    cleaned = _re.sub(rf"^\s*(?:{LEADING})\b\s*", "", query, flags=_re.I)
    cleaned = _re.sub(rf"\s+(?:{TRAILING})\b\s*[.?!,]?\s*$", "",
                       cleaned, flags=_re.I)
    cleaned = cleaned.strip(" ?.!,'\"")
    if cleaned and 2 <= len(cleaned.split()) <= 8 and len(cleaned) <= 80:
        _push(cleaned)
        # Also push contiguous N-grams of length 2..min(5, words) of the
        # cleaned query so partial matches still surface relevant titles.
        words = cleaned.split()
        max_n = min(5, len(words))
        for n in range(max_n, 1, -1):
            for i in range(0, len(words) - n + 1):
                _push(" ".join(words[i:i + n]))

    return phrases


def vector_search(
    session: Session,
    query_vec: list[float],
    limit: int = 10,
    since_mtime: float | None = None,
    query_text: str = "",
    tag_filter: str = "",
) -> list[SearchResult]:
    """Cosine-distance vector search with title-substring boost.

    Pulls 4× the requested limit from raw cosine ranking, then if any
    rows have titles containing a phrase from `query_text`, those move
    to the front. Truncates to `limit`.

    Without this boost, when the corpus has many short-titled notes
    sharing a token (e.g. 12 nodes titled "dbt"), exact-phrase matches
    like "dbt Layer Design" get buried under cosine-similar duplicates.

    `since_mtime` is a unix timestamp; rows whose `nodes.mtime` predates
    it are excluded. Use this to constrain "last week" queries.

    `tag_filter` (e.g. "code" or "code:python") is a SQL-level filter
    applied BEFORE cosine ranking. Critical for narrow corpora (e.g.
    code-only search in a vault dominated by org notes) where post-
    filtering would crowd out the relevant candidates entirely.
    """
    blob = to_blob(query_vec)
    where = "WHERE n.embedding IS NOT NULL"
    params: dict = {"qvec": blob, "lim": max(limit * 4, limit + 16)}
    if since_mtime is not None:
        where += " AND n.mtime >= :since"
        params["since"] = float(since_mtime)
    if tag_filter:
        # Substring match on the space-separated tag column. Using
        # space-pad both sides catches "code" without false-matching
        # "decode" or "code:python" without false-matching "barcode".
        where += " AND (' ' || n.tags || ' ') LIKE :tagpat"
        params["tagpat"] = f"% {tag_filter} %"
    rows = session.execute(text(f"""
        SELECT
            n.node_id,
            n.title,
            n.body,
            n.tags,
            f.path,
            vec_distance_cosine(n.embedding, :qvec) AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        {where}
        ORDER BY score ASC
        LIMIT :lim
    """), params).fetchall()
    results = [SearchResult(*r) for r in rows]

    # Direct title-phrase query — guarantees any title containing a query
    # phrase is in the candidate pool, even if cosine pushed it past the
    # limit cap. Without this, "dbt Layer Design" can be invisible behind
    # 30 cosine-similar nodes titled just "dbt".
    if query_text:
        phrases = _extract_title_phrases(query_text)
        if phrases:
            seen_ids = {(r.node_id, r.title, r.file_path) for r in results}
            mtime_clause = ""
            phrase_params: dict = {}
            if since_mtime is not None:
                mtime_clause = "AND n.mtime >= :since"
                phrase_params["since"] = float(since_mtime)
            # Apply the same tag filter to the title-phrase boost path
            # so a tag-scoped search (e.g. code-only) doesn't backdoor
            # generic notes with matching titles.
            tag_clause = ""
            if tag_filter:
                tag_clause = "AND (' ' || n.tags || ' ') LIKE :tagpat_phrase"
                phrase_params["tagpat_phrase"] = f"% {tag_filter} %"
            for i, p in enumerate(phrases):
                phrase_params[f"p{i}"] = f"%{p}%"
                phrase_rows = session.execute(text(f"""
                    SELECT n.node_id, n.title, n.body, n.tags, f.path,
                           1.0 AS score
                    FROM nodes n
                    JOIN files f ON f.id = n.file_id
                    WHERE LOWER(n.title) LIKE LOWER(:p{i})
                    {mtime_clause}
                    {tag_clause}
                    LIMIT 8
                """), phrase_params).fetchall()
                for row in phrase_rows:
                    sr = SearchResult(*row)
                    key = (sr.node_id, sr.title, sr.file_path)
                    if key not in seen_ids:
                        results.append(sr)
                        seen_ids.add(key)

        results = _apply_signal_boosts(results, query_text)
    return results[:limit]


# ── Signal boosts: things vector-similarity alone misses ──────────────────
#
# Pure cosine on the body+title can drown out specific signals when the
# corpus has many short-titled notes sharing a frequent token. Each boost
# is a SUBTRACTION from the raw distance (lower is better in cosine).
# The user-visible effect: notes whose title/path/tags directly match the
# query move to the front, stale notes drop unless asked-for, etc.

import re as _re_module


def _extract_query_words(query: str) -> set[str]:
    """Lowercase content-bearing word set from a query, for title overlap."""
    stop = {"what", "why", "how", "where", "when", "which", "who",
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "do", "does", "did", "to", "for", "from", "of", "on",
            "in", "and", "or", "but", "as", "by", "with", "about",
            "tell", "show", "explain", "find", "summarise", "summarize",
            "say", "says", "said", "give", "list", "describe", "any",
            "this", "that", "these", "those", "i", "my", "me", "you", "your",
            "can", "could", "would", "should", "have", "has", "had", "lately"}
    words = _re_module.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", (query or "").lower())
    return {w for w in words if w not in stop}


def _extract_path_segments(query: str) -> list[str]:
    """Pull plausible path segments — daily/journal/inbox/archive/etc.

    Anything the user mentions that resembles a folder or filename gets
    a soft boost when retrieved nodes' file_path contains it as a segment.
    """
    out: list[str] = []
    for w in _re_module.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", (query or "").lower()):
        # Heuristic: keep words ≥ 4 chars to avoid pollution. Common file
        # names like "daily" / "inbox" / "journal" / "archive" pass.
        if 4 <= len(w) <= 24:
            out.append(w)
    return out


def _apply_signal_boosts(results: list[SearchResult],
                           query_text: str) -> list[SearchResult]:
    """Rerank vector results using non-semantic signals the embedding misses.

    Boosts (each subtracts from distance — lower is better):
      - Exact title match           ─ −1.0
      - Title-substring phrase      ─ −0.5
      - Title word-bag overlap      ─ −0.04 × (matching words; cap −0.3)
      - File-path segment match     ─ −0.15 each (cap −0.3)
      - Body-substring phrase       ─ −0.05
      - Tag matches a query word    ─ −0.10 each (cap −0.2)

    Penalties (positive — pushes down rank):
      - Tagged :stale:              ─ +0.6 (unless query mentions history /
                                      formerly / before / used to)
      - Tagged :drift:              ─ +0.3 same condition
      - File path under archive/    ─ +0.4 same condition
    """
    if not results or not query_text:
        return results

    phrases       = [p.lower() for p in _extract_title_phrases(query_text)]
    query_words   = _extract_query_words(query_text)
    path_segments = _extract_path_segments(query_text)
    asks_for_history = bool(_re_module.search(
        r"\b(history|historic|formerly|previous(?:ly)?|"
        r"before|used\s+to|in\s+the\s+past|originally|old)\b",
        query_text, _re_module.I))

    def _score(r: SearchResult) -> float:
        title  = (r.title or "").lower()
        body   = (r.body  or "").lower()
        tags   = (r.tags  or "").lower().split()
        path   = (r.file_path or "").lower()

        bonus  = 0.0

        # Exact / substring phrase match in title
        for p in phrases:
            if title == p:
                bonus -= 1.0
            elif p in title:
                bonus -= 0.5
            elif p in body:
                bonus -= 0.05

        # Title word-bag overlap (catches "what's the layer design for dbt?"
        # when title is "dbt Layer Design")
        title_words = set(_re_module.findall(r"[a-z][a-z0-9_-]{2,}", title))
        overlap = query_words & title_words
        if overlap:
            bonus -= min(0.3, 0.04 * len(overlap))

        # Path segment match — folders / filenames the user named
        if path_segments:
            path_low = path
            seg_hits = sum(1 for seg in path_segments if seg in path_low)
            if seg_hits:
                bonus -= min(0.3, 0.15 * seg_hits)

        # Tag matches any query word
        if query_words and tags:
            tag_hits = sum(1 for t in tags
                            if t in query_words or t.replace("-", "_") in query_words)
            if tag_hits:
                bonus -= min(0.2, 0.10 * tag_hits)

        # Stale / drift / archive penalty (skipped when user wants history)
        if not asks_for_history:
            if "stale" in tags:
                bonus += 0.6
            elif "drift" in tags:
                bonus += 0.3
            if "/archive/" in path or path.endswith(".org_archive"):
                bonus += 0.4

        return r.score + bonus

    return sorted(results, key=_score)


def recent_nodes(
    session: Session,
    limit: int = 10,
    since_mtime: float | None = None,
) -> list[SearchResult]:
    """Return the most-recently-modified nodes (most-recent first).

    Used to augment vector_search for date-anchored questions where the
    user's actual recent files might have weak semantic similarity to the
    query phrasing (e.g. files literally titled "2026-04-26.org" vs. a
    query mentioning "daily notes").
    """
    where = "WHERE 1=1"
    params: dict = {"lim": limit}
    if since_mtime is not None:
        where += " AND n.mtime >= :since"
        params["since"] = float(since_mtime)
    rows = session.execute(text(f"""
        SELECT
            n.node_id,
            n.title,
            n.body,
            n.tags,
            f.path,
            0.0 AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        {where}
        ORDER BY n.mtime DESC
        LIMIT :lim
    """), params).fetchall()
    return [SearchResult(*r) for r in rows]


def recent_in_path(
    session: Session,
    path_substring: str,
    limit: int = 10,
    one_per_file: bool = True,
) -> list[SearchResult]:
    """Most-recently-modified nodes whose file path contains a given substring.

    Used to surface daily-journal-style folders (`/daily/`, `/journal/`,
    `/diary/`) when a query references them by topic regardless of when
    the file was last touched.

    one_per_file=True (default): return one row per file (the file-level
    node, which the indexer always inserts first). For daily notes this
    means "6 most recent days" rather than "6 sub-headings of one day".
    """
    if one_per_file:
        # One row per file — the file-level node (lowest id), with a
        # synthesized body that includes its own pre-heading text PLUS
        # sub-heading titles/bodies. Without this concat, daily notes
        # would arrive with empty bodies because the indexer puts all
        # content under sub-headings.
        rows = session.execute(text("""
            WITH per_file AS (
                SELECT
                    f.id              AS file_id,
                    MIN(n.id)         AS file_node_id,
                    MAX(n.mtime)      AS mtime,
                    f.path            AS path
                FROM nodes n
                JOIN files f ON f.id = n.file_id
                WHERE f.path LIKE :pat
                GROUP BY f.id
            ),
            file_level AS (
                SELECT n.id, n.node_id, n.title, n.body, n.tags
                FROM nodes n
                JOIN per_file pf ON pf.file_node_id = n.id
            ),
            child_bodies AS (
                SELECT n.file_id,
                       GROUP_CONCAT(
                           '## ' || n.title || char(10) || substr(n.body, 1, 400),
                           char(10) || char(10)
                       ) AS combined
                FROM nodes n
                JOIN per_file pf ON pf.file_id = n.file_id
                WHERE n.id != pf.file_node_id
                GROUP BY n.file_id
            )
            SELECT
                fl.node_id,
                fl.title,
                CASE
                    WHEN coalesce(cb.combined, '') = '' THEN fl.body
                    WHEN fl.body = '' THEN cb.combined
                    ELSE fl.body || char(10) || char(10) || cb.combined
                END AS body,
                fl.tags,
                pf.path,
                0.0 AS score
            FROM per_file pf
            JOIN file_level fl ON fl.id = pf.file_node_id
            LEFT JOIN child_bodies cb ON cb.file_id = pf.file_id
            ORDER BY pf.mtime DESC
            LIMIT :lim
        """), {"pat": f"%/{path_substring}/%", "lim": limit}).fetchall()
    else:
        rows = session.execute(text("""
            SELECT
                n.node_id, n.title, n.body, n.tags,
                f.path,
                0.0 AS score
            FROM nodes n
            JOIN files f ON f.id = n.file_id
            WHERE f.path LIKE :pat
            ORDER BY n.mtime DESC
            LIMIT :lim
        """), {"pat": f"%/{path_substring}/%", "lim": limit}).fetchall()
    return [SearchResult(*r) for r in rows]


def existing_tags(session: Session) -> set[str]:
    """Return the set of all distinct tags currently used across all nodes.

    The Node.tags column is whitespace-separated tag names. This splits each
    row, strips colons, lowercases, and unions everything.
    """
    rows = session.execute(text(
        "SELECT DISTINCT tags FROM nodes WHERE tags != ''"
    )).fetchall()
    out: set[str] = set()
    for (raw,) in rows:
        for t in (raw or "").split():
            t = t.strip(":").lower().strip()
            if t:
                out.add(t)
    return out


def nodes_with_tag(
    session: Session,
    tag: str,
    limit: int = 10,
    one_per_file: bool = True,
) -> list[SearchResult]:
    """Most-recent nodes containing a specific tag (case-insensitive).

    Tags in `Node.tags` are whitespace-separated — we use `LIKE %tag%`
    rather than full-text indexing because the tag table is small. With
    one_per_file=True (default) we collapse multi-heading files to the
    file-level node and synthesize its body to include sub-heading content,
    matching `recent_in_path`'s behaviour.
    """
    # Match against the merged file-tags + auto-tags string so an
    # LLM-applied tag is just as findable as a hand-typed one.
    from .db import merged_tags_sql
    merged_n = merged_tags_sql("n")
    merged_m = merged_tags_sql("m")
    merged_fl = merged_tags_sql("fl")
    pat = f"%{tag.lower()}%"
    if one_per_file:
        rows = session.execute(text(f"""
            WITH per_file AS (
                SELECT f.id AS file_id, MIN(n.id) AS file_node_id,
                       MAX(n.mtime) AS mtime, f.path AS path
                FROM nodes n JOIN files f ON f.id = n.file_id
                WHERE EXISTS (
                    SELECT 1 FROM nodes m
                    WHERE m.file_id = f.id AND lower({merged_m}) LIKE :pat
                )
                GROUP BY f.id
            ),
            file_level AS (
                SELECT n.id, n.node_id, n.title, n.body,
                       {merged_n} AS tags
                FROM nodes n JOIN per_file pf ON pf.file_node_id = n.id
            ),
            child_bodies AS (
                SELECT n.file_id,
                       GROUP_CONCAT(
                           '## ' || n.title || char(10) || substr(n.body, 1, 400),
                           char(10) || char(10)
                       ) AS combined
                FROM nodes n JOIN per_file pf ON pf.file_id = n.file_id
                WHERE n.id != pf.file_node_id
                GROUP BY n.file_id
            )
            SELECT fl.node_id, fl.title,
                   CASE
                       WHEN coalesce(cb.combined, '') = '' THEN fl.body
                       WHEN fl.body = '' THEN cb.combined
                       ELSE fl.body || char(10) || char(10) || cb.combined
                   END AS body,
                   fl.tags, pf.path, 0.0 AS score
            FROM per_file pf
            JOIN file_level fl ON fl.id = pf.file_node_id
            LEFT JOIN child_bodies cb ON cb.file_id = pf.file_id
            ORDER BY pf.mtime DESC
            LIMIT :lim
        """), {"pat": pat, "lim": limit}).fetchall()
    else:
        rows = session.execute(text(f"""
            SELECT n.node_id, n.title, n.body, {merged_n} AS tags,
                   f.path, 0.0 AS score
            FROM nodes n JOIN files f ON f.id = n.file_id
            WHERE lower({merged_n}) LIKE :pat
            ORDER BY n.mtime DESC
            LIMIT :lim
        """), {"pat": pat, "lim": limit}).fetchall()
    return [SearchResult(*r) for r in rows]


def keyword_search(
    session: Session,
    query: str,
    limit: int = 10,
) -> list[SearchResult]:
    from .db import merged_tags_sql
    merged = merged_tags_sql("n")
    like = f"%{query}%"
    rows = session.execute(text(f"""
        SELECT
            n.node_id,
            n.title,
            n.body,
            {merged} AS tags,
            f.path,
            0.0 AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.title LIKE :q OR n.body LIKE :q OR {merged} LIKE :q
        LIMIT :lim
    """), {"q": like, "lim": limit}).fetchall()
    return [SearchResult(*r) for r in rows]
# search.py:1 ends here
