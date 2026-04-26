# [[file:../../../org/20260425230731-org_llm.org::*tests/test_mcp_server.py][test_mcp_server.py:1]]
"""Tests for org_llm.mcp_server — every MCP tool exercised against a real DB.

Uses ORG_LLM_DB env var so create_mcp_server() points at a tmp_path SQLite file.
Calls each tool's underlying .fn() directly to exercise the real query paths
(detached-session bugs, type mismatches, attribute lookups would all surface).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from org_llm.db import Config, File, Node, get_session, init_db, make_engine
from org_llm.search import to_blob
from org_llm.skills import Skill


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    """Set ORG_LLM_DB → tmp DB and seed with realistic vault data."""
    db_path = tmp_path / "mcp.db"
    monkeypatch.setenv("ORG_LLM_DB", str(db_path))
    engine = make_engine(db_path)
    init_db(engine)

    now = time.time()
    old = now - (40 * 86400)   # 40 days ago — outside default 14-day window

    with get_session(engine) as s:
        # File A — a recent note with embedding
        f1 = File(path="/vault/notes/recent.org", indexed_at="now",
                  node_count=2, mtime=now)
        s.add(f1); s.flush()
        s.add(Node(file_id=f1.id, node_id="id-recent-1",
                   title="Recent socialism note",
                   body="Workers of the world unite — solidarity now.",
                   tags="politics work",
                   mtime=now,
                   embedding=to_blob([1.0, 0.0, 0.0])))
        s.add(Node(file_id=f1.id, node_id="id-recent-2",
                   title="Untagged thought", body="Random idea.",
                   tags="", mtime=now))

        # File B — old note (outside recent window)
        f2 = File(path="/vault/archive/old.org", indexed_at="now",
                  node_count=1, mtime=old)
        s.add(f2); s.flush()
        s.add(Node(file_id=f2.id, node_id="id-old", title="Vintage note",
                   body="Ancient wisdom about Star Trek.",
                   tags="archive trek", mtime=old,
                   embedding=to_blob([0.0, 1.0, 0.0])))

        # A skill
        s.add(Skill(name="echo_test", lang="python", model_key="text_model",
                    source="print('{{input}}')",
                    file_path="/vault/skills.org", heading="Echo"))
        s.commit()

    yield db_path
    engine.dispose()


@pytest.fixture
def server(mcp_db):
    from org_llm.mcp_server import create_mcp_server
    return create_mcp_server()


def _tool(server, name):
    return server._tool_manager._tools[name].fn


# ── Server construction ───────────────────────────────────────────────────────

class TestServerBuilds:
    def test_create_returns_fastmcp(self, server):
        from mcp.server.fastmcp import FastMCP
        assert isinstance(server, FastMCP)

    def test_expected_tools_registered(self, server):
        names = set(server._tool_manager._tools)
        expected = {
            "search_notes", "ask_notes", "capture_note", "get_node",
            "list_nodes_by_tag", "list_recent_nodes", "get_vault_stats",
            "list_skills", "run_skill", "tangle_file", "get_config",
            "list_tutor_steps", "get_tutor_step",
        }
        assert expected <= names, f"Missing tools: {expected - names}"

    def test_server_has_instructions(self, server):
        assert server.instructions
        assert "org-roam" in server.instructions


# ── search_notes ──────────────────────────────────────────────────────────────

class TestSearchNotes:
    def test_keyword_finds_match(self, server):
        out = _tool(server, "search_notes")(query="socialism", keyword=True)
        assert "Recent socialism" in out

    def test_keyword_no_match(self, server):
        out = _tool(server, "search_notes")(query="quantumchromodynamics", keyword=True)
        assert "No results" in out

    def test_keyword_respects_limit(self, server):
        # Force-add many matches
        from org_llm.db import make_engine, get_session, File, Node
        db = Path(os.environ["ORG_LLM_DB"])
        engine = make_engine(db)
        with get_session(engine) as s:
            f = File(path="/many.org", indexed_at="now", node_count=20, mtime=time.time())
            s.add(f); s.flush()
            for i in range(20):
                s.add(Node(file_id=f.id, title=f"hit-{i}",
                           body="needle", tags="", mtime=time.time()))
            s.commit()
        out = _tool(server, "search_notes")(query="needle", keyword=True, limit=3)
        # Sections separated by ---; should be ≤ 3
        assert out.count("---") <= 3

    def test_falls_back_to_keyword_when_ollama_offline(self, server, monkeypatch):
        # Force the embed call to fail; tool should keyword-fallback
        def boom(*a, **kw):
            raise RuntimeError("ollama down")
        monkeypatch.setattr("org_llm.llm.embed", boom)
        out = _tool(server, "search_notes")(query="solidarity", keyword=False)
        # Either matched on keyword fallback or returned no-results gracefully
        assert isinstance(out, str)


# ── get_node ──────────────────────────────────────────────────────────────────

class TestGetNode:
    def test_finds_by_partial_title(self, server):
        out = _tool(server, "get_node")(title="socialism")
        assert "Recent socialism" in out
        assert "/vault/notes/recent.org" in out

    def test_includes_id_and_tags(self, server):
        out = _tool(server, "get_node")(title="Recent socialism")
        assert "id-recent-1" in out
        assert "politics" in out

    def test_modified_is_iso_date(self, server):
        out = _tool(server, "get_node")(title="Recent socialism")
        # Must be parseable as ISO; not a raw float
        from datetime import datetime
        # Find the line "Modified:    YYYY-..."
        line = next(l for l in out.splitlines() if l.startswith("Modified:"))
        ts = line.split("Modified:")[1].strip()
        datetime.fromisoformat(ts)   # raises if unparseable

    def test_unknown_title_is_helpful(self, server):
        out = _tool(server, "get_node")(title="zzz-doesnt-exist-zzz")
        assert "search_notes" in out


# ── list_nodes_by_tag ─────────────────────────────────────────────────────────

class TestListNodesByTag:
    def test_finds_tagged(self, server):
        out = _tool(server, "list_nodes_by_tag")(tag="politics")
        assert "Recent socialism" in out
        assert "recent.org" in out

    def test_substring_match(self, server):
        # "trek" lives alongside "archive"
        out = _tool(server, "list_nodes_by_tag")(tag="trek")
        assert "Vintage note" in out

    def test_unknown_tag(self, server):
        out = _tool(server, "list_nodes_by_tag")(tag="not-a-real-tag")
        assert "No nodes tagged" in out

    def test_no_session_leaks(self, server):
        # If a session was leaked, the row tuple access would raise DetachedInstanceError
        out = _tool(server, "list_nodes_by_tag")(tag="archive")
        assert isinstance(out, str)
        assert "Vintage" in out


# ── list_recent_nodes ─────────────────────────────────────────────────────────

class TestListRecentNodes:
    def test_finds_recent(self, server):
        out = _tool(server, "list_recent_nodes")(days=14)
        assert "Recent socialism" in out
        # Old note (40 days) must NOT show up
        assert "Vintage note" not in out

    def test_date_format_is_iso(self, server):
        out = _tool(server, "list_recent_nodes")(days=14)
        # Each entry should end with "(YYYY-MM-DD)"
        import re
        assert re.search(r"\(\d{4}-\d{2}-\d{2}\)", out), out

    def test_window_includes_old_when_wide_enough(self, server):
        out = _tool(server, "list_recent_nodes")(days=365)
        assert "Vintage note" in out

    def test_empty_window(self, server):
        out = _tool(server, "list_recent_nodes")(days=0)
        assert "No nodes" in out


# ── get_vault_stats ───────────────────────────────────────────────────────────

class TestGetVaultStats:
    def test_reports_counts(self, server):
        out = _tool(server, "get_vault_stats")()
        assert "2 files" in out
        assert "3 nodes" in out

    def test_embed_percentage(self, server):
        out = _tool(server, "get_vault_stats")()
        # 2 of 3 nodes have embeddings → 66%
        assert "2/3" in out


# ── capture_note ──────────────────────────────────────────────────────────────

class TestCaptureNote:
    def test_writes_into_org_dir(self, server, tmp_path, monkeypatch):
        # Override org_dir config to a temp path
        from org_llm.db import make_engine, get_session, Config
        engine = make_engine(Path(os.environ["ORG_LLM_DB"]))
        with get_session(engine) as s:
            s.get(Config, "org_dir").value = str(tmp_path)
            s.commit()

        out = _tool(server, "capture_note")(title="Manifesto",
                                             body="Property is theft.",
                                             file="inbox.org")
        assert "Captured" in out
        assert "Manifesto" in out
        target = tmp_path / "inbox.org"
        assert target.exists()
        content = target.read_text()
        assert "Manifesto" in content
        assert "Property is theft." in content
        # ID should have been generated and embedded as a property
        assert ":ID:" in content


# ── list_skills / run_skill ──────────────────────────────────────────────────

class TestSkills:
    def test_list_skills_shows_seeded(self, server):
        out = _tool(server, "list_skills")()
        assert "echo_test" in out

    def test_run_skill_unknown(self, server):
        out = _tool(server, "run_skill")(name="nonexistent", input="hi")
        assert "not found" in out

    def test_run_skill_executes(self, server):
        out = _tool(server, "run_skill")(name="echo_test", input="solidarity ✊")
        assert "solidarity" in out


# ── get_config ────────────────────────────────────────────────────────────────

class TestGetConfig:
    def test_includes_defaults(self, server):
        out = _tool(server, "get_config")()
        assert "embed_model" in out
        assert "chat_model" in out
        assert "org_dir" in out


# ── tangle_file ──────────────────────────────────────────────────────────────

class TestTangleFile:
    def test_missing_emacsclient_message(self, server, monkeypatch):
        # Force FileNotFoundError on subprocess.run
        import subprocess
        def boom(*a, **kw):
            raise FileNotFoundError("emacsclient")
        monkeypatch.setattr(subprocess, "run", boom)
        out = _tool(server, "tangle_file")(file_path="/tmp/x.org")
        assert "emacsclient" in out


# ── tutor tools ──────────────────────────────────────────────────────────────

class TestTutorTools:
    def test_list_tutor_steps(self, server):
        out = _tool(server, "list_tutor_steps")()
        assert "welcome" in out
        assert "embed" in out

    def test_get_tutor_step_known(self, server):
        out = _tool(server, "get_tutor_step")(step="welcome")
        assert "org-llm tutor: welcome" in out
        # Rich markup should be stripped
        assert "[lcars1]" not in out
        assert "[bold]" not in out

    def test_get_tutor_step_unknown(self, server):
        out = _tool(server, "get_tutor_step")(step="not-a-step")
        assert "Unknown step" in out


# ── stdio JSON-RPC integration smoke test ────────────────────────────────────

class TestStdioInitialize:
    def test_server_responds_to_initialize(self, mcp_db, monkeypatch):
        """Run `org-llm mcp` as a subprocess and send a JSON-RPC initialize.

        This catches regressions in:
          - module-level imports that crash before the loop starts
          - FastMCP API surface changes (transport, capabilities)
          - schema mismatches that prevent the response from being emitted
        """
        import subprocess
        env = {**os.environ, "ORG_LLM_DB": str(mcp_db)}
        msg = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        }) + "\n"
        proc = subprocess.run(
            ["uv", "run", "org-llm", "mcp"],
            input=msg, capture_output=True, text=True, timeout=15, env=env,
        )
        assert proc.stdout, f"no stdout: {proc.stderr[:300]}"
        first_line = proc.stdout.splitlines()[0]
        resp = json.loads(first_line)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "org-llm"
        # tools capability advertised
        assert "tools" in resp["result"]["capabilities"]
# test_mcp_server.py:1 ends here
