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
) -> list[SearchResult]:
    blob = to_blob(query_vec)
    rows = session.execute(text("""
        SELECT
            n.node_id,
            n.title,
            n.body,
            n.tags,
            f.path,
            vec_distance_cosine(n.embedding, :qvec) AS score
        FROM nodes n
        JOIN files f ON f.id = n.file_id
        WHERE n.embedding IS NOT NULL
        ORDER BY score ASC
        LIMIT :lim
    """), {"qvec": blob, "lim": limit}).fetchall()
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
