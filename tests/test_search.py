# [[file:../../../org/20260425230731-org_llm.org::*test_search.py][test_search.py:1]]
from __future__ import annotations

import pytest
from org_llm.search import to_blob, from_blob, keyword_search, vector_search
from org_llm.db import File, Node


def _add_node(session, title, body="", tags="", vec=None):
    f = File(path=f"/tmp/{title}.org", indexed_at="now", node_count=1, mtime=1.0)
    session.add(f)
    session.flush()
    session.add(Node(
        file_id=f.id, title=title, body=body, tags=tags, mtime=1.0,
        embedding=to_blob(vec) if vec else None,
    ))
    session.commit()


class TestBlob:
    def test_roundtrip(self):
        vec = [1.0, 0.5, -0.3, 0.0]
        assert from_blob(to_blob(vec)) == pytest.approx(vec, abs=1e-6)

    def test_length(self):
        vec = [float(i) for i in range(128)]
        assert len(to_blob(vec)) == 128 * 4

    def test_empty(self):
        assert from_blob(to_blob([])) == []


class TestKeywordSearch:
    def test_finds_body_match(self, session):
        _add_node(session, "solidarity", body="workers of the world unite")
        results = keyword_search(session, "workers")
        assert any(r.title == "solidarity" for r in results)

    def test_finds_title_match(self, session):
        _add_node(session, "pride and solidarity")
        results = keyword_search(session, "pride")
        assert any("pride" in r.title for r in results)

    def test_finds_tag_match(self, session):
        _add_node(session, "tagged", tags="lgbtq work")
        results = keyword_search(session, "lgbtq")
        assert any(r.title == "tagged" for r in results)

    def test_no_match_returns_empty(self, session):
        _add_node(session, "nothing", body="unrelated content")
        assert keyword_search(session, "xyznonexistent") == []

    def test_respects_limit(self, session):
        for i in range(10):
            _add_node(session, f"note{i}", body="common word")
        results = keyword_search(session, "common", limit=3)
        assert len(results) <= 3


class TestVectorSearch:
    def test_nearest_first(self, session):
        _add_node(session, "exact",   vec=[1.0, 0.0, 0.0])
        _add_node(session, "close",   vec=[0.9, 0.1, 0.0])
        _add_node(session, "distant", vec=[0.0, 0.0, 1.0])
        results = vector_search(session, [1.0, 0.0, 0.0], limit=3)
        titles = [r.title for r in results]
        assert titles.index("exact") < titles.index("close") < titles.index("distant")

    def test_score_between_zero_and_two(self, session):
        _add_node(session, "v", vec=[1.0, 0.0, 0.0])
        results = vector_search(session, [1.0, 0.0, 0.0], limit=1)
        assert 0.0 <= results[0].score <= 2.0

    def test_empty_without_embeddings(self, session):
        _add_node(session, "no-embed", vec=None)
        assert vector_search(session, [1.0, 0.0, 0.0]) == []

    def test_respects_limit(self, session):
        for i in range(5):
            _add_node(session, f"vec{i}", vec=[float(i), 1.0, 0.0])
        results = vector_search(session, [1.0, 0.0, 0.0], limit=2)
        assert len(results) == 2
# test_search.py:1 ends here
