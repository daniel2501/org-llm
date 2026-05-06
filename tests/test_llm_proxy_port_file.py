"""Tests for DEC-015 v0.1 — proxy port-file discovery.

The Emacs chat surface (`doom/org-llm-chat.el`) reads
`~/.local/share/org-llm/proxy-port` to find the running proxy
without shelling out. These tests verify `start_proxy` writes
that file atomically, honours the `ORG_LLM_PROXY_PORT_FILE`
override, and removes the file on shutdown.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from org_llm import llm_proxy


def _free_unused_port() -> int:
    """Bind a throwaway socket to find an unused port (closed
    immediately; small race-window is acceptable for tests)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def temp_port_file(tmp_path, monkeypatch):
    """Redirect the port-file via env override and clean it up."""
    path = tmp_path / "proxy-port"
    monkeypatch.setenv("ORG_LLM_PROXY_PORT_FILE", str(path))
    yield path
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


@pytest.fixture
def started_proxy(temp_port_file):
    """Start a real proxy (port=0, OS-assigned) and tear it down.

    Yields (port, server, port_file_path).
    """
    # Use a bogus upstream — the tests don't actually proxy traffic,
    # they only care about the port-file side-effect.
    port, server = llm_proxy.start_proxy(
        upstream_url="http://127.0.0.1:1",
        interceptors=[],
    )
    try:
        # Give serve_forever a moment to spin up; the port file is
        # written synchronously before we return so the existence
        # check is safe immediately, but the server thread needs a
        # tick before it accepts.
        time.sleep(0.05)
        yield port, server, temp_port_file
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


# ── core path: file written ───────────────────────────────────────────────


def test_port_file_exists_after_start(started_proxy):
    port, _server, path = started_proxy
    assert path.exists(), f"expected port file at {path}"
    assert port > 0


def test_port_file_content_matches_bound_port(started_proxy):
    port, _server, path = started_proxy
    content = path.read_text(encoding="utf-8").strip()
    assert content == str(port)


# ── env override ──────────────────────────────────────────────────────────


def test_env_var_redirects_port_file(tmp_path, monkeypatch):
    """ORG_LLM_PROXY_PORT_FILE redirects the write target."""
    target = tmp_path / "custom" / "deep" / "port.txt"
    monkeypatch.setenv("ORG_LLM_PROXY_PORT_FILE", str(target))
    # Default location should NOT receive the write.
    default_path = (
        Path.home() / ".local" / "share" / "org-llm" / "proxy-port"
    )
    default_existed_before = default_path.exists()
    default_mtime_before = (
        default_path.stat().st_mtime if default_existed_before else None
    )

    port, server = llm_proxy.start_proxy(
        upstream_url="http://127.0.0.1:1",
        interceptors=[],
    )
    try:
        time.sleep(0.05)
        assert target.exists(), "env-override target was not written"
        assert target.read_text(encoding="utf-8").strip() == str(port)
        # Default path either didn't exist before/after or wasn't
        # modified by THIS start_proxy call.
        if default_existed_before:
            assert (
                default_path.stat().st_mtime == default_mtime_before
            ), "default path was clobbered despite env override"
        else:
            assert not default_path.exists(), (
                "default path created despite env override"
            )
    finally:
        server.shutdown()
        server.server_close()
        if target.exists():
            target.unlink()


# ── shutdown removes file ─────────────────────────────────────────────────


def test_port_file_removed_on_shutdown(temp_port_file):
    port, server = llm_proxy.start_proxy(
        upstream_url="http://127.0.0.1:1",
        interceptors=[],
    )
    try:
        time.sleep(0.05)
        assert temp_port_file.exists()
    finally:
        server.shutdown()
        server.server_close()
    # After shutdown the file should be gone (the wrapped shutdown
    # removes it; atexit is the belt-and-braces tier).
    assert not temp_port_file.exists(), (
        "port file should be removed by server.shutdown()"
    )


# ── opt-out ───────────────────────────────────────────────────────────────


def test_write_port_file_false_skips_side_effect(temp_port_file):
    """`write_port_file=False` keeps the legacy behaviour for tests."""
    port, server = llm_proxy.start_proxy(
        upstream_url="http://127.0.0.1:1",
        interceptors=[],
        write_port_file=False,
    )
    try:
        time.sleep(0.05)
        assert not temp_port_file.exists(), (
            "port file written despite write_port_file=False"
        )
        assert port > 0
    finally:
        server.shutdown()
        server.server_close()


# ── atomicity ─────────────────────────────────────────────────────────────


def test_concurrent_writes_are_atomic(tmp_path, monkeypatch):
    """A concurrent reader never sees a torn write.

    We pound `_write_proxy_port_file` from multiple threads while a
    reader thread polls; any read must yield either an empty string
    (file not yet replaced) or a parseable int. We never expect a
    half-written ASCII number like "1234\\n5678".
    """
    path = tmp_path / "race" / "proxy-port"
    ports = list(range(20000, 20100))

    def writer(p: int) -> None:
        for _ in range(20):
            llm_proxy._write_proxy_port_file(path, p)

    saw_torn = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                continue
            stripped = raw.strip()
            if not stripped:
                continue
            # Must be a single int line — never something like "1234\n5678"
            if not stripped.isdigit():
                saw_torn.append(stripped)
                return
            if "\n" in stripped:  # trailing \n was stripped above
                saw_torn.append(stripped)
                return

    threads = [threading.Thread(target=writer, args=(p,)) for p in ports[:8]]
    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    rt.join(timeout=1.0)

    assert saw_torn == [], f"observed torn write(s): {saw_torn!r}"


# ── helper-level coverage ─────────────────────────────────────────────────


def test_proxy_port_file_path_default_no_env(monkeypatch):
    monkeypatch.delenv("ORG_LLM_PROXY_PORT_FILE", raising=False)
    p = llm_proxy._proxy_port_file_path()
    expected = Path.home() / ".local" / "share" / "org-llm" / "proxy-port"
    assert p == expected


def test_proxy_port_file_path_respects_env(monkeypatch, tmp_path):
    target = tmp_path / "alt" / "port"
    monkeypatch.setenv("ORG_LLM_PROXY_PORT_FILE", str(target))
    p = llm_proxy._proxy_port_file_path()
    assert p == target


def test_remove_proxy_port_file_swallows_missing(tmp_path):
    """No error if the file is already gone."""
    path = tmp_path / "missing"
    # Should not raise.
    llm_proxy._remove_proxy_port_file(path)
