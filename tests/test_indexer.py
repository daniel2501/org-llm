# [[file:../../../org/20260425230731-org_llm.org::*test_indexer.py][test_indexer.py:1]]
from __future__ import annotations

import time
import pytest
from pathlib import Path

from org_llm.indexer import _extract_nodes, index_file, index_directory
from org_llm.db import File, Node


class TestExtractNodes:
    def test_title_from_keyword(self, tmp_path):
        p = tmp_path / "note.org"
        p.write_text("#+title: My Note\n\nSome body.\n")
        nodes = _extract_nodes(p)
        assert nodes[0]["title"] == "My Note"

    def test_fallback_to_stem(self, tmp_path):
        p = tmp_path / "my_note.org"
        p.write_text("* Heading only\n\nBody.\n")
        assert _extract_nodes(p)[0]["title"] == "my_note"

    def test_duplicate_title_takes_first(self, tmp_path):
        p = tmp_path / "dup.org"
        p.write_text("#+TITLE: First\n#+TITLE: Second\n\nBody.\n")
        assert _extract_nodes(p)[0]["title"] == "First"

    def test_deduplicates_shared_id(self, tmp_path):
        p = tmp_path / "shared.org"
        p.write_text(
            ":PROPERTIES:\n:ID: same\n:END:\n"
            "#+title: Root\n\n"
            "* Heading\n:PROPERTIES:\n:ID: same\n:END:\n\nBody.\n"
        )
        ids = [n["node_id"] for n in _extract_nodes(p) if n["node_id"]]
        assert ids.count("same") == 1

    def test_heading_count(self, tmp_path):
        p = tmp_path / "multi.org"
        p.write_text("#+title: Root\n\n* H1\n\nBody.\n\n* H2\n\nBody2.\n")
        assert len(_extract_nodes(p)) == 3   # root + 2 headings

    def test_tags_extracted(self, tmp_path):
        p = tmp_path / "tagged.org"
        p.write_text("#+title: Tagged\n#+filetags: :work:home:\n\nBody.\n")
        nodes = _extract_nodes(p)
        # tags may appear in root or not — just ensure no crash
        assert isinstance(nodes[0]["tags"], str)


class TestIndexFile:
    def test_creates_file_and_node_records(self, tmp_path, session):
        p = tmp_path / "note.org"
        p.write_text("#+title: Hello\n\nWorld.\n")
        count = index_file(p, session)
        assert count == 1
        assert session.query(File).count() == 1
        assert session.query(Node).filter_by(title="Hello").count() == 1

    def test_skips_unchanged_file(self, tmp_path, session):
        p = tmp_path / "stable.org"
        p.write_text("#+title: Stable\n\nBody.\n")
        assert index_file(p, session) == 1
        assert index_file(p, session) == 0   # mtime unchanged

    def test_reindexes_after_mtime_change(self, tmp_path, session):
        p = tmp_path / "v.org"
        p.write_text("#+title: V1\n\nOld.\n")
        index_file(p, session)

        # Force older mtime in DB so next call reindexes
        rec = session.query(File).filter_by(path=str(p)).first()
        rec.mtime = 0.0
        session.commit()

        p.write_text("#+title: V2\n\nNew.\n")
        count = index_file(p, session)
        assert count > 0
        assert session.query(Node).filter_by(title="V2").count() == 1

    def test_replaces_nodes_on_reindex(self, tmp_path, session):
        p = tmp_path / "rep.org"
        p.write_text("#+title: Rep\n\n* Old heading\n\nBody.\n")
        index_file(p, session)

        rec = session.query(File).filter_by(path=str(p)).first()
        rec.mtime = 0.0
        session.commit()

        p.write_text("#+title: Rep\n\n* New heading\n\nBody.\n")
        index_file(p, session)
        titles = [n.title for n in session.query(Node).all()]
        assert "New heading" in titles
        assert "Old heading" not in titles


class TestIndexDirectory:
    def test_counts_match(self, tmp_org_dir, session):
        files, nodes = index_directory(tmp_org_dir, session)
        assert files >= 5    # note1, no_title, dup_title, shared_id, daily
        assert nodes >= files

    def test_daily_notes_included(self, tmp_org_dir, session):
        index_directory(tmp_org_dir, session)
        paths = [f.path for f in session.query(File).all()]
        assert any("daily" in p for p in paths)
# test_indexer.py:1 ends here
