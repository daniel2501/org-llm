"""Confusion-signal registry — Phase 24.3 — confusion detector.

The third layer of the supervision trinity (DEC-006 — supervision
is deterministic by default). Pre-flight resolvers (Phase 24.1 —
pre-flight resolvers) fire *before* a tool call; tool-failure
recovery hooks (Phase 24.2 — recovery hooks) fire *at the moment*
a tool call raises; the confusion detector watches *across* turns
in the captain's-log event stream and decides — deterministically
— whether the agent has lost the plot.

Motivating bug (2026-05-04): an agent looped on a missing path,
called the same tool with the same args three times, returned an
empty narration, and gave up — none of which any single-tool-call
hook can see. The detector closes that gap by inspecting a
*window* of recent =crew_log= events.

The orchestration top priority — /smoother + faster + higher-
quality than solo/ — fails the moment a confusion loop wastes
the user's turn silently. Confusion signals turn those into
deterministic local interrupts: the framework can short-circuit
a runaway loop, retry once on an empty response, or escalate
to the user without a costly LLM-judge round-trip.

This module ships the registry + signal type + dispatch only.
The integration wiring (running =detect_confusion()= against the
live =crew_log= ring buffer at MCP tool-call sites in
=mcp_server.py= / =logbook.py=) is the v0.1 follow-up sprint —
deferred to dodge conflicts with the in-flight recovery-hook
sweep editing those files.

Adding a new signal: write a `ConfusionSignal` in a new module
under this package and register it via `register()` (or append
to `_DEFAULT_SIGNALS` below). Order matters — the first signal
whose `matches(window)` returns True wins. No core changes.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing      import Any, Callable, Iterable


# ── public types ────────────────────────────────────────────────


class ConfusionAction(enum.Enum):
    """What the framework should do after a confusion signal trips.

    `OK`                 — no confusion detected; let the turn
                            proceed unchanged. The default when
                            no signal matches.
    `RETRY`              — re-run the last action once (e.g. an
                            empty-response retry). The framework
                            owns the attempt cap; signals just
                            request a single retry.
    `INTERRUPT`          — stop the current loop and inject a
                            ~MANAGER NOTE~ into the agent's next
                            turn so it changes strategy. Used for
                            tool-loops where the agent will
                            otherwise keep calling the same thing.
    `ESCALATE_TO_USER`   — the framework cannot resolve this from
                            code; surface the situation to the
                            user (e.g. an agent ↔ user ping-pong
                            with no progress). Mirrors recovery's
                            ESCALATE but addresses the user, not
                            the LLM.
    """
    OK               = "ok"
    RETRY            = "retry"
    INTERRUPT        = "interrupt"
    ESCALATE_TO_USER = "escalate_to_user"


@dataclass(frozen=True)
class CrewLogEvent:
    """One row from the captain's-log event stream the detector
    reads.

    This is the *minimum* shape the signal modules need. The v0.1
    wiring sprint will adapt the live source — either =crew_log=
    DB rows (=org_llm.db.CrewLog=) or an in-memory ring buffer in
    =logbook.py= — into this dataclass. Keeping the contract local
    to this package means the detector unit-tests don't need a DB
    fixture, and a future schema change to =crew_log= doesn't
    cascade into signal modules.

    Fields mirror =CrewLog= columns we care about:

    `timestamp`   — ISO-8601 UTC, used for time-window arithmetic
    `session_id`  — groups events from one user turn / chat session
    `action`      — what the manager did: =delegate=, =tool_call=,
                    =narration=, =user_reply=, =escalate=, etc.
                    Free-form string; signals match on it.
    `agent_from`  — who initiated (=crew=, =user=, an agent name)
    `agent_to`    — target (an agent name, a tool name, =user=)
    `tool`        — name of the tool called, if applicable. May be
                    empty for non-tool actions.
    `args`        — structured args of a tool call. Hashable-like
                    (signals may stringify for similarity checks).
    `outcome`     — =ok= / =empty= / =timeout= / =error= / =rejected=
    `result`      — short result excerpt; signals may inspect for
                    emptiness or hedging.
    """
    timestamp:  str
    session_id: str             = ""
    action:     str             = ""
    agent_from: str             = ""
    agent_to:   str             = ""
    tool:       str             = ""
    args:       dict[str, Any]  = field(default_factory=dict)
    outcome:    str             = "ok"
    result:     str             = ""


@dataclass(frozen=True)
class ConfusionSignal:
    """One confusion-detection rule.

    `name`     — short tag for logs + dispatch ("loop",
                 "escalation_loop", "empty_response")
    `matches`  — `(window: list[CrewLogEvent]) -> bool`. Cheap,
                 deterministic, must not raise. The first
                 matcher to return True wins. Signals should
                 match narrowly: explicit window-shape conditions,
                 not heuristics that fire on every turn.
    `signal`   — `(window: list[CrewLogEvent]) -> ConfusionAction`.
                 Returns the action the framework should take
                 when this signal trips. Must be deterministic
                 given the window — same input → same action —
                 so the dispatcher's `detect_confusion()` lookup
                 is reproducible.
    """
    name:    str
    matches: Callable[[list[CrewLogEvent]], bool]
    signal:  Callable[[list[CrewLogEvent]], ConfusionAction]


# ── registry ────────────────────────────────────────────────────


_REGISTERED: list[ConfusionSignal] = []


def register(sig: ConfusionSignal) -> ConfusionSignal:
    """Register a signal. Idempotent on (name): a re-registration
    replaces the prior entry in place so its position in the
    matcher chain is preserved (load-order is the chain order).
    """
    for i, existing in enumerate(_REGISTERED):
        if existing.name == sig.name:
            _REGISTERED[i] = sig
            return sig
    _REGISTERED.append(sig)
    return sig


def unregister(name: str) -> bool:
    """Remove a signal by name. Returns True if removed. Tests
    use this between fixtures to keep the registry clean."""
    for i, s in enumerate(_REGISTERED):
        if s.name == name:
            del _REGISTERED[i]
            return True
    return False


def registered_signals() -> list[ConfusionSignal]:
    """Snapshot of the current registry (in match-order). Mainly
    for tests + introspection."""
    return list(_REGISTERED)


def detect_confusion(
    window:  list[CrewLogEvent],
    *,
    signals: Iterable[ConfusionSignal] | None = None,
) -> ConfusionAction:
    """Find the first registered signal that matches `window` and
    return its `signal()` action. If no signal matches, return
    `OK` so the framework lets the turn proceed unchanged.

    Sequential (not parallel) — order is meaningful, and matchers
    must be cheap by contract. A signal that raises during
    `matches` or `signal` is treated as non-matching; the search
    continues. The supervision rule says supervision can never
    tax the user's turn — that includes its own failure modes.
    """
    chain = list(signals) if signals is not None else _REGISTERED
    for sig in chain:
        try:
            if not sig.matches(window):
                continue
        except Exception:
            continue
        try:
            return sig.signal(window)
        except Exception:
            # A misbehaving signal must not eat the turn — fall
            # through and let the next one (or OK) decide.
            continue
    return ConfusionAction.OK


# ── eager-import builtin signal modules ─────────────────────────

# Each module registers its signal(s) at import-time via
# `register()`. We import them here so that `from
# org_llm.confusion import detect_confusion` is enough to get
# the default chain. Order matters: the chain is matched
# first-to-last, so put narrow / specific matchers ahead of
# broad ones.
def _load_builtins() -> None:
    from . import loop            as _loop             # noqa: F401
    from . import escalation_loop as _escalation_loop  # noqa: F401
    from . import empty_response  as _empty_response   # noqa: F401


_load_builtins()


__all__ = [
    "ConfusionAction",
    "ConfusionSignal",
    "CrewLogEvent",
    "register",
    "unregister",
    "registered_signals",
    "detect_confusion",
]
# end
