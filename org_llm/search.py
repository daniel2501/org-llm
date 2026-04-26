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


def vector_search(
    session: Session,
    query_vec: list[float],
    limit: int = 10,
    since_mtime: float | None = None,
) -> list[SearchResult]:
    """Cosine-distance vector search. Optional mtime filter for temporal queries.

    `since_mtime` is a unix timestamp; rows whose `nodes.mtime` predates it
    are excluded. Use this to constrain "last week" / "last 30 days" queries.
    """
    blob = to_blob(query_vec)
    where = "WHERE n.embedding IS NOT NULL"
    params: dict = {"qvec": blob, "lim": limit}
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
            vec_distance_cosine(n.embedding, :qvec) AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        {where}
        ORDER BY score ASC
        LIMIT :lim
    """), params).fetchall()
    return [SearchResult(*r) for r in rows]


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
    pat = f"%{tag.lower()}%"
    if one_per_file:
        rows = session.execute(text("""
            WITH per_file AS (
                SELECT f.id AS file_id, MIN(n.id) AS file_node_id,
                       MAX(n.mtime) AS mtime, f.path AS path
                FROM nodes n JOIN files f ON f.id = n.file_id
                WHERE EXISTS (
                    SELECT 1 FROM nodes m
                    WHERE m.file_id = f.id AND lower(m.tags) LIKE :pat
                )
                GROUP BY f.id
            ),
            file_level AS (
                SELECT n.id, n.node_id, n.title, n.body, n.tags
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
        rows = session.execute(text("""
            SELECT n.node_id, n.title, n.body, n.tags, f.path, 0.0 AS score
            FROM nodes n JOIN files f ON f.id = n.file_id
            WHERE lower(n.tags) LIKE :pat
            ORDER BY n.mtime DESC
            LIMIT :lim
        """), {"pat": pat, "lim": limit}).fetchall()
    return [SearchResult(*r) for r in rows]


def keyword_search(
    session: Session,
    query: str,
    limit: int = 10,
) -> list[SearchResult]:
    like = f"%{query}%"
    rows = session.execute(text("""
        SELECT
            n.node_id,
            n.title,
            n.body,
            n.tags,
            f.path,
            0.0 AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.title LIKE :q OR n.body LIKE :q OR n.tags LIKE :q
        LIMIT :lim
    """), {"q": like, "lim": limit}).fetchall()
    return [SearchResult(*r) for r in rows]
# search.py:1 ends here
