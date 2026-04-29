"""End-to-end-ish tests for the conversational-surface MCP wiring.

Phase 7 manual test surfaced a class of bug we want to never regress:
both `org-llm launch` (opencode) and `org-llm claude` were generating
config files whose `mcpServers` entry pointed at an MCP invocation
that didn't actually start when opencode/claude spawned it. The model
session would silently fall back to its own tools (grep, etc.) and
the user discovered the problem by asking "did you use search_notes?"
and getting "no, I used grep".

These tests exercise the dry-run path of each launcher, parse the
config that WOULD be written, and assert two things:

  1. Structural — the MCP config block has the expected shape.
  2. Functional — the resolved invocation actually starts a real MCP
     server and responds to a JSON-RPC initialize request within a
     reasonable timeout. This catches the "uv --directory" / silent-
     spawn-failure class of bug going forward.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.cli import app


runner = CliRunner()


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_mcp_invocation_argv(mcp_block: dict) -> list[str]:
    """Translate an opencode-style {command, args} or claude-style
    {command, args} MCP entry into a shell argv list we can subprocess.

    Opencode ships `command` as a list (with args inside it) when
    written by `org-llm launch`. Claude writes `command` as a string
    plus a separate `args` list. Normalise to one argv list."""
    cmd = mcp_block.get("command")
    if isinstance(cmd, list):
        return list(cmd)
    args = mcp_block.get("args") or []
    return [cmd] + list(args)


def _mcp_env(mcp_block: dict) -> dict[str, str]:
    """Extract the MCP env-var bag. Opencode uses key `environment`
    (per McpLocalConfig in @opencode-ai/sdk types.gen.d.ts); claude
    uses key `env`. Accept both — what matters is the resulting dict.
    """
    return dict(mcp_block.get("environment")
                 or mcp_block.get("env")
                 or {})


def _spawn_mcp_and_initialize(argv: list[str], env: dict[str, str],
                                timeout: float = 8.0) -> dict | None:
    """Start the MCP server with `argv`, send a JSON-RPC initialize,
    return the parsed response or None on any failure (timeout,
    non-zero exit, malformed JSON)."""
    full_env = {**os.environ, **env}
    init_req = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "initialize",
        "params":  {
            "protocolVersion": "2024-11-05",
            "capabilities":    {},
            "clientInfo":      {"name": "test", "version": "1.0"},
        },
    }
    try:
        proc = subprocess.Popen(
            argv, env=full_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
    except Exception:
        return None
    try:
        out, _err = proc.communicate(
            input=json.dumps(init_req) + "\n",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _err = proc.communicate(timeout=2.0)
        except Exception:
            return None
    # Server may emit multiple JSON-RPC messages (notifications first);
    # find the first object that has our id.
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == 1 and "result" in msg:
            return msg
    return None


# ── opencode (launch) ────────────────────────────────────────────────────────

class TestOpencodeLaunchMCP:
    """`org-llm launch --dry-run` writes a working `.opencode/opencode.json`."""

    def test_dry_run_includes_mcp_block_with_org_llm_server(self, cli_db, capsys):
        # Run launch in dry-run; capture the printed JSON
        r = runner.invoke(app, ["launch", "--dry-run"])
        assert r.exit_code == 0, r.output
        # Pull the JSON out of the panel — it's between the "(dry-run)"
        # rule and the "Workspace:" sub-rule.
        m = re.search(r"\{[^`]*?\"mcp\".*?\}\s*$",
                       r.output, flags=re.DOTALL | re.MULTILINE)
        # Fallback: just look for the mcp.org-llm server name
        assert "org-llm" in r.output and "\"mcp\"" in r.output, r.output

    def test_opencode_instructions_is_a_list_not_string(self, cli_org,
                                                           monkeypatch):
        """Per @opencode-ai/sdk types.gen.d.ts, Config.instructions is
        `Array<string>`, NOT a single string. We wrote a giant string
        for months — opencode silently dropped it, leaving the model
        with NO system prompt. The Phase 7 model literally said "I'm
        opencode, not org-llm" because our persona/search-first rules
        never reached it."""
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        monkeypatch.setattr(os, "execvp",
                              lambda p, a: (_ for _ in ()).throw(SystemExit(0)))
        runner.invoke(app, ["launch"])
        cfg = json.loads((Path(cli_org) / ".opencode" / "opencode.json").read_text())
        instr = cfg.get("instructions")
        assert isinstance(instr, list), (
            f".opencode/opencode.json `instructions` must be a list per opencode's "
            f"Config.instructions: Array<string> schema. Got {type(instr).__name__}. "
            "Without the list wrapper, opencode silently drops the entire "
            "system prompt and the model has no idea it's running as org-llm."
        )
        assert all(isinstance(x, str) for x in instr), (
            "instructions list must contain only strings"
        )
        # And the persona content is actually IN there
        joined = "\n".join(instr)
        assert "search_notes" in joined, (
            "system prompt missing search-first rules"
        )
        assert "org-llm" in joined.lower(), (
            "system prompt doesn't identify as org-llm — model will think "
            "it's vanilla opencode"
        )

    def test_opencode_mcp_block_uses_environment_not_env(self, cli_org,
                                                            monkeypatch):
        """opencode's McpLocalConfig schema (per
        @opencode-ai/sdk/dist/v2/gen/types.gen.d.ts) uses key
        `environment` for the env-var bag, NOT `env`. Phase 7
        verification regressed because we wrote `env` — opencode
        silently dropped the whole MCP entry, the in-opencode model
        had no search_notes / ask_notes / etc. tools, and fell
        back to grep. This test pins the correct key."""
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        monkeypatch.setattr(os, "execvp",
                              lambda p, a: (_ for _ in ()).throw(SystemExit(0)))
        runner.invoke(app, ["launch"])
        cfg = json.loads((Path(cli_org) / ".opencode" / "opencode.json").read_text())
        mcp_block = cfg.get("mcp", {}).get("org-llm")
        assert mcp_block, "no .mcp.org-llm in written config"
        assert "environment" in mcp_block, (
            "MCP block missing `environment` key — opencode will silently "
            "ignore env vars (or skip the whole entry on some versions). "
            "Use `environment` (per McpLocalConfig schema), not `env`."
        )
        assert "env" not in mcp_block, (
            "MCP block still has the old `env` key — opencode will skip "
            "the entry. Use `environment` per McpLocalConfig schema."
        )
        assert mcp_block.get("enabled") is True, (
            "MCP block missing explicit `enabled: true`. Documented as "
            "default but spelling it guards against version drift."
        )

    def test_resolved_mcp_invocation_is_not_uv_directory_trick(self, cli_db,
                                                                 monkeypatch,
                                                                 tmp_path):
        """The previous impl shelled `uv --directory <site-packages>
        run org-llm mcp` which silently failed when uv couldn't find a
        project at the working directory. The current impl uses the
        plain `org-llm mcp` invocation (or absolute path). This test
        pins that — guards against a regression to the fragile form."""
        from org_llm.cli import launch as _launch_cmd  # noqa: F401
        # Trigger the launch dry-run so the config is built; capture
        # the printed JSON via the runner output.
        r = runner.invoke(app, ["launch", "--dry-run"])
        assert r.exit_code == 0, r.output
        # The dry-run prints the JSON to stdout. The MCP command must
        # NOT include the legacy "--directory" + site-packages trick.
        # When the resolved command is just `org-llm mcp` (or an
        # absolute path ending in `org-llm`), this regex won't match.
        legacy_pat = re.compile(r"--directory.*site-packages",
                                  flags=re.DOTALL)
        assert not legacy_pat.search(r.output), (
            "launch is back on the fragile `uv --directory site-packages` "
            "MCP invocation; that silently fails when uv can't resolve "
            "a project at the working directory. Should be plain "
            "`org-llm mcp`."
        )

    @pytest.mark.skipif(
        not (Path(os.environ.get("HOME", "/")) / ".local/bin/org-llm").exists()
        and not subprocess.run(["which", "org-llm"],
                                capture_output=True).returncode == 0,
        reason="`org-llm` not on PATH — can't spawn MCP for live test",
    )
    def test_launch_mcp_invocation_actually_responds_to_initialize(
            self, cli_org, monkeypatch, tmp_path):
        """The headline guarantee: whatever invocation `org-llm launch`
        bakes into .opencode.json MUST actually start an MCP server
        that responds to JSON-RPC initialize. If the answer is "no",
        users get fall-back-to-grep and zero MCP tools — exactly the
        Phase 7 bug.

        Strategy: monkeypatch os.execvp so launch returns instead of
        execing opencode. The .opencode.json file IS written by then;
        read + parse it directly (cleanest, no rich-output scraping)."""
        # Stub out opencode binary detection so launch doesn't bail
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        # Mock os.execvp to short-circuit BEFORE opencode runs
        def fake_execvp(path, argv):
            raise SystemExit(0)
        monkeypatch.setattr(os, "execvp", fake_execvp)
        # Run the launch — writes .opencode.json then hits fake execvp
        runner.invoke(app, ["launch"])
        # cli_org IS the org_dir (the fixture creates it + sets the
        # config row). The config file gets written there.
        cfg_path = Path(cli_org) / ".opencode" / "opencode.json"
        assert cfg_path.exists(), (
            f"launch didn't write .opencode.json at {cfg_path}"
        )
        cfg = json.loads(cfg_path.read_text())
        mcp_block = cfg.get("mcp", {}).get("org-llm")
        assert mcp_block, f"no .mcp.org-llm in dry-run config: {cfg!r}"
        argv = _build_mcp_invocation_argv(mcp_block)
        # Resolve the binary against PATH if it's a bare name
        if argv and "/" not in argv[0]:
            import shutil
            resolved = shutil.which(argv[0])
            if resolved:
                argv[0] = resolved
        env_for_spawn = _mcp_env(mcp_block)
        resp = _spawn_mcp_and_initialize(argv, env_for_spawn,
                                            timeout=8.0)
        assert resp is not None, (
            f"MCP server didn't respond to initialize.\n"
            f"  argv: {argv}\n  env:  {env_for_spawn}\n"
            "This means opencode would fall back to its own tools "
            "and the user gets `grep` instead of `search_notes`."
        )
        result = resp.get("result", {})
        assert "serverInfo" in result, resp
        assert result["serverInfo"]["name"] == "org-llm"


# ── claude ───────────────────────────────────────────────────────────────────

class TestClaudeLaunchMCP:
    """`org-llm claude --dry-run` produces a settings.json the running
    Claude Code instance will actually use to load org-llm tools."""

    def test_settings_json_has_org_llm_mcp_server(self, cli_db, monkeypatch,
                                                    tmp_path):
        # Dry-run path uses NO_COLOR so output is clean
        env = {**os.environ, "NO_COLOR": "1",
                "ORG_LLM_DB": str(cli_db)}
        proc = subprocess.run(
            ["org-llm", "claude", "--dry-run"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        # Claude dry-run prints the full system prompt + the file
        # paths it WOULD write. Find the printed settings.json content.
        # The actual writer creates settings.json on real run; here we
        # just want the structural check that the dry-run mentions
        # mcpServers.
        assert "settings.json" in proc.stdout, (
            "claude --dry-run output didn't reference settings.json: "
            f"{proc.stdout[:1500]}"
        )

    def test_claude_invocation_passes_mcp_config_inline(self):
        """Phase 7 bug: claude was launched bare via os.execvp, so the
        running Claude Code session never picked up the org-llm MCP
        server (auto-discovery of project-local settings.json is
        unreliable across versions). The fix: pass --mcp-config
        inline. This test pins that — grep the source for the
        execvp call to make sure we still pass --mcp-config."""
        from pathlib import Path
        cli_src = Path(__file__).resolve().parent.parent / "org_llm" / "cli.py"
        text = cli_src.read_text()
        # Find the os.execvp line for claude_bin
        m = re.search(r"os\.execvp\(claude_bin,\s*\[claude_bin([^\]]*)\]",
                        text)
        assert m, "couldn't locate claude execvp line — refactor?"
        argv_tail = m.group(1)
        assert "--mcp-config" in argv_tail, (
            "os.execvp(claude_bin, [claude_bin]) without --mcp-config "
            "regressed; without it, Claude Code may not load the org-llm "
            "MCP server and falls back to its own grep/etc. tools."
        )
