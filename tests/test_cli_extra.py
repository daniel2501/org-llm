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
        assert "read-only" in r.output.lower() or "Only SELECT" in r.output


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


class TestLaunchWorkspaces:
    """Workspace presets shape the system prompt + slash-command set."""
    def test_default_workspace_is_all(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run"])
        assert r.exit_code == 0
        flat = " ".join(r.output.split())
        assert "Workspace: all" in flat

    def test_researcher_workspace(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run", "--workspace", "researcher"])
        assert r.exit_code == 0
        flat = " ".join(r.output.split())
        assert "Workspace: researcher" in flat
        # Research workspace should add /explore command
        assert "/explore" in r.output

    def test_scribe_workspace(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run", "--workspace", "scribe"])
        assert r.exit_code == 0
        assert "/capture" in r.output

    def test_engineer_workspace(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run", "--workspace", "engineer"])
        assert r.exit_code == 0
        assert "/repo" in r.output

    def test_invalid_workspace_errors(self, populated_org):
        r = runner.invoke(app, ["launch", "--dry-run", "--workspace", "wizard"])
        assert r.exit_code == 1
        assert "Unknown workspace" in r.output

    def test_short_flag(self, populated_org):
        r = runner.invoke(app, ["launch", "-n", "-w", "scribe"])
        assert r.exit_code == 0


class TestOpenCodeHelpers:
    """Theme + slash-command generators are pure — test directly."""

    def test_lcars_theme_has_palette(self):
        from org_llm.cli import _opencode_lcars_theme
        t = _opencode_lcars_theme()
        assert t["name"] == "org-llm-lcars"
        assert "primary" in t["theme"]
        # LCARS orange should be in the palette somewhere.
        assert any("FF9900" in str(v) or "B36300" in str(v)
                   for v in t["theme"].values())

    def test_default_slash_commands(self):
        from org_llm.cli import _opencode_slash_commands
        cmds = _opencode_slash_commands("all")
        for required in ("discover", "recent", "health", "stats",
                          "tags", "tutor", "code"):
            assert required in cmds
            assert "description:" in cmds[required]

    def test_workspace_specific_commands(self):
        from org_llm.cli import _opencode_slash_commands
        assert "explore" in _opencode_slash_commands("researcher")
        assert "capture" in _opencode_slash_commands("scribe")
        assert "repo"    in _opencode_slash_commands("engineer")
        # 'all' workspace gets none of the workspace-specific extras.
        all_cmds = _opencode_slash_commands("all")
        assert "explore" not in all_cmds
        assert "capture" not in all_cmds
        assert "repo"    not in all_cmds


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


class TestEmbedModelDetection:
    """Regression for the doctor-diagnose crash: an embedding model was
    falling through as the chat model when nothing else was pulled.
    """
    def test_recognises_common_embed_models(self):
        from org_llm.cli import _is_embed_model
        for name in (
            "nomic-embed-text:latest",
            "nomic-embed-text",
            "mxbai-embed-large:latest",
            "snowflake-arctic-embed:33m",
            "bge-m3:latest",
            "bge-large:latest",
            "all-minilm-l6-v2:latest",
        ):
            assert _is_embed_model(name), f"{name} should be flagged as embedding"

    def test_does_not_flag_chat_models(self):
        from org_llm.cli import _is_embed_model
        for name in (
            "llama3.2:latest", "llama3.2:3b", "llama3.3:70b",
            "phi3.5:3.8b", "phi4:14b", "qwen2.5-coder:7b",
            "deepseek-r1:7b", "mistral-nemo:12b", "gemma3:9b",
        ):
            assert not _is_embed_model(name), f"{name} should NOT be flagged as embedding"

    def test_default_models_match_catalog_stems(self):
        """Every MODEL_DEFAULTS *_model value should resolve to a non-default
        quality score. This catches my earlier mistake of defaulting to `phi3`
        when the catalog only has `phi3.5`.
        """
        from org_llm.db import MODEL_DEFAULTS
        from org_llm.models import _quality
        for key, val in MODEL_DEFAULTS.items():
            if not key.endswith("_model") or key == "embed_model":
                continue
            q = _quality(val)
            assert q > 50, f"{key}={val!r} → quality {q}: stem doesn't match any catalog entry"


class TestShellQuoteRepair:
    def test_glues_extra_args_for_ask(self):
        from org_llm.cli import _shell_quote_repair
        broken = ["ask", "connect", "Foo", "Bar", "to", "vault"]
        repaired = _shell_quote_repair(broken)
        assert repaired == ["ask", "connect Foo Bar to vault"]

    def test_returns_none_for_unknown_verb(self):
        from org_llm.cli import _shell_quote_repair
        # `init` doesn't take a single string arg → out of scope
        assert _shell_quote_repair(["init", "extra"]) is None

    def test_returns_none_for_short_argv(self):
        from org_llm.cli import _shell_quote_repair
        assert _shell_quote_repair(["ask"]) is None
        assert _shell_quote_repair(["ask", "single-token"]) is None

    def test_preserves_flags_unmodified(self):
        from org_llm.cli import _shell_quote_repair
        broken = ["ask", "what", "is", "this", "--cloud"]
        repaired = _shell_quote_repair(broken)
        assert repaired is not None
        assert "--cloud" in repaired
        assert any("what is this" in x for x in repaired)


class TestSuggestNoteAskShellSafety:
    """The Try-it suggestion must be safely copy-pasteable. Wrap in single
    quotes; never embed unescaped double quotes that would close the outer
    string."""
    def test_suggestion_is_single_quoted(self, tmp_path, monkeypatch):
        from pathlib import Path
        from org_llm.cli import _suggest_note_ask
        from org_llm.db import (Config, File, Node, get_session, init_db,
                                  make_engine)
        import time
        db_path = tmp_path / "shell.db"
        monkeypatch.setenv("ORG_LLM_DB", str(db_path))
        engine = make_engine(Path(db_path))
        init_db(engine)
        now = time.time()
        with get_session(engine) as s:
            f = File(path="/v/a.org", indexed_at="now",
                     node_count=1, mtime=now)
            s.add(f); s.flush()
            s.add(Node(file_id=f.id, node_id="n1",
                        title="Has a 'single' and \"double\" quote",
                        body="x", tags="poetry",
                        mtime=now))
            s.commit()
        with get_session(engine) as s:
            out = _suggest_note_ask(s, prefix="Try: ")
        assert "org-llm ask" in out
        # Must be single-quoted (so nested double quotes are safe)
        assert "'[/bold]" in out
        # And no inner single quotes that would close the outer wrapper —
        # the title's apostrophes should have been stripped.
        # Strip Rich markup before counting.
        import re as _re
        plain = _re.sub(r"\[/?[^\]]+\]", "", out)
        # Outer '...' wrapper plus possibly stripped inner content; should be
        # exactly two single quotes (open + close).
        assert plain.count("'") == 2, (
            f"Unexpected single-quote count in {plain!r}")
# test_cli_extra.py:1 ends here
