"""Escalation-loop confusion signal — Phase 24.3 — confusion detector.

When the conversation alternates agent → user → agent → user
twice in a row without a tool call or a substantive narration
change in between, the agent isn't making progress on its own
and the user is being asked to babysit. That's a reasoning-shape
confusion deterministic code can't unstick — escalate to the
user with the framework's "we're going in circles" surface so
they can intervene with new context or change agents.

Action: ESCALATE_TO_USER — the framework's recovery surface for
the user, not the LLM. Distinct from recovery hooks' ESCALATE
(which surfaces a hint to the LLM): the LLM is the thing that's
stuck here.

Why ≥2 cycles: one cycle (agent asks → user answers → agent
proceeds) is normal collaboration. Two cycles with no tool call
or new-information narration means the agent's questions aren't
helping and we should hand the wheel back to the user
explicitly rather than letting the chat drift.
"""
from __future__ import annotations

from . import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    register,
)


_NAME = "escalation_loop"
_MIN_CYCLES = 2  # agent→user→agent→user→agent→user = 2 full cycles


def _is_agent_to_user(ev: CrewLogEvent) -> bool:
    """Agent asking the user something. Matches multiple producer
    conventions: explicit `action="escalate"`, an `agent_to="user"`
    target, or a `narration` from an agent that ended with a
    question shape ("?"). Free-form on purpose — the producers
    don't yet agree on a canonical action name (the v0.1 wiring
    sprint will pick one).
    """
    if ev.action in ("escalate", "ask_user", "question"):
        return True
    if ev.agent_to == "user" and ev.agent_from and ev.agent_from != "user":
        return True
    if (ev.action == "narration"
            and ev.agent_from
            and ev.agent_from != "user"
            and ev.result.rstrip().endswith("?")):
        return True
    return False


def _is_user_to_agent(ev: CrewLogEvent) -> bool:
    """User replying to / prompting an agent."""
    if ev.action in ("user_reply", "user_prompt"):
        return True
    if ev.agent_from == "user":
        return True
    return False


def _is_progress(ev: CrewLogEvent) -> bool:
    """Anything that means "we're making progress, not just
    talking past each other": a tool call, a successful delegate,
    a non-question narration from an agent. The escalation-loop
    signal RESETS on any of these — we only fire when N cycles
    happen without any progress in between.
    """
    if ev.tool:
        return True
    if ev.action == "tool_call":
        return True
    if ev.action == "delegate" and ev.outcome == "ok":
        return True
    if (ev.action == "narration"
            and ev.agent_from
            and ev.agent_from != "user"
            and not ev.result.rstrip().endswith("?")):
        return True
    return False


def _matches(window: list[CrewLogEvent]) -> bool:
    # Walk the window forward, counting alternating
    # agent→user / user→agent transitions. Reset the counter on
    # any progress event. Need _MIN_CYCLES * 2 transitions
    # (a full cycle is two — agent→user, user→agent).
    needed = _MIN_CYCLES * 2
    transitions = 0
    expecting = "agent_to_user"  # first transition we look for
    for ev in window:
        if _is_progress(ev):
            transitions = 0
            expecting = "agent_to_user"
            continue
        if expecting == "agent_to_user" and _is_agent_to_user(ev):
            transitions += 1
            expecting = "user_to_agent"
            if transitions >= needed:
                return True
            continue
        if expecting == "user_to_agent" and _is_user_to_agent(ev):
            transitions += 1
            expecting = "agent_to_user"
            if transitions >= needed:
                return True
            continue
        # Event of an unexpected shape — neither progress nor an
        # alternation step. Don't reset (might just be a manager
        # bookkeeping row); just skip.
    return transitions >= needed


def _signal(window: list[CrewLogEvent]) -> ConfusionAction:
    return ConfusionAction.ESCALATE_TO_USER


SIGNAL = ConfusionSignal(name=_NAME, matches=_matches, signal=_signal)
register(SIGNAL)


__all__ = ["SIGNAL"]
# end
