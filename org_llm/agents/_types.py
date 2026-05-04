"""Agent dataclass.

Phase 23.1 shipped the structural core (birth_name + aliases +
origin/pack/addressable). Phase 23.2-23.4 added the framework
slots:

* `capabilities` — tuple of capability strings the agent has
  declared. Enforcement at the MCP-dispatch layer is deferred
  (the proxy / opencode bridge doesn't currently pass the
  invoking agent's identity through to MCP tool calls). Until
  that signal is wired, capability declarations are advisory —
  they document intent and inform `list_agents` / hygiene
  tooling.

* `recipes` — tuple of recipe names the agent owns. Currently
  documentation-only; `orchestration._RECIPES` remains the
  active matcher. A future PR rewires the matcher to compose
  the global list from `get_builtins()`.

* `hygiene_scan` — optional callable returning a list of
  Suggestion rows for `org-llm hygiene`. No scans are
  implemented yet; the slot exists so they can land
  per-agent without further dataclass churn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing      import Callable, Literal, Optional


Origin = Literal["builtin", "library", "user"]


# Canonical capability set — agents declare which they need.
# Match the table in docs/wiki/agent-framework.org.
CAPABILITY_READ_VAULT       = "read.vault"
CAPABILITY_WRITE_VAULT      = "write.vault"
CAPABILITY_READ_CONFIG      = "read.config"
CAPABILITY_WRITE_CONFIG     = "write.config"
CAPABILITY_SHELL            = "shell"
CAPABILITY_EXTERNAL_NETWORK = "external.network"

ALL_CAPABILITIES: frozenset[str] = frozenset({
    CAPABILITY_READ_VAULT,
    CAPABILITY_WRITE_VAULT,
    CAPABILITY_READ_CONFIG,
    CAPABILITY_WRITE_CONFIG,
    CAPABILITY_SHELL,
    CAPABILITY_EXTERNAL_NETWORK,
})


@dataclass(frozen=True)
class Agent:
    """One specialist persona.

    Frozen so callers can use Agents as dict keys / hash them and
    so accidental mutation of the built-in list is impossible.
    Use `dataclasses.replace(agent, …)` to derive a tweaked copy.
    """

    birth_name:   str
    description:  str
    persona:      str
    model_role:   str             = "chat_model"
    aliases:      tuple[str, ...] = ()
    triggers:     tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    recipes:      tuple[str, ...] = ()
    hygiene_scan: Optional[Callable[[], list]] = None
    origin:       Origin          = "builtin"
    pack:         str             = "starfleet-core"
    addressable:  bool            = True

    def all_names(self) -> tuple[str, ...]:
        """Birth-name plus every alias — every `@<name>` that
        should route to this agent."""
        return (self.birth_name, *self.aliases)

    def has_capability(self, cap: str) -> bool:
        """True when the agent has declared `cap`. Used by
        documentation/listing today; will gate MCP tool dispatch
        once the agent-identity signal reaches the MCP layer."""
        return cap in self.capabilities

    def to_legacy_dict(self) -> dict[str, str]:
        """Adapter for callers that still expect the old
        `{description, model_role, prompt}` shape (e.g. the
        opencode.json launcher block, the `agents --tangle`
        verb, the `delegate` MCP tool). Drops the new fields the
        legacy callers don't know about."""
        return {
            "description": self.description,
            "model_role":  self.model_role,
            "prompt":      self.persona,
        }
