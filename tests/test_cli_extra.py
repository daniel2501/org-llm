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


class TestCloudFallback:
    """When --cloud is requested but no cloud_endpoint_url is configured,
    the command must fall back to local Ollama instead of red_alert+exit."""

    def test_ask_cloud_without_endpoint_falls_back(self, populated_org):
        # No cloud config in fixture's DB → must fall back, not exit 1
        r = runner.invoke(app, ["ask", "test", "--cloud"])
        # Either falls back to local (and then fails on its own merits because
        # there's no Ollama in the test env), OR shows the fallback message.
        # Critically: should NOT be `--cloud requested but no cloud_endpoint_url`
        # red-alert (that was the old behaviour).
        assert "Falling back to local" in r.output or r.exit_code != 1 or \
               "Ollama" in r.output or "Hailing" in r.output

    def test_helper_falls_back_on_rate_limit(self, monkeypatch):
        from org_llm.cli import _cloud_chat_with_local_fallback
        from urllib.error import HTTPError
        # Simulate cloud_chat raising 429
        def _fake_cloud(*args, **kwargs):
            raise HTTPError("http://x", 429, "Too Many Requests", {}, None)
        def _fake_local(*args, **kwargs):
            return "local-fallback-result"
        import org_llm.cloud as _cloud
        import org_llm.llm   as _llm
        monkeypatch.setattr(_cloud, "cloud_chat", _fake_cloud)
        monkeypatch.setattr(_llm,   "chat",       _fake_local)
        result = _cloud_chat_with_local_fallback(
            "hello", cloud_model="x", cloud_endpoint="http://x",
            cloud_api_key="k", local_model="phi3.5", local_url="http://localhost:11434")
        assert result == "local-fallback-result"

    def test_helper_falls_back_on_auth_error(self, monkeypatch):
        from org_llm.cli import _cloud_chat_with_local_fallback
        from urllib.error import HTTPError
        def _fake_cloud(*args, **kwargs):
            raise HTTPError("http://x", 401, "Unauthorized", {}, None)
        def _fake_local(*args, **kwargs):
            return "local-result"
        import org_llm.cloud as _cloud
        import org_llm.llm   as _llm
        monkeypatch.setattr(_cloud, "cloud_chat", _fake_cloud)
        monkeypatch.setattr(_llm,   "chat",       _fake_local)
        assert _cloud_chat_with_local_fallback(
            "x", cloud_model="m", cloud_endpoint="http://x",
            cloud_api_key="", local_model="phi3.5",
            local_url="http://localhost:11434") == "local-result"

    def test_helper_propagates_unclassified(self, monkeypatch):
        from org_llm.cli import _cloud_chat_with_local_fallback
        def _fake_cloud(*args, **kwargs):
            raise ValueError("totally random unrelated bug")
        import org_llm.cloud as _cloud
        monkeypatch.setattr(_cloud, "cloud_chat", _fake_cloud)
        with pytest.raises(ValueError):
            _cloud_chat_with_local_fallback(
                "x", cloud_model="m", cloud_endpoint="http://x",
                cloud_api_key="", local_model="phi3.5",
                local_url="http://localhost:11434")


class TestConfigFuzzyMatch:
    def test_unset_key_suggests_close_matches(self, populated_org):
        # `chat_modle` is a typo for `chat_model`
        r = runner.invoke(app, ["config", "chat_modle"])
        assert r.exit_code == 0
        # Either prints "not set" with suggestions or directly suggests
        assert ("Did you mean" in r.output or "chat_model" in r.output)

    def test_setting_unknown_key_warns(self, populated_org):
        r = runner.invoke(app, ["config", "chat_modle", "phi3.5"])
        assert r.exit_code == 0   # still allowed (might be a custom knob key)
        assert ("did you mean" in r.output.lower()
                or "Did you mean" in r.output
                or "chat_model" in r.output)


class TestModelTagFuzzyMatch:
    def test_helper_finds_close_match(self, monkeypatch):
        from org_llm.cli import _suggest_model_tag
        # Stub out list_models so we don't need a live Ollama
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "list_models",
                              lambda url: [{"name": "llama3.2:1b"},
                                           {"name": "phi3.5"}])
        # Typo of llama3.2 with one extra char
        assert _suggest_model_tag("llama3.21b", "http://x") in (
            "llama3.2:1b", "llama3.2", "llama3")
        # Typo of phi3.5
        assert _suggest_model_tag("phi3", "http://x") == "phi3.5"

    def test_helper_returns_none_when_no_candidates(self, monkeypatch):
        from org_llm.cli import _suggest_model_tag
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "list_models", lambda url: [])
        # Catalog still adds candidates, but for a totally bogus tag
        # returns None
        assert _suggest_model_tag("zzznotacatmodel999", "http://x") is None


class TestSkillFuzzyMatch:
    def test_unknown_skill_suggests_closest(self, populated_org):
        from pathlib import Path
        from org_llm.skills import Skill
        from org_llm.db    import get_session, make_engine
        import os
        engine = make_engine(Path(os.environ["ORG_LLM_DB"]))
        with get_session(engine) as s:
            s.add(Skill(name="echo_test", lang="python",
                         model_key="text_model",
                         source="print('{{input}}')",
                         file_path="/v/s.org", heading="Echo"))
            s.commit()
        # Typo of echo_test; positional "input" is empty
        r = runner.invoke(app, ["skill", "echo_tst", "--yes"])
        # Either uses fuzzy-match and runs, OR fails with closest names list
        assert "echo_test" in r.output


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


class TestPMFallback:
    """Package-manager fallback for FOSS tool installs."""

    def test_no_pm_returns_false(self, monkeypatch):
        """When no package manager is on PATH, install_via_pm returns False."""
        from org_llm.models import install_via_pm
        # Pretend nothing is installed
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda _x: None)
        assert install_via_pm("bat") is False

    def test_uses_apt_alternate_name(self, monkeypatch):
        """fd is `fd-find` on apt — verify the override map kicks in."""
        from org_llm.models import _PM_PACKAGE_NAMES
        assert _PM_PACKAGE_NAMES[("apt-get", "fd")] == "fd-find"
        assert _PM_PACKAGE_NAMES[("apt-get", "rg")] == "ripgrep"
        assert _PM_PACKAGE_NAMES[("apt-get", "delta")] == "git-delta"

    def test_pacman_priority_higher_than_apt(self):
        """guix > pacman > apt > dnf > brew > zypper — verifies expected
        order for fallback chain."""
        from org_llm.models import _PM_COMMANDS
        names = [pm for pm, _ in _PM_COMMANDS]
        assert names.index("pacman") < names.index("apt-get")
        assert names.index("guix")   < names.index("pacman")
        assert names.index("apt-get") < names.index("dnf")

    def test_pm_command_prefixes_use_sudo_where_appropriate(self):
        from org_llm.models import _PM_COMMANDS
        d = dict(_PM_COMMANDS)
        # sudo wrapper for system PMs that need root
        for pm in ("pacman", "apt-get", "dnf", "zypper"):
            assert "sudo" in d[pm], f"{pm} install command missing sudo"
        # User-space PMs don't need sudo
        for pm in ("guix", "brew"):
            assert "sudo" not in d[pm], f"{pm} should not require sudo"


class TestStallWatcher:
    """`_run_with_stall_watch` streams subprocess output and detects stalls."""

    def test_returns_exit_code_on_normal_completion(self, tmp_path):
        from org_llm.cli import _run_with_stall_watch
        # Quick subprocess that exits cleanly with stdout
        rc = _run_with_stall_watch(
            ["python3", "-c", "print('hello'); exit(0)"],
            stall_secs=10.0, label="quick test",
        )
        assert rc == 0

    def test_returns_nonzero_on_failure(self):
        from org_llm.cli import _run_with_stall_watch
        rc = _run_with_stall_watch(
            ["python3", "-c", "exit(7)"],
            stall_secs=10.0, label="failing test",
        )
        assert rc == 7

    def test_returns_127_on_missing_binary(self, capsys):
        from org_llm.cli import _run_with_stall_watch
        rc = _run_with_stall_watch(
            ["this-binary-truly-does-not-exist-12345"],
            stall_secs=2.0,
        )
        assert rc == 127


class TestPersonalizeShowClear:
    """`personalize --show` and `--clear` work without invoking LLM."""

    def test_show_with_no_knobs(self, populated_org):
        r = runner.invoke(app, ["personalize", "--show"])
        assert r.exit_code == 0
        assert ("No user knobs" in r.output
                or "Registered theme knobs" in r.output)

    def test_clear_when_empty_does_nothing(self, populated_org):
        r = runner.invoke(app, ["personalize", "--clear"])
        assert r.exit_code == 0
        assert "No user knobs" in r.output


class TestContextStaleAlias:
    """`context stale` is an alias for top-level `stale`."""

    def test_context_stale_subcommand_exists(self):
        # Smoke test: it's registered and shows help
        r = runner.invoke(app, ["context", "stale", "--help"])
        assert r.exit_code == 0
        assert "stale" in r.output.lower()


class TestSelfMod:
    """`org-llm self snapshot/snapshots/rollback/log` — read + revise + rollback."""

    def test_snapshot_creates_directory_and_tarball(self, tmp_path, monkeypatch):
        """create_snapshot bundles the running package source and DB."""
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_ORG_DIR", str(tmp_path / "org"))
        # Ensure DB exists so the snapshot picks it up
        from org_llm.db import init_db, make_engine
        init_db(make_engine(tmp_path / "test.db"))
        from org_llm import self_mod as _sm
        snap = _sm.create_snapshot(label="test-label")
        assert snap.path.exists()
        assert snap.tarball.exists()
        assert snap.label == "test-label"
        # Manifest captures the right metadata
        assert (snap.path / "manifest.json").exists()
        assert (snap.path / "rollback.sh").exists()
        # rollback.sh is executable
        import stat as _stat
        mode = (snap.path / "rollback.sh").stat().st_mode
        assert mode & _stat.S_IXUSR
        # Source files copied
        assert (snap.path / "org_llm" / "cli.py").exists()
        # DB copied
        assert (snap.path / "org-llm.db.snapshot").exists()

    def test_list_snapshots_returns_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_ORG_DIR", str(tmp_path / "org"))
        from org_llm.db       import init_db, make_engine
        from org_llm          import self_mod as _sm
        init_db(make_engine(tmp_path / "test.db"))
        import time as _t
        snap_a = _sm.create_snapshot(label="first")
        _t.sleep(1.1)   # ensure unique timestamp
        snap_b = _sm.create_snapshot(label="second")
        snaps = _sm.list_snapshots()
        assert len(snaps) == 2
        assert snaps[0].id == snap_b.id   # newest first
        assert snaps[1].id == snap_a.id

    def test_find_snapshot_by_label_and_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_ORG_DIR", str(tmp_path / "org"))
        from org_llm.db       import init_db, make_engine
        from org_llm          import self_mod as _sm
        init_db(make_engine(tmp_path / "test.db"))
        snap = _sm.create_snapshot(label="my-feature")
        # Find by label
        assert _sm.find_snapshot("my-feature").id == snap.id
        # Find by exact id
        assert _sm.find_snapshot(snap.id).id == snap.id
        # Find by prefix
        assert _sm.find_snapshot(snap.id[:8]).id == snap.id
        # Empty → newest
        assert _sm.find_snapshot("").id == snap.id
        # Non-match
        assert _sm.find_snapshot("zzz-nothing") is None

    def test_log_appends_to_org_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_ORG_DIR", str(tmp_path / "org"))
        monkeypatch.setenv("ORG_LLM_SELFMOD_LOG",
                            str(tmp_path / "selfmod.org"))
        from org_llm.db       import init_db, make_engine
        from org_llm          import self_mod as _sm
        init_db(make_engine(tmp_path / "test.db"))
        snap = _sm.create_snapshot(label="logtest")
        _sm.log_snapshot(snap)
        log = (tmp_path / "selfmod.org").read_text()
        assert "snapshot " + snap.id in log
        assert ":SELFMOD_KIND:   snapshot" in log
        # Rollback script captured as a tangle block
        assert ":tangle " in log

    def test_apply_plan_rejects_ambiguous_old(self, tmp_path):
        from org_llm import self_mod as _sm
        f = tmp_path / "x.py"
        f.write_text("x = 1\nx = 1\n")    # 'x = 1' appears twice
        ok, msg = _sm.apply_plan(f, {"ops": [
            {"type": "replace", "old": "x = 1", "new": "x = 2"}
        ]})
        assert not ok
        assert "matches >1" in msg

    def test_apply_plan_rejects_unfound_old(self, tmp_path):
        from org_llm import self_mod as _sm
        f = tmp_path / "y.py"
        f.write_text("def foo(): pass\n")
        ok, msg = _sm.apply_plan(f, {"ops": [
            {"type": "replace", "old": "bar", "new": "baz"}
        ]})
        assert not ok
        assert "not found" in msg

    def test_apply_plan_succeeds_with_unique_old(self, tmp_path):
        from org_llm import self_mod as _sm
        f = tmp_path / "z.py"
        f.write_text("def foo(): return 1\n")
        ok, msg = _sm.apply_plan(f, {"ops": [
            {"type": "replace", "old": "return 1", "new": "return 2"}
        ]})
        assert ok
        assert f.read_text() == "def foo(): return 2\n"


class TestSelfModRollback:
    """Round-trip: snapshot → mutate code → rollback → verify restored."""

    def test_rollback_restores_package_source(self, tmp_path, monkeypatch):
        """Mutate a copy of the package source, snapshot the original
        first, then verify rollback() restores it."""
        # Set up a fake package dir we can mutate without touching the
        # real org_llm package.
        fake_pkg = tmp_path / "fake_org_llm"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("# v1\n")
        (fake_pkg / "module.py").write_text("X = 'original'\n")

        from org_llm import self_mod as _sm
        # Patch package_dir() and db_path() to point at our test fixtures
        monkeypatch.setattr(_sm, "package_dir", lambda: fake_pkg)
        db_file = tmp_path / "test.db"
        from org_llm.db import init_db, make_engine
        init_db(make_engine(db_file))
        monkeypatch.setattr(_sm, "db_path", lambda: db_file)
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snaps"))

        # Take snapshot of original
        snap = _sm.create_snapshot(label="orig")
        assert (snap.path / "org_llm" / "module.py").read_text() == "X = 'original'\n"

        # Mutate the live source
        (fake_pkg / "module.py").write_text("X = 'broken'\n")
        assert (fake_pkg / "module.py").read_text() == "X = 'broken'\n"

        # Roll back
        summary = _sm.rollback(snap, also_db=True)
        assert summary["package"] is True
        # Verify restored
        assert (fake_pkg / "module.py").read_text() == "X = 'original'\n"
        # Pre-rollback backup captured the broken state
        assert summary["backup"]
        from pathlib import Path as _P
        backup_module = _P(summary["backup"]) / "org_llm" / "module.py"
        assert backup_module.read_text() == "X = 'broken'\n"

    def test_rollback_preserves_db_when_no_db_set(self, tmp_path, monkeypatch):
        """`also_db=False` only restores package, not the DB."""
        fake_pkg = tmp_path / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("v1")
        from org_llm import self_mod as _sm
        from org_llm.db import init_db, make_engine
        monkeypatch.setattr(_sm, "package_dir", lambda: fake_pkg)
        db_file = tmp_path / "test.db"
        init_db(make_engine(db_file))
        monkeypatch.setattr(_sm, "db_path", lambda: db_file)
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "s"))

        snap = _sm.create_snapshot()
        # Mutate the DB after snapshot
        from sqlalchemy import text
        engine = make_engine(db_file)
        with engine.connect() as c:
            c.execute(text("UPDATE config SET value='mutated' WHERE key='theme'"))
            c.commit()

        # Roll back package only
        summary = _sm.rollback(snap, also_db=False)
        assert summary["package"] is True
        assert summary["db"] is False
        # DB still has the mutation
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT value FROM config WHERE key='theme'"
            )).first()
        assert row is not None and row[0] == "mutated"


class TestSelfModRollbackScript:
    """rollback.sh is well-formed and points at correct paths."""

    def test_script_references_snapshot_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORG_LLM_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        from org_llm.db import init_db, make_engine
        from org_llm    import self_mod as _sm
        init_db(make_engine(tmp_path / "test.db"))
        snap = _sm.create_snapshot(label="rs-test")
        script = (snap.path / "rollback.sh").read_text()
        # Sanity: bash shebang + set -euo pipefail
        assert script.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in script
        # References the snapshot's own directory and the actual db path
        assert str(snap.path) in script
        assert str(tmp_path / "test.db") in script
        # Has the "Continue?" prompt — destructive ops should never be silent
        assert "Continue?" in script
        # Backs up current state before overwrite
        assert "BACKUP=" in script and "Pre-rollback state preserved" in script


class TestStallWatcherInteractive:
    """Stall detection: feed a stalling subprocess and verify the
    LLM-diagnosis path triggers."""

    def test_actual_stall_triggers_kill_path(self, monkeypatch):
        """Sleep longer than the stall window; auto-answer 'k' to kill."""
        # Stub the LLM advice so we don't make a real network call
        from org_llm import cli as _cli
        monkeypatch.setattr(_cli, "_llm_one_liner",
                              lambda *a, **kw: "Looks stuck. Kill it.")
        # Stub typer.prompt to auto-answer 'k'
        import typer as _t
        monkeypatch.setattr(_t, "prompt",
                              lambda *a, **kw: "k")

        from org_llm.cli import _run_with_stall_watch
        # Sleeps 8s with no output; stall threshold is 2s → kill triggers
        rc = _run_with_stall_watch(
            ["python3", "-c", "import time; time.sleep(8)"],
            stall_secs=2.0, label="stall test",
        )
        assert rc == 130    # killed → 128 + SIGINT-like exit code

    def test_wait_choice_resets_timer_and_completes(self, monkeypatch):
        """User answers 'w' to the stall prompt; subprocess completes."""
        from org_llm import cli as _cli
        monkeypatch.setattr(_cli, "_llm_one_liner",
                              lambda *a, **kw: "Probably fine.")
        import typer as _t
        monkeypatch.setattr(_t, "prompt", lambda *a, **kw: "w")
        from org_llm.cli import _run_with_stall_watch
        # 3s sleep, 1s threshold → first stall fires, user waits, completes
        rc = _run_with_stall_watch(
            ["python3", "-c", "import time; time.sleep(3); print('ok')"],
            stall_secs=1.0, label="wait test",
        )
        assert rc == 0


class TestRecoveryAdvice:
    """`_llm_recovery_advice` formats LLM output into bullet lines."""

    def test_returns_bullet_lines_from_llm(self, monkeypatch):
        from org_llm import cli as _cli
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system:
                              "Try this\nAlso try that\nMaybe this too")
        out = _cli._llm_recovery_advice("something failed",
                                          context="testing",
                                          max_bullets=3)
        # Returned as 3 bullets
        lines = out.splitlines()
        assert len(lines) == 3
        assert all(l.startswith("• ") for l in lines)
        assert "Try this" in out

    def test_returns_empty_on_chat_exception(self, monkeypatch):
        from org_llm import cli as _cli
        import org_llm.llm as _llm
        def _boom(*a, **kw):
            raise ConnectionError("ollama down")
        monkeypatch.setattr(_llm, "chat", _boom)
        # Don't crash — just return ""
        assert _cli._llm_recovery_advice("x") == ""

    def test_caps_bullet_count(self, monkeypatch):
        from org_llm import cli as _cli
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system:
                              "\n".join(f"recovery suggestion #{i}" for i in range(20)))
        out = _cli._llm_recovery_advice("x", max_bullets=2)
        assert len(out.splitlines()) == 2


class TestOllamaPullErrorDetection:
    """`_ollama_pull` reads stderr and surfaces specific recovery paths."""

    @pytest.fixture
    def fake_ollama(self, tmp_path, monkeypatch):
        """Create a fake `ollama` binary on a tmp PATH so the pre-flight
        existence check passes; return the path so subprocess can mock it."""
        fake = tmp_path / "ollama"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        import shutil as _sh
        monkeypatch.setattr(_sh, "which",
                              lambda x: str(fake) if x == "ollama" else None)
        return fake

    def test_disk_full_message(self, monkeypatch, capsys, fake_ollama):
        from org_llm import cli as _cli
        from collections import namedtuple
        Result = namedtuple("R", "returncode stdout stderr")
        import subprocess as _sub
        monkeypatch.setattr(_sub, "run",
                              lambda *a, **kw:
                              Result(1, "", "Error: no space left on device"))
        ok = _cli._ollama_pull("phi4")
        assert ok is False
        captured = capsys.readouterr()
        assert "ollama rm" in captured.out or "db --vacuum" in captured.out

    def test_network_failure_message(self, monkeypatch, capsys, fake_ollama):
        from org_llm import cli as _cli
        from collections import namedtuple
        Result = namedtuple("R", "returncode stdout stderr")
        import subprocess as _sub
        monkeypatch.setattr(_sub, "run",
                              lambda *a, **kw:
                              Result(1, "", "Error: connection refused"))
        ok = _cli._ollama_pull("phi4")
        assert ok is False
        captured = capsys.readouterr()
        assert "network" in captured.out.lower() or "cloud" in captured.out

    def test_404_manifest_message(self, monkeypatch, capsys, fake_ollama):
        from org_llm import cli as _cli
        from collections import namedtuple
        Result = namedtuple("R", "returncode stdout stderr")
        import subprocess as _sub
        monkeypatch.setattr(_sub, "run",
                              lambda *a, **kw:
                              Result(1, "", "pull model manifest: not found 404"))
        ok = _cli._ollama_pull("nonsense-model")
        assert ok is False
        captured = capsys.readouterr()
        assert "registry" in captured.out.lower() or "discover" in captured.out


class TestPMFallbackExec:
    """`install_via_pm` actually invokes a PM when one is on PATH."""

    def test_invokes_first_available_pm(self, monkeypatch):
        from org_llm import models as _m
        from collections import namedtuple
        Result = namedtuple("R", "returncode")
        # Pretend only pacman is on PATH
        import shutil as _sh
        def _which(x):
            return "/usr/bin/pacman" if x == "pacman" else (
                "/tmp/bat" if x == "bat" else None)
        monkeypatch.setattr(_sh, "which", _which)
        # Track which command was invoked
        seen: list[list[str]] = []
        def _fake_run(cmd, **kw):
            seen.append(list(cmd))
            return Result(0)
        import subprocess as _sub
        monkeypatch.setattr(_sub, "run", _fake_run)
        ok = _m.install_via_pm("bat")
        assert ok is True   # bat shows up on PATH after install
        assert any("pacman" in " ".join(c) for c in seen)
        # The right package name was passed (bat is fine, no override)
        assert any(c[-1] == "bat" for c in seen)

    def test_returns_false_when_no_pm_available(self, monkeypatch):
        from org_llm import models as _m
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda x: None)
        assert _m.install_via_pm("bat") is False

    def test_apt_uses_alternate_package_name(self, monkeypatch):
        """fd → fd-find on apt — verify the override actually fires."""
        from org_llm import models as _m
        from collections import namedtuple
        Result = namedtuple("R", "returncode")
        import shutil as _sh
        def _which(x):
            return "/usr/bin/apt-get" if x == "apt-get" else None
        monkeypatch.setattr(_sh, "which", _which)
        seen: list[list[str]] = []
        def _fake_run(cmd, **kw):
            seen.append(list(cmd))
            return Result(0)
        import subprocess as _sub
        monkeypatch.setattr(_sub, "run", _fake_run)
        # which('fd') stays None → the post-install assertion fails →
        # install_via_pm returns False, BUT the apt-get call should have
        # used "fd-find" as the package name.
        _m.install_via_pm("fd")
        assert any("fd-find" in c for c in seen)


class TestRichHelpPanels:
    """`--help` groups commands into named panels."""

    def test_top_level_help_shows_panels(self):
        r = runner.invoke(app, ["--help"])
        assert r.exit_code == 0
        for panel in ("Onboarding", "Indexing", "Querying",
                       "Models & Cloud", "Workspaces", "Maintenance"):
            assert panel in r.output, f"missing panel: {panel}"
# test_cli_extra.py:1 ends here
