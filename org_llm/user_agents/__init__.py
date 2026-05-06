"""Phase 23.5 — user-supplied agents library — extension path leg 1.

Reads a literate org file (default ``~/org/org-llm-agents.org``,
overridable via ``ORG_LLM_USER_AGENTS_PATH``) and turns each
top-level :agent:-tagged heading into a structured ``UserAgent``
record the registry-wiring layer can register at startup.

Public API::

    from org_llm.user_agents import load_user_agents

    agents = load_user_agents()         # default path
    for a in agents:
        print(a.handle, a.description)
        print(a.system_prompt)

The ``register_user_agents(registry, ...)`` helper registers every
loaded user-agent into the live agents registry — but the v0.1
registry-wiring is deferred. For now this package loads + parses +
validates cleanly standalone; wiring is a follow-up.

Sister surface to :mod:`org_llm.literate_mcp` (Phase 23.6 — literate
MCP tools): same parser/validator/round-trip shape applied to the
agent surface instead of the tool surface. See
``docs/wiki/user-supplied-agents-library.org`` for the design page,
``docs/wiki/decisions.org`` § DEC-009 for the curated-core +
extension-path decision this leg implements.

Sandbox boundary (see ``validator.py`` for the full set):

  • User-defined agents may NOT shadow a Bridge Crew handle (per
    DEC-014 — Bridge Crew). The seven canonical handles + their
    documented aliases are reserved.
  • System prompts may not contain raw Python ``exec`` / ``eval``
    constructs (defence in depth — the system prompt is a string,
    but a careless user pasting a code-shaped instruction shouldn't
    end up routed into anything ``exec``-y downstream).
  • User-defined agents are read-only on the registry — they
    register *new* personas; they don't mutate built-ins.

The package is self-contained: parser + validator + dataclasses.
The registry-wiring step (loading at startup, dedup against DB
rows, rendering into ``opencode.json``) is intentionally NOT here
— that's the v0.1 follow-up and lives in ``cli.py`` next to the
existing ``_apply_org_file_to_db`` flow.
"""
from __future__ import annotations

from pathlib import Path

from .parser import (
    ParsedUserAgent,
    parse_file,
    parse_text,
    user_agents_path,
)
from .validator import (
    BRIDGE_CREW_RESERVED,
    UserAgent,
    UserAgentError,
    validate_all,
    validate_one,
)


__all__ = (
    "BRIDGE_CREW_RESERVED",
    "ParsedUserAgent",
    "UserAgent",
    "UserAgentError",
    "load_user_agents",
    "parse_file",
    "parse_text",
    "register_user_agents",
    "user_agents_path",
    "validate_all",
    "validate_one",
)


def load_user_agents(path: Path | None = None) -> list[UserAgent]:
    """Read + parse + validate the user-agents file.

    Returns the list of validated user-agents. Personas that fail
    validation are silently skipped — callers wanting error
    visibility should use :func:`parse_file` + :func:`validate_all`
    directly. This matches the literate-config posture: a malformed
    user-file is a soft failure, never a startup crash.
    """
    target = path or user_agents_path()
    parsed = parse_file(target)
    ok, _bad = validate_all(parsed)
    return ok


def register_user_agents(registry, path: Path | None = None) -> int:
    """Register every user-supplied agent into the live registry.

    Returns the count of agents registered. Wires each validated
    user-agent through ``registry.add(agent)`` so the launcher,
    proxy, and MCP layer see them alongside the built-in roster.

    Deferred to v0.1: actually calling this from
    ``cli._resolve_active_agents`` (the existing flow already handles
    a tangled mirror of ``~/org/org-llm-agents.org`` via
    ``_apply_org_file_to_db``). Today, this helper exists for
    standalone tests + the future wiring point; reconciliation
    against the existing tangled-mirror flow is the v0.1 task.
    """
    agents = load_user_agents(path)
    n = 0
    for a in agents:
        try:
            registry.add(a)
            n += 1
        except Exception:
            # Best-effort: a single bad registration shouldn't take
            # down the whole startup. Errors surface through whatever
            # diagnostic the caller's registry exposes.
            continue
    return n
