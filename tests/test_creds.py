# [[file:../../../org/20260425230731-org_llm.org::*tests/test_creds.py][test_creds.py:1]]
"""Tests for org_llm.creds — the `pass` credential-store wrapper.

Strategy:
  - For unit tests of the API surface, we point PASSWORD_STORE_DIR at a tmp_path
    and either monkeypatch _run_pass or, when available, exercise a real `pass`
    binary against a throwaway GPG key.
  - The real-binary tests are skipped automatically when `pass` and `gpg` are
    not on PATH (e.g. on CI containers without them).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from org_llm import creds


# ── Slug helpers ──────────────────────────────────────────────────────────────

class TestSlugs:
    def test_cloud_slug_format(self):
        assert creds.cloud_slug("runpod") == "org-llm/cloud/runpod/api-key"

    def test_cloud_slug_unknown_provider_still_canonical(self):
        # No validation — just produces the slug for whatever caller passes.
        assert creds.cloud_slug("madeupcloud") == "org-llm/cloud/madeupcloud/api-key"

    def test_anthropic_slug(self):
        assert creds.anthropic_slug() == "org-llm/anthropic/api-key"


# ── Status detection ─────────────────────────────────────────────────────────

class TestStatus:
    def test_is_installed_returns_bool(self):
        assert isinstance(creds.is_installed(), bool)

    def test_is_initialized_false_when_store_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(creds, "PASS_STORE", tmp_path / "no-store")
        assert creds.is_initialized() is False

    def test_is_initialized_true_when_gpg_id_exists(self, tmp_path, monkeypatch):
        store = tmp_path / "ps"; store.mkdir()
        (store / ".gpg-id").write_text("KEYID\n")
        monkeypatch.setattr(creds, "PASS_STORE", store)
        assert creds.is_initialized() is True

    def test_is_available_requires_both(self, tmp_path, monkeypatch):
        # Even if initialized, returns False when binary is missing
        store = tmp_path / "ps"; store.mkdir()
        (store / ".gpg-id").write_text("KEYID\n")
        monkeypatch.setattr(creds, "PASS_STORE", store)
        monkeypatch.setattr(creds, "is_installed", lambda: False)
        assert creds.is_available() is False
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        assert creds.is_available() is True


# ── install_help() ───────────────────────────────────────────────────────────

class TestInstallHelp:
    def test_help_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(creds, "is_installed", lambda: False)
        out = creds.install_help()
        assert "not installed" in out
        assert "guix" in out.lower() or "apt" in out.lower()

    def test_help_when_installed_but_uninitialized(self, monkeypatch):
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        monkeypatch.setattr(creds, "is_initialized", lambda: False)
        out = creds.install_help()
        assert "init" in out
        assert "gpg" in out.lower()

    def test_help_when_ready(self, monkeypatch):
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        monkeypatch.setattr(creds, "is_initialized", lambda: True)
        out = creds.install_help()
        assert "ready" in out.lower()


# ── read_secret / write_secret behaviour with mocked subprocess ──────────────

class TestSecretIO:
    def test_read_secret_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: False)
        assert creds.read_secret("any/slug") is None

    def test_write_secret_returns_false_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: False)
        assert creds.write_secret("any/slug", "value") is False

    def test_delete_secret_returns_false_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: False)
        assert creds.delete_secret("any/slug") is False

    def test_read_secret_returns_first_line(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: True)
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        def fake_run(args, stdin=None, timeout=30):
            return SimpleNamespace(returncode=0, stdout="sk-ant-secret\nextra meta\n", stderr="")
        monkeypatch.setattr(creds, "_run_pass", fake_run)
        assert creds.read_secret("foo") == "sk-ant-secret"

    def test_read_secret_returns_none_on_nonzero(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: True)
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        monkeypatch.setattr(creds, "_run_pass",
                            lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="missing"))
        assert creds.read_secret("missing/slug") is None

    def test_read_secret_handles_exception(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: True)
        def boom(*a, **kw):
            raise OSError("gpg-agent died")
        monkeypatch.setattr(creds, "_run_pass", boom)
        assert creds.read_secret("any/slug") is None

    def test_write_secret_passes_value_on_stdin(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(creds, "is_available", lambda: True)
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        def fake_run(args, stdin=None, timeout=30):
            captured["args"] = args
            captured["stdin"] = stdin
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(creds, "_run_pass", fake_run)

        ok = creds.write_secret("org-llm/cloud/runpod/api-key", "rp-1234")
        assert ok is True
        assert "insert" in captured["args"]
        assert "--multiline" in captured["args"]
        assert "--force" in captured["args"]
        assert captured["args"][-1] == "org-llm/cloud/runpod/api-key"
        assert captured["stdin"].startswith("rp-1234")

    def test_write_secret_returns_false_on_failure(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: True)
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        monkeypatch.setattr(creds, "_run_pass",
                            lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="oops"))
        assert creds.write_secret("slug", "value") is False

    def test_delete_secret_invokes_rm_force(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(creds, "is_available", lambda: True)
        monkeypatch.setattr(creds, "is_installed", lambda: True)
        def fake_run(args, stdin=None, timeout=30):
            captured["args"] = args
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(creds, "_run_pass", fake_run)
        assert creds.delete_secret("slug") is True
        assert "rm" in captured["args"]
        assert "--force" in captured["args"]


# ── list_secrets walks the store filesystem ─────────────────────────────────

class TestListSecrets:
    def test_returns_empty_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(creds, "is_available", lambda: False)
        assert creds.list_secrets("org-llm") == []

    def test_returns_empty_when_prefix_missing(self, tmp_path, monkeypatch):
        store = tmp_path / "ps"; store.mkdir()
        (store / ".gpg-id").write_text("KEYID\n")
        monkeypatch.setattr(creds, "PASS_STORE", store)
        monkeypatch.setattr(creds, "is_available", lambda: True)
        assert creds.list_secrets("does-not-exist") == []

    def test_lists_gpg_files_recursively(self, tmp_path, monkeypatch):
        store = tmp_path / "ps"; store.mkdir()
        (store / ".gpg-id").write_text("KEYID\n")
        # Simulate a real layout
        (store / "org-llm" / "cloud" / "runpod").mkdir(parents=True)
        (store / "org-llm" / "cloud" / "runpod" / "api-key.gpg").write_bytes(b"fake")
        (store / "org-llm" / "cloud" / "vast").mkdir(parents=True)
        (store / "org-llm" / "cloud" / "vast" / "api-key.gpg").write_bytes(b"fake")
        (store / "org-llm" / "anthropic").mkdir(parents=True)
        (store / "org-llm" / "anthropic" / "api-key.gpg").write_bytes(b"fake")
        # Unrelated entry should NOT appear
        (store / "other").mkdir()
        (store / "other" / "thing.gpg").write_bytes(b"fake")

        monkeypatch.setattr(creds, "PASS_STORE", store)
        monkeypatch.setattr(creds, "is_available", lambda: True)

        slugs = creds.list_secrets("org-llm")
        assert "org-llm/cloud/runpod/api-key" in slugs
        assert "org-llm/cloud/vast/api-key" in slugs
        assert "org-llm/anthropic/api-key" in slugs
        assert "other/thing" not in slugs
        # Sorted alphabetically
        assert slugs == sorted(slugs)


# ── status() snapshot ─────────────────────────────────────────────────────────

class TestStatusSnapshot:
    def test_status_when_uninstalled(self, monkeypatch):
        monkeypatch.setattr(creds, "is_installed", lambda: False)
        monkeypatch.setattr(creds, "is_initialized", lambda: False)
        s = creds.status()
        assert s.installed is False
        assert s.initialized is False
        assert s.gpg_id == ""
        assert s.secrets == []

    def test_status_reports_gpg_id_when_initialized(self, tmp_path, monkeypatch):
        store = tmp_path / "ps"; store.mkdir()
        (store / ".gpg-id").write_text("LONGKEYID42\n")
        monkeypatch.setattr(creds, "PASS_STORE", store)
        monkeypatch.setattr(creds, "is_installed",   lambda: True)
        monkeypatch.setattr(creds, "is_initialized", lambda: True)
        monkeypatch.setattr(creds, "is_available",   lambda: True)
        monkeypatch.setattr(creds, "list_secrets",   lambda prefix: [])
        s = creds.status()
        assert s.gpg_id == "LONGKEYID42"
        assert s.installed and s.initialized


# ── PASSWORD_STORE_DIR override ───────────────────────────────────────────────

class TestStoreDirOverride:
    def test_default_is_home_password_store(self, monkeypatch):
        # Reload the module under a clean env to recompute PASS_STORE
        monkeypatch.delenv("PASSWORD_STORE_DIR", raising=False)
        import importlib
        import org_llm.creds as fresh
        fresh = importlib.reload(fresh)
        assert fresh.PASS_STORE == Path("~/.password-store").expanduser()

    def test_env_override_respected(self, tmp_path, monkeypatch):
        custom = tmp_path / "secrets"
        monkeypatch.setenv("PASSWORD_STORE_DIR", str(custom))
        import importlib
        import org_llm.creds as fresh
        fresh = importlib.reload(fresh)
        assert fresh.PASS_STORE == custom


# ── End-to-end against real pass+gpg (skipped if not available) ──────────────

PASS_OK = shutil.which("pass") is not None and shutil.which("gpg") is not None


@pytest.mark.skipif(not PASS_OK, reason="pass and gpg required for live integration test")
def test_live_pass_roundtrip(tmp_path, monkeypatch):
    """Initialize a throwaway store with a transient GPG key, then read/write/list/delete.

    Only runs on systems where both `pass` and `gpg` are present. The GPG key
    lives in a fully isolated GNUPGHOME, so no risk of polluting the user's keyring.
    """
    gnupghome = tmp_path / "gnupg"
    gnupghome.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(gnupghome))

    # Generate a passwordless key non-interactively
    batch = (
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 2048\n"
        "Name-Real: org-llm test\n"
        "Name-Email: test@org-llm.invalid\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    r = subprocess.run(
        ["gpg", "--batch", "--gen-key"], input=batch, text=True,
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        pytest.skip(f"gpg key gen failed: {r.stderr[:200]}")
    # Find the key id
    r2 = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True, timeout=10,
    )
    key_id = next(
        (line.split(":")[4] for line in r2.stdout.splitlines() if line.startswith("pub")),
        None,
    )
    if not key_id:
        pytest.skip("could not locate generated gpg key")

    # Point pass at a fresh store dir
    store = tmp_path / "ps"
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(store))
    import importlib
    import org_llm.creds as fresh
    fresh = importlib.reload(fresh)

    init = subprocess.run(["pass", "init", key_id], capture_output=True, text=True, timeout=30)
    assert init.returncode == 0, init.stderr

    # Roundtrip
    slug = fresh.cloud_slug("runpod")
    assert fresh.write_secret(slug, "rp-test-12345") is True
    assert fresh.read_secret(slug) == "rp-test-12345"
    # Update overwrites
    assert fresh.write_secret(slug, "rp-rotated-67890") is True
    assert fresh.read_secret(slug) == "rp-rotated-67890"
    # List finds it
    assert slug in fresh.list_secrets("org-llm")
    # Delete
    assert fresh.delete_secret(slug) is True
    assert fresh.read_secret(slug) is None
# test_creds.py:1 ends here
