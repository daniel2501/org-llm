"""Agent registry — Phase 23.1 dataclass refactor.

The single source of truth for org-llm's bundled agent personas.
The legacy `_PRECONFIGURED_AGENT_PROMPTS` and `_AGENT_TRIGGERS`
dicts in `cli.py` are now thin adapters over the Agent list
defined here.

Three concepts:

* **birth_name** — canonical identifier. What `crew_log` records,
  what code references, what the user types `@<name>` against.
  For starfleet-core agents it's a Trek crew name (Picard, Spock,
  …); for legacy agents it stays functional (`gardener`,
  `analyst`, …) until they migrate.

* **aliases** — additional `@<name>`s that route to the same
  agent. `@crew` and `@captain` both reach Picard. The launcher
  writes one opencode.json entry per (birth_name + alias) so
  opencode autocomplete shows them.

* **pack** — `"starfleet-core"` for the OOB nine; `"legacy-extras"`
  for personas slated to migrate to the (future) agent library.
  The launcher filters by pack via the `agents_include_legacy`
  knob (default `false` → only core registers).

See docs/wiki/agent-framework.org for the broader design.
"""

from __future__ import annotations

from ._types    import Agent
from ._builtins import BUILTIN_AGENTS, get_builtins, resolve_alias


def _csv(s: str) -> tuple[str, ...]:
    """Split a comma-separated DB column to a clean tuple."""
    if not s:
        return ()
    return tuple(part.strip() for part in s.split(",") if part.strip())


def agent_from_row(row) -> Agent:
    """Build an Agent from a `db.AgentRow`. Layer-2 of the
    three-tier resolution (Python builtins → DB rows → org file).
    `hygiene_scan` stays None because Callables don't round-trip
    through SQLite — Python-side registration owns that slot.
    """
    return Agent(
        birth_name=row.birth_name,
        description=row.description or "",
        persona=row.persona or "",
        model_role=row.model_role or "chat_model",
        aliases=_csv(row.aliases or ""),
        triggers=_csv(row.triggers or ""),
        capabilities=_csv(row.capabilities or ""),
        recipes=_csv(row.recipes or ""),
        origin=row.origin or "builtin",
        pack=row.pack or "starfleet-core",
        addressable=bool(row.addressable),
    )


def agent_to_row_kwargs(agent: Agent) -> dict:
    """Inverse of `agent_from_row` — returns kwargs suitable for
    `db.AgentRow(**kwargs)` or for an `update()`. Use when seeding
    the table from `get_builtins()`.

    `updated_at` is set to LOCAL time, not UTC, so it can be
    compared directly to a file's mtime (also local-time epoch
    seconds) by `_maybe_auto_apply_org_file`. UTC would make
    auto-apply fire / not fire incorrectly across timezones.
    """
    import datetime as _dt
    return {
        "birth_name":   agent.birth_name,
        "description":  agent.description,
        "persona":      agent.persona,
        "model_role":   agent.model_role,
        "aliases":      ",".join(agent.aliases),
        "triggers":     ",".join(agent.triggers),
        "capabilities": ",".join(agent.capabilities),
        "recipes":      ",".join(agent.recipes),
        "origin":       agent.origin,
        "pack":         agent.pack,
        "addressable":  1 if agent.addressable else 0,
        "enabled":      1,
        "updated_at":   _dt.datetime.now().isoformat(timespec="seconds"),
    }


__all__ = [
    "Agent", "BUILTIN_AGENTS",
    "get_builtins", "resolve_alias",
    "agent_from_row", "agent_to_row_kwargs",
]
