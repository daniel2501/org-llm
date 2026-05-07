"""Unit tests for =org_llm.captains_log_agor_bridge=.

Approach (mirrors =tests/test_agor_mcp_refresh.py= for HTTP-stub
shape but stays in-process — no shell): a tiny stdlib HTTP server
impersonates the Agor daemon's GET/PATCH /sessions/:id surface.
The captain's-log sink is exercised against a real isolated DB
(uses ORG_LLM_DB env override + ORG_LLM_LOG_PATH for the org file)
so we test the actual write_event path the public API depends on.

Coverage:
  - tee() writes to BOTH sinks with the same UUIDv7 event_id
  - event_id sorts chronologically (UUIDv7 monotonic-ish)
  - idempotent rewrite: re-attaching same event_id is a no-op
  - Agor down → captain's log keeps writing; deadletter flagged
  - record_session_event public verb returns the event_id
  - query_session_events round-trips via the History table
  - query_genealogy resolves children + their event_ids
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest


# ── helpers ─────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_admin_token(path: Path) -> None:
    """Mint an admin-token file in the shape AgorClient expects."""
    path.write_text(json.dumps({
        "accessToken": "admin-jwt-stub",
        "expiresAt":  (int(time.time()) + 3600) * 1000,
    }))


# ── fake daemon ─────────────────────────────────────────────────


class _FakeAgorDaemon:
    """In-process HTTP server impersonating the Agor session API.

    Stores Sessions keyed by session_id. Supports:
      GET   /sessions/:id  → 200 / 404
      PATCH /sessions/:id  → merges into the stored Session

    Set `down=True` to make every request fail with 500 (simulates
    daemon outage).
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.down: bool = False
        self.requests: list[tuple[str, str, Any]] = []  # (method, path, body)
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def add_session(self, sid: str, **fields: Any) -> dict[str, Any]:
        sess = {
            "session_id":      sid,
            "worktree_id":     fields.pop("worktree_id", "wt-1"),
            "git_state":       fields.pop("git_state", {
                "ref": "refs/heads/trunk",
                "base_sha":   "deadbeef",
                "current_sha": "cafef00d",
            }),
            "genealogy":       fields.pop("genealogy", {
                "children": [],
            }),
            "custom_context":  fields.pop("custom_context", {}),
            **fields,
        }
        self.sessions[sid] = sess
        return sess

    def start(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):  # noqa: D401
                return

            def _send(self, status: int, body: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def _read_body(self) -> dict | None:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return None

            def do_GET(self):  # noqa: N802
                outer.requests.append(("GET", self.path, None))
                if outer.down:
                    self._send(500, {"error": "down"}); return
                p = urlparse(self.path).path
                if not p.startswith("/sessions/"):
                    self._send(404, {"error": "no route"}); return
                sid = p.split("/", 2)[2]
                sess = outer.sessions.get(sid)
                if sess is None:
                    self._send(404, {"error": "not found"}); return
                self._send(200, sess)

            def do_PATCH(self):  # noqa: N802
                body = self._read_body() or {}
                outer.requests.append(("PATCH", self.path, body))
                if outer.down:
                    self._send(500, {"error": "down"}); return
                p = urlparse(self.path).path
                if not p.startswith("/sessions/"):
                    self._send(404, {"error": "no route"}); return
                sid = p.split("/", 2)[2]
                sess = outer.sessions.get(sid)
                if sess is None:
                    self._send(404, {"error": "not found"}); return
                # Shallow merge — matches Feathers' patch semantics
                # well enough for the bridge's single-field writes.
                for k, v in body.items():
                    sess[k] = v
                self._send(200, sess)

        self._server = HTTPServer(("127.0.0.1", self.port), H)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def daemon():
    d = _FakeAgorDaemon()
    d.start()
    yield d
    d.stop()


@pytest.fixture
def bridge_env(tmp_path, daemon, monkeypatch):
    """Wire the bridge to use the fake daemon + an isolated DB +
    isolated org log path. Resets the bridge's deadletter + default
    client between tests."""
    admin_token = tmp_path / "cli-token"
    _write_admin_token(admin_token)

    # Captain's log isolation — env-overrides logbook's defaults.
    db_path  = tmp_path / "bridge.db"
    org_path = tmp_path / "captains-log.org"
    monkeypatch.setenv("ORG_LLM_DB",       str(db_path))
    monkeypatch.setenv("ORG_LLM_LOG_PATH", str(org_path))
    # Initialise the DB so logbook.write_event has something to
    # write against. (logbook itself swallows errors when the DB
    # is missing — but we want to assert on rows in tests.)
    from org_llm.db import init_db, make_engine
    engine = make_engine(db_path)
    init_db(engine)
    engine.dispose()

    # Install a fresh bridge default-client wired at the daemon.
    from org_llm.captains_log_agor_bridge import (
        AgorClient, set_default_client,
    )
    from org_llm.captains_log_agor_bridge.tee import _reset_deadletter
    _reset_deadletter()
    set_default_client(AgorClient(
        base_url   = daemon.base_url,
        token_path = admin_token,
        ttl        = 0.0,    # disable caching for assertion clarity
    ))

    yield {
        "tmp_path":   tmp_path,
        "db_path":    db_path,
        "org_path":   org_path,
        "daemon":     daemon,
    }

    _reset_deadletter()


# ── tests ───────────────────────────────────────────────────────


def test_event_id_is_uuidv7_and_chronological():
    """UUIDv7 strings sort chronologically and have the version
    nibble in the right place. Bridge promises the join key is
    naturally time-ordered."""
    from org_llm.captains_log_agor_bridge.event import new_event_id

    ids: list[str] = []
    for _ in range(8):
        ids.append(new_event_id())
        time.sleep(0.005)
    assert sorted(ids) == ids, "UUIDv7 should be monotonic across mints"
    for eid in ids:
        # version nibble is byte 6, top half
        version_hex = eid.split("-")[2][0]
        assert version_hex == "7", f"expected UUIDv7, got version {version_hex}"


def test_tee_writes_to_both_sinks_with_shared_event_id(bridge_env):
    """The single tee call lands the SAME event_id in BOTH sinks."""
    from org_llm.captains_log_agor_bridge import tee
    daemon = bridge_env["daemon"]
    daemon.add_session("sess-1")

    ev = tee(
        action          = "narration",
        agent           = "geordi",
        prompt          = "Engaging warp.",
        outcome         = "ok",
        agor_session_id = "sess-1",
        kind            = "llm",
        payload         = {"model": "claude-opus-4-7"},
    )

    assert ev.event_id and len(ev.event_id) == 36
    assert ev.session_id == "sess-1"
    # Backfilled from the daemon's git_state:
    assert ev.commit_sha == "cafef00d"
    assert ev.worktree_id == "wt-1"

    # Sink 1 — captain's log SQLite History row exists with same id
    from org_llm.db import History, make_engine
    from sqlalchemy.orm import Session as SaSession
    engine = make_engine(bridge_env["db_path"])
    with SaSession(engine) as s:
        rows = s.query(History).all()
    assert rows, "captain's log should have one row"
    args_blob = json.loads(rows[-1].args or "{}")
    assert args_blob["event_id"]   == ev.event_id
    assert args_blob["session_id"] == "sess-1"

    # Sink 2 — Agor session has the event_id in custom_context
    sess = daemon.sessions["sess-1"]
    assert sess["custom_context"]["captain_log_event_ids"] == [ev.event_id]


def test_record_session_event_returns_event_id(bridge_env):
    """The convenience verb returns the event_id and propagates
    payload through to both sinks."""
    from org_llm.captains_log_agor_bridge import record_session_event
    bridge_env["daemon"].add_session("sess-rec")

    eid = record_session_event(
        "sess-rec", "crew",
        agent  = "riker",
        action = "spawn",
        prompt = "Forking off subtask",
        outcome = "ok",
        subtask_id = "T-7",
    )
    assert eid and "-" in eid

    sess = bridge_env["daemon"].sessions["sess-rec"]
    assert eid in sess["custom_context"]["captain_log_event_ids"]


def test_idempotent_rewrite(bridge_env):
    """Re-attaching the same event_id should NOT duplicate it.

    Implementation re-tees the same event manually — exercises the
    `_attach_event_to_session` dedup branch directly."""
    from org_llm.captains_log_agor_bridge.event import Event, new_event_id
    from org_llm.captains_log_agor_bridge.tee   import _attach_event_to_session
    from org_llm.captains_log_agor_bridge       import get_default_client

    daemon = bridge_env["daemon"]
    daemon.add_session("sess-idem")
    client = get_default_client()
    eid = new_event_id()
    ev = Event(event_id=eid, session_id="sess-idem", kind="crew")

    err1 = _attach_event_to_session(client, ev)
    err2 = _attach_event_to_session(client, ev)
    err3 = _attach_event_to_session(client, ev)
    assert err1 is None and err2 is None and err3 is None

    sess = daemon.sessions["sess-idem"]
    assert sess["custom_context"]["captain_log_event_ids"] == [eid], \
        "re-attaching same event_id must not duplicate"


def test_agor_down_falls_back_to_captains_log_only(bridge_env):
    """When Agor is unreachable, the captain's log still gets the
    event AND the deadletter records the failure for the v0.1
    reconciler."""
    from org_llm.captains_log_agor_bridge import (
        deadletter_snapshot, tee,
    )

    daemon = bridge_env["daemon"]
    daemon.add_session("sess-down")
    daemon.down = True   # next request will 500

    ev = tee(
        action          = "tool_call",
        agent           = "data",
        prompt          = "rg --json …",
        outcome         = "ok",
        agor_session_id = "sess-down",
        kind            = "mcp",
    )
    assert ev.event_id

    # Captain's log row still landed
    from org_llm.db import History, make_engine
    from sqlalchemy.orm import Session as SaSession
    engine = make_engine(bridge_env["db_path"])
    with SaSession(engine) as s:
        rows = s.query(History).all()
    assert rows, "captain's log must keep writing under Agor outage"

    # Deadletter has the event flagged for reconcile
    dl = deadletter_snapshot()
    assert any(d.event_id == ev.event_id for d in dl), \
        "Agor sink failure must enqueue the event_id for reconcile"


def test_query_session_events_returns_chronological(bridge_env):
    """query_session_events reads back the events the bridge wrote
    for one Agor session, in chronological order."""
    from org_llm.captains_log_agor_bridge import (
        query_session_events,
        record_session_event,
    )

    daemon = bridge_env["daemon"]
    daemon.add_session("sess-q")

    eids: list[str] = []
    for i in range(3):
        eids.append(record_session_event(
            "sess-q", "llm",
            agent   = "geordi",
            action  = "narration",
            prompt  = f"step {i}",
            outcome = "ok",
        ))
        time.sleep(0.005)

    out = query_session_events("sess-q")
    out_ids = [e.event_id for e in out]
    assert out_ids == sorted(eids), \
        f"expected chronological order; got {out_ids} vs {sorted(eids)}"
    # All events should carry the session id back
    for ev in out:
        assert ev.session_id == "sess-q"


def test_query_genealogy_returns_tree(bridge_env):
    """Parent + children + their event_ids assemble into a SessionTree."""
    from org_llm.captains_log_agor_bridge import (
        query_genealogy,
        record_session_event,
    )

    daemon = bridge_env["daemon"]
    # parent forks two children
    daemon.add_session("sess-parent",
                        genealogy={"children": ["sess-c1", "sess-c2"]})
    daemon.add_session("sess-c1",
                        genealogy={"parent_session_id": "sess-parent",
                                    "children": []})
    daemon.add_session("sess-c2",
                        genealogy={"parent_session_id": "sess-parent",
                                    "children": []})

    p_eid  = record_session_event("sess-parent", "crew", action="spawn")
    c1_eid = record_session_event("sess-c1",     "llm",  action="narration")
    c2_eid = record_session_event("sess-c2",     "mcp",  action="tool_call")

    tree = query_genealogy("sess-parent")
    assert tree is not None
    assert tree.session_id == "sess-parent"
    assert sorted(tree.children_session_ids) == ["sess-c1", "sess-c2"]
    assert p_eid in tree.event_ids
    child_event_ids = {c.session_id: c.event_ids for c in tree.children}
    assert c1_eid in child_event_ids["sess-c1"]
    assert c2_eid in child_event_ids["sess-c2"]


def test_no_session_id_writes_captains_log_only(bridge_env):
    """Events without an Agor session_id are captain's-log-only
    and do NOT touch the deadletter (they're not failures)."""
    from org_llm.captains_log_agor_bridge import (
        deadletter_snapshot,
        tee,
    )

    ev = tee(
        action  = "cli",
        agent   = "",
        prompt  = "org-llm doctor",
        outcome = "ok",
        kind    = "cli",
    )
    assert ev.event_id
    assert ev.session_id == ""
    # No Agor sink was attempted → no deadletter pollution
    assert all(d.event_id != ev.event_id for d in deadletter_snapshot())
