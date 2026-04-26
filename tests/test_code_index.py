# [[file:../../../org/20260425230731-org_llm.org::*tests/test_code_index.py][test_code_index.py:1]]
"""Tests for org_llm.code_index — code-aware file walking and indexing."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.cli import app
from org_llm.code_index import (
    CODE_EXTENSIONS,
    SKIP_DIRS,
    _read_truncated,
    _walk,
    index_code_dir,
)
from org_llm.db import File, Node, get_session, make_engine

runner = CliRunner()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "code.db"))
    runner.invoke(app, ["init"])
    return tmp_path / "code.db"


@pytest.fixture
def fake_repo(tmp_path):
    """Build a small repo tree with mixed file types + skip directories."""
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("def hello():\n    return 'world'\n")
    (repo / "src" / "config.toml").write_text("[server]\nport = 8000\n")
    (repo / "README.md").write_text("# myrepo\nA test repo.\n")
    (repo / "run.sh").write_text("#!/bin/sh\necho run\n")
    # Stuff we should SKIP
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "node_modules" / "lib").mkdir(parents=True)
    (repo / "node_modules" / "lib" / "huge.js").write_text("var x = 1;\n" * 1000)
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "site.py").write_text("# venv internals\n")
    # Non-code file with code-ish extension should still skip noisy dirs
    (repo / "target").mkdir()
    (repo / "target" / "compiled.rs").write_text("// build artifact\n")
    # Binary masquerading as .py — should still be readable (errors=replace)
    (repo / "src" / "binary.py").write_bytes(b"\xff\xfe\x00not really utf8")
    return repo


# ── _walk filters and includes correctly ─────────────────────────────────────

class TestWalk:
    def test_includes_known_extensions(self, fake_repo):
        files = {p.name for p in _walk(fake_repo)}
        assert "main.py" in files
        assert "config.toml" in files
        assert "README.md" in files
        assert "run.sh" in files

    def test_skips_noise_dirs(self, fake_repo):
        files = list(_walk(fake_repo))
        for skip in SKIP_DIRS:
            # Match the dir as a path component, not a filename substring.
            # Without this, /tmp/pytest-of-daniel/... would false-match "tmp".
            needle = f"/{skip}/"
            assert not any(needle in str(p) for p in files), \
                f"_walk leaked into {skip}/"

    def test_skips_hidden_dirs(self, fake_repo):
        files = list(_walk(fake_repo))
        # .git is in SKIP_DIRS, but the leading-dot rule should also catch
        # any other dot-prefixed dirs (e.g. .pytest_cache)
        (fake_repo / ".secret").mkdir()
        (fake_repo / ".secret" / "x.py").write_text("# secret\n")
        files2 = list(_walk(fake_repo))
        assert not any(".secret" in str(p) for p in files2)

    def test_skips_unknown_extensions(self, tmp_path):
        (tmp_path / "x.exe").write_text("...")
        (tmp_path / "y.bin").write_text("...")
        files = list(_walk(tmp_path))
        assert files == []


# ── _read_truncated never raises ────────────────────────────────────────────

class TestReadTruncated:
    def test_truncates_huge_files(self, tmp_path):
        path = tmp_path / "big.py"
        path.write_text("x" * 100_000)
        out = _read_truncated(path)
        # Truncated body + trailer
        assert "truncated" in out
        assert len(out) < 100_000

    def test_handles_binary_gracefully(self, tmp_path):
        path = tmp_path / "bin.py"
        path.write_bytes(b"\xff\xfe\x00\x01")
        # Should NOT raise
        out = _read_truncated(path)
        assert isinstance(out, str)

    def test_returns_empty_when_unreadable(self, tmp_path):
        # File doesn't exist
        out = _read_truncated(tmp_path / "ghost.py")
        assert out == ""


# ── index_code_dir against a session ────────────────────────────────────────

class TestIndexCodeDir:
    def test_indexes_visible_files_only(self, fake_repo, session):
        files, nodes = index_code_dir(fake_repo, session)
        # Visible: main.py, config.toml, README.md, run.sh, binary.py = 5
        assert files >= 4   # binary.py might or might not stick depending on body strip
        # SKIP dirs (.git, node_modules, .venv, target) must NOT be indexed
        paths = [f.path for f in session.query(File).all()]
        for skip in (".git", "node_modules", ".venv", "target"):
            assert not any(f"/{skip}/" in p for p in paths), \
                f"index reached into {skip}/"

    def test_each_indexed_file_gets_one_node(self, fake_repo, session):
        index_code_dir(fake_repo, session)
        for f in session.query(File).all():
            count = session.query(Node).filter_by(file_id=f.id).count()
            assert count == 1, f"{f.path}: expected 1 node, got {count}"

    def test_node_has_code_tag(self, fake_repo, session):
        index_code_dir(fake_repo, session)
        py_nodes = [n for n in session.query(Node).all()
                    if "main.py" in (n.title or "")]
        assert py_nodes
        # Every code node should be tagged
        for n in py_nodes:
            assert "code" in (n.tags or "")
            assert "code:python" in (n.tags or "")

    def test_skip_dirs_completely_invisible(self, fake_repo, session):
        index_code_dir(fake_repo, session)
        paths = [f.path for f in session.query(File).all()]
        # The huge.js file under node_modules should never appear
        assert not any("node_modules" in p for p in paths)
        # .venv internals likewise
        assert not any(".venv" in p for p in paths)
        # build artifacts (target/) likewise
        assert not any("/target/" in p for p in paths)

    def test_idempotent_unchanged_files(self, fake_repo, session):
        files1, _ = index_code_dir(fake_repo, session)
        # Re-running with no file changes returns 0 (mtime check skips)
        files2, _ = index_code_dir(fake_repo, session)
        assert files2 == 0

    def test_handles_nonexistent_root(self, tmp_path, session):
        # Should silently return (0, 0), not raise
        f, n = index_code_dir(tmp_path / "no-such-dir", session)
        assert (f, n) == (0, 0)


# ── CLI integration ────────────────────────────────────────────────────────

class TestCodeIndexCLI:
    def test_help_lists_command(self, cli_db):
        r = runner.invoke(app, ["--help"])
        assert "code-index" in r.output

    def test_dry_run_against_tmp_repo(self, cli_db, fake_repo):
        # --no-embed avoids needing Ollama up
        r = runner.invoke(app, ["code-index", str(fake_repo), "--no-embed"])
        assert r.exit_code == 0, r.output
        assert "Indexed" in r.output

    def test_missing_dir_autoheals_or_errors(self, cli_db, tmp_path):
        # User declines the auto-heal offer → exit 1.
        r = runner.invoke(app, ["code-index", str(tmp_path / "ghost"),
                                "--no-embed"], input="n\n")
        assert r.exit_code == 1
        # Rich may wrap output, so collapse whitespace before searching.
        flat = " ".join(r.output.lower().split())
        assert "does not exist" in flat
        # Either offered candidates ("scanning filesystem") or none-found path.
        assert ("scanning filesystem" in flat
                or "none of the requested paths exist" in flat)

    def test_force_clears_existing(self, cli_db, fake_repo):
        runner.invoke(app, ["code-index", str(fake_repo), "--no-embed"])
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            count_before = s.query(File).count()
        assert count_before > 0
        # Run again with --force; should re-index everything
        r = runner.invoke(app, ["code-index", str(fake_repo),
                                "--force", "--no-embed"])
        assert r.exit_code == 0
        assert "Cleared" in r.output

    def test_uses_code_dirs_default(self, cli_db, fake_repo, monkeypatch):
        # Set the config to point at fake_repo, then run with no args
        from org_llm.db import Config
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            row = s.get(Config, "code_dirs")
            row.value = str(fake_repo)
            s.commit()
        r = runner.invoke(app, ["code-index", "--no-embed"])
        assert r.exit_code == 0
        assert "Indexed" in r.output


class TestSuggestCodeAsk:
    """Context-aware Try-it line: pulls a real file from the index, varies
    across runs, falls back gracefully when nothing is indexed yet."""

    def test_references_real_file_after_index(self, cli_db, fake_repo):
        # Run code-index against fake_repo so the DB has real samples.
        r = runner.invoke(app, ["code-index", str(fake_repo), "--no-embed"])
        assert r.exit_code == 0, r.output
        flat = " ".join(r.output.split())
        assert "Try it:" in flat
        # Should reference one of the real things we just indexed,
        # NOT the old hardcoded `cli.py wire MCP` string.
        assert "cli.py wire MCP" not in r.output
        assert any(name in flat for name in (
            "main.py", "README.md", "run.sh", "config.toml",
            "binary.py", "myrepo",
            # Or the parent dir of any indexed file
            str(fake_repo), "src",
        ))

    def test_varies_across_runs(self, cli_db, fake_repo):
        from org_llm.cli import _suggest_code_ask
        # Index once so samples exist, then call the suggester many times.
        runner.invoke(app, ["code-index", str(fake_repo), "--no-embed"])
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            seen = {_suggest_code_ask(s, [fake_repo]) for _ in range(40)}
        # With multiple files × multiple templates × cloud-flag toggle,
        # 40 draws should produce more than one distinct suggestion.
        assert len(seen) > 1, f"Suggester is deterministic — only saw: {seen}"

    def test_fallback_when_no_samples(self, cli_db, tmp_path):
        # Empty DB, fake root path — should still produce a usable Try-it line.
        from org_llm.cli import _suggest_code_ask
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            out = _suggest_code_ask(s, [tmp_path / "nowhere"])
        assert "Try it:" in out
        assert "org-llm ask" in out


# ── _parse_tag_hints regression ─────────────────────────────────────────────

class TestTagHints:
    def test_parses_my_X_tag(self):
        from org_llm.cli import _parse_tag_hints
        hints = _parse_tag_hints("what's the throughline between my politics tag and my tech tag?")
        assert "politics" in hints
        assert "tech" in hints

    def test_parses_tagged_X(self):
        from org_llm.cli import _parse_tag_hints
        hints = _parse_tag_hints("show me anything tagged work")
        assert "work" in hints

    def test_parses_org_colon_syntax(self):
        from org_llm.cli import _parse_tag_hints
        hints = _parse_tag_hints("notes with :queer: and :trek:")
        assert "queer" in hints
        assert "trek" in hints

    def test_skips_stopwords(self):
        from org_llm.cli import _parse_tag_hints
        hints = _parse_tag_hints("any tag, all tags, the tag")
        assert "tag" not in hints
        assert "tags" not in hints

    def test_no_tags_returns_empty(self):
        from org_llm.cli import _parse_tag_hints
        assert _parse_tag_hints("what is org-mode about") == []


class TestDidYouMean:
    def test_finds_close(self):
        from org_llm.cli import _did_you_mean
        out = _did_you_mean("politics", {"agenda", "queer", "trek", "policy"})
        assert "policy" in out

    def test_finds_nothing_far(self):
        from org_llm.cli import _did_you_mean
        out = _did_you_mean("xyzzy", {"agenda", "queer"})
        assert out == []
# test_code_index.py:1 ends here
