"""Tests for the env-gated Agor-pilot health-check section in
``org-llm doctor``.

The check is implemented as `org_llm.cli.check_agor_pilot()` — a pure
function returning a list of `(kind, label, detail)` tuples. These
tests exercise that function directly (so we don't need a live Agor
daemon, real `pass` store, etc.) plus one end-to-end test that
verifies the env-gated wiring actually fires inside `doctor`.

Probes covered:
  1. daemon reachable / unreachable
  2. pass slug missing
  3. smoke script missing / not-executable / missing-strict-mode
  4. all-green path (everything mocked into a happy state)
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from org_llm.cli import check_agor_pilot


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _make_smoke(tmp_path: Path, *, executable: bool = True,
                strict: bool = True, name: str = "agor-smoke.sh") -> Path:
    """Create a fake agor-smoke.sh fixture in tmp_path."""
    p = tmp_path / name
    body = "#!/usr/bin/env bash\n"
    if strict:
        body += "set -euo pipefail\n"
    body += 'echo "smoke"\n'
    p.write_text(body)
    if executable:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return p


def _kinds(rows):
    """Extract the kind column ([{ok|warn|fail|info}, ...]) from check rows."""
    return [r[0] for r in rows]


def _row_for(rows, label_substring):
    """First row whose label contains the given substring."""
    for r in rows:
        if label_substring.lower() in r[1].lower():
            return r
    raise AssertionError(
        f"no row matching {label_substring!r} in {[r[1] for r in rows]}"
    )


# ──────────────────────────────────────────────────────────────────────
# 1. Daemon probes
# ──────────────────────────────────────────────────────────────────────
class TestDaemonProbe:
    def test_daemon_unreachable_fails(self, tmp_path, monkeypatch):
        """A bogus daemon URL on a closed port must yield a `fail` row."""
        smoke = _make_smoke(tmp_path)
        # Keep pass/agor probes from polluting the result by pointing
        # PATH at an empty dir (so neither binary is found).
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep /usr/bin so shebangs (e.g. `/usr/bin/env bash` in shim
        # scripts other tests place) still resolve, but make sure no
        # real `pass` / `agor` binary leaks in.
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")

        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",  # port 1 is privileged + closed
            pass_slug="org-llm/agor/admin-password",
            smoke_script=smoke,
            timeout_s=0.5,
        )
        kind, label, detail = _row_for(rows, "Agor daemon")
        assert kind == "fail", rows
        assert "unreachable" in detail

    def test_daemon_reachable_ok(self, tmp_path, monkeypatch):
        """A live HTTP server on localhost should produce an `ok` row."""
        import http.server
        import socketserver
        import threading

        class _OK(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a, **k):  # silence test output
                return

        srv = socketserver.TCPServer(("127.0.0.1", 0), _OK)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            smoke = _make_smoke(tmp_path)
            empty_bin = tmp_path / "empty-bin"
            empty_bin.mkdir()
            monkeypatch.setenv("PATH", str(empty_bin))
            rows = check_agor_pilot(
                daemon_url=f"http://127.0.0.1:{port}/health",
                pass_slug="org-llm/agor/admin-password",
                smoke_script=smoke,
                timeout_s=2.0,
            )
            kind, _, detail = _row_for(rows, "Agor daemon")
            assert kind == "ok", rows
            assert "200" in detail
        finally:
            srv.shutdown()
            srv.server_close()


# ──────────────────────────────────────────────────────────────────────
# 2. Pass-slug probe
# ──────────────────────────────────────────────────────────────────────
class TestPassProbe:
    def test_pass_binary_missing_fails(self, tmp_path, monkeypatch):
        """If the `pass` binary isn't on PATH, the admin-pass row must fail."""
        smoke = _make_smoke(tmp_path)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep /usr/bin so shebangs (e.g. `/usr/bin/env bash` in shim
        # scripts other tests place) still resolve, but make sure no
        # real `pass` / `agor` binary leaks in.
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")

        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",
            pass_slug="org-llm/agor/admin-password",
            smoke_script=smoke,
            timeout_s=0.5,
        )
        kind, _, detail = _row_for(rows, "Agor admin pass")
        assert kind == "fail", rows
        assert "pass" in detail.lower()

    def test_pass_slug_missing_fails(self, tmp_path, monkeypatch):
        """When `pass` exists but the slug doesn't resolve, fail with the slug name."""
        # Stub a `pass` shim that always exits 1 (slug missing).
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pass_shim = bin_dir / "pass"
        pass_shim.write_text(
            "#!/usr/bin/env bash\nexit 1\n"
        )
        pass_shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))

        smoke = _make_smoke(tmp_path)
        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",
            pass_slug="org-llm/agor/admin-password",
            smoke_script=smoke,
            timeout_s=0.5,
        )
        kind, _, detail = _row_for(rows, "Agor admin pass")
        assert kind == "fail", rows
        assert "org-llm/agor/admin-password" in detail


# ──────────────────────────────────────────────────────────────────────
# 3. Smoke-script probe
# ──────────────────────────────────────────────────────────────────────
class TestSmokeScriptProbe:
    def test_smoke_missing_fails(self, tmp_path, monkeypatch):
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep /usr/bin so shebangs (e.g. `/usr/bin/env bash` in shim
        # scripts other tests place) still resolve, but make sure no
        # real `pass` / `agor` binary leaks in.
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")
        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",
            pass_slug="org-llm/agor/admin-password",
            smoke_script=tmp_path / "does-not-exist.sh",
            timeout_s=0.5,
        )
        kind, _, detail = _row_for(rows, "Agor smoke script")
        assert kind == "fail", rows
        assert "missing" in detail.lower()

    def test_smoke_not_executable_warns(self, tmp_path, monkeypatch):
        smoke = _make_smoke(tmp_path, executable=False, strict=True)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep /usr/bin so shebangs (e.g. `/usr/bin/env bash` in shim
        # scripts other tests place) still resolve, but make sure no
        # real `pass` / `agor` binary leaks in.
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")
        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",
            pass_slug="org-llm/agor/admin-password",
            smoke_script=smoke,
            timeout_s=0.5,
        )
        kind, _, detail = _row_for(rows, "Agor smoke script")
        assert kind == "warn", rows
        assert "executable" in detail.lower()

    def test_smoke_missing_strict_warns(self, tmp_path, monkeypatch):
        smoke = _make_smoke(tmp_path, executable=True, strict=False)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep /usr/bin so shebangs (e.g. `/usr/bin/env bash` in shim
        # scripts other tests place) still resolve, but make sure no
        # real `pass` / `agor` binary leaks in.
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")
        rows = check_agor_pilot(
            daemon_url="http://127.0.0.1:1/health",
            pass_slug="org-llm/agor/admin-password",
            smoke_script=smoke,
            timeout_s=0.5,
        )
        kind, _, detail = _row_for(rows, "Agor smoke script")
        assert kind == "warn", rows
        assert "set -euo pipefail" in detail


# ──────────────────────────────────────────────────────────────────────
# 4. All-green path
# ──────────────────────────────────────────────────────────────────────
class TestAllGreen:
    def test_all_three_probes_ok(self, tmp_path, monkeypatch):
        """With a live daemon, a working `pass` shim, a working `agor`
        shim, and a healthy smoke script, every Agor row is `ok`."""
        import http.server
        import socketserver
        import threading

        class _OK(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a, **k):
                return

        srv = socketserver.TCPServer(("127.0.0.1", 0), _OK)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # `pass` shim → returns a fake password
        (bin_dir / "pass").write_text(
            "#!/usr/bin/env bash\necho 'fake-admin-password'\n"
        )
        (bin_dir / "pass").chmod(0o755)
        # `agor` shim → admin login always succeeds
        (bin_dir / "agor").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        (bin_dir / "agor").chmod(0o755)
        # bin_dir first so our shims win; /usr/bin so shebang `env`
        # can resolve `bash`.
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

        smoke = _make_smoke(tmp_path, executable=True, strict=True)

        try:
            rows = check_agor_pilot(
                daemon_url=f"http://127.0.0.1:{port}/health",
                pass_slug="org-llm/agor/admin-password",
                smoke_script=smoke,
                timeout_s=2.0,
            )
        finally:
            srv.shutdown()
            srv.server_close()

        kinds = _kinds(rows)
        assert all(k == "ok" for k in kinds), rows
        # Sanity: at least the four expected labels exist.
        labels = " | ".join(r[1] for r in rows)
        for needle in ("Agor daemon", "Agor admin pass",
                        "Agor auth login", "Agor smoke script"):
            assert needle in labels, labels


# ──────────────────────────────────────────────────────────────────────
# 5. Wiring: env-gate on, the doctor verb runs the section.
# ──────────────────────────────────────────────────────────────────────
class TestDoctorWiring:
    def test_env_off_no_section(self, cli_db, monkeypatch):
        """Without ORG_LLM_AGOR_HEALTHCHECK=1, doctor must NOT print the section."""
        from typer.testing import CliRunner
        from org_llm.cli import app
        monkeypatch.delenv("ORG_LLM_AGOR_HEALTHCHECK", raising=False)
        r = CliRunner().invoke(app, ["doctor"])
        # doctor exits non-zero on warnings; we only care about output here.
        assert "Agor pilot" not in r.output

    def test_env_on_section_present(self, cli_db, monkeypatch):
        """With ORG_LLM_AGOR_HEALTHCHECK=1, the section header appears."""
        from typer.testing import CliRunner
        from org_llm.cli import app
        monkeypatch.setenv("ORG_LLM_AGOR_HEALTHCHECK", "1")
        r = CliRunner().invoke(app, ["doctor"])
        assert "Agor pilot" in r.output, r.output
