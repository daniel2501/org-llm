"""Tests for org_llm.confusion — Phase 24.3 — confusion detector.

Coverage:
- registry: register / replace-by-name / unregister / lookup
- detect_confusion: no-match → OK; first-match-wins; misbehaving
  matcher / signal are skipped silently; explicit-signals override
- loop signal: matches >=3 same-tool same-args calls inside the
  60s window; respects time-window guard; INTERRUPT
- escalation_loop signal: agent ↔ user ping-pong >=2 cycles → ESCALATE_TO_USER;
  resets on a progress event (tool call / non-question narration)
- empty_response signal: one empty narration → RETRY; second empty
  in window → ESCALATE_TO_USER; non-empty most-recent → no match
- determinism: same window → same action across calls
- builtin chain: importing the package registers all three example
  signals in order; end-to-end dispatch works
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from org_llm.confusion import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    detect_confusion,
    register,
    registered_signals,
    unregister,
)
from org_llm.confusion.empty_response  import SIGNAL as EMPTY_SIGNAL
from org_llm.confusion.escalation_loop import SIGNAL as ESC_SIGNAL
from org_llm.confusion.loop            import SIGNAL as LOOP_SIGNAL


# ── shared fixtures ─────────────────────────────────────────────


@pytest.fixture
def clean_registry():
    """Snapshot the registry, clear it, run the test, restore.

    Lets each test register its own signals without contaminating
    siblings. We don't import the builtin signal modules here —
    tests that want them call register() explicitly.
    """
    saved = registered_signals()
    for s in list(saved):
        unregister(s.name)
    yield
    for s in list(registered_signals()):
        unregister(s.name)
    for s in saved:
        register(s)


def _ts(seconds_ago: float = 0.0, base: datetime | None = None) -> str:
    """Build an ISO-8601 timestamp matching log_crew_action's
    `datetime.utcnow().isoformat(timespec="seconds") + "Z"` shape."""
    base = base or datetime(2026, 5, 6, 14, 0, 0)
    return (base - timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds") + "Z"


def _tool_call(tool: str, args: dict | None = None,
                seconds_ago: float = 0.0,
                outcome: str = "ok") -> CrewLogEvent:
    return CrewLogEvent(
        timestamp=_ts(seconds_ago),
        action="tool_call",
        agent_from="atoz",
        tool=tool,
        args=args or {},
        outcome=outcome,
    )


def _narration(result: str, *, agent: str = "atoz",
                outcome: str = "ok",
                seconds_ago: float = 0.0) -> CrewLogEvent:
    return CrewLogEvent(
        timestamp=_ts(seconds_ago),
        action="narration",
        agent_from=agent,
        result=result,
        outcome=outcome,
    )


def _agent_to_user(*, agent: str = "atoz",
                    seconds_ago: float = 0.0) -> CrewLogEvent:
    return CrewLogEvent(
        timestamp=_ts(seconds_ago),
        action="escalate",
        agent_from=agent,
        agent_to="user",
    )


def _user_to_agent(seconds_ago: float = 0.0) -> CrewLogEvent:
    return CrewLogEvent(
        timestamp=_ts(seconds_ago),
        action="user_reply",
        agent_from="user",
        agent_to="atoz",
    )


# ── registry mechanics ──────────────────────────────────────────


class TestRegistry:
    def test_register_appends(self, clean_registry):
        s = ConfusionSignal(name="x",
                              matches=lambda w: True,
                              signal=lambda w: ConfusionAction.RETRY)
        register(s)
        names = [r.name for r in registered_signals()]
        assert "x" in names

    def test_register_replaces_in_place(self, clean_registry):
        s1 = ConfusionSignal(name="dup",
                                matches=lambda w: False,
                                signal=lambda w: ConfusionAction.RETRY)
        s2 = ConfusionSignal(name="dup",
                                matches=lambda w: True,
                                signal=lambda w: ConfusionAction.INTERRUPT)
        before = ConfusionSignal(name="before",
                                    matches=lambda w: False,
                                    signal=lambda w: ConfusionAction.OK)
        after = ConfusionSignal(name="after",
                                   matches=lambda w: False,
                                   signal=lambda w: ConfusionAction.OK)
        register(before)
        register(s1)
        register(after)
        register(s2)
        names = [r.name for r in registered_signals()]
        assert names == ["before", "dup", "after"]
        # And the action is the new one.
        assert detect_confusion([]) == ConfusionAction.INTERRUPT

    def test_unregister_returns_true_when_removed(self, clean_registry):
        register(ConfusionSignal(name="t",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.OK))
        assert unregister("t") is True
        assert unregister("t") is False

    def test_no_match_returns_ok(self, clean_registry):
        register(ConfusionSignal(name="never",
                                    matches=lambda w: False,
                                    signal=lambda w: ConfusionAction.RETRY))
        assert detect_confusion([]) == ConfusionAction.OK

    def test_first_match_wins(self, clean_registry):
        register(ConfusionSignal(name="a",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.INTERRUPT))
        register(ConfusionSignal(name="b",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.RETRY))
        assert detect_confusion([]) == ConfusionAction.INTERRUPT

    def test_matcher_exception_is_skipped(self, clean_registry):
        def boom(w):
            raise ValueError("matcher boom")
        register(ConfusionSignal(name="boom", matches=boom,
                                    signal=lambda w: ConfusionAction.RETRY))
        register(ConfusionSignal(name="good",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.INTERRUPT))
        assert detect_confusion([]) == ConfusionAction.INTERRUPT

    def test_signal_exception_falls_through(self, clean_registry):
        def boom(w):
            raise ValueError("signal boom")
        register(ConfusionSignal(name="bad",
                                    matches=lambda w: True,
                                    signal=boom))
        register(ConfusionSignal(name="good",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.RETRY))
        # The misbehaving signal is skipped; the next matching
        # one takes over. If none match, OK.
        assert detect_confusion([]) == ConfusionAction.RETRY

    def test_explicit_signals_arg_overrides_registry(self, clean_registry):
        register(ConfusionSignal(name="reg",
                                    matches=lambda w: True,
                                    signal=lambda w: ConfusionAction.INTERRUPT))
        custom = [ConfusionSignal(name="custom",
                                     matches=lambda w: True,
                                     signal=lambda w: ConfusionAction.RETRY)]
        assert detect_confusion([], signals=custom) == ConfusionAction.RETRY

    def test_empty_window_is_ok_with_builtins(self):
        # Builtins should not fire on an empty window — the signal
        # of "nothing happened yet" is not a confusion.
        assert detect_confusion([]) == ConfusionAction.OK


# ── loop signal ─────────────────────────────────────────────────


class TestLoopSignal:
    def test_three_same_calls_match(self):
        window = [
            _tool_call("read_file", {"path": "wiki/superset.org"}, 5),
            _tool_call("read_file", {"path": "wiki/superset.org"}, 3),
            _tool_call("read_file", {"path": "wiki/superset.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is True
        assert LOOP_SIGNAL.signal(window) == ConfusionAction.INTERRUPT

    def test_two_same_calls_no_match(self):
        window = [
            _tool_call("read_file", {"path": "wiki/superset.org"}, 3),
            _tool_call("read_file", {"path": "wiki/superset.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is False

    def test_three_different_args_no_match(self):
        window = [
            _tool_call("read_file", {"path": "a.org"}, 5),
            _tool_call("read_file", {"path": "b.org"}, 3),
            _tool_call("read_file", {"path": "c.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is False

    def test_three_different_tools_no_match(self):
        window = [
            _tool_call("read_file",      {"path": "a.org"}, 5),
            _tool_call("list_directory", {"path": "a.org"}, 3),
            _tool_call("search_notes",   {"path": "a.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is False

    def test_normalised_args_match_case_and_whitespace(self):
        # Same path with capitalisation / whitespace differences
        # should be treated as the same call (the loop normaliser
        # collapses both).
        window = [
            _tool_call("read_file", {"path": "WIKI/Superset.org"}, 5),
            _tool_call("read_file", {"path": "wiki/superset.org "}, 3),
            _tool_call("read_file", {"path": "wiki/superset.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is True

    def test_outside_time_window_no_match(self):
        # First call is 120s before the latest — outside the 60s
        # window — so the streak should break.
        window = [
            _tool_call("read_file", {"path": "x.org"}, 120),
            _tool_call("read_file", {"path": "x.org"}, 3),
            _tool_call("read_file", {"path": "x.org"}, 1),
        ]
        assert LOOP_SIGNAL.matches(window) is False

    def test_streak_broken_by_intervening_different_call(self):
        window = [
            _tool_call("read_file", {"path": "x.org"}, 8),
            _tool_call("read_file", {"path": "y.org"}, 5),  # break
            _tool_call("read_file", {"path": "x.org"}, 3),
            _tool_call("read_file", {"path": "x.org"}, 1),
        ]
        # Only the last two are a streak — under threshold.
        assert LOOP_SIGNAL.matches(window) is False

    def test_ignores_non_tool_events(self):
        window = [
            _narration("looking it up", seconds_ago=10),
            _tool_call("read_file", {"path": "x.org"}, 5),
            _narration("hmm not there", seconds_ago=4),
            _tool_call("read_file", {"path": "x.org"}, 3),
            _narration("trying again", seconds_ago=2),
            _tool_call("read_file", {"path": "x.org"}, 1),
        ]
        # Three same tool calls amid narrations should still match.
        assert LOOP_SIGNAL.matches(window) is True


# ── escalation_loop signal ──────────────────────────────────────


class TestEscalationLoopSignal:
    def test_two_full_cycles_match(self):
        # agent→user, user→agent, agent→user, user→agent
        window = [
            _agent_to_user(seconds_ago=40),
            _user_to_agent(seconds_ago=30),
            _agent_to_user(seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert ESC_SIGNAL.matches(window) is True
        assert ESC_SIGNAL.signal(window) == ConfusionAction.ESCALATE_TO_USER

    def test_one_cycle_no_match(self):
        window = [
            _agent_to_user(seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert ESC_SIGNAL.matches(window) is False

    def test_progress_resets_counter(self):
        # One full cycle, then a tool call (= progress), then
        # another full cycle. Total transitions visible to the
        # walker is only the second cycle's two — not enough.
        window = [
            _agent_to_user(seconds_ago=50),
            _user_to_agent(seconds_ago=45),
            _tool_call("read_file", {"path": "a.org"}, 30),  # progress
            _agent_to_user(seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert ESC_SIGNAL.matches(window) is False

    def test_question_narration_counts_as_agent_to_user(self):
        # An agent narration ending in "?" is treated as an
        # implicit ask — paired with a user reply, two such
        # cycles should still match.
        window = [
            _narration("which file did you mean?", seconds_ago=40),
            _user_to_agent(seconds_ago=30),
            _narration("could you clarify the path?", seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert ESC_SIGNAL.matches(window) is True

    def test_non_question_narration_is_progress(self):
        # A non-question narration counts as progress and resets.
        window = [
            _agent_to_user(seconds_ago=50),
            _user_to_agent(seconds_ago=45),
            _narration("got it; wrote the section.", seconds_ago=30),
            _agent_to_user(seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert ESC_SIGNAL.matches(window) is False


# ── empty_response signal ───────────────────────────────────────


class TestEmptyResponseSignal:
    def test_single_empty_most_recent_retries(self):
        window = [_narration("", seconds_ago=1)]
        assert EMPTY_SIGNAL.matches(window) is True
        assert EMPTY_SIGNAL.signal(window) == ConfusionAction.RETRY

    def test_whitespace_only_counts_as_empty(self):
        window = [_narration("   \n\t  ", seconds_ago=1)]
        assert EMPTY_SIGNAL.matches(window) is True
        assert EMPTY_SIGNAL.signal(window) == ConfusionAction.RETRY

    def test_outcome_empty_marker_counts_as_empty(self):
        # Even with non-blank result text, an explicit
        # outcome="empty" should count.
        window = [_narration("…", seconds_ago=1, outcome="empty")]
        assert EMPTY_SIGNAL.matches(window) is True

    def test_two_empties_escalate(self):
        window = [
            _narration("",  seconds_ago=20),
            _narration("",  seconds_ago=1),
        ]
        assert EMPTY_SIGNAL.matches(window) is True
        assert (EMPTY_SIGNAL.signal(window)
                 == ConfusionAction.ESCALATE_TO_USER)

    def test_non_empty_most_recent_no_match(self):
        # Old empty is healed by a fresh non-empty narration.
        window = [
            _narration("",          seconds_ago=20),
            _narration("here it is", seconds_ago=1),
        ]
        assert EMPTY_SIGNAL.matches(window) is False

    def test_user_message_is_not_a_narration(self):
        # A blank user reply must NOT count as an empty agent
        # response — only agent narrations are inspected.
        window = [_narration("", agent="user", seconds_ago=1)]
        assert EMPTY_SIGNAL.matches(window) is False

    def test_no_narrations_no_match(self):
        # A window of only tool calls / user messages doesn't
        # have anything to call empty.
        window = [
            _tool_call("read_file", {"path": "x.org"}, 5),
            _user_to_agent(seconds_ago=1),
        ]
        assert EMPTY_SIGNAL.matches(window) is False


# ── determinism ─────────────────────────────────────────────────


class TestDeterminism:
    """Same input → same action. The supervision rule says
    deterministic by default; tests pin that contract."""

    def test_loop_signal_deterministic(self):
        window = [
            _tool_call("read_file", {"path": "x"}, 5),
            _tool_call("read_file", {"path": "x"}, 3),
            _tool_call("read_file", {"path": "x"}, 1),
        ]
        a = LOOP_SIGNAL.signal(window)
        b = LOOP_SIGNAL.signal(window)
        c = LOOP_SIGNAL.signal(window)
        assert a == b == c == ConfusionAction.INTERRUPT

    def test_empty_signal_deterministic(self):
        window = [_narration("", seconds_ago=1)]
        a = EMPTY_SIGNAL.signal(window)
        b = EMPTY_SIGNAL.signal(window)
        assert a == b == ConfusionAction.RETRY

    def test_detect_confusion_deterministic(self, clean_registry):
        register(ConfusionSignal(
            name="d",
            matches=lambda w: bool(w),
            signal=lambda w: ConfusionAction.INTERRUPT,
        ))
        first  = detect_confusion([_tool_call("x")])
        second = detect_confusion([_tool_call("x")])
        third  = detect_confusion([_tool_call("x")])
        assert first == second == third == ConfusionAction.INTERRUPT


# ── builtin chain wiring ────────────────────────────────────────


class TestBuiltinChain:
    """Sanity-check that importing the package registers all
    three example signals in order. This is the contract the
    v0.1 integration sprint will rely on."""

    def test_all_three_signals_registered(self):
        names = [s.name for s in registered_signals()]
        assert "loop"            in names
        assert "escalation_loop" in names
        assert "empty_response"  in names

    def test_loop_dispatches_to_interrupt(self):
        window = [
            _tool_call("read_file", {"path": "x.org"}, 5),
            _tool_call("read_file", {"path": "x.org"}, 3),
            _tool_call("read_file", {"path": "x.org"}, 1),
        ]
        assert detect_confusion(window) == ConfusionAction.INTERRUPT

    def test_escalation_loop_dispatches_to_escalate_to_user(self):
        window = [
            _agent_to_user(seconds_ago=40),
            _user_to_agent(seconds_ago=30),
            _agent_to_user(seconds_ago=20),
            _user_to_agent(seconds_ago=10),
        ]
        assert (detect_confusion(window)
                 == ConfusionAction.ESCALATE_TO_USER)

    def test_empty_response_dispatches_to_retry(self):
        window = [_narration("", seconds_ago=1)]
        assert detect_confusion(window) == ConfusionAction.RETRY

    def test_empty_response_second_dispatches_to_escalate(self):
        window = [
            _narration("", seconds_ago=10),
            _narration("", seconds_ago=1),
        ]
        assert (detect_confusion(window)
                 == ConfusionAction.ESCALATE_TO_USER)

    def test_unrecognised_window_returns_ok(self):
        # A window that's just one happy tool call + one happy
        # narration shouldn't trip any signal.
        window = [
            _tool_call("read_file", {"path": "x.org"}, 5),
            _narration("here you go", seconds_ago=1),
        ]
        assert detect_confusion(window) == ConfusionAction.OK
# end
