# [[file:../../../org/20260425230731-org_llm.org::*test_cli.py][test_cli.py:1]]
from __future__ import annotations

import os
import pytest
from pathlib import Path
from typer.testing import CliRunner

from org_llm.cli import app
from org_llm.db import make_engine, get_session, Config

runner = CliRunner()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """Set ORG_LLM_DB env var so all CLI commands use a temp DB."""
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("ORG_LLM_DB", str(db_path))
    runner.invoke(app, ["init"])
    return db_path


@pytest.fixture
def cli_org(tmp_path, cli_db):
    """Temp org dir wired into the CLI DB config."""
    org = tmp_path / "org"
    org.mkdir()
    (org / "test.org").write_text("#+title: CLI Test Note\n\nTest body about socialism.\n")

    engine = make_engine(cli_db)
    with get_session(engine) as s:
        s.get(Config, "org_dir").value = str(org)
        s.commit()
    return org


class TestInit:
    def test_creates_db(self, cli_db):
        assert cli_db.exists()

    def test_idempotent(self, cli_db):
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

    def test_prints_path(self, cli_db):
        result = runner.invoke(app, ["init"])
        assert str(cli_db) in result.output


class TestConfig:
    def test_shows_all_keys(self, cli_db):
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "chat_model" in result.output

    def test_set_and_get(self, cli_db):
        runner.invoke(app, ["config", "chat_model", "test-model"])
        result = runner.invoke(app, ["config", "chat_model"])
        assert "test-model" in result.output

    def test_missing_key(self, cli_db):
        result = runner.invoke(app, ["config", "nonexistent_key"])
        assert result.exit_code == 0
        assert "not set" in result.output


class TestIndex:
    def test_indexes_files(self, cli_org):
        result = runner.invoke(app, ["index"])
        assert result.exit_code == 0
        assert "Indexed" in result.output

    def test_force_flag(self, cli_org):
        runner.invoke(app, ["index"])
        result = runner.invoke(app, ["index", "--force"])
        assert result.exit_code == 0
        assert "Cleared" in result.output


class TestSearch:
    def test_keyword_search(self, cli_org):
        runner.invoke(app, ["index"])
        result = runner.invoke(app, ["search", "--keyword", "socialism"])
        assert result.exit_code == 0
        assert "CLI Test Note" in result.output

    def test_no_results(self, cli_org):
        runner.invoke(app, ["index"])
        result = runner.invoke(app, ["search", "--keyword", "xyznonexistent"])
        assert result.exit_code == 0


class TestReport:
    def test_overview_runs(self, cli_org):
        runner.invoke(app, ["index"])
        result = runner.invoke(app, ["report", "overview"])
        assert result.exit_code == 0

    def test_tags_runs(self, cli_org):
        runner.invoke(app, ["index"])
        result = runner.invoke(app, ["report", "tags"])
        assert result.exit_code == 0


class TestTagProvenance:
    """`tag` writes only to auto_tags, never the file-source `tags`. The
    indexer is the only writer of `tags`; this split is what makes
    `--redo` and `--repair` non-destructive to hand-curated tags."""

    def test_schema_has_auto_tag_columns(self, cli_db):
        """Migration must add auto_tags + auto_tagged_at + auto_tagger_model."""
        from sqlalchemy import inspect
        engine = make_engine(cli_db)
        cols = {c["name"] for c in inspect(engine).get_columns("nodes")}
        assert {"auto_tags", "auto_tagged_at", "auto_tagger_model"} <= cols

    def test_merged_tags_preserves_order_and_dedups(self):
        from org_llm.db import merged_tags
        class N: pass
        n = N(); n.tags = "python tools"; n.auto_tags = "tools cli python"
        # File order first, auto-only tags appended; dupes dropped.
        assert merged_tags(n) == "python tools cli"

    def test_tag_writes_to_auto_tags_only(self, cli_org, monkeypatch):
        """The most important guarantee: `tag` never touches Node.tags,
        no matter how the LLM responds. File-source tags are sacred."""
        runner.invoke(app, ["index"])
        engine = make_engine(cli_org.parent / "cli.db")
        # Simulate a hand-tagged file by setting tags directly.
        from org_llm.db import Node
        with get_session(engine) as s:
            n = s.query(Node).first()
            n.tags = "human-curated"
            s.commit()
        # Stub out Ollama chat so the test is deterministic and offline.
        monkeypatch.setattr("org_llm.llm.chat",
                            lambda *a, **kw: "python tools cli")
        r = runner.invoke(app, ["tag", "--limit", "10"])
        assert r.exit_code == 0, r.output
        with get_session(engine) as s:
            n = s.query(Node).first()
            assert n.tags == "human-curated"          # untouched
            assert "python" in (n.auto_tags or "")    # auto-tagged
            assert n.auto_tagger_model               # provenance recorded

    def test_tag_default_skips_already_auto_tagged(self, cli_org, monkeypatch):
        runner.invoke(app, ["index"])
        engine = make_engine(cli_org.parent / "cli.db")
        from org_llm.db import Node
        with get_session(engine) as s:
            n = s.query(Node).first()
            n.auto_tags = "preset auto tags"
            s.commit()
        # If the LLM is consulted at all, the test fails.
        called = []
        def boom(*a, **kw):
            called.append(1)
            return "should-not-be-used"
        monkeypatch.setattr("org_llm.llm.chat", boom)
        r = runner.invoke(app, ["tag", "--limit", "10"])
        assert r.exit_code == 0
        assert not called, "default mode must skip nodes with existing auto_tags"

    def test_tag_redo_targets_already_tagged(self, cli_org, monkeypatch):
        runner.invoke(app, ["index"])
        engine = make_engine(cli_org.parent / "cli.db")
        from org_llm.db import Node
        with get_session(engine) as s:
            n = s.query(Node).first()
            n.auto_tags = "stale-result"
            s.commit()
        monkeypatch.setattr("org_llm.llm.chat",
                            lambda *a, **kw: "fresh tags now")
        r = runner.invoke(app, ["tag", "--redo", "--limit", "10"])
        assert r.exit_code == 0
        with get_session(engine) as s:
            n = s.query(Node).first()
            assert "fresh" in n.auto_tags
            assert "stale-result" not in n.auto_tags

    def test_repair_targets_only_low_quality(self):
        from org_llm.cli import _auto_tags_look_bad
        # Healthy auto-tags: 2+ tokens, all real, no dupes.
        assert not _auto_tags_look_bad("python tools cli")
        assert not _auto_tags_look_bad("")  # empty isn't "bad" — handled separately
        # Bad: single token, dupes, junk markers, single-char.
        assert _auto_tags_look_bad("python")
        assert _auto_tags_look_bad("python python tools")
        assert _auto_tags_look_bad("python tools no_tags_found")
        assert _auto_tags_look_bad("a b c")  # all single-char

    def test_force_tags_everything_but_preserves_human(self, cli_org, monkeypatch):
        runner.invoke(app, ["index"])
        engine = make_engine(cli_org.parent / "cli.db")
        from org_llm.db import Node
        with get_session(engine) as s:
            n = s.query(Node).first()
            n.tags      = "from-file"
            n.auto_tags = "old-auto"
            s.commit()
        monkeypatch.setattr("org_llm.llm.chat",
                            lambda *a, **kw: "force result")
        r = runner.invoke(app, ["tag", "--force", "--limit", "10"])
        assert r.exit_code == 0
        with get_session(engine) as s:
            n = s.query(Node).first()
            assert n.tags == "from-file"             # human tags preserved
            assert "force" in n.auto_tags            # auto refreshed

    def test_flag_mutex(self, cli_db):
        r = runner.invoke(app, ["tag", "--redo", "--repair"])
        assert r.exit_code == 1
        r = runner.invoke(app, ["tag", "--redo", "--force"])
        assert r.exit_code == 1

    def test_search_finds_via_auto_tag(self, cli_org, monkeypatch):
        """A reader-side guarantee: keyword search must hit auto_tags too,
        otherwise LLM-applied tags would be effectively invisible."""
        runner.invoke(app, ["index"])
        engine = make_engine(cli_org.parent / "cli.db")
        from org_llm.db import Node
        with get_session(engine) as s:
            n = s.query(Node).first()
            n.tags      = ""
            n.auto_tags = "synthwave underground"
            s.commit()
        from org_llm.search import keyword_search
        with get_session(engine) as s:
            hits = keyword_search(s, "synthwave", limit=5)
        assert any("synthwave" in (h.tags or "") for h in hits)


class TestHeartbeat:
    """`heartbeat()` is the on-the-fly progress utility for any blocking
    block of code — spinner ticks, elapsed time updates, slow-warning."""

    def test_heartbeat_yields_callable_tick(self):
        from org_llm.ui import heartbeat
        with heartbeat("doing thing", stall_secs=10.0) as tick:
            tick()  # must be callable
            tick()

    def test_heartbeat_warns_when_silent_past_threshold(self):
        """No tick() for >stall_secs/2 → emits a yellow warning. We use a
        very short poll/stall budget here so the test stays fast."""
        import io, time
        from rich.console import Console
        from org_llm import ui as _ui
        captured = io.StringIO()
        old_console = _ui.console
        _ui.console = Console(file=captured, force_terminal=False, width=120)
        try:
            with _ui.heartbeat("idle", stall_secs=0.4, warn_at=0.1,
                                poll_secs=0.05):
                time.sleep(0.5)  # > stall_secs * 0.5 = 0.2s
        finally:
            _ui.console = old_console
        out = captured.getvalue()
        assert "silent" in out.lower() or "heartbeat" in out.lower(), out

    def test_ollama_pull_progress_renders_via_api(self, monkeypatch):
        """Pull's API path streams ProgressResponse events into a Rich bar.
        We mock the client to yield three events and assert the function
        completes quickly + reports success."""
        import time
        from org_llm import cli as _cli

        class _Ev:
            def __init__(self, status, total, completed):
                self.status, self.total, self.completed = status, total, completed

        class _Cli:
            def __init__(self, *a, **kw): pass
            def pull(self, model, stream=True):
                yield _Ev("pulling manifest", None,    None)
                yield _Ev("downloading",     1_000_000, 500_000)
                yield _Ev("verifying",       1_000_000, 1_000_000)

        import ollama as _ol
        monkeypatch.setattr(_ol, "Client", _Cli)
        # Ollama binary lookup happens before the API call — give it a
        # path that exists.
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda x: "/bin/sh" if x == "ollama" else None)

        t0 = time.monotonic()
        ok = _cli._ollama_pull("fake-model", stall_secs=5.0)
        elapsed = time.monotonic() - t0
        assert ok is True
        assert elapsed < 3.0


class TestSkills:
    def test_skill_index_and_list(self, cli_org):
        (cli_org / "skill.org").write_text(
            "* Test skill                           :skill:\n"
            ":PROPERTIES:\n:SKILL_NAME: cli_skill\n:END:\n\n"
            "#+begin_src python\nprint('hi')\n#+end_src\n"
        )
        result = runner.invoke(app, ["skill-index"])
        assert result.exit_code == 0

        result = runner.invoke(app, ["skills"])
        assert result.exit_code == 0
        assert "cli_skill" in result.output
# test_cli.py:1 ends here
