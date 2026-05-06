"""Tests for org_llm.confusion.wiring — Phase 24.3 — confusion
detector v0.1 follow-up wiring.

Coverage:
- install_detector: wires + records events into the ring
- sample-every-N policy: detector runs on Nth event, not earlier
- non-OK action triggers listeners; OK action does not
- 1Hz wall-clock floor prevents detector overrun
- empty event window is safe
- multiple listeners + a misbehaving listener don't break others
- backward-compat: write_event without install is a clean no-op
- write_event end-to-end: real logbook hook delivers events into
  the ring (proves the integration point — Phase 24.2's recovery
  hooks don't shadow the new wiring)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from org_llm.confusion import (
    ConfusionAction,
    ConfusionSignal,
    CrewLogEvent,
    register,
    registered_signals,
    unregister,
)
from org_llm.confusion import wiring as W


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def clean_wiring():
    """Snapshot wiring state, reset, run, restore. Each test gets
    a clean detector slate so listeners + ring buffers don't bleed."""
    W.uninstall_detector()
    yield
    W.uninstall_detector()


@pytest.fixture
def clean_registry():
    """Snapshot the signal registry, clear it for the test, then
    restore. Lets each test register exactly the signals it needs."""
    saved = registered_signals()
    for s in list(saved):
        unregister(s.name)
    yield
    for s in list(registered_signals()):
        unregister(s.name)
    for s in saved:
        register(s)


def _ev(action: str = "tool_call", tool: str = "read_file",
         args: dict | None = None) -> CrewLogEvent:
    return CrewLogEvent(
        timestamp="2026-05-06T14:00:00Z",
        action=action,
        agent_from="atoz",
        tool=tool,
        args=args or {},
        outcome="ok",
        result="",
    )


def _always_match(action: ConfusionAction) -> ConfusionSignal:
    return ConfusionSignal(
        name=f"always_{action.value}",
        matches=lambda w: bool(w),
        signal=lambda w: action,
    )


# ── install / uninstall ─────────────────────────────────────────


class TestInstall:
    def test_install_detector_wires_successfully(self, clean_wiring):
        assert W.is_installed() is False
        W.install_detector(sample_every_n_events=5)
        assert W.is_installed() is True

    def test_install_is_idempotent_and_preserves_ring(self, clean_wiring):
        W.install_detector(sample_every_n_events=10)
        W.notify_event(_ev())
        W.notify_event(_ev())
        assert len(W.snapshot_window()) == 2
        # Re-install with a tighter policy — ring contents must
        # survive (long-running session protection).
        W.install_detector(sample_every_n_events=1)
        assert len(W.snapshot_window()) == 2

    def test_uninstall_makes_notify_a_noop(self, clean_wiring):
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        W.uninstall_detector()
        # No detector installed → returns OK without raising.
        assert W.notify_event(_ev()) == ConfusionAction.OK
        assert W.snapshot_window() == []

    def test_invalid_sample_rate_rejected(self, clean_wiring):
        with pytest.raises(ValueError):
            W.install_detector(sample_every_n_events=0)


# ── sample-every-N ──────────────────────────────────────────────


class TestSamplePolicy:
    def test_detector_skipped_until_N_events(self, clean_wiring,
                                                clean_registry):
        register(_always_match(ConfusionAction.INTERRUPT))
        # min_seconds_between=0 isolates the test to the per-event
        # sampler — wall-clock floor must not interfere.
        W.install_detector(sample_every_n_events=5,
                            min_seconds_between=0.0)
        seen = []
        W.add_listener(lambda action, window: seen.append(action))
        # 4 appends — under the threshold; detector must NOT run.
        for _ in range(4):
            assert W.notify_event(_ev()) == ConfusionAction.OK
        assert seen == []
        # 5th append — detector runs and the always-INTERRUPT
        # signal fires.
        assert W.notify_event(_ev()) == ConfusionAction.INTERRUPT
        assert seen == [ConfusionAction.INTERRUPT]

    def test_detector_resamples_after_N_more_events(self, clean_wiring,
                                                       clean_registry):
        register(_always_match(ConfusionAction.INTERRUPT))
        W.install_detector(sample_every_n_events=3,
                            min_seconds_between=0.0)
        seen = []
        W.add_listener(lambda action, window: seen.append(action))
        # First 3 trigger the first run.
        for _ in range(3):
            W.notify_event(_ev())
        assert len(seen) == 1
        # Next 2 are below threshold again — no fire.
        W.notify_event(_ev())
        W.notify_event(_ev())
        assert len(seen) == 1
        # 3rd post-run event re-fires.
        W.notify_event(_ev())
        assert len(seen) == 2


# ── action propagation ──────────────────────────────────────────


class TestActionPropagation:
    def test_non_ok_action_triggers_listeners(self, clean_wiring,
                                                  clean_registry):
        register(_always_match(ConfusionAction.ESCALATE_TO_USER))
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        captured: list = []
        W.add_listener(
            lambda action, window: captured.append((action, len(window))))
        result = W.notify_event(_ev())
        assert result == ConfusionAction.ESCALATE_TO_USER
        assert captured == [(ConfusionAction.ESCALATE_TO_USER, 1)]

    def test_ok_action_is_a_noop_for_listeners(self, clean_wiring,
                                                  clean_registry):
        # No signals registered → detector returns OK by contract.
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        captured: list = []
        W.add_listener(lambda action, window: captured.append(action))
        result = W.notify_event(_ev())
        assert result == ConfusionAction.OK
        assert captured == []

    def test_misbehaving_listener_does_not_break_others(self,
                                                           clean_wiring,
                                                           clean_registry):
        register(_always_match(ConfusionAction.RETRY))
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        seen: list = []
        def boom(action, window):
            raise RuntimeError("listener boom")
        W.add_listener(boom)
        W.add_listener(lambda action, window: seen.append(action))
        # Even with a raising listener, the second one still runs
        # AND notify_event returns the action cleanly.
        assert W.notify_event(_ev()) == ConfusionAction.RETRY
        assert seen == [ConfusionAction.RETRY]

    def test_remove_listener(self, clean_wiring, clean_registry):
        register(_always_match(ConfusionAction.RETRY))
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        seen: list = []
        listener = lambda action, window: seen.append(action)
        W.add_listener(listener)
        assert W.remove_listener(listener) is True
        # Second remove → False (already gone).
        assert W.remove_listener(listener) is False
        W.notify_event(_ev())
        assert seen == []


# ── rate limit ──────────────────────────────────────────────────


class TestRateLimit:
    def test_min_seconds_between_floors_detector_runs(self, clean_wiring,
                                                          clean_registry):
        register(_always_match(ConfusionAction.INTERRUPT))
        # sample_every_n_events=1 means "every event would normally
        # run the detector", but min_seconds_between=10 wall-clock-
        # floors it to one run per 10s.
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=10.0)
        seen: list = []
        W.add_listener(lambda action, window: seen.append(action))
        # First call: monotonic floor is 0.0, so it runs.
        assert W.notify_event(_ev()) == ConfusionAction.INTERRUPT
        # Subsequent burst: floor blocks them.
        for _ in range(5):
            assert W.notify_event(_ev()) == ConfusionAction.OK
        assert len(seen) == 1


# ── empty window edge case ──────────────────────────────────────


class TestEmptyWindow:
    def test_detector_handles_empty_ring_safely(self, clean_wiring):
        """Snapshot before any append → empty list, no crash."""
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        assert W.snapshot_window() == []

    def test_uninstalled_detector_returns_ok(self, clean_wiring):
        # No install → notify is a clean no-op returning OK.
        assert W.notify_event(_ev()) == ConfusionAction.OK


# ── concurrent hooks coexistence ────────────────────────────────


class TestCoexistence:
    """Phase 24.2 — recovery hooks already wraps tool-call sites in
    `mcp_server.py`. The new logbook post-emit hook must not break
    that — `write_event` is on the logging path, not the tool-call
    path, but the test pins the contract."""

    def test_write_event_delivers_to_ring_after_install(
            self, clean_wiring, clean_registry, monkeypatch, tmp_path):
        # Point the logbook at a tmp DB / org log so the test
        # doesn't touch the user's real vault.
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_LOG_PATH",
                            str(tmp_path / "captains-log.org"))
        # Init the DB so write_event's History insert can land.
        from org_llm.db import init_db, make_engine
        engine = make_engine(tmp_path / "test.db")
        init_db(engine)
        engine.dispose()

        from org_llm import logbook
        # Register a signal that we can prove fired, then install
        # the detector with sample=1 so every event triggers a run.
        register(_always_match(ConfusionAction.RETRY))
        W.install_detector(sample_every_n_events=1,
                            min_seconds_between=0.0)
        seen: list = []
        W.add_listener(lambda action, window: seen.append(action))

        logbook.write_event("cli", "test-cmd", args="{}",
                              response="hi", model="", outcome="ok")

        # The hook should have pushed an event into the ring AND
        # the always-RETRY signal should have fired.
        assert len(W.snapshot_window()) == 1
        assert seen == [ConfusionAction.RETRY]

    def test_write_event_is_noop_for_wiring_when_not_installed(
            self, clean_wiring, monkeypatch, tmp_path):
        # No install_detector call → logbook.write_event must not
        # raise + the wiring snapshot stays empty.
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "test.db"))
        monkeypatch.setenv("ORG_LLM_LOG_PATH",
                            str(tmp_path / "captains-log.org"))
        from org_llm.db import init_db, make_engine
        engine = make_engine(tmp_path / "test.db")
        init_db(engine)
        engine.dispose()

        from org_llm import logbook
        # Should NOT raise. Should NOT touch wiring state.
        logbook.write_event("cli", "test-cmd", args="{}",
                              response="hi", outcome="ok")
        assert W.is_installed() is False
        assert W.snapshot_window() == []


# ── adapter shape ───────────────────────────────────────────────


class TestEventAdapter:
    """The `event_from_write_event` adapter is what makes the
    detector usable from logbook's kwargs shape. Pin its mapping
    so signal modules keep matching after wiring."""

    def test_mcp_kind_maps_to_tool_call(self):
        ev = W.event_from_write_event(
            kind="mcp", command="read_file",
            args='{"path": "wiki/superset.org"}')
        assert ev.action == "tool_call"
        assert ev.tool == "read_file"
        assert ev.args == {"path": "wiki/superset.org"}

    def test_llm_kind_maps_to_narration(self):
        ev = W.event_from_write_event(
            kind="llm", command="chat", response="here you go")
        assert ev.action == "narration"
        assert ev.result == "here you go"

    def test_crew_kind_unwraps_command_and_agents(self):
        ev = W.event_from_write_event(
            kind="crew", command="crew/delegate",
            args='{"agent_from": "manager", "agent_to": "atoz"}')
        assert ev.action == "delegate"
        assert ev.agent_from == "manager"
        assert ev.agent_to   == "atoz"

    def test_unparseable_args_fall_back_to_raw(self):
        ev = W.event_from_write_event(
            kind="cli", command="x", args="not-json")
        assert ev.args == {"_raw": "not-json"}
# end
