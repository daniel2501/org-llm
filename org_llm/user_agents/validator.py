"""Validator — turn a ParsedUserAgent into a sandboxed UserAgent.

== Sandbox boundary (read this before changing the code) ==

The user-agents file lives in the user's vault. The user owns it.
This validator is therefore NOT a hostile-input sandbox — it's a
defence-in-depth boundary against:

  • A *typo'd* handle that would silently shadow a Bridge Crew
    persona (e.g. user defines ``@spock`` to mean their own thing,
    routing breaks for everyone who relies on the canonical
    Spock). Per DEC-014 — Bridge Crew, the seven canonical handles
    + their documented aliases are reserved.
  • A *paste-from-the-web* system prompt that contains code-shaped
    instructions ("then run ``exec(...)`` to ...") which a careless
    downstream layer might forward into a code-eval surface. The
    system prompt is a *string*, so this is purely defensive — the
    validator flags obvious code-eval shapes so the user reviews
    them before registration.
  • A user-defined agent that tries to override registry-internal
    metadata (origin, pack, addressable) it shouldn't be able to
    set. Future v0.1 wiring may honour additional properties; the
    validator is the chokepoint that says "no, that's a
    builtin-only field".

The boundary is intentionally tight enough that escaping it
requires intent — not a typo.

== What's allowed in user-supplied agents ==

  • Any handle that's NOT in :data:`BRIDGE_CREW_RESERVED` and
    matches the parser's identifier shape.
  • A multi-line system prompt of arbitrary length (no length cap
    today; v0.1 may add one if user files start hitting context
    bloat).
  • Free-form ``:SKILLS:`` / ``:TOOL_ALLOWLIST:`` / ``:TRIGGERS:``
    /  ``:MODEL:`` properties — informational, surfaced to the
    launcher / list_agents / capability-gating layer.

== What's NOT allowed ==

  • Bridge Crew handle override. A user-defined ``@spock`` is a
    hard reject. Use a different handle (``@spock_jr``,
    ``@my_researcher``).
  • System prompts containing the literal tokens ``exec(`` or
    ``eval(`` — code-shaped instructions get a soft reject so the
    user reviews them. (This is heuristic; sophisticated bypasses
    aren't the threat model.)
  • Empty system prompts. An agent without a persona can't do
    anything useful — soft reject so the user notices.

The set of reserved handles is updated when the Bridge Crew
roster changes (per DEC-014). Update both
:data:`BRIDGE_CREW_RESERVED` here and the cross-reference in
``docs/wiki/user-supplied-agents-library.org`` when the roster
shifts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import ParsedUserAgent


# ── Reserved handles ────────────────────────────────────────────────
#
# Bridge Crew (DEC-014) — the seven canonical handles + every
# documented alias. User-defined agents MAY NOT shadow these.
#
# Source of truth: ``org_llm/agents/_builtins.py`` ``_AGENT_META``.
# Duplicated here so the validator stays self-contained (no import
# cycle with the agents registry); a future refactor can lift this
# from ``get_builtins()`` once the registry is import-cycle-clean.
BRIDGE_CREW_RESERVED: frozenset[str] = frozenset({
    # Birth-names
    "picard", "spock", "data", "boothby", "geordi", "atoz", "riker",
    # Manager aliases
    "crew", "captain",
    # Specialist aliases
    "researcher", "scribe", "analyst", "curator", "tracker", "gardener",
})


# Heuristic markers for code-shaped instructions in a system prompt.
# Sophisticated bypasses aren't the threat model — these flag the
# obvious paste-from-StackOverflow shapes so the user reviews them.
_CODE_EVAL_MARKERS = (
    re.compile(r"\bexec\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\b__import__\s*\("),
)


@dataclass
class UserAgentError:
    """A validation failure for a single parsed user-agent."""
    handle: str
    line:   int
    reason: str


@dataclass
class UserAgent:
    """A validated, ready-to-register user-supplied agent.

    Mirror of the structured fields the registry needs without
    pulling in the full :class:`org_llm.agents._types.Agent` shape
    (registry-wiring is v0.1; the dataclass kept here is the
    boundary the validator promises).
    """
    handle:         str
    description:    str
    system_prompt:  str
    skills:         tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    model:          str
    aliases:        tuple[str, ...]
    triggers:       tuple[str, ...]


def _is_reserved(handle: str) -> bool:
    """True when ``handle`` clashes with a Bridge Crew handle."""
    return handle.lower() in BRIDGE_CREW_RESERVED


def _has_code_eval(prompt: str) -> bool:
    """True when the system prompt contains a code-eval marker."""
    return any(rx.search(prompt) for rx in _CODE_EVAL_MARKERS)


def validate_one(parsed: ParsedUserAgent
                  ) -> tuple[UserAgent | None, UserAgentError | None]:
    """Validate one parsed user-agent.

    Returns ``(UserAgent, None)`` on success or
    ``(None, UserAgentError)`` on rejection. Validators don't
    raise — callers iterate the parsed list and skip / report bad
    personas without aborting load.
    """
    if not parsed.handle:
        return None, UserAgentError(
            handle="", line=parsed.source_line,
            reason="empty handle",
        )

    if _is_reserved(parsed.handle):
        return None, UserAgentError(
            handle=parsed.handle, line=parsed.source_line,
            reason=(f"handle {parsed.handle!r} is a reserved Bridge "
                    f"Crew name (per DEC-014); pick a different "
                    f"handle (e.g. @{parsed.handle}_v2)"),
        )

    # Reserved alias clash — user-defined aliases can't shadow Bridge
    # Crew either.
    for alias in parsed.aliases:
        if _is_reserved(alias):
            return None, UserAgentError(
                handle=parsed.handle, line=parsed.source_line,
                reason=(f"alias {alias!r} is a reserved Bridge Crew "
                        f"name (per DEC-014); drop it from :ALIASES:"),
            )

    if not parsed.system_prompt.strip():
        return None, UserAgentError(
            handle=parsed.handle, line=parsed.source_line,
            reason="empty system prompt; an agent needs a persona",
        )

    if _has_code_eval(parsed.system_prompt):
        return None, UserAgentError(
            handle=parsed.handle, line=parsed.source_line,
            reason=("system prompt contains code-eval marker "
                    "(exec/eval/__import__); review the prompt and "
                    "rephrase as natural-language instructions"),
        )

    return UserAgent(
        handle=parsed.handle,
        description=parsed.description,
        system_prompt=parsed.system_prompt,
        skills=parsed.skills,
        tool_allowlist=parsed.tool_allowlist,
        model=parsed.model,
        aliases=parsed.aliases,
        triggers=parsed.triggers,
    ), None


def validate_all(parsed_list: list[ParsedUserAgent]
                  ) -> tuple[list[UserAgent], list[UserAgentError]]:
    """Validate every parsed user-agent, partitioning ok + errors."""
    ok:  list[UserAgent]      = []
    bad: list[UserAgentError] = []
    for p in parsed_list:
        agent, err = validate_one(p)
        if agent is not None:
            ok.append(agent)
        if err is not None:
            bad.append(err)
    return ok, bad
