"""Phase 23.6 — literate MCP tools — extension path leg 2.

Reads a literate org file (default ``~/org/org-llm-tools.org``,
overridable via ``ORG_LLM_LITERATE_TOOLS_PATH``) and turns each
top-level heading into a runnable Python callable that the MCP
server can register.

Public API::

    from org_llm.literate_mcp import load_literate_tools

    tools = load_literate_tools()        # default path
    for t in tools:
        print(t.name, t.description)
        print(t.callable("hello world"))

The ``register_literate_tools(server, ...)`` helper wires every
loaded tool into a FastMCP server via ``@server.tool()`` — but
the v0.1 MCP-server integration is deferred (see
``docs/wiki/literate-tools.org`` § Tier 3c). For now this package
loads + compiles + tests cleanly standalone; wiring is a follow-up.

See ``compiler.py`` module docstring for the safety boundary.
See ``parser.py`` module docstring for the file-format spec.
"""
from __future__ import annotations

from pathlib import Path

from .compiler import (
    CompileError,
    LiterateTool,
    compile_all,
    compile_tool,
)
from .parser import (
    ParsedTool,
    literate_tools_path,
    parse_file,
    parse_text,
)


__all__ = (
    "CompileError",
    "LiterateTool",
    "ParsedTool",
    "compile_all",
    "compile_tool",
    "literate_tools_path",
    "load_literate_tools",
    "parse_file",
    "parse_text",
    "register_literate_tools",
)


def load_literate_tools(path: Path | None = None, *,
                         dangerous_enabled: bool = False,
                         ) -> list[LiterateTool]:
    """Read + parse + compile the literate-tools file.

    Returns the list of compiled tools. Tools that fail to compile
    are silently skipped — callers wanting error visibility should
    use :func:`parse_file` + :func:`compile_all` directly. This
    matches the literate-config posture: a malformed user-file is
    a soft failure, never a startup crash.
    """
    target = path or literate_tools_path()
    parsed = parse_file(target)
    ok, _bad = compile_all(parsed, dangerous_enabled=dangerous_enabled)
    return ok


def register_literate_tools(server, path: Path | None = None, *,
                              dangerous_enabled: bool = False,
                              ) -> int:
    """Register every literate tool with a FastMCP server.

    Returns the count of tools registered. Wires each compiled
    callable through ``@server.tool()`` so the MCP client (opencode,
    Claude Code, Pi) sees them alongside the host's built-in tools.

    Deferred to v0.1: actually calling this from
    ``mcp_server.create_mcp_server``. Today, this helper exists for
    standalone tests + future wiring; the parent agent's Phase 24.2
    work owns ``mcp_server.py`` and we don't touch it yet.
    """
    tools = load_literate_tools(path,
                                  dangerous_enabled=dangerous_enabled)
    n = 0
    for t in tools:
        try:
            server.tool()(t.callable)
            n += 1
        except Exception:
            # Best-effort: a single bad registration shouldn't take
            # down the whole startup. The host's existing rescue
            # wrapper around server.tool will surface a structured
            # error if the tool ever runs.
            continue
    return n
