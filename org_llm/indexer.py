# [[file:../../../org/20260425230731-org_llm.org::*indexer.py][indexer.py:1]]
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orgparse
from sqlalchemy.orm import Session

from .db import File, Node


def _extract_nodes(path: Path) -> list[dict]:
    """Parse one org file into a flat list of node dicts."""
    org = orgparse.load(str(path))
    mtime = path.stat().st_mtime
    nodes = []
    seen_ids: set[str] = set()

    def _node_id(n) -> str | None:
        nid = n.get_property("ID")
        if not nid or nid in seen_ids:
            return None
        seen_ids.add(nid)
        return nid

    # File-level node (some files have duplicate #+TITLE lines)
    titles = org.get_file_property_list("TITLE")
    title = titles[0] if titles else path.stem
    nodes.append(
        dict(
            node_id=_node_id(org),
            title=title,
            body=(org.body or "").strip(),
            tags=" ".join(org.tags) if org.tags else "",
            mtime=mtime,
        )
    )

    for heading in org[1:]:
        nodes.append(
            dict(
                node_id=_node_id(heading),
                title=heading.heading,
                body=(heading.body or "").strip(),
                tags=" ".join(heading.tags) if heading.tags else "",
                mtime=mtime,
            )
        )

    return nodes


def index_file(path: Path, session: Session) -> int:
    """Index one org file; skip if unchanged. Returns node count."""
    mtime = path.stat().st_mtime
    existing: File | None = (
        session.query(File).filter_by(path=str(path)).first()
    )
    if existing and existing.mtime >= mtime:
        return 0

    raw_nodes = _extract_nodes(path)
    now = datetime.now().isoformat()

    if existing:
        existing.mtime = mtime
        existing.indexed_at = now
        existing.node_count = len(raw_nodes)
        session.query(Node).filter_by(file_id=existing.id).delete()
        file_rec = existing
    else:
        file_rec = File(
            path=str(path), indexed_at=now,
            node_count=len(raw_nodes), mtime=mtime,
        )
        session.add(file_rec)
        session.flush()

    for n in raw_nodes:
        session.add(Node(file_id=file_rec.id, **n))

    session.commit()
    return len(raw_nodes)


def _call_cb(cb, *args):
    """Invoke a progress callback, tolerating both no-arg legacy
    callbacks (`def tick(): ...`) and the new (current, total[, msg])
    signature. Lets us instrument with rich detail without breaking
    existing CLI Rich-progress callers that just want to advance a bar.
    """
    if cb is None:
        return
    try:
        cb(*args)
    except TypeError:
        try:
            cb()
        except Exception:
            pass


def index_directory(org_dir: Path, session: Session,
                     progress_cb=None) -> tuple[int, int]:
    """Index all .org files under org_dir. Returns (files_indexed, nodes_indexed).

    `progress_cb`, if given, is called with (current_file_index,
    total_files, current_path_str) after each file. Legacy no-arg
    callbacks are tolerated via `_call_cb`."""
    paths = sorted(org_dir.rglob("*.org"))
    total = len(paths)
    files, nodes = 0, 0
    for i, p in enumerate(paths, 1):
        try:
            count = index_file(p, session)
            if count:
                files += 1
                nodes += count
        except Exception:
            session.rollback()
        _call_cb(progress_cb, i, total, str(p))
    return files, nodes


def embed_nodes(
    session: Session,
    model: str,
    base_url: str,
    force: bool = False,
    progress_cb=None,
) -> int:
    """Generate embeddings for unembedded nodes. Returns count embedded.

    `progress_cb`, if given, is called with (current, total,
    current_node_title) after each embedding attempt. Old no-arg
    callbacks are tolerated via `_call_cb`."""
    from .llm import embed
    from .search import to_blob

    q = session.query(Node)
    if not force:
        q = q.filter(Node.embedding.is_(None))
    nodes = q.all()
    total = len(nodes)

    embedded = 0
    for i, node in enumerate(nodes, 1):
        text = f"{node.title}\n{node.body}".strip()[:2048]
        try:
            vec = embed(text, model=model, base_url=base_url)
            node.embedding = to_blob(vec)
            session.commit()
            embedded += 1
        except Exception:
            session.rollback()
        _call_cb(progress_cb, i, total, node.title or "(untitled)")

    return embedded
# indexer.py:1 ends here
