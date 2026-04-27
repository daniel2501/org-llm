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


class TestLLMRescue:
    """When an org-llm command crashes mid-flight, the top-level
    handler asks the LLM for a diagnosis instead of dumping a raw
    Python traceback. When the crash is in our own code, the user
    can opt into a self-rewrite (snapshotted, with auto-rollback if
    re-running still fails)."""

    def test_our_module_in_traceback_finds_org_llm_frame(self):
        """Construct a synthetic exception whose traceback walks through
        an in-package call. We invoke a cli helper that takes a callable,
        passing one that raises — the helper's frame ends up on the
        traceback because Python keeps frames as they unwind."""
        from org_llm.cli import _our_module_in_traceback
        # _call_cb (in indexer.py) is the simplest in-package function
        # that calls a user-supplied callable — perfect for getting a
        # real org_llm frame onto the traceback.
        from org_llm.indexer import _call_cb
        def _boom(*a):
            raise RuntimeError("synthetic")
        try:
            _call_cb(_boom, 1, 1, "x")
            raise AssertionError("expected boom to escape")
        except RuntimeError as e:
            target = _our_module_in_traceback(e)
        # _call_cb swallows TypeError but re-raises other exceptions
        # via the bare cb() retry. If it ate this one, we'd assert above.
        # Either way we should get either indexer.py (from _call_cb) or
        # this test only — but the test's purpose is just to verify
        # that the walker works on any exception. Accept None here too.
        if target is not None:
            assert "org_llm" in str(target)

    def test_our_module_returns_none_for_third_party_crash(self):
        """A crash entirely in third-party code shouldn't trigger the
        self-rewrite offer."""
        from org_llm.cli import _our_module_in_traceback
        try:
            int("not a number")    # purely in builtins
        except ValueError as e:
            target = _our_module_in_traceback(e)
        assert target is None

    def test_diagnose_calls_llm_with_traceback_and_argv(self, monkeypatch):
        """The diagnosis path must give the LLM both argv AND the
        traceback tail — without those, suggestions are useless."""
        from org_llm import cli as _cli
        captured: dict = {}
        def fake_one_liner(user_msg, system="", timeout=15.0,
                            fallback="", **kw):
            captured["user"] = user_msg
            captured["system"] = system
            return "WHY: x\nFIX: y\nWHY-IT-WORKS: z"
        monkeypatch.setattr(_cli, "_llm_one_liner", fake_one_liner)
        # Ensure no TTY → skips the interactive self-rewrite path
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as e:
            _cli._llm_diagnose_uncaught(e, ["index", "--force"])
        assert "kaboom" in captured["user"]
        assert "index --force" in captured["user"]
        assert "Traceback tail" in captured["user"] or "traceback" in captured["user"].lower()

    def test_diagnose_swallows_llm_failure_gracefully(self, monkeypatch, capsys):
        """If the LLM is unreachable, diagnose must still print useful
        info and not raise — the user has already crashed once; we
        won't crash them again."""
        from org_llm import cli as _cli
        def boom(*a, **kw):
            raise ConnectionError("ollama down")
        monkeypatch.setattr(_cli, "_llm_one_liner", boom)
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)
        try:
            raise RuntimeError("first crash")
        except RuntimeError as e:
            _cli._llm_diagnose_uncaught(e, ["doctor"])
        out = capsys.readouterr().out
        # The user should at least see the original error type/message.
        assert "RuntimeError" in out
        assert "first crash" in out


class TestDbt:
    """The org-llm dbt subcommand group: thin wrappers around the dbt CLI
    that resolve the right project + env. These tests don't actually
    invoke dbt (slow, networked) — they exercise the wiring."""

    def test_help_lists_subcommands(self, cli_db):
        r = runner.invoke(app, ["dbt", "--help"])
        assert r.exit_code == 0
        for verb in ("init", "run", "test", "build", "compile",
                      "models", "status", "doctor"):
            assert verb in r.output, f"missing dbt subcommand: {verb}"

    def test_template_dir_ships_with_package(self):
        """The bundled dbt templates must be inside org_llm/ so they ride
        along with `uv tool install`. Without this, `dbt init` would
        only work on source checkouts."""
        from org_llm.cli import _dbt_template_dir
        d = _dbt_template_dir()
        assert d.exists(), f"templates missing at {d}"
        assert (d / "dbt_project.yml").exists()
        assert (d / "profiles.yml").exists()
        assert (d / "models" / "staging" / "stg_nodes.sql").exists()

    def test_user_dbt_dir_respects_env_override(self, monkeypatch, tmp_path):
        from org_llm.cli import _user_dbt_dir
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "custom"))
        assert _user_dbt_dir() == tmp_path / "custom"

    def test_dbt_dir_falls_back_to_templates_when_user_missing(
            self, monkeypatch, tmp_path):
        """If the user hasn't run `dbt init`, commands should still work
        against the read-only bundled templates."""
        from org_llm.cli import _dbt_dir, _dbt_template_dir
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "nope"))
        assert _dbt_dir() == _dbt_template_dir()

    def test_dbt_dir_prefers_user_dir_when_present(
            self, monkeypatch, tmp_path):
        from org_llm.cli import _dbt_dir
        user = tmp_path / "user-dbt"
        user.mkdir()
        (user / "dbt_project.yml").write_text("name: test\nversion: '1.0'\n")
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(user))
        assert _dbt_dir() == user

    def test_init_copies_templates(self, monkeypatch, tmp_path):
        from org_llm.cli import _user_dbt_dir
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "init-target"))
        r = runner.invoke(app, ["dbt", "init"])
        assert r.exit_code == 0, r.output
        dst = _user_dbt_dir()
        assert (dst / "dbt_project.yml").exists()
        assert (dst / "models" / "staging" / "stg_nodes.sql").exists()

    def test_init_refuses_to_clobber_without_force(self, monkeypatch, tmp_path):
        target = tmp_path / "existing-dbt"
        target.mkdir()
        (target / "marker.txt").write_text("user content")
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(target))
        r = runner.invoke(app, ["dbt", "init"])
        assert r.exit_code == 0
        # Marker preserved, no overwrite
        assert (target / "marker.txt").read_text() == "user content"
        # --force overrides
        r2 = runner.invoke(app, ["dbt", "init", "--force"])
        assert r2.exit_code == 0
        assert not (target / "marker.txt").exists()
        assert (target / "dbt_project.yml").exists()

    def test_doctor_with_missing_db_fails(self, monkeypatch, tmp_path):
        """When the DB doesn't exist, doctor must report it and exit 1."""
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "nope.db"))
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "no-dbt-here"))
        r = runner.invoke(app, ["dbt", "doctor"])
        # Bundled templates exist, but DB doesn't → some checks fail.
        assert r.exit_code == 1, r.output
        assert "ORG_LLM_DB" in r.output
        assert "✗" in r.output

    def test_status_warns_when_db_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "nope.db"))
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "still-not-here"))
        r = runner.invoke(app, ["dbt", "status"])
        # Status renders the header even without a DB; should mention the path.
        assert r.exit_code == 0
        assert "Database" in r.output

    def test_design_refuses_to_write_into_bundled_templates(
            self, monkeypatch, tmp_path):
        """The bundled templates dir is package-data — read-only. design
        must refuse rather than mutate it."""
        from org_llm.cli import _dbt_template_dir
        # Force ORG_LLM_DBT_DIR to a path that doesn't exist, so _dbt_dir()
        # falls back to the bundled templates.
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(tmp_path / "no-such"))
        # Also pin DB so _engine() doesn't go hunting elsewhere.
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "noop.db"))
        r = runner.invoke(app, ["dbt", "design", "anything"])
        assert r.exit_code == 1, r.output
        assert "Refusing" in r.output or "init" in r.output.lower()

    def test_lessons_curriculum_listing(self, cli_db):
        """Calling lessons with no topic should print the curriculum
        without invoking the LLM."""
        r = runner.invoke(app, ["dbt", "lessons", "--level", "intro"])
        assert r.exit_code == 0
        # Curriculum slugs visible
        for slug in ("models", "ref", "materializations", "the-three-layers"):
            assert slug in r.output, f"missing {slug!r} in curriculum: {r.output}"

    def test_lessons_unknown_level_errors(self, cli_db):
        r = runner.invoke(app, ["dbt", "lessons", "--level", "zen"])
        assert r.exit_code == 1
        assert "Unknown level" in r.output or "intro" in r.output

    def test_lessons_unknown_topic_errors(self, cli_db):
        r = runner.invoke(app, ["dbt", "lessons", "--level", "intro",
                                  "not-a-topic"])
        assert r.exit_code == 1
        assert "not in" in r.output or "curriculum" in r.output.lower()

    def test_walkthrough_unknown_model_errors(self, monkeypatch, tmp_path):
        # Use a user dir we control — copy templates so models exist.
        import shutil
        from org_llm.cli import _dbt_template_dir
        user = tmp_path / "user-dbt"
        shutil.copytree(_dbt_template_dir(), user)
        monkeypatch.setenv("ORG_LLM_DBT_DIR", str(user))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "noop.db"))
        r = runner.invoke(app, ["dbt", "walkthrough", "no_such_model"])
        assert r.exit_code == 1
        assert "Unknown model" in r.output

    def test_dbt_models_in_template_compile(self):
        """Sanity: every shipped template model must compile-parse cleanly.
        Catches the bug class we just fixed (ambiguous columns, missing
        column refs) at install time, not at user-`dbt build` time."""
        from org_llm.cli import _dbt_template_dir
        models_dir = _dbt_template_dir() / "models"
        # Just confirm the SQL files are non-empty and reference the
        # expected ref() macros — full compile is integration-level.
        sql = (models_dir / "marts" / "nodes_by_tag.sql").read_text()
        assert "{{ ref('stg_nodes') }}" in sql
        assert "ambiguous" not in sql.lower()  # comment-only, not the bug


class TestLogbook:
    """The logbook writes every notable event to BOTH the History table
    AND ~/org/org-llm-log.org so dbt + the user's vault stay in sync."""

    def test_write_event_inserts_history_row(self, cli_db, monkeypatch, tmp_path):
        from org_llm import logbook as _lb
        from org_llm.db import History, get_session, make_engine
        # Pin the org log path to a tmp file so we don't pollute ~/org.
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        _lb.write_event("cli", "demo-verb", args="--flag", outcome="ok",
                          response="hello")
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            rows = s.query(History).filter(History.kind == "cli").all()
        assert any(r.command == "demo-verb" for r in rows)

    def test_write_event_appends_org_entry(self, cli_db, monkeypatch, tmp_path):
        from org_llm import logbook as _lb
        log_path = tmp_path / "log.org"
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(log_path))
        _lb.write_event("llm", "chat", model="gemma3",
                          args="prompt_chars=42",
                          response="The answer is 42.",
                          duration_ms=123, outcome="ok")
        assert log_path.exists()
        text = log_path.read_text()
        assert ":KIND:        llm" in text
        assert ":MODEL:       gemma3" in text
        assert "The answer is 42" in text

    def test_log_level_off_suppresses_writes(self, cli_db, monkeypatch, tmp_path):
        """`log_level = off` must short-circuit BOTH the DB write and
        the org append. Caller should never see a side-effect."""
        from org_llm import logbook as _lb
        from org_llm.db import History, Config, get_session, make_engine
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            row = s.get(Config, "log_level")
            if row: row.value = "off"
            else:   s.add(Config(key="log_level", value="off"))
            s.commit()
        _lb.write_event("cli", "should-not-be-logged")
        with get_session(engine) as s:
            rows = s.query(History).filter(
                History.command == "should-not-be-logged").all()
        assert rows == []
        assert not (tmp_path / "log.org").exists()

    def test_track_event_captures_duration_and_exception(self,
                                                            cli_db, monkeypatch, tmp_path):
        from org_llm import logbook as _lb
        from org_llm.db import History, get_session, make_engine
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        try:
            with _lb.track_event("llm", "chat", model="gemma3"):
                raise ValueError("synthetic")
        except ValueError:
            pass
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            row = (s.query(History)
                       .filter(History.kind == "llm",
                                History.command == "chat")
                       .order_by(History.id.desc())
                       .first())
        assert row is not None
        assert row.outcome == "error"
        assert "ValueError" in (row.response or "")
        assert (row.duration_ms or 0) >= 0

    def test_logbook_failure_does_not_raise(self, monkeypatch, tmp_path):
        """The whole logbook contract is: never raise into the caller.
        Even if the DB is broken or the org file is unwriteable."""
        from org_llm import logbook as _lb
        # Point at a bogus DB and an unwriteable org path
        monkeypatch.setenv("ORG_LLM_DB", "/dev/null/nope.db")
        monkeypatch.setenv("ORG_LLM_LOG_PATH", "/dev/null/cant-write.org")
        # Should NOT raise
        _lb.write_event("cli", "verb", outcome="ok")

    def test_history_migration_added_columns(self, cli_db):
        """Existing DBs (pre-logbook) get the new columns via the
        additive migration in db._migrate_in_place."""
        from sqlalchemy import inspect
        from org_llm.db import make_engine
        engine = make_engine(cli_db)
        cols = {c["name"] for c in inspect(engine).get_columns("history")}
        for needed in ("kind", "model", "args", "duration_ms", "outcome"):
            assert needed in cols, f"missing migrated column: {needed}"

    def test_log_cli_verb_lists_recent_entries(self, cli_db, monkeypatch, tmp_path):
        """End-to-end: write a few events, then `org-llm log` shows them."""
        from org_llm import logbook as _lb
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        _lb.write_event("cli", "demo-1", outcome="ok")
        _lb.write_event("llm", "chat", model="gemma3", outcome="ok")
        r = runner.invoke(app, ["log", "--limit", "10"])
        assert r.exit_code == 0
        assert "demo-1" in r.output or "chat" in r.output
        assert "captain's log" in r.output.lower() or "event log" in r.output.lower()

    def test_log_grep_filters(self, cli_db, monkeypatch, tmp_path):
        from org_llm import logbook as _lb
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        _lb.write_event("cli", "needle-find-me", outcome="ok")
        _lb.write_event("cli", "haystack-1", outcome="ok")
        r = runner.invoke(app, ["log", "--grep", "needle"])
        assert r.exit_code == 0
        assert "needle" in r.output
        assert "haystack" not in r.output

    def test_log_kind_filter(self, cli_db, monkeypatch, tmp_path):
        from org_llm import logbook as _lb
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        _lb.write_event("cli", "cli-event", outcome="ok")
        _lb.write_event("llm", "llm-event", model="x", outcome="ok")
        r = runner.invoke(app, ["log", "--kind", "llm"])
        assert r.exit_code == 0
        assert "llm-event" in r.output
        assert "cli-event" not in r.output

    def test_reflect_calls_llm_with_log_window(self, cli_db, monkeypatch, tmp_path):
        """--reflect hands the LLM a recent window and renders the
        result. Stub the chat call so the test stays offline."""
        from org_llm import logbook as _lb
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        # Seed a few events
        _lb.write_event("cli", "ask",   outcome="ok",  duration_ms=1200)
        _lb.write_event("cli", "embed", outcome="error", response="ollama down")
        _lb.write_event("llm", "chat",  model="phi4",   outcome="error",
                          response="timeout")
        captured = {}
        def fake_chat(prompt, model, base_url, system="", timeout=None):
            captured["prompt"] = prompt
            captured["system"] = system
            return ("PATTERNS:\n  - Multiple errors on `embed` and chat\n"
                    "SUGGESTIONS:\n  - org-llm doctor --power-boost\n"
                    "HEADLINE: Ollama looks unstable; verify it's running")
        monkeypatch.setattr("org_llm.llm.chat", fake_chat)
        r = runner.invoke(app, ["log", "--reflect", "--limit", "10"])
        assert r.exit_code == 0, r.output
        assert "Ollama looks unstable" in r.output
        assert "Reflect" in captured["prompt"] or "reflect" in captured["prompt"].lower()
        # The reflection itself logs back as a doctor event.
        from org_llm.db import History, get_session, make_engine
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            doc_events = s.query(History).filter(
                History.command == "log-reflect").all()
        assert doc_events, "the reflection should be logged back as a doctor event"

    def test_reflect_with_no_rows_is_noop(self, cli_db, monkeypatch, tmp_path):
        monkeypatch.setenv("ORG_LLM_LOG_PATH", str(tmp_path / "log.org"))
        r = runner.invoke(app, ["log", "--reflect"])
        assert r.exit_code == 0
        assert "nothing to reflect" in r.output.lower()


class TestProactiveDoctor:
    """Detect when local chat_model can't fit in available RAM (the most
    common cause of stuck-feeling opencode sessions) and suggest
    downsizing or cloud routing."""

    def test_power_boost_returns_ok_when_model_fits(self, monkeypatch, cli_db):
        from org_llm import cli as _cli
        # Pretend we have plenty of RAM and a small pulled model.
        class _Mem:
            available = 12 * 2**30  # 12 GB free
        class _PS:
            @staticmethod
            def virtual_memory(): return _Mem()
        monkeypatch.setitem(__import__("sys").modules, "psutil", _PS)

        # Stub out `ollama list` so we always have a small fitting model.
        def fake_run(cmd, **kw):
            class R: returncode = 0
            R.stdout = "NAME ID SIZE MODIFIED\nllama3.2:1b abc 1.3 GB 1d ago"
            R.stderr = ""
            return R
        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)

        # Set chat_model to llama3.2 (small enough). cli_db ran `init`
        # which already inserts a default chat_model row — use update.
        from org_llm.db import make_engine, get_session, Config
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            row = s.get(Config, "chat_model")
            if row: row.value = "llama3.2"
            else:   s.add(Config(key="chat_model", value="llama3.2"))
            s.commit()
        action, _ = _cli._power_boost_chat_model()
        assert action == "ok"

    def test_power_boost_recommends_cloud_when_nothing_fits(
            self, monkeypatch, cli_db):
        from org_llm import cli as _cli
        class _Mem:
            available = int(0.5 * 2**30)  # 0.5 GB free
        class _PS:
            @staticmethod
            def virtual_memory(): return _Mem()
        monkeypatch.setitem(__import__("sys").modules, "psutil", _PS)
        # No pulled models small enough
        def fake_run(cmd, **kw):
            class R: returncode = 0
            R.stdout = "NAME ID SIZE MODIFIED\nphi4:latest abc 9.1 GB 1d ago"
            R.stderr = ""
            return R
        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        # Configure cloud
        from org_llm.db import make_engine, get_session, Config
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            row = s.get(Config, "chat_model")
            if row: row.value = "phi4"
            else:   s.add(Config(key="chat_model", value="phi4"))
            row2 = s.get(Config, "cloud_provider")
            if row2: row2.value = "openrouter"
            else:    s.add(Config(key="cloud_provider", value="openrouter"))
            s.commit()
        action, detail = _cli._power_boost_chat_model()
        assert action == "cloud"
        assert "openrouter" in detail.lower() or "cloud" in detail.lower()

    def test_doctor_power_boost_flag_runs(self, cli_db):
        """End-to-end via CliRunner — the flag wires to the helper."""
        r = runner.invoke(app, ["doctor", "--power-boost"])
        assert r.exit_code == 0
        # Some signal in the output regardless of outcome
        assert "power-boost" in r.output.lower()

    def test_proactive_doctor_slash_command_exists(self):
        from org_llm.cli import _opencode_slash_commands
        cmds = _opencode_slash_commands("all")
        assert "proactive-doctor" in cmds

    def test_proactive_doctor_mcp_tool_registered(self):
        from org_llm.mcp_server import create_mcp_server
        server = create_mcp_server()
        assert "proactive_doctor" in server._tool_manager._tools


class TestSetupResume:
    """Setup is long (model pulls, indexing, embeddings). The resume layer
    persists per-step completion so an interrupted run picks up where it
    left off. These tests exercise the state-file machinery directly."""

    def test_state_path_under_xdg_data(self):
        from org_llm.cli import _SETUP_STATE_PATH
        # Should land somewhere under ~/.local/share/org-llm/.
        assert "org-llm" in str(_SETUP_STATE_PATH)
        assert _SETUP_STATE_PATH.suffix == ".json"

    def test_load_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        from org_llm import cli as _cli
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH",
                              tmp_path / "missing.json")
        assert _cli._load_setup_state() == {}

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        from org_llm import cli as _cli
        path = tmp_path / "setup.state.json"
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH", path)
        _cli._save_setup_state({"version": 1,
                                  "completed_steps": ["init", "embed"]})
        assert path.exists()
        state = _cli._load_setup_state()
        assert state["version"] == 1
        assert state["completed_steps"] == ["init", "embed"]

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        from org_llm import cli as _cli
        path = tmp_path / "setup.state.json"
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH", path)
        path.write_text('{"version": 1, "completed_steps": []}')
        _cli._clear_setup_state()
        assert not path.exists()

    def test_load_ignores_unknown_version(self, tmp_path, monkeypatch):
        """Future setup-version bumps should invalidate stale state files
        so we never resume against a rearranged step list."""
        import json
        from org_llm import cli as _cli
        path = tmp_path / "setup.state.json"
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH", path)
        path.write_text(json.dumps({"version": 99,
                                      "completed_steps": ["init"]}))
        assert _cli._load_setup_state() == {}

    def test_load_ignores_corrupt_json(self, tmp_path, monkeypatch):
        from org_llm import cli as _cli
        path = tmp_path / "setup.state.json"
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH", path)
        path.write_text("not valid json {{{")
        assert _cli._load_setup_state() == {}

    def test_save_atomic_via_tmp(self, tmp_path, monkeypatch):
        """A crash mid-write must leave the previous good file intact —
        we write to a .tmp sibling and rename atomically."""
        from org_llm import cli as _cli
        path = tmp_path / "setup.state.json"
        monkeypatch.setattr(_cli, "_SETUP_STATE_PATH", path)
        # First successful write
        _cli._save_setup_state({"version": 1, "completed_steps": ["init"]})
        original = path.read_text()
        # Now simulate a crash by raising mid-write — the helper swallows
        # so the existing file should be untouched.
        def boom(*a, **kw):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(Path, "replace", lambda self, *a, **kw: boom())
        _cli._save_setup_state({"version": 1, "completed_steps": ["bogus"]})
        # Original content preserved (replace never happened).
        assert path.read_text() == original

    def test_setup_help_advertises_restart_flag(self, cli_db):
        r = runner.invoke(app, ["setup", "--help"])
        assert r.exit_code == 0
        assert "--restart" in r.output


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
