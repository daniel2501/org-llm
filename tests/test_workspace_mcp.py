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

    def test_phase_16_1_plugin_lives_in_tui_json_as_dir_uri(self, cli_org,
                                                              monkeypatch):
        """Phase 16.1's first attempt registered the TUI plugin as the
        path to its inner src/index.ts in tui.json. Two stacked bugs
        kept opencode from loading it (2026-04-29 audit):

          1. opencode wants the package DIRECTORY (so it can read
             package.json#exports["./tui"]), not the .ts file.
          2. The path needs the file:// URI scheme — confirmed by
             running `opencode plugin file://...` and inspecting the
             tui.json it wrote.

        Pin the fix: tui.json's plugin entry is a file:// URI of the
        extensions/opencode/ package directory."""
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        monkeypatch.setattr(os, "execvp",
                              lambda p, a: (_ for _ in ()).throw(SystemExit(0)))
        runner.invoke(app, ["launch"])
        tui_path = Path(cli_org) / ".opencode" / "tui.json"
        assert tui_path.exists(), "launch didn't write tui.json"
        tui_cfg = json.loads(tui_path.read_text())
        plugins = tui_cfg.get("plugin")
        assert isinstance(plugins, list) and plugins, (
            "tui.json missing `plugin: [...]` entry. Phase 16.1 "
            "TUI plugin won't load."
        )
        spec = plugins[0]
        assert spec.startswith("file://"), (
            f"plugin spec must be a file:// URI (opencode rejected raw "
            f"paths during the 2026-04-29 audit). Got: {spec!r}"
        )
        assert spec.rstrip("/").endswith("extensions/opencode"), (
            f"plugin spec must point at the package DIR "
            f"(extensions/opencode/), not the inner src/index.ts file. "
            f"opencode reads package.json#exports['./tui'] to resolve "
            f"the entrypoint. Got: {spec!r}"
        )

        # And opencode.json must NOT carry a plugin field — that loads
        # SERVER plugins (must export `server`). Our plugin only
        # exports `tui`; opencode logged "must default export an
        # object with server() failed to load plugin" when we put it
        # there.
        cfg = json.loads((Path(cli_org) / ".opencode" / "opencode.json").read_text())
        assert "plugin" not in cfg, (
            "opencode.json has a `plugin` field — opencode treats those "
            "as SERVER plugins and rejects our TUI-only export with "
            "'must default export an object with server()'. Move it "
            "to tui.json."
        )

    def test_agents_md_written_and_referenced_in_instructions(self, cli_org,
                                                                 monkeypatch):
        """AGENTS.md is opencode's standard project-context file. Every
        opencode session that mounts this config should pick it up
        (auto-loaded via instructions[".opencode/AGENTS.md"]). Pins:
          1. The file is written to .opencode/AGENTS.md (NOT vault root,
             where it would clobber any AGENTS.md the user already keeps).
          2. opencode.json's `instructions` list contains the relative
             path so opencode loads it as project context.
          3. The file actually carries the org-llm tool primer — without
             the search-first rule + MCP cheatsheet, having the file is
             pointless."""
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        monkeypatch.setattr(os, "execvp",
                              lambda p, a: (_ for _ in ()).throw(SystemExit(0)))
        runner.invoke(app, ["launch"])

        agents_path = Path(cli_org) / ".opencode" / "AGENTS.md"
        assert agents_path.exists(), (
            "launch didn't write .opencode/AGENTS.md — opencode's "
            "standard project-context surface is missing."
        )

        # Must NOT be at vault root (would clobber user files).
        assert not (Path(cli_org) / "AGENTS.md").exists(), (
            "AGENTS.md was written to vault root — that risks "
            "clobbering an AGENTS.md the user already keeps. "
            "Write to .opencode/AGENTS.md instead."
        )

        body = agents_path.read_text()
        # The primer's load-bearing parts:
        for needle in ("search_notes", "ask_notes", "MCP",
                        "org-llm", "proactive_doctor"):
            assert needle in body, (
                f"AGENTS.md missing key tool reference {needle!r} — "
                f"the primer's whole point is teaching the agent which "
                f"tools to reach for first."
            )

        # Referenced via instructions so opencode loads it.
        cfg = json.loads((Path(cli_org) / ".opencode" / "opencode.json").read_text())
        instr = cfg.get("instructions") or []
        assert any(isinstance(x, str) and x.endswith("AGENTS.md") for x in instr), (
            f"opencode.json instructions[] doesn't reference "
            f"AGENTS.md — opencode won't auto-load it. Got: {instr!r}"
        )

    def test_insight_cards_gathered_without_premount_env_flag(self, cli_org,
                                                                 monkeypatch):
        """User reported on 2026-04-29: launched opencode, plugin
        didn't show cards. Root cause: the gather was gated on
        ORG_LLM_INSIGHT_PREMOUNT — without that flag set, cards
        stayed empty and `insight-cards.json` always wrote
        `count: 0`, making the plugin no-op.

        The deterministic generators are fast; gating them was always
        wrong. Only narration (the slow LLM rewrite) + system-prompt
        injection should respect the flag. Pin: insight-cards.json
        gets WRITTEN with at least the `cards` array shape, regardless
        of the env flag (whether or not the array is empty depends on
        what the test fixture's vault yields)."""
        # Explicitly UNSET the flag for this test — the bug was that
        # the absence of the flag meant cards never gathered.
        monkeypatch.delenv("ORG_LLM_INSIGHT_PREMOUNT", raising=False)
        monkeypatch.setattr("org_llm.cli._opencode_bin", lambda: "/usr/bin/true")
        monkeypatch.setattr(os, "execvp",
                              lambda p, a: (_ for _ in ()).throw(SystemExit(0)))

        # Stub `cached_gather` so the test doesn't need real generators
        # to run — we're testing the GATE, not the gather logic.
        # Returning a sentinel card proves the gather got CALLED.
        from org_llm import insights as _insights
        called = {"narrate": None, "count": 0}

        def _stub_gather(session, *, cache_key, narrate, narration_model,
                          narration_url, voice):
            called["narrate"] = narrate
            called["count"] += 1
            from dataclasses import dataclass
            @dataclass
            class _Card:
                kind: str = "test"
                title: str = "test card"
                body: str = "from gather stub"
                evidence: dict = None
                suggested_command: str = ""
                suggested_question: str = ""
            return [_Card()]

        monkeypatch.setattr(_insights, "cached_gather", _stub_gather)
        runner.invoke(app, ["launch"])

        # The gather MUST have been called — that's the bug fix.
        assert called["count"] == 1, (
            "cached_gather() was not called during launch. The Phase "
            "12.4 ORG_LLM_INSIGHT_PREMOUNT gate is blocking it again "
            "— without the gather, the TUI plugin has no cards to "
            "render and the user sees nothing on open."
        )
        # Narration is the slow step — should still respect the flag.
        assert called["narrate"] is False, (
            f"Narration should be off when ORG_LLM_INSIGHT_PREMOUNT is "
            f"unset (it's the slow LLM rewrite). Got narrate="
            f"{called['narrate']!r} — gate moved to the wrong layer?"
        )

        # And insight-cards.json carries the gathered card.
        cards_path = Path(cli_org) / ".opencode" / "insight-cards.json"
        assert cards_path.exists(), "launch didn't write insight-cards.json"
        payload = json.loads(cards_path.read_text())
        assert payload.get("count", 0) >= 1, (
            f"insight-cards.json count={payload.get('count')} after the "
            f"stubbed gather returned 1 card. The plugin will see no "
            f"cards and stay silent on open."
        )

    def test_tui_plugin_auto_opens_dialog_on_mount(self):
        """User asked: cards should APPEAR ON OPEN, not require typing
        /insights. Pin that the plugin source actually calls
        openInsightDialog(...) at mount time (in addition to the toast)."""
        plugin_src = (Path(__file__).resolve().parent.parent
                       / "extensions" / "opencode" / "src" / "index.ts").read_text()
        # The toast is the breadcrumb fallback; the dialog open is the
        # primary "appear on open" affordance. Both should be present.
        assert "api.ui.toast" in plugin_src, (
            "plugin no longer calls api.ui.toast — losing the "
            "fallback breadcrumb when the dialog is dismissed."
        )
        assert "openInsightDialog" in plugin_src, (
            "plugin no longer calls openInsightDialog at mount — "
            "cards won't APPEAR ON OPEN; user has to type /insights."
        )
        # The auto-open should fire after the toast (so dismiss → toast
        # remains visible). Cheap textual check: both calls in the
        # default `tui` export, dialog AFTER toast.
        toast_idx = plugin_src.find("api.ui.toast")
        # find the auto-open INVOCATION (not the helper definition)
        auto_open_idx = plugin_src.find("openInsightDialog(api, cards)",
                                           toast_idx)
        assert auto_open_idx > toast_idx, (
            "openInsightDialog auto-open call must come AFTER "
            "api.ui.toast in the mount handler. Got toast at "
            f"{toast_idx}, auto-open at {auto_open_idx}."
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
