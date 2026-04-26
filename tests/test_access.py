# [[file:../../../org/20260425230731-org_llm.org::*tests/test_access.py][test_access.py:1]]
"""Tests for org_llm.access — permission-gated file + browser access for MCP."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm import access
from org_llm.cli import app

runner = CliRunner()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "access.db"))
    runner.invoke(app, ["init"])
    return tmp_path / "access.db"


# ── grant / revoke / allowlist ─────────────────────────────────────────────

class TestGrantRevoke:
    def test_empty_by_default(self, cli_db):
        assert access.allowlist() == []

    def test_grant_adds_path(self, cli_db, tmp_path):
        target = tmp_path / "vault"
        target.mkdir()
        assert access.grant(str(target))
        paths = access.allowlist()
        assert any(str(p) == str(target.resolve()) for p in paths)

    def test_grant_idempotent(self, cli_db, tmp_path):
        target = tmp_path / "x"
        target.mkdir()
        access.grant(str(target))
        access.grant(str(target))
        assert sum(1 for p in access.allowlist() if str(p) == str(target.resolve())) == 1

    def test_revoke_removes(self, cli_db, tmp_path):
        target = tmp_path / "y"
        target.mkdir()
        access.grant(str(target))
        access.revoke(str(target))
        assert not any(str(p) == str(target.resolve()) for p in access.allowlist())


# ── is_allowed gating ─────────────────────────────────────────────────────

class TestIsAllowed:
    def test_no_grants_blocks_everything(self, cli_db, tmp_path):
        f = tmp_path / "any.txt"
        f.write_text("data")
        ok, _ = access.is_allowed(str(f))
        assert ok is False

    def test_granted_dir_allows_descendants(self, cli_db, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        (root / "sub").mkdir()
        (root / "sub" / "file.org").write_text("hi")
        access.grant(str(root))
        ok, _ = access.is_allowed(str(root / "sub" / "file.org"))
        assert ok

    def test_outside_grant_blocked(self, cli_db, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        access.grant(str(root))
        sibling = tmp_path / "elsewhere.txt"
        sibling.write_text("nope")
        ok, _ = access.is_allowed(str(sibling))
        assert ok is False


# ── read_file behavior ────────────────────────────────────────────────────

class TestReadFile:
    def test_denial_message_explains(self, cli_db, tmp_path):
        f = tmp_path / "secret.txt"
        f.write_text("private")
        result = access.read_file(str(f))
        assert result.ok is False
        assert "Access denied" in result.error
        assert "org-llm grant" in result.error

    def test_allowed_returns_content(self, cli_db, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        f = root / "note.org"
        f.write_text("the actual content")
        access.grant(str(root))
        result = access.read_file(str(f))
        assert result.ok
        assert "actual content" in result.content

    def test_truncates_huge_files(self, cli_db, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        big = root / "big.txt"
        big.write_text("x" * 200_000)
        access.grant(str(root))
        result = access.read_file(str(big))
        assert result.ok
        assert result.truncated
        assert "truncated" in result.content

    def test_directory_not_a_file(self, cli_db, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        access.grant(str(root))
        result = access.read_file(str(root))
        assert result.ok is False
        assert "directory" in result.error


# ── Sensitive deny-list: ALWAYS refuses, even with grants ────────────────

class TestSensitiveDenylist:
    def test_ssh_dir_blocked_under_home_root(self, cli_db, tmp_path, monkeypatch):
        # Simulate ~/.ssh under our trusted root
        home = tmp_path / "fakehome"
        home.mkdir()
        ssh = home / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("PRIVATE")
        access.add_auto_root(str(home))
        result = access.request_self_grant(str(ssh / "id_rsa"), reason="probing")
        assert result.granted is False
        assert result.sensitive is True

    def test_password_store_blocked(self, cli_db, tmp_path):
        home = tmp_path / "fakehome"
        home.mkdir()
        pstore = home / ".password-store"
        pstore.mkdir()
        (pstore / "secret.gpg").write_bytes(b"\x00\x01")
        access.add_auto_root(str(home))
        result = access.request_self_grant(str(pstore / "secret.gpg"))
        assert result.granted is False
        assert result.sensitive is True

    def test_aws_credentials_blocked(self, cli_db, tmp_path):
        home = tmp_path / "fakehome"; home.mkdir()
        aws = home / ".aws"; aws.mkdir()
        (aws / "credentials").write_text("[default]\nakey=...")
        access.add_auto_root(str(home))
        result = access.request_self_grant(str(aws / "credentials"))
        assert result.granted is False
        assert result.sensitive is True


# ── Auto-grant: LLM self-extends within trusted roots ────────────────────

class TestAutoGrant:
    def test_no_roots_refuses(self, cli_db, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        result = access.request_self_grant(str(f))
        assert result.granted is False
        assert "auto-grant root" in result.message.lower() or "grant-root" in result.message

    def test_grants_under_trusted_root(self, cli_db, tmp_path):
        root = tmp_path / "repos"
        root.mkdir()
        (root / "proj").mkdir()
        target = root / "proj" / "main.py"
        target.write_text("print('hi')")
        access.add_auto_root(str(root))
        result = access.request_self_grant(str(target), reason="code analysis")
        assert result.granted
        # Path is now in the regular allow-list
        ok, _ = access.is_allowed(str(target))
        assert ok

    def test_outside_root_refused(self, cli_db, tmp_path):
        trusted = tmp_path / "trusted"
        trusted.mkdir()
        access.add_auto_root(str(trusted))
        outside = tmp_path / "outside.txt"
        outside.write_text("nope")
        result = access.request_self_grant(str(outside))
        assert result.granted is False
        assert "not under any auto-grant root" in result.message

    def test_roots_listed_correctly(self, cli_db, tmp_path):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        access.add_auto_root(str(a))
        access.add_auto_root(str(b))
        roots = access.auto_grant_roots()
        assert any(str(r) == str(a.resolve()) for r in roots)
        assert any(str(r) == str(b.resolve()) for r in roots)

    def test_remove_auto_root(self, cli_db, tmp_path):
        a = tmp_path / "a"; a.mkdir()
        access.add_auto_root(str(a))
        access.remove_auto_root(str(a))
        assert not any(str(r) == str(a.resolve()) for r in access.auto_grant_roots())


# ── Browser access toggle ────────────────────────────────────────────────

class TestBrowserAccess:
    def test_disabled_by_default(self, cli_db):
        assert access.browser_enabled() is False

    def test_enable_disable_round_trip(self, cli_db):
        access.set_browser_enabled(True)
        assert access.browser_enabled()
        access.set_browser_enabled(False)
        assert access.browser_enabled() is False

    def test_open_url_refused_when_disabled(self, cli_db):
        ok, msg = access.open_url("https://example.com")
        assert ok is False
        assert "disabled" in msg.lower()

    def test_open_url_refuses_javascript(self, cli_db):
        access.set_browser_enabled(True)
        ok, msg = access.open_url("javascript:alert(1)")
        assert ok is False
        assert "non-http" in msg.lower() or "refus" in msg.lower()


# ── CLI commands ──────────────────────────────────────────────────────────

class TestGrantCLI:
    def test_grant_cli(self, cli_db, tmp_path):
        target = tmp_path / "v"; target.mkdir()
        r = runner.invoke(app, ["grant", str(target)])
        assert r.exit_code == 0
        assert any(str(p) == str(target.resolve()) for p in access.allowlist())

    def test_revoke_cli(self, cli_db, tmp_path):
        target = tmp_path / "v"; target.mkdir()
        runner.invoke(app, ["grant", str(target)])
        r = runner.invoke(app, ["revoke", str(target)])
        assert r.exit_code == 0
        assert not any(str(p) == str(target.resolve()) for p in access.allowlist())

    def test_grants_command_lists(self, cli_db, tmp_path):
        target = tmp_path / "v"; target.mkdir()
        runner.invoke(app, ["grant", str(target)])
        r = runner.invoke(app, ["grants"])
        assert r.exit_code == 0
        # Path may be wrapped — collapse whitespace before searching
        flat = "".join(r.output.split())
        assert str(target.resolve()).replace("/", "").replace(" ", "") \
               in flat.replace("/", "").replace(" ", "")

    def test_grant_root_cli(self, cli_db, tmp_path):
        root = tmp_path / "myrepos"; root.mkdir()
        r = runner.invoke(app, ["grant-root", str(root)])
        assert r.exit_code == 0
        assert any(str(p) == str(root.resolve()) for p in access.auto_grant_roots())

    def test_grant_root_rejects_file(self, cli_db, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("x")
        r = runner.invoke(app, ["grant-root", str(f)])
        assert r.exit_code == 1
        assert "directory" in r.output.lower()

    def test_grant_browser_cli(self, cli_db):
        r = runner.invoke(app, ["grant-browser"])
        assert r.exit_code == 0
        assert access.browser_enabled()
        runner.invoke(app, ["revoke-browser"])
        assert access.browser_enabled() is False
# test_access.py:1 ends here
