"""Tool-loop confusion signal — Phase 24.3 — confusion detector.

When the same tool is called >=3 times with similar args inside
a short window, the agent has almost certainly lost the plot:
either the tool is broken (recovery hooks would have caught a
clean exception; this pattern is the silent-failure cousin) or
the agent is stuck in a strategy that can't make progress.

Action: INTERRUPT — short-circuit the loop and let the framework
inject a ~MANAGER NOTE~ into the agent's next turn so it changes
strategy. The framework owns the note text; the signal owns the
detection.

Why >=3 with similar args (not >=2): two repeats can be a
legitimate retry-with-correction (e.g. the agent fixed a typo
between calls). Three identical calls in <60s is the
unambiguous loop shape — empirically the motivating bug from
2026-05-04 (an agent reading =wiki/superset.org= against three
candidate roots) hits this signal on call #3.

Why "similar args" not "exact": tiny normalisation differences
(absolute vs. relative path, trailing slash, whitespace) shouldn't
mask the loop. We compare a *normalised* arg signature: lowercased,
whitespace-collapsed JSON of the sorted-key args dict. Stricter
exact-match would let the agent dodge the signal by appending a
space.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    register,
)


_NAME = "loop"
_MIN_REPEATS = 3
_WINDOW_SECONDS = 60


def _normalise_value(v):
    """Recursively lowercase + whitespace-collapse string values
    so trailing spaces / casing don't dodge the loop signal. Path
    args are the common case, and `wiki/superset.org` vs.
    `wiki/superset.org ` should be the same call. Non-string
    values are returned unchanged."""
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v.strip().lower())
    if isinstance(v, dict):
        return {k: _normalise_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_normalise_value(x) for x in v]
    return v


def _normalise_args(args: dict) -> str:
    """Stable signature for arg-similarity comparison. JSON with
    sorted keys, with string values normalised (lowercased,
    whitespace-collapsed). Best-effort: non-JSON-serialisable
    values fall back to repr()."""
    try:
        norm = _normalise_value(args) if isinstance(args, dict) else args
        return json.dumps(norm, sort_keys=True, default=repr)
    except Exception:
        return repr(sorted(args.items()) if isinstance(args, dict) else args)


def _parse_ts(ts: str) -> datetime | None:
    """Parse the ISO-8601 timestamps `log_crew_action` writes.

    The producer uses `datetime.utcnow().isoformat(timespec="seconds")
    + "Z"`. Python <3.11 chokes on the trailing Z in fromisoformat,
    so strip it before parsing. Returns None on any failure — the
    signal then degrades to "no time-window check", which means
    every same-tool-3x sequence trips, which is still the correct
    direction (better than silently missing loops).
    """
    if not ts:
        return None
    try:
        clean = ts.rstrip("Z")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def _tool_call_events(window: list[CrewLogEvent]) -> list[CrewLogEvent]:
    """Filter the window to events that look like tool calls.

    We treat any event with a non-empty `tool` field as a tool
    call. Some producers write `action="tool_call"` instead, so
    we accept that too.
    """
    out = []
    for ev in window:
        if ev.tool:
            out.append(ev)
        elif ev.action == "tool_call":
            out.append(ev)
    return out


def _matches(window: list[CrewLogEvent]) -> bool:
    calls = _tool_call_events(window)
    if len(calls) < _MIN_REPEATS:
        return False
    # Group the most-recent run by (tool, normalised-args). We
    # only care about the *latest* contiguous repeat — older
    # repeats from earlier in the session aren't this turn's
    # confusion. Walk backwards collecting matches until we hit
    # a different (tool, args).
    last = calls[-1]
    last_key = (last.tool or last.action, _normalise_args(last.args))
    last_ts = _parse_ts(last.timestamp)
    streak = [last]
    for ev in reversed(calls[:-1]):
        key = (ev.tool or ev.action, _normalise_args(ev.args))
        if key != last_key:
            break
        # Time-window guard: if both timestamps parsed, require
        # the run to fit inside _WINDOW_SECONDS. If either side
        # is unparsable, skip the time check (signal degrades to
        # call-count only).
        ev_ts = _parse_ts(ev.timestamp)
        if last_ts is not None and ev_ts is not None:
            if last_ts - ev_ts > timedelta(seconds=_WINDOW_SECONDS):
                break
        streak.append(ev)
        if len(streak) >= _MIN_REPEATS:
            return True
    return len(streak) >= _MIN_REPEATS


def _signal(window: list[CrewLogEvent]) -> ConfusionAction:
    return ConfusionAction.INTERRUPT


SIGNAL = ConfusionSignal(name=_NAME, matches=_matches, signal=_signal)
register(SIGNAL)


__all__ = ["SIGNAL"]
# end
