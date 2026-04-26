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
) -> list[SearchResult]:
    """Most-recently-modified nodes whose file path contains a given substring.

    Used to surface daily-journal-style folders (`/daily/`, `/journal/`,
    `/diary/`) when a query references them by topic regardless of when
    the file was last touched.
    """
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
        WHERE f.path LIKE :pat
        ORDER BY n.mtime DESC
        LIMIT :lim
    """), {"pat": f"%/{path_substring}/%", "lim": limit}).fetchall()
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
