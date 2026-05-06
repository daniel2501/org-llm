"""Confusion-detector live wiring — Phase 24.3 — confusion detector
v0.1 follow-up.

Bridges the deterministic signal registry in
=org_llm.confusion.__init__= to the live captain's-log event stream
in =org_llm/logbook.py=. The registry stays a pure-functions package
(unit-testable without a DB or filesystem); this module owns:

  - the in-memory ring buffer of recent =CrewLogEvent=s
  - the sample-every-N-events policy + 1Hz wall-clock floor that
    keeps the detector cheap (DEC-006 — supervision is
    deterministic by default; supervision must never tax the
    user's turn, including its own runtime cost)
  - the listener dispatch that propagates =RETRY= /
    =INTERRUPT= / =ESCALATE_TO_USER= to whoever cares
    (chat surface, manager, recovery layer)

Usage shape (called once at startup, e.g. from
=org_llm.cli.cli_chat= or the MCP server boot):

    from org_llm.confusion.wiring import install_detector
    install_detector(sample_every_n_events=5)

After install, every call to =logbook.write_event(...)= appends
into the ring + (when the sampler trips) runs
=detect_confusion(window)=. Non-OK actions go out to listeners.

Backward-compat: if =install_detector= is never called the hook
list stays empty and =logbook.write_event= behaves *exactly* as
before — no extra work, no allocations beyond an empty list-walk.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing      import Any, Callable, Iterable, Optional

from . import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    detect_confusion,
)


# ── public types ────────────────────────────────────────────────


# A listener is called whenever the detector returns a non-OK
# action. Signature: `(action, window) -> None`. Listeners must
# be cheap + non-raising — the wiring swallows exceptions so a
# misbehaving listener can't tax the turn.
ConfusionListener = Callable[[ConfusionAction, list[CrewLogEvent]], None]


@dataclass
class _DetectorState:
    """All mutable state lives here so tests can swap it cleanly."""
    ring:                    deque
    sample_every_n_events:   int
    min_seconds_between:     float
    events_since_last_check: int
    last_check_monotonic:    float
    listeners:               list[ConfusionListener]
    enabled:                 bool
    lock:                    threading.Lock


# Default ring depth — large enough for the empty-response-twice
# pattern + the 3-tool-call loop pattern + multi-cycle escalation
# pattern, small enough to keep matchers O(window) cheap.
_DEFAULT_RING_SIZE = 64

# Default sample policy. The ring still records every event; the
# *detector* runs at most every Nth append. Sparser of the two
# floors wins — see _should_run_detector below.
_DEFAULT_SAMPLE_EVERY = 5

# Hard floor on detector wall-clock cadence: 1Hz. Even if N
# events arrive in the same millisecond (e.g. a burst tool-call
# fan-out), the detector will only run once that second.
_DEFAULT_MIN_SECONDS_BETWEEN = 1.0


_state: Optional[_DetectorState] = None
_state_lock = threading.Lock()


# ── installation ────────────────────────────────────────────────


def install_detector(
    *,
    sample_every_n_events: int   = _DEFAULT_SAMPLE_EVERY,
    min_seconds_between:   float = _DEFAULT_MIN_SECONDS_BETWEEN,
    ring_size:             int   = _DEFAULT_RING_SIZE,
    listeners:             Iterable[ConfusionListener] | None = None,
) -> _DetectorState:
    """Wire the detector into the live event stream.

    Idempotent: calling twice resets the policy + listeners but
    keeps the existing ring contents (so a re-install during a
    long-running session doesn't lose recent context).

    `sample_every_n_events`
        Run `detect_confusion` only on every Nth append.
        Default 5. Must be >= 1.
    `min_seconds_between`
        Hard floor on detector cadence in seconds. Default 1.0.
        Burst events still get *recorded*; only the detector run
        is rate-limited.
    `ring_size`
        How many events the in-memory window keeps. Default 64.
    `listeners`
        Initial listener list. More can be added with
        `add_listener` after install.
    """
    if sample_every_n_events < 1:
        raise ValueError(
            "sample_every_n_events must be >= 1; "
            "use uninstall_detector() to disable instead.")
    global _state
    with _state_lock:
        if _state is None:
            _state = _DetectorState(
                ring=deque(maxlen=ring_size),
                sample_every_n_events=sample_every_n_events,
                min_seconds_between=min_seconds_between,
                events_since_last_check=0,
                last_check_monotonic=0.0,
                listeners=list(listeners or []),
                enabled=True,
                lock=threading.Lock(),
            )
        else:
            # Re-install: refresh policy + listeners, preserve ring
            # contents so we keep the recent event window.
            new_ring = deque(_state.ring, maxlen=ring_size)
            _state.ring = new_ring
            _state.sample_every_n_events = sample_every_n_events
            _state.min_seconds_between = min_seconds_between
            _state.listeners = list(listeners or [])
            _state.enabled = True
        return _state


def uninstall_detector() -> None:
    """Remove the wiring entirely. After this, =logbook.write_event=
    is a no-op w.r.t. confusion detection. Used by tests + by a
    future =confusion off= verb."""
    global _state
    with _state_lock:
        _state = None


def is_installed() -> bool:
    return _state is not None and _state.enabled


def add_listener(listener: ConfusionListener) -> None:
    """Append a listener. No-op if no detector is installed."""
    if _state is None:
        return
    with _state.lock:
        _state.listeners.append(listener)


def remove_listener(listener: ConfusionListener) -> bool:
    """Remove a previously-added listener. Returns True if removed."""
    if _state is None:
        return False
    with _state.lock:
        try:
            _state.listeners.remove(listener)
            return True
        except ValueError:
            return False


def snapshot_window() -> list[CrewLogEvent]:
    """Return a copy of the current ring contents — for tests +
    introspection. Never returns the live ring."""
    if _state is None:
        return []
    with _state.lock:
        return list(_state.ring)


# ── notify hook (called by logbook) ─────────────────────────────


def _should_run_detector(state: _DetectorState, now: float) -> bool:
    """Sample policy: run if we've seen >= N events *and* the wall-
    clock floor has been cleared. The sparser of the two wins."""
    if state.events_since_last_check < state.sample_every_n_events:
        return False
    if (now - state.last_check_monotonic) < state.min_seconds_between:
        return False
    return True


def notify_event(event: CrewLogEvent) -> ConfusionAction:
    """Append `event` to the ring + (if sampling permits) run
    `detect_confusion` against the current window. Returns the
    action the detector decided on (`OK` if rate-limited / not
    installed). Listeners are notified for non-OK actions.

    Pure best-effort — never raises into the caller. The supervision
    rule says supervision can never tax the user's turn.
    """
    if _state is None or not _state.enabled:
        return ConfusionAction.OK

    state = _state
    try:
        with state.lock:
            state.ring.append(event)
            state.events_since_last_check += 1
            now = time.monotonic()
            if not _should_run_detector(state, now):
                return ConfusionAction.OK
            # Snapshot the window and reset counters before
            # releasing the lock — keeps the detector run
            # outside the critical section so a slow signal
            # can't block other threads' appends.
            window = list(state.ring)
            state.events_since_last_check = 0
            state.last_check_monotonic = now
            listeners = list(state.listeners)
    except Exception:
        return ConfusionAction.OK

    try:
        action = detect_confusion(window)
    except Exception:
        return ConfusionAction.OK

    if action != ConfusionAction.OK:
        for listener in listeners:
            try:
                listener(action, window)
            except Exception:
                # A misbehaving listener must not propagate.
                continue

    return action


# ── adapter — write_event kwargs → CrewLogEvent ──────────────────


def event_from_write_event(
    *,
    kind:        str,
    command:     str,
    args:        str = "",
    response:    str = "",
    model:       str = "",
    duration_ms: int | None = None,
    outcome:     str = "ok",
    timestamp:   str = "",
) -> CrewLogEvent:
    """Adapt the kwargs `logbook.write_event` already has into
    the `CrewLogEvent` shape the detector expects.

    Mapping rationale:
      - kind="crew" rows → action / agent_from / agent_to / tool
        all live inside the structured args JSON the
        log_crew_action mirror writes; we best-effort parse.
      - kind="mcp" rows  → action="tool_call", tool=command
      - kind="llm" rows  → action="narration", result=response
      - everything else  → action=kind, result=response (so
        non-confusion-relevant events are still ring-recorded
        but won't trip narration / tool-call signals).
    """
    import json as _json

    action     = kind
    agent_from = ""
    agent_to   = ""
    tool       = ""
    args_dict: dict[str, Any] = {}

    # Best-effort parse of the args string into a dict for
    # signal matchers that compare arg signatures (loop signal).
    if args:
        try:
            parsed = _json.loads(args)
            if isinstance(parsed, dict):
                args_dict = parsed
        except Exception:
            args_dict = {"_raw": args[:200]}

    if kind == "crew":
        # log_crew_action's History mirror packs agent_from /
        # agent_to / session_id into args JSON. The command
        # itself is "crew/<action>".
        if command.startswith("crew/"):
            action = command[len("crew/"):]
        agent_from = str(args_dict.get("agent_from", "") or "")
        agent_to   = str(args_dict.get("agent_to",   "") or "")
    elif kind == "mcp":
        action = "tool_call"
        tool = command
    elif kind == "llm":
        action = "narration"

    return CrewLogEvent(
        timestamp  = timestamp,
        action     = action,
        agent_from = agent_from,
        agent_to   = agent_to,
        tool       = tool,
        args       = args_dict,
        outcome    = outcome,
        result     = response,
    )


__all__ = [
    "ConfusionListener",
    "install_detector",
    "uninstall_detector",
    "is_installed",
    "add_listener",
    "remove_listener",
    "snapshot_window",
    "notify_event",
    "event_from_write_event",
]
# end
