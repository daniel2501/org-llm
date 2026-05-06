"""Built-in Agent metadata + the joined registry.

The persona strings + triggers still live in `cli.py` as
`_PRECONFIGURED_AGENT_PROMPTS` / `_AGENT_TRIGGERS` (Phase 23.1
keeps that intact to avoid a 600-line transcription in this
PR). What this module adds is the *structured* metadata —
canonical Trek birth-names, alias lists, and pack/addressability
classification — joined with cli.py's content via
`get_builtins()`.

A future PR can lift the persona literals into this package and
make `cli.py` derive its dicts from here. The Agent shape stays
stable across that migration.

OOB cut decisions (locked 2026-05-03; Bridge Crew rename 2026-05-06):
  * starfleet-core agents — manager + specialists. Bridge Crew
    (curated 7 per DEC-014 — Bridge Crew): @picard, @spock, @data,
    @boothby, @geordi, @atoz, @riker. Other starfleet-core agents
    (engineer, planner, agenda, ops, agentsmith, doom) are core but
    not part of the Bridge Crew cap.
  * Other personas tagged `legacy-extras`. The launcher
    filters to core by default; `agents_include_legacy=true`
    re-enables them during the transition window.
  * Trek = canonical birth-name; functional name = alias.
  * No `tuvok` (classifier persona was already removed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing      import Iterable

from ._types import Agent


@dataclass(frozen=True)
class _AgentMeta:
    """Per-agent metadata layered on top of the legacy dict —
    everything the structured Agent shape needs that the legacy
    `{description, model_role, prompt}` triple doesn't carry.
    """
    birth_name:   str
    aliases:      tuple[str, ...] = ()
    pack:         str             = "starfleet-core"
    addressable:  bool            = True
    capabilities: tuple[str, ...] = ()
    recipes:      tuple[str, ...] = ()


# Capability shortcuts (kept here so the meta table fits on one
# screen). Definitions in _types.py.
_R   = "read.vault"
_W   = "write.vault"
_RC  = "read.config"
_WC  = "write.config"
_SH  = "shell"
_NET = "external.network"


# Keyed by the LEGACY agent name (the key in
# `_PRECONFIGURED_AGENT_PROMPTS`). `get_builtins()` joins this
# with the persona/trigger dicts to produce Agent instances.
#
# Order preserved: starfleet-core first, then legacy-extras in
# declaration order. `route_prompt` ties broken by declaration
# order, so put manager last (matches lots-fewer-trigger-hits
# semantics).
#
# `capabilities` are DECLARATIVE only today — enforcement at the
# MCP layer is deferred (Phase 23.2). They inform list_agents,
# documentation, and future gating; they do NOT currently
# prevent any tool call.
#
# `recipes` lists recipe NAMES owned by the agent. Currently
# documentation-only; orchestration._RECIPES is the active
# matcher (Phase 23.4 rewires that).
_AGENT_META: dict[str, _AgentMeta] = {
    # ── starfleet-core ───────────────────────────────────────
    "researcher": _AgentMeta(
        birth_name="spock", aliases=("researcher",),
        capabilities=(_R,),
        recipes=("count_across_vault",),
    ),
    "scribe":     _AgentMeta(
        birth_name="data", aliases=("scribe",),
        capabilities=(_R, _W),
    ),
    "engineer":   _AgentMeta(
        # Bridge Crew rename 2026-05-06: birth_name "geordi" reassigned to
        # @geordi (analyst) per DEC-014 — Bridge Crew. @engineer keeps a
        # functional birth_name; pack stays starfleet-core for routing.
        birth_name="engineer", aliases=("engineer",),
        capabilities=(_R, _W, _SH),
    ),
    "planner":    _AgentMeta(
        # Bridge Crew rename 2026-05-06: birth_name "riker" reassigned to
        # @riker (tracker) per DEC-014 — Bridge Crew. @planner keeps a
        # functional birth_name; pack stays starfleet-core for routing.
        birth_name="planner", aliases=("planner",),
        capabilities=(_R,),
    ),
    "agenda":     _AgentMeta(
        birth_name="janeway", aliases=("agenda",),
        capabilities=(_R, _NET),
        recipes=("weather_aware_agenda",),
    ),
    "ops":        _AgentMeta(
        birth_name="scotty", aliases=("ops",),
        capabilities=(_R, _RC, _WC, _SH),
    ),
    "agentsmith": _AgentMeta(
        birth_name="soong", aliases=("agentsmith",),
        capabilities=(_R, _W, _SH),
    ),
    "geordi":     _AgentMeta(
        # Lt. Cmdr. Geordi La Forge — TNG chief engineer; "sees across
        # spectrum w/ VISOR"; runs diagnostics. Fits dashboards/analytics.
        # Bridge Crew rename 2026-05-06: replaces @analyst (prior
        # legacy-extras birth_name `analyst`). Legacy alias `analyst`
        # retained for back-compat.
        birth_name="geordi", aliases=("analyst", "geordi"),
        capabilities=(_R,),
    ),
    "atoz":       _AgentMeta(
        # Mr. Atoz — librarian on Sarpeidon ("All Our Yesterdays",
        # TOS S3E23). The most-direct curator/archivist character
        # in canon. Bridge Crew rename 2026-05-06: birth-name promoted
        # to canonical handle; legacy alias `curator` retained.
        birth_name="atoz", aliases=("curator", "atoz"),
        capabilities=(_R, _W, _SH),
    ),
    "riker":      _AgentMeta(
        # Cmdr. William T. Riker — TNG first officer / XO. Owns the
        # duty-roster + ops; pairs with @picard the captain. Bridge
        # Crew rename 2026-05-06: replaces @tracker (prior birth-name
        # `boothby` reassigned to @boothby — Boothby is the gardener,
        # not a tracker). Legacy alias `tracker` retained for back-compat.
        birth_name="riker", aliases=("tracker", "riker"),
        capabilities=(_R, _W, _SH),
    ),
    "boothby":    _AgentMeta(
        # Boothby — Starfleet Academy groundskeeper (TNG/VOY,
        # recurring). Direct role-fit: Boothby IS the gardener in
        # Trek canon. Bridge Crew rename 2026-05-06: replaces
        # @gardener (prior birth-name `keiko` retired). Legacy alias
        # `gardener` retained for back-compat.
        birth_name="boothby", aliases=("gardener", "boothby"),
        capabilities=(_R, _SH),
    ),
    "crew":       _AgentMeta(
        birth_name="picard", aliases=("crew", "captain"),
        # Manager delegates; doesn't call vault tools directly.
        # Capabilities empty by design — capability declaration
        # is "what THIS agent calls", not "what specialists it
        # routes to call."
        capabilities=(),
    ),

    # ── legacy-extras (slated for library) ───────────────────
    "librarian":      _AgentMeta(birth_name="librarian",
                                   pack="legacy-extras",
                                   capabilities=(_R, _W)),
    "vision-analyst": _AgentMeta(birth_name="vision-analyst",
                                   pack="legacy-extras",
                                   capabilities=(_R,)),
    "reviewer":       _AgentMeta(birth_name="reviewer",
                                   pack="legacy-extras",
                                   capabilities=(_R,)),
    "translator":     _AgentMeta(birth_name="translator",
                                   pack="legacy-extras"),
    "doom":           _AgentMeta(birth_name="doom",
                                   capabilities=(_R, _SH)),
    "almanac":        _AgentMeta(birth_name="almanac",
                                   pack="legacy-extras",
                                   capabilities=(_R, _NET, _WC)),
    "journalist":     _AgentMeta(birth_name="journalist",
                                   pack="legacy-extras",
                                   capabilities=(_R,)),
}


def get_builtins() -> list[Agent]:
    """Join _AGENT_META with cli.py's persona + trigger dicts to
    produce Agent instances. Lazy import to avoid the circular
    cli.py ↔ agents import.

    Agents listed in `_AGENT_META` but missing from cli.py's
    dict are skipped silently — covers the case where someone
    deletes a persona without updating the metadata.
    """
    from ..cli import _PRECONFIGURED_AGENT_PROMPTS as _P
    from ..cli import _AGENT_TRIGGERS as _T
    out: list[Agent] = []
    for legacy_key, meta in _AGENT_META.items():
        d = _P.get(legacy_key)
        if not d:
            continue
        out.append(Agent(
            birth_name=meta.birth_name,
            aliases=meta.aliases,
            description=d.get("description", ""),
            persona=d.get("prompt", ""),
            model_role=d.get("model_role", "chat_model"),
            triggers=tuple(_T.get(legacy_key, ())),
            capabilities=meta.capabilities,
            recipes=meta.recipes,
            origin="builtin",
            pack=meta.pack,
            addressable=meta.addressable,
        ))
    return out


# Module-level convenience — re-derive on each access so test
# monkeypatching of cli.py's dicts is visible. Cheap (~17
# Agent constructions, no I/O).
def __getattr__(name: str):
    if name == "BUILTIN_AGENTS":
        return get_builtins()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def resolve_alias(typed: str,
                  agents: Iterable[Agent] | None = None) -> str | None:
    """Map any user-typed `@<name>` (birth_name OR alias) to the
    agent's canonical birth_name. Returns None when nothing
    matches.

    Used by the proxy's `intercept_agent_prefix` so downstream
    code (logging, sticky overlay, capability checks) sees one
    stable name regardless of which alias the user typed.
    """
    if not typed:
        return None
    pool = agents if agents is not None else get_builtins()
    typed_lc = typed.lower().strip()
    for a in pool:
        if a.birth_name.lower() == typed_lc:
            return a.birth_name
        for alias in a.aliases:
            if alias.lower() == typed_lc:
                return a.birth_name
    return None
