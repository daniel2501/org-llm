# [[file:../../../org/20260425230731-org_llm.org::*tests/test_report.py][test_report.py:1]]
"""Tests for org_llm.report — exercises every report renderer against a real DB."""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from rich.console import Console

import org_llm.report as report_mod
import org_llm.ui as ui
from org_llm.db import File, Node


def _capture(fn, *args, **kwargs):
    """Render a report through a captured Rich console."""
    buf = io.StringIO()
    saved = ui.console
    cap = Console(theme=ui.THEME, file=buf, force_terminal=False, width=120)
    # Patch both the ui-level and report-level console references
    ui.console = cap
    report_mod.console = cap
    try:
        fn(*args, **kwargs)
    finally:
        ui.console = saved
        report_mod.console = saved
    return buf.getvalue()


@pytest.fixture
def populated(session):
    """Seed the DB with a realistic mix of files, nodes, tags, daily notes."""
    now  = time.time()
    week = now - (5 * 86400)
    old  = now - (60 * 86400)

    f1 = File(path="/vault/notes/recent.org", indexed_at="now", node_count=2, mtime=now)
    f2 = File(path="/vault/daily/2026-04-26.org", indexed_at="now", node_count=1, mtime=week)
    f3 = File(path="/vault/archive/old.org", indexed_at="now", node_count=1, mtime=old)
    session.add_all([f1, f2, f3])
    session.flush()

    session.add(Node(file_id=f1.id, node_id="n1", title="Solidarity ✊",
                     body="Workers unite", tags="politics work", mtime=now,
                     embedding=b"\x00\x00\x80?\x00\x00\x00\x00\x00\x00\x00\x00"))
    session.add(Node(file_id=f1.id, node_id="n2", title="Untagged",
                     body="Body", tags="", mtime=now))
    session.add(Node(file_id=f2.id, node_id="n3", title="Daily entry",
                     body="What I did today", tags="daily", mtime=week))
    session.add(Node(file_id=f3.id, node_id="n4", title="Vintage thought",
                     body="Old idea — links to [[id:n1]]", tags="archive",
                     mtime=old))
    # An orphan node (has ID, no outgoing links)
    session.add(Node(file_id=f1.id, node_id="orphan-1", title="Orphan",
                     body="No backlinks", tags="lonely", mtime=now))
    session.commit()
    return session


class TestOverview:
    def test_renders_counts(self, populated):
        out = _capture(report_mod.report_overview, populated)
        assert "3" in out                    # files
        # 5 nodes seeded above
        assert "5" in out

    def test_includes_pride_banner(self, populated):
        out = _capture(report_mod.report_overview, populated)
        # Trans stripe + pride banner emit unicode block characters
        assert "█" in out


class TestTopTags:
    def test_lists_known_tags(self, populated):
        out = _capture(report_mod.report_top_tags, populated)
        # The SQL splits on space, so "politics" and "work" should each surface
        assert "politics" in out
        assert "work" in out
        assert "daily" in out

    def test_handles_empty(self, session):
        out = _capture(report_mod.report_top_tags, session)
        assert "No tags" in out


class TestRecent:
    def test_includes_recent_titles(self, populated):
        out = _capture(report_mod.report_recent, populated, days=14)
        assert "Solidarity" in out
        # 60-day-old entry must NOT appear
        assert "Vintage thought" not in out

    def test_wide_window_includes_old(self, populated):
        out = _capture(report_mod.report_recent, populated, days=365)
        assert "Vintage thought" in out


class TestOrphans:
    def test_finds_orphan_node(self, populated):
        out = _capture(report_mod.report_orphans, populated)
        # "Orphan" has an ID and no [[id:...]] outgoing links
        assert "Orphan" in out
        # "Vintage thought" links to n1 → not an orphan
        assert "Vintage thought" not in out


class TestDaily:
    def test_finds_daily_files(self, populated):
        out = _capture(report_mod.report_daily, populated)
        assert "Daily entry" in out
        assert "What I did" in out
# test_report.py:1 ends here
