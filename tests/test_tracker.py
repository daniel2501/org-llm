"""Tests for `org-llm tracker {init,review,pace}` (Phase 29.0).

Per project_tracker_self_hosting_goal: the dev-tracker is migrating
from a wiki page to a vault file owned by @riker (Bridge Crew rename
2026-05-06; verb name `tracker` stays). These tests pin the
deterministic skeleton so the LLM persona has a stable substrate
to narrate on top of.

Per feedback_test_before_handoff: tests pass before reporting.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.cli import app
from org_llm.tracker import (
    InitResult,
    init_tracker,
    review_tracker,
    pace_tracker,
    _parse_org_entries,
    _parse_effort,
    _logged_hours_from_body,
    _started_claims,
)


runner = CliRunner()


# ── Fixture: tiny tracker + claims org files ─────────────────────────────────

_TRACKER_FIXTURE = """:PROPERTIES:
:ID:       fixture-tracker-id
:END:
#+TITLE: Fixture dev tracker
#+TODO: TODO(t) NEXT(n) STARTED(s!) HOLD(h@) WAITING(w@) | DONE(d!) CANCELLED(c@)

* Active work — STARTED  :@active:

** STARTED Build the tracker self-hosting verbs                  :plan:dogfood:
:PROPERTIES:
:PRIORITY:  A
:EFFORT:    4h
:ACTUAL:    ~2h
:END:

Body of the active work item.

** TODO Fix a bug                                                 :bug:blocker:
:PROPERTIES:
:EFFORT:    30m
:END:

A blocker we need to surface.

* Plan queue                                                      :@plan:

** TODO Future work                                               :plan:
:PROPERTIES:
:EFFORT:    2h
:END:

Pace data: scheduled but no clock yet.

** TODO Already-overrun work                                      :plan:
:PROPERTIES:
:EFFORT:    1h
:END:

CLOCK: [2026-05-04 Mon 09:00]--[2026-05-04 Mon 12:00] =>  3:00

That CLOCK line should be summed → 3.0h actual vs. 1.0h estimate
= 3.0x ratio (a clear overrun).

* Bugs                                                            :@bug:

** TODO A surprise                                                :surprise:
:PROPERTIES:
:END:

Tagged surprise; should appear in the blockers/surprises section.
"""


_CLAIMS_FIXTURE = """:PROPERTIES:
:ID:       fixture-claims-id
:END:
#+TITLE: Active claims fixture

* Active claims                                                 :@active:

** STARTED test-actor — sample claim
   :PROPERTIES:
   :CLAIM:   foo.py bar.py
   :BRANCH:  trunk
   :STARTED: [2026-05-05 Tue 11:30]
   :END:

* Recently closed                                              :@closed:

** DONE test-actor — old claim
   :PROPERTIES:
   :CLAIM:   baz.py
   :BRANCH:  trunk
   :END:
"""


@pytest.fixture
def tracker_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "fixture-tracker.org"
    p.write_text(_TRACKER_FIXTURE)
    return p


@pytest.fixture
def claims_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "fixture-claims.org"
    p.write_text(_CLAIMS_FIXTURE)
    return p


@pytest.fixture
def repo_root() -> Path:
    """Real repo root — `git log` calls in review_tracker need a real
    repo. The fixture itself doesn't depend on commit content; we
    only need git to succeed."""
    return Path(__file__).resolve().parent.parent


# ── Pure-function tests (no CLI runner) ──────────────────────────────────────

class TestParseOrgEntries:
    def test_parses_state_and_tags(self):
        entries = _parse_org_entries(_TRACKER_FIXTURE)
        states = [(e.state, e.title) for e in entries]
        assert ("STARTED", "Build the tracker self-hosting verbs") in states
        assert ("TODO", "Fix a bug") in states
        assert ("TODO", "A surprise") in states

    def test_captures_properties(self):
        entries = _parse_org_entries(_TRACKER_FIXTURE)
        bug = next(e for e in entries if e.title == "Fix a bug")
        assert bug.properties.get("EFFORT") == "30m"

    def test_section_tagging(self):
        entries = _parse_org_entries(_TRACKER_FIXTURE)
        # The "Fix a bug" item is in the :@active: top-level section
        bug = next(e for e in entries if e.title == "Fix a bug")
        assert ":@active" in bug.section


class TestParseEffort:
    @pytest.mark.parametrize("s,expected", [
        ("30m", 0.5),
        ("1h", 1.0),
        ("2h", 2.0),
        ("1d", 8.0),
        ("1.5h", 1.5),
        ("1:30", 1.5),
        ("", None),
        ("garbage", None),
    ])
    def test_parses(self, s, expected):
        assert _parse_effort(s) == expected


class TestClockSum:
    def test_sums_clock_lines(self):
        body = (
            "Some text.\n"
            "CLOCK: [2026-05-04 Mon 09:00]--[2026-05-04 Mon 12:00] =>  3:00\n"
            "More text.\n"
            "CLOCK: [2026-05-04 Mon 13:00]--[2026-05-04 Mon 13:30] =>  0:30\n"
        )
        assert _logged_hours_from_body(body) == 3.5

    def test_no_clock_lines(self):
        assert _logged_hours_from_body("just a body") == 0.0


class TestStartedClaims:
    def test_filters_to_started(self):
        started = _started_claims(_CLAIMS_FIXTURE)
        assert len(started) == 1
        assert started[0].title == "test-actor — sample claim"


# ── init verb ────────────────────────────────────────────────────────────────

class TestTrackerInit:
    def test_tracker_init_creates_file(self, tmp_path: Path, tracker_fixture: Path):
        target = tmp_path / "vault" / "org-llm-dev-tracker.org"
        result = init_tracker(target, tracker_fixture)
        assert result.created is True
        assert target.exists()
        # Header was prepended
        text = target.read_text()
        assert "Managed by @riker" in text
        # Original body preserved
        assert "Build the tracker self-hosting verbs" in text

    def test_tracker_init_idempotent(self, tmp_path: Path, tracker_fixture: Path):
        target = tmp_path / "vault" / "org-llm-dev-tracker.org"
        first = init_tracker(target, tracker_fixture)
        assert first.created is True
        second = init_tracker(target, tracker_fixture)
        assert second.created is False
        assert "target exists" in second.skipped_reason

    def test_tracker_init_force_overwrites(self, tmp_path: Path, tracker_fixture: Path):
        target = tmp_path / "vault" / "org-llm-dev-tracker.org"
        init_tracker(target, tracker_fixture)
        # Modify target so we can detect the overwrite
        target.write_text("stale content")
        result = init_tracker(target, tracker_fixture, force=True)
        assert result.created is True
        assert "stale content" not in target.read_text()
        assert "Build the tracker self-hosting verbs" in target.read_text()

    def test_tracker_init_missing_source(self, tmp_path: Path):
        target = tmp_path / "vault" / "tracker.org"
        result = init_tracker(target, tmp_path / "nonexistent.org")
        assert result.created is False
        assert "source missing" in result.skipped_reason


# ── review verb ──────────────────────────────────────────────────────────────

class TestTrackerReview:
    def test_tracker_review_runs(self, tracker_fixture: Path,
                                  claims_fixture: Path, repo_root: Path):
        report = review_tracker(tracker_fixture, claims_fixture, repo_root,
                                 commits_n=5)
        # All four section headers should appear
        assert "STARTED claims" in report
        assert "In-flight (@active + @plan)" in report
        assert "Shipped" in report
        assert "Blockers / surprises" in report
        # The fixture STARTED claim shows up
        assert "test-actor — sample claim" in report
        # The :surprise: tagged TODO shows up
        assert "A surprise" in report
        # The :@active: STARTED entry shows up
        assert "Build the tracker self-hosting verbs" in report

    def test_tracker_review_handles_missing_files(self, tmp_path: Path,
                                                    repo_root: Path):
        report = review_tracker(
            tmp_path / "nope-tracker.org",
            tmp_path / "nope-claims.org",
            repo_root, commits_n=3,
        )
        # Should report missing files but not crash
        assert "tracker missing" in report
        assert "no active-claims" in report

    def test_tracker_review_bounded_lines(self, tracker_fixture: Path,
                                           claims_fixture: Path,
                                           repo_root: Path):
        report = review_tracker(tracker_fixture, claims_fixture, repo_root,
                                 commits_n=20, max_lines=80)
        assert len(report.splitlines()) <= 80


# ── pace verb ────────────────────────────────────────────────────────────────

class TestTrackerPace:
    def test_tracker_pace_runs(self, tracker_fixture: Path):
        report = pace_tracker(tracker_fixture)
        assert "Pace" in report
        assert "items with EFFORT" in report

    def test_tracker_pace_no_efforts(self, tmp_path: Path):
        """No EFFORT properties → no crash, just an explanatory line."""
        no_effort = tmp_path / "no-effort.org"
        no_effort.write_text(
            "#+TITLE: No effort fixture\n"
            "* Active                                                  :@active:\n"
            "** TODO Item without effort                                :plan:\n"
            "Body without :EFFORT:.\n"
        )
        report = pace_tracker(no_effort)
        assert "Pace" in report
        assert "no items with parseable" in report.lower()

    def test_tracker_pace_flags_overrun(self, tracker_fixture: Path):
        """Fixture has a clear 3:00 clock vs 1h estimate; should flag."""
        report = pace_tracker(tracker_fixture)
        # The "Already-overrun work" item: 3.0h actual / 1.0h estimate = 3x
        assert "Already-overrun work" in report

    def test_tracker_pace_missing_file(self, tmp_path: Path):
        report = pace_tracker(tmp_path / "missing.org")
        assert "tracker missing" in report


# ── CLI integration ──────────────────────────────────────────────────────────

class TestTrackerCLI:
    def test_init_via_cli(self, tmp_path: Path, monkeypatch,
                            tracker_fixture: Path):
        target = tmp_path / "vault" / "org-llm-dev-tracker.org"
        monkeypatch.setenv("ORG_LLM_TRACKER_PATH", str(target))
        result = runner.invoke(app, [
            "tracker", "init",
            "--source", str(tracker_fixture),
        ])
        assert result.exit_code == 0, result.output
        assert target.exists()
        assert "@riker initialized" in result.output

    def test_init_idempotent_via_cli(self, tmp_path: Path, monkeypatch,
                                       tracker_fixture: Path):
        target = tmp_path / "vault" / "org-llm-dev-tracker.org"
        monkeypatch.setenv("ORG_LLM_TRACKER_PATH", str(target))
        # First run creates
        runner.invoke(app, ["tracker", "init", "--source", str(tracker_fixture)])
        # Second run bails gracefully
        result = runner.invoke(app, [
            "tracker", "init", "--source", str(tracker_fixture),
        ])
        assert result.exit_code == 0
        assert "skipped" in result.output.lower() or "exists" in result.output.lower()

    def test_review_via_cli(self, tmp_path: Path, monkeypatch,
                              tracker_fixture: Path, claims_fixture: Path):
        monkeypatch.setenv("ORG_LLM_TRACKER_PATH", str(tracker_fixture))
        monkeypatch.setenv("ORG_LLM_CLAIMS_PATH", str(claims_fixture))
        result = runner.invoke(app, ["tracker", "review", "--commits", "3"])
        assert result.exit_code == 0, result.output
        assert "STARTED claims" in result.output

    def test_pace_via_cli(self, tmp_path: Path, monkeypatch,
                            tracker_fixture: Path):
        monkeypatch.setenv("ORG_LLM_TRACKER_PATH", str(tracker_fixture))
        result = runner.invoke(app, ["tracker", "pace"])
        assert result.exit_code == 0, result.output
        assert "Pace" in result.output
