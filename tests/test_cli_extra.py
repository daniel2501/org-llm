# [[file:../../../org/20260425230731-org_llm.org::*tests/test_cli_extra.py][test_cli_extra.py:1]]
"""Deep CLI tests: cloud, db, models, launch dry-run, claude dry-run, doctor.

Goals:
  - Catch regressions in the previously-buggy mtime → date display path.
  - Lock in the multi-provider cloud command surface.
  - Exercise help text and the tutor index so docs stay in sync with code.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.cli import app, _MODULE_MAP, _TUTOR_STEPS, _TASK_MODEL_KEYS
from org_llm.db import Config, File, Node, get_session, make_engine

runner = CliRunner()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("ORG_LLM_DB", str(db_path))
    runner.invoke(app, ["init"])
    return db_path


@pytest.fixture
def populated_org(tmp_path, cli_db):
    """Org dir wired into the CLI DB with one indexed, recent .org file."""
    org = tmp_path / "vault"
    org.mkdir()
    (org / "note.org").write_text(
        ":PROPERTIES:\n:ID: id-001\n:END:\n"
        "#+title: First Note\n\nBody about solidarity.\n"
        "* Sub heading\n:PROPERTIES:\n:ID: id-sub\n:END:\n\nMore body.\n"
    )
    engine = make_engine(cli_db)
    with get_session(engine) as s:
        s.get(Config, "org_dir").value = str(org)
        s.commit()
    runner.invoke(app, ["index"])
    return org


# ── help & version surface ────────────────────────────────────────────────────

class TestHelp:
    def test_root_help(self):
        r = runner.invoke(app, ["--help"])
        assert r.exit_code == 0
        # Every commandable feature should be discoverable from --help
        for cmd in ("init", "index", "embed", "search", "ask", "models",
                    "config", "db", "install", "report", "doctor", "tutor",
                    "source", "capture", "tag", "code", "launch", "claude",
                    "cloud", "mcp", "skill", "skills", "skill-index", "skill-new"):
            assert cmd in r.output, f"`{cmd}` missing from --help"

    def test_cloud_help_mentions_pass(self):
        r = runner.invoke(app, ["cloud", "--help"])
        assert r.exit_code == 0
        assert "pass" in r.output.lower()
        assert "--creds" in r.output


# ── cloud subcommand surface ─────────────────────────────────────────────────

class TestCloud:
    def test_providers_lists_all(self):
        r = runner.invoke(app, ["cloud", "--providers"])
        assert r.exit_code == 0
        # All seven slugs should be in the table
        for slug in ("runpod", "vast", "lambda", "tensordock",
                     "salad", "paperspace", "coreweave"):
            assert slug in r.output

    def test_signup_list_alias(self):
        r = runner.invoke(app, ["cloud", "--signup", "list"])
        assert r.exit_code == 0
        assert "runpod" in r.output

    def test_signup_unknown_provider(self):
        r = runner.invoke(app, ["cloud", "--signup", "not-a-provider"])
        assert r.exit_code == 1
        assert "Unknown provider" in r.output

    def test_cost_shows_all_providers(self):
        r = runner.invoke(app, ["cloud", "--cost"])
        assert r.exit_code == 0
        assert "RunPod"     in r.output
        assert "Vast.ai"    in r.output
        assert "Salad"      in r.output
        # Cents-per-1k formatting
        assert "¢" in r.output

    def test_creds_status_when_pass_unavailable(self, cli_db, monkeypatch):
        # Force pass to look unavailable so the test is deterministic
        from org_llm import creds
        monkeypatch.setattr(creds, "is_installed",   lambda: False)
        monkeypatch.setattr(creds, "is_initialized", lambda: False)
        r = runner.invoke(app, ["cloud", "--creds"])
        assert r.exit_code == 0
        assert "not installed" in r.output

    def test_status_with_no_endpoint_explains(self, cli_db):
        r = runner.invoke(app, ["cloud", "--status"])
        assert r.exit_code == 0
        assert "No cloud backend configured" in r.output


# ── db subcommand ────────────────────────────────────────────────────────────

class TestDbCommand:
    def test_default_shows_overview(self, cli_db):
        r = runner.invoke(app, ["db"])
        assert r.exit_code == 0
        assert "files"  in r.output
        assert "nodes"  in r.output
        assert "config" in r.output

    def test_schema_flag(self, cli_db):
        r = runner.invoke(app, ["db", "--schema"])
        assert r.exit_code == 0
        assert "CREATE TABLE" in r.output

    def test_dict_flag(self, cli_db):
        r = runner.invoke(app, ["db", "--dict"])
        assert r.exit_code == 0
        assert "files"  in r.output
        assert "nodes"  in r.output

    def test_query_select_only(self, cli_db):
        r = runner.invoke(app, ["db", "-q", "SELECT key FROM config LIMIT 3"])
        assert r.exit_code == 0
        assert "key" in r.output

    def test_query_rejects_non_select(self, cli_db):
        r = runner.invoke(app, ["db", "-q", "DROP TABLE config"])
        assert r.exit_code == 1
        assert "Only SELECT" in r.output


# ── models subcommand ────────────────────────────────────────────────────────

class TestModelsCommand:
    def test_default_renders(self, cli_db):
        r = runner.invoke(app, ["models"])
        assert r.exit_code == 0
        assert "Model Assignments" in r.output

    def test_discover_shows_catalog(self, cli_db):
        r = runner.invoke(app, ["models", "--discover"])
        assert r.exit_code == 0
        # A few canonical models should appear
        assert "llama" in r.output.lower()
        assert "phi" in r.output.lower()


# ── source subcommand ────────────────────────────────────────────────────────

class TestSource:
    def test_unknown_module_errors(self, cli_db):
        r = runner.invoke(app, ["source", "not-a-module"])
        assert r.exit_code == 1
        assert "Unknown module" in r.output

    def test_creds_module_resolves(self, cli_db):
        # Module map should include creds (added in this audit pass)
        assert "creds" in _MODULE_MAP
        r = runner.invoke(app, ["source", "creds"])
        assert r.exit_code == 0
        assert "pass" in r.output.lower()


# ── tutor index integrity ────────────────────────────────────────────────────

class TestTutorIntegrity:
    def test_creds_step_present(self):
        names = [n for n, _ in _TUTOR_STEPS]
        assert "creds" in names

    def test_step_names_unique(self):
        names = [n for n, _ in _TUTOR_STEPS]
        assert len(names) == len(set(names))

    def test_each_step_has_body(self):
        for name, body in _TUTOR_STEPS:
            assert body, f"empty tutor body for {name}"

    def test_welcome_lists_creds(self):
        body = next(b for n, b in _TUTOR_STEPS if n == "welcome")
        assert "creds" in body

    def test_creds_step_renders_via_cli(self, cli_db):
        r = runner.invoke(app, ["tutor", "creds"])
        assert r.exit_code == 0
        assert "pass" in r.output.lower()


# ── launch / claude dry-runs ─────────────────────────────────────────────────
#
# These exercise the previously-buggy mtime path. Before the audit, both
# commands crashed with `TypeError: 'float' object is not subscriptable`
# the moment a recent node existed. Indexing populated_org makes mtime
# a real epoch float — so a dry-run is sufficient to catch the regression.

class TestLaunchDryRun:
    """Regression test: launch used to crash with TypeError on n.mtime[:10]."""
    def test_dry_run_does_not_crash_with_recent_nodes(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run"])
        assert r.exit_code == 0, r.output
        # Rich wraps long lines; collapse whitespace before substring check
        flat = "".join(r.output.split())
        assert ".opencode.json" in flat

    def test_no_context_dry_run(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run", "--no-context"])
        assert r.exit_code == 0


class TestClaudeDryRun:
    """Regression test: claude used to crash with TypeError on n.mtime[:10]."""
    def test_dry_run_does_not_crash_with_recent_nodes(self, populated_org):
        r = runner.invoke(app, ["claude", "--dry-run"])
        assert r.exit_code == 0, r.output
        flat = "".join(r.output.split())
        assert "settings.json" in flat
        assert "CLAUDE.md" in flat

    def test_no_context_dry_run(self, populated_org):
        r = runner.invoke(app, ["claude", "--dry-run", "--no-context"])
        assert r.exit_code == 0


# ── _TASK_MODEL_KEYS shape ───────────────────────────────────────────────────

class TestTaskModelKeys:
    def test_each_entry_is_triple(self):
        for entry in _TASK_MODEL_KEYS:
            assert len(entry) == 3
            role, key, purpose = entry
            assert role and key and purpose
            assert key.endswith("_model")

    def test_keys_match_db_defaults(self):
        from org_llm.db import MODEL_DEFAULTS
        for _, key, _ in _TASK_MODEL_KEYS:
            assert key in MODEL_DEFAULTS, f"{key} not in MODEL_DEFAULTS"
# test_cli_extra.py:1 ends here
