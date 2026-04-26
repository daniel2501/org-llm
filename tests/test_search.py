# [[file:../../../org/20260425230731-org_llm.org::*test_search.py][test_search.py:1]]
from __future__ import annotations

import time

import pytest
from org_llm.search import (
    from_blob, keyword_search, recent_in_path, recent_nodes, to_blob,
    vector_search,
)
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

    def test_since_mtime_filter_excludes_old(self, session):
        now = time.time()
        old = now - 30 * 86400
        # New node: mtime now
        f1 = File(path="/tmp/recent.org", indexed_at="now", node_count=1, mtime=now)
        session.add(f1); session.flush()
        session.add(Node(file_id=f1.id, title="recent", body="b", tags="",
                         mtime=now, embedding=to_blob([1.0, 0.0, 0.0])))
        # Old node: 30 days back
        f2 = File(path="/tmp/old.org", indexed_at="now", node_count=1, mtime=old)
        session.add(f2); session.flush()
        session.add(Node(file_id=f2.id, title="old", body="b", tags="",
                         mtime=old, embedding=to_blob([1.0, 0.0, 0.0])))
        session.commit()
        cutoff = now - 7 * 86400   # last 7 days
        results = vector_search(session, [1.0, 0.0, 0.0], limit=10,
                                since_mtime=cutoff)
        titles = [r.title for r in results]
        assert "recent" in titles
        assert "old" not in titles


class TestRecentNodes:
    def test_returns_most_recent_first(self, session):
        now = time.time()
        for offset, title in [(0, "newest"), (5, "middle"), (15, "oldest")]:
            f = File(path=f"/tmp/{title}.org", indexed_at="now",
                     node_count=1, mtime=now - offset * 86400)
            session.add(f); session.flush()
            session.add(Node(file_id=f.id, title=title, body="b", tags="",
                             mtime=now - offset * 86400))
        session.commit()
        results = recent_nodes(session, limit=10)
        titles = [r.title for r in results]
        assert titles[0] == "newest"
        assert titles[-1] == "oldest"

    def test_respects_since_mtime(self, session):
        now = time.time()
        for offset, title in [(0, "today"), (10, "ten_days_ago"), (60, "two_months_ago")]:
            f = File(path=f"/tmp/{title}.org", indexed_at="now",
                     node_count=1, mtime=now - offset * 86400)
            session.add(f); session.flush()
            session.add(Node(file_id=f.id, title=title, body="", tags="",
                             mtime=now - offset * 86400))
        session.commit()
        cutoff = now - 14 * 86400
        results = recent_nodes(session, limit=10, since_mtime=cutoff)
        titles = [r.title for r in results]
        assert "today" in titles
        assert "ten_days_ago" in titles
        assert "two_months_ago" not in titles


class TestRecentInPath:
    def test_finds_daily_folder(self, session):
        now = time.time()
        # File in daily folder
        f1 = File(path="/vault/daily/2026-04-12.org", indexed_at="now",
                  node_count=1, mtime=now)
        session.add(f1); session.flush()
        session.add(Node(file_id=f1.id, title="2026-04-12", body="entry",
                         tags="", mtime=now))
        # File NOT in daily folder
        f2 = File(path="/vault/notes/random.org", indexed_at="now",
                  node_count=1, mtime=now)
        session.add(f2); session.flush()
        session.add(Node(file_id=f2.id, title="random", body="b",
                         tags="", mtime=now))
        session.commit()

        results = recent_in_path(session, "daily", limit=10)
        titles = [r.title for r in results]
        assert "2026-04-12" in titles
        assert "random" not in titles

    def test_ignores_path_outside_substring(self, session):
        now = time.time()
        f = File(path="/vault/notes/journal-style.org", indexed_at="now",
                 node_count=1, mtime=now)
        session.add(f); session.flush()
        session.add(Node(file_id=f.id, title="journal-style",
                         body="", tags="", mtime=now))
        session.commit()
        # Looking for /journal/ as a folder, not a substring of filename
        results = recent_in_path(session, "journal", limit=10)
        assert all(r.title != "journal-style" for r in results)


class TestParseDaysWindow:
    def test_numeric_phrases(self):
        from org_llm.cli import _parse_days_window
        assert _parse_days_window("what about the last 30 days") == 30
        assert _parse_days_window("past 7 days notes") == 7
        assert _parse_days_window("things from 14 days ago") == 14

    def test_keyword_phrases(self):
        from org_llm.cli import _parse_days_window
        assert _parse_days_window("summarize last week") == 7
        assert _parse_days_window("what did I do this month") == 30
        assert _parse_days_window("anything from last quarter") == 90
        assert _parse_days_window("recent thoughts on X") == 14

    def test_no_temporal_phrase(self):
        from org_llm.cli import _parse_days_window
        assert _parse_days_window("what is org-mode") is None
        assert _parse_days_window("explain my politics tag") is None

    def test_numeric_wins_over_keyword(self):
        from org_llm.cli import _parse_days_window
        # "last 60 days" should beat the bare "last week"
        assert _parse_days_window("notes from the last 60 days last week") == 60
# test_search.py:1 ends here
