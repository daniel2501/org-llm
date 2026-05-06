"""Empty-response confusion signal — Phase 24.3 — confusion detector.

When the most recent agent narration is empty / whitespace-only
(or the matching =crew_log= row outcome is `empty`), the agent
returned nothing useful — silent failure, the worst confusion
shape because it's invisible to the user. First occurrence:
RETRY (transient empty completion is common with small local
models; one retry is cheap). Second occurrence in the window:
ESCALATE_TO_USER (the model is stuck in a degenerate mode and
won't recover by re-running the same prompt).

Action ladder: RETRY → ESCALATE_TO_USER. Mirrors the
=connection_error= recovery hook's exhaust-to-RAISE shape but
for a confusion (not an exception): one cheap retry, then hand
the wheel back to the user.

Why look at the most-recent narration only: a long session may
contain old empty responses that already got handled. The
signal answers "is *this* turn stuck?" — the framework calls
the detector after each narration, so the relevant evidence
is the latest one + count of recent ones.
"""
from __future__ import annotations

from . import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    register,
)


_NAME = "empty_response"
_RETRY_THRESHOLD    = 1   # one empty → retry
_ESCALATE_THRESHOLD = 2   # two empty in window → escalate


def _is_agent_narration(ev: CrewLogEvent) -> bool:
    """A narration / delegate result from an agent (not the user)."""
    if ev.action not in ("narration", "delegate"):
        return False
    if ev.agent_from == "user":
        return False
    return True


def _is_empty(ev: CrewLogEvent) -> bool:
    """Empty / whitespace-only result, OR the producer flagged
    `outcome="empty"` itself (which db.log_crew_action accepts as
    a first-class outcome value alongside ok/timeout/error)."""
    if ev.outcome == "empty":
        return True
    return not (ev.result or "").strip()


def _empty_narrations(window: list[CrewLogEvent]) -> list[CrewLogEvent]:
    return [ev for ev in window
            if _is_agent_narration(ev) and _is_empty(ev)]


def _matches(window: list[CrewLogEvent]) -> bool:
    # The most-recent narration must itself be empty for the
    # signal to be relevant. We match if there's at least one
    # empty narration AND the most-recent narration in the
    # window is the empty one. That gates the signal on "right
    # now is stuck" rather than "this session ever had an empty
    # response".
    narrations = [ev for ev in window if _is_agent_narration(ev)]
    if not narrations:
        return False
    if not _is_empty(narrations[-1]):
        return False
    return len(_empty_narrations(window)) >= _RETRY_THRESHOLD


def _signal(window: list[CrewLogEvent]) -> ConfusionAction:
    n = len(_empty_narrations(window))
    if n >= _ESCALATE_THRESHOLD:
        return ConfusionAction.ESCALATE_TO_USER
    return ConfusionAction.RETRY


SIGNAL = ConfusionSignal(name=_NAME, matches=_matches, signal=_signal)
register(SIGNAL)


__all__ = ["SIGNAL"]
# end
