"""Tests for scripts/agor-mcp-refresh.sh — token refresh primitive for
long-lived Agor sessions.

Approach: pytest + subprocess driving the bash script directly. We spin
up a tiny stdlib HTTP server to impersonate the Agor daemon's
GET /sessions/:id endpoint, mint our own JWT-shaped tokens with
controlled `exp` claims (HS256 signature is irrelevant — the script
base64url-decodes the payload only), and assert on:

  * fresh-token short-circuit (no rewrite)
  * stale-token rewrite (atomic, preserves siblings)
  * malformed config → exit 13 (CONFIG_BAD)
  * missing session → exit 12 (SESSION_GONE)
  * concurrent invocations don't corrupt the file
  * --check-only emits EXPIRES_IN <sec> + does not rewrite

The script's auto re-login path is NOT exercised here (would require
shimming `agor` and `pass`). Covered indirectly: a valid bootstrap
token file short-circuits the re-login branch.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agor-mcp-refresh.sh"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_jwt(*, exp: int, sub: str = "sess-abc") -> str:
    """Mint a JWT-shaped token. Signature is fake — the script decodes
    only the payload."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {"sub": sub, "uid": "u1", "aud": "agor:mcp:internal", "iss": "agor",
             "iat": int(time.time()), "exp": exp, "jti": "jti-x"}
        ).encode()
    )
    sig = _b64url(b"sig")
    return f"{header}.{payload}.{sig}"


def write_config(path: Path, token: str) -> None:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "agor": {
                        "type": "http",
                        "url": "http://localhost:9999/mcp",
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                },
                # Sibling fields we want the rewrite to preserve verbatim:
                "_smoke_metadata": {"shipped_at": "2026-05-06", "harness_pid": 12345},
            },
            indent=2,
        )
    )


def write_admin_token(path: Path) -> None:
    """Mint an admin-token file with expiresAt 1h in the future, so the
    script's admin_jwt_valid() returns true and skips the re-login."""
    path.write_text(
        json.dumps(
            {
                "accessToken": "admin-jwt-stub",
                "expiresAt": (int(time.time()) + 3600) * 1000,
            }
        )
    )


# ── Fake Agor daemon ──────────────────────────────────────────────────────


class _FakeDaemon:
    """One-shot HTTP server impersonating GET /sessions/:id.

    Configure response with `set_session(token=..., status=200)`. Set
    status=404 to simulate SESSION_GONE.
    """

    def __init__(self) -> None:
        self._port = self._free_port()
        self._token: str | None = None
        self._status: int = 200
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.hits: list[str] = []

    @staticmethod
    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def set_session(self, *, token: str | None, status: int = 200) -> None:
        self._token = token
        self._status = status

    def start(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):  # noqa: D401 — silence
                return

            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                if not self.path.startswith("/sessions/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(outer._status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if outer._status == 200:
                    body = {"session_id": "sess-abc", "mcp_token": outer._token}
                else:
                    body = {"error": "not found"}
                self.wfile.write(json.dumps(body).encode())

        self._server = HTTPServer(("127.0.0.1", self._port), H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


@pytest.fixture
def daemon():
    d = _FakeDaemon()
    d.start()
    yield d
    d.stop()


@pytest.fixture(autouse=True)
def _ensure_executable():
    if not SCRIPT.exists():
        pytest.skip(f"refresh script missing at {SCRIPT}")
    st = SCRIPT.stat()
    SCRIPT.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    aug_path = _augmented_path()
    if shutil.which("jq", path=aug_path) is None or shutil.which("curl", path=aug_path) is None:
        pytest.skip("jq and/or curl not installed")


def _augmented_path() -> str:
    """Ensure jq + curl are visible to the bash subprocess. On Guix
    systems the user's jq lives under ~/.guix-profile/bin which is on
    the interactive shell PATH but not the test runner's."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    extras = [
        os.path.expanduser("~/.guix-profile/bin"),
        "/usr/bin",
        "/usr/local/bin",
    ]
    for e in extras:
        if e and e not in parts and os.path.isdir(e):
            parts.append(e)
    return os.pathsep.join(parts)


def run_refresh(*args: str, env_extra: dict | None = None, timeout: int = 15):
    env = os.environ.copy()
    env["PATH"] = _augmented_path()
    env.setdefault("REFRESH_QUIET", "0")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# ── Tests ─────────────────────────────────────────────────────────────────


def test_check_only_reports_expires_in(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    fresh_exp = int(time.time()) + 3600
    write_config(cfg, make_jwt(exp=fresh_exp))

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--check-only",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 0, r.stderr
    assert "EXPIRES_IN" in r.stdout
    # Daemon must NOT have been hit on --check-only
    assert daemon.hits == []
    # Config must NOT have been rewritten
    parsed = json.loads(cfg.read_text())
    assert parsed["mcpServers"]["agor"]["headers"]["Authorization"].startswith("Bearer ")


def test_fresh_token_short_circuits(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    fresh_exp = int(time.time()) + 7200  # 2h ahead
    original = make_jwt(exp=fresh_exp)
    write_config(cfg, original)

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--ttl-buffer", "600",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert daemon.hits == []  # never asked for a refresh
    parsed = json.loads(cfg.read_text())
    assert parsed["mcpServers"]["agor"]["headers"]["Authorization"] == f"Bearer {original}"


def test_stale_token_triggers_rewrite_and_preserves_siblings(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    stale_exp = int(time.time()) + 60  # 60s ahead, buffer is 600s
    write_config(cfg, make_jwt(exp=stale_exp))

    new_token = make_jwt(exp=int(time.time()) + 86400)
    daemon.set_session(token=new_token, status=200)

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--ttl-buffer", "600",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert any(h.endswith("/sessions/sess-abc") for h in daemon.hits)

    parsed = json.loads(cfg.read_text())
    assert parsed["mcpServers"]["agor"]["headers"]["Authorization"] == f"Bearer {new_token}"
    # Siblings preserved verbatim
    assert parsed["_smoke_metadata"] == {"shipped_at": "2026-05-06", "harness_pid": 12345}
    # The url + type are intact
    assert parsed["mcpServers"]["agor"]["type"] == "http"
    assert parsed["mcpServers"]["agor"]["url"] == "http://localhost:9999/mcp"


def test_malformed_config_exits_13(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    cfg.write_text("{this is not json at all")

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 13, f"expected CONFIG_BAD=13; got {r.returncode}\n{r.stderr}"


def test_non_jwt_token_exits_13(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    # Valid JSON, but the bearer is not a JWT (no dots)
    write_config(cfg, "not-a-jwt-token")

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 13, f"expected CONFIG_BAD=13; got {r.returncode}\n{r.stderr}"


def test_missing_session_exits_12(tmp_path, daemon):
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    stale_exp = int(time.time()) + 60
    write_config(cfg, make_jwt(exp=stale_exp))

    daemon.set_session(token=None, status=404)

    r = run_refresh(
        "--config", str(cfg),
        "--session-id", "sess-abc",
        "--bootstrap-token", str(admin),
        env_extra={"AGOR_BASE_URL": daemon.base_url},
    )
    assert r.returncode == 12, f"expected SESSION_GONE=12; got {r.returncode}\n{r.stderr}"


def test_concurrent_invocations_dont_corrupt(tmp_path, daemon):
    """Two concurrent refreshes must leave the config valid JSON with
    one of the new tokens — never a partial write."""
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    stale_exp = int(time.time()) + 60
    write_config(cfg, make_jwt(exp=stale_exp))

    new_token = make_jwt(exp=int(time.time()) + 86400)
    daemon.set_session(token=new_token, status=200)

    def fire():
        return run_refresh(
            "--config", str(cfg),
            "--session-id", "sess-abc",
            "--ttl-buffer", "600",
            "--bootstrap-token", str(admin),
            env_extra={"AGOR_BASE_URL": daemon.base_url},
        )

    results: list[subprocess.CompletedProcess] = []
    threads = [threading.Thread(target=lambda: results.append(fire())) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    rcs = [r.returncode for r in results]
    assert all(rc == 0 for rc in rcs), f"non-zero exit among concurrent runs: {rcs}"

    # File must be valid JSON with the new token
    parsed = json.loads(cfg.read_text())
    assert parsed["mcpServers"]["agor"]["headers"]["Authorization"] == f"Bearer {new_token}"
    assert parsed["_smoke_metadata"] == {"shipped_at": "2026-05-06", "harness_pid": 12345}

    # No leftover .tmp.<pid> files in the dir
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"leftover temp files: {leftovers}"


def test_pid_mode_resolves_session_from_jwt_sub(tmp_path, daemon):
    """When --pid is given without --session-id, the script should
    derive session_id from the JWT's .sub claim."""
    cfg = tmp_path / "mcp.json"
    admin = tmp_path / "cli-token"
    write_admin_token(admin)
    stale_exp = int(time.time()) + 60
    write_config(cfg, make_jwt(exp=stale_exp, sub="sess-abc"))

    new_token = make_jwt(exp=int(time.time()) + 86400, sub="sess-abc")
    daemon.set_session(token=new_token, status=200)

    # Use our own pid — guaranteed to exist; SIGHUP to self is harmless
    # because Python's default SIGHUP is to terminate, but the test
    # process installs its own handlers (pytest)... so trap it first.
    import signal
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        r = run_refresh(
            "--config", str(cfg),
            "--pid", str(os.getpid()),
            "--ttl-buffer", "600",
            "--bootstrap-token", str(admin),
            env_extra={"AGOR_BASE_URL": daemon.base_url},
        )
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)

    assert r.returncode == 0, r.stderr + r.stdout
    assert any(h.endswith("/sessions/sess-abc") for h in daemon.hits)
