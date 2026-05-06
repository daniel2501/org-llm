"""Agent orientation primers — deterministic resolvers that return
the conventions a specialist needs for a given domain.

Pattern canonized 2026-05-04 in ``docs/wiki/agent-orientation.org``;
policy in ``docs/wiki/lazy-loading.org``. Each primer reads from
authoritative source files at call time (always-fresh rule); no
LLM is involved.

Public API::

    from org_llm.primers import primer, list_namespaces, manifest

    primer("wiki-authorship")    # -> str
    primer("dev-tracker-entry")  # -> str
    primer("agent-report")       # -> str
    primer("new-agent")          # -> str
    list_namespaces()            # -> list[str]
    manifest()                   # -> str  (the spawn-prompt block)

Adding a primer: drop a module in this package with a
``def render() -> str`` and register it in ``_REGISTRY`` below.
"""

from __future__ import annotations

from . import (
    agent_report,
    dev_tracker_entry,
    new_agent,
    wiki_authorship,
)

_REGISTRY = {
    "wiki-authorship": wiki_authorship.render,
    "agent-report": agent_report.render,
    "dev-tracker-entry": dev_tracker_entry.render,
    "new-agent": new_agent.render,
}


def primer(namespace: str) -> str:
    """Return the primer for the given namespace.

    Raises ``KeyError`` listing valid namespaces if unknown.
    """
    try:
        return _REGISTRY[namespace]()
    except KeyError:
        valid = ", ".join(sorted(_REGISTRY))
        raise KeyError(
            f"Unknown primer namespace {namespace!r}. "
            f"Valid namespaces: {valid}"
        ) from None


def list_namespaces() -> list[str]:
    """Return the sorted list of registered primer namespaces."""
    return sorted(_REGISTRY)


def manifest() -> str:
    """The spawn-prompt manifest block.

    Sized to fit in ~10–15 lines per the
    ``docs/wiki/agent-orientation.org`` § /The manifest/ contract.
    Names each core agent and its primer namespace(s); inlines
    the always-on rule (Rule 2b). Stable across sessions — only
    changes when a steward or namespace is added/removed.
    """
    return _MANIFEST


_MANIFEST = """\
ORG-LLM AGENT MANIFEST (lazy primers — fetch when relevant)

- @atoz owns wiki conventions. Call primer("wiki-authorship")
  before writing any docs/wiki/*.org page; primer("agent-report")
  before writing a docs/notes/ post-action report.
  Always-on: Rule 2b — every wiki concept leads with *Summary.*
  + *Expanded.*
- @riker owns dev-tracker.org. Call primer("dev-tracker-entry")
  before adding/editing a phase entry.
- @agentsmith owns new-agent shape. Call primer("new-agent")
  before drafting persona/triggers/capabilities.

Manifest is awareness, not authority. Fetch the primer for full
rules, file paths, and templates."""
