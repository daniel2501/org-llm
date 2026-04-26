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
