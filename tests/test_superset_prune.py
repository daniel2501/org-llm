"""Tests for the `org-llm superset prune` verb.

Covers the four contract-shaped seams of `org_llm/superset_prune.py`:

  - duration parsing (Nd/Nh/Nw)
  - slug-convention recognition (org-llm vs user-authored)
  - dry-run vs apply behavior (CLI level)
  - delete-order: charts → dashboards → datasets

All HTTP is mocked — no live Superset required. The mocks mirror the
shape of `requests.Session` just enough that `SupersetClient` works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from org_llm.superset_prune import (
    PruneCandidate,
    build_candidates,
    execute_prune,
    is_org_llm_dashboard,
    parse_duration,
    split_auth,
)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


def test_parse_duration_days() -> None:
    assert parse_duration("7d") == timedelta(days=7)


def test_parse_duration_hours() -> None:
    assert parse_duration("24h") == timedelta(hours=24)
    # 24h is the same as 1d for practical purposes.
    assert parse_duration("24h") == timedelta(days=1)


def test_parse_duration_weeks() -> None:
    assert parse_duration("1w") == timedelta(weeks=1)
    assert parse_duration("1w") == timedelta(days=7)


def test_parse_duration_case_insensitive() -> None:
    assert parse_duration("7D") == timedelta(days=7)
    assert parse_duration("2W") == timedelta(weeks=2)


def test_parse_duration_rejects_minutes() -> None:
    with pytest.raises(ValueError):
        parse_duration("30m")


def test_parse_duration_rejects_bare_int() -> None:
    with pytest.raises(ValueError):
        parse_duration("30")


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("eventually")


# ---------------------------------------------------------------------------
# split_auth
# ---------------------------------------------------------------------------


def test_split_auth_basic() -> None:
    assert split_auth("admin:hunter2") == ("admin", "hunter2")


def test_split_auth_password_with_colon() -> None:
    # First colon splits — colons in the password survive.
    assert split_auth("admin:p:a:s:s") == ("admin", "p:a:s:s")


def test_split_auth_missing_colon() -> None:
    with pytest.raises(ValueError):
        split_auth("admin")


def test_split_auth_empty_user() -> None:
    with pytest.raises(ValueError):
        split_auth(":secret")


def test_split_auth_empty_pass() -> None:
    with pytest.raises(ValueError):
        split_auth("admin:")


# ---------------------------------------------------------------------------
# is_org_llm_dashboard
# ---------------------------------------------------------------------------


def test_recognizes_oneoff_slug() -> None:
    assert is_org_llm_dashboard({"slug": "oneoff-2026-05-06-llm-time"})


def test_recognizes_pinned_slug() -> None:
    assert is_org_llm_dashboard({"slug": "pinned-picard-bridge"})


def test_rejects_user_slug() -> None:
    assert not is_org_llm_dashboard({"slug": "team-okrs"})


def test_rejects_substring_match() -> None:
    # `team-oneoff-survey` is not anchored at start.
    assert not is_org_llm_dashboard({"slug": "team-oneoff-survey"})


def test_rejects_missing_slug() -> None:
    assert not is_org_llm_dashboard({"slug": None})
    assert not is_org_llm_dashboard({})


# ---------------------------------------------------------------------------
# build_candidates: 5 dashboards, 3 org-llm-shaped, 2 user-shaped
# ---------------------------------------------------------------------------


def _fake_dashboards(now: datetime) -> list[dict[str, Any]]:
    """Build a fake list response. Three org-llm-shaped, two user."""
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000000")  # noqa: E731
    return [
        {
            "id": 1,
            "slug": "oneoff-2026-04-01-old",
            "dashboard_title": "Old one-off",
            "created_on": fmt(now - timedelta(days=60)),
        },
        {
            "id": 2,
            "slug": "oneoff-2026-05-05-recent",
            "dashboard_title": "Recent one-off",
            "created_on": fmt(now - timedelta(days=1)),
        },
        {
            "id": 3,
            "slug": "pinned-picard-bridge",
            "dashboard_title": "Captain's Bridge",
            "created_on": fmt(now - timedelta(days=45)),
        },
        {
            "id": 4,
            "slug": "team-okrs",
            "dashboard_title": "Team OKRs (user-authored)",
            "created_on": fmt(now - timedelta(days=365)),
        },
        {
            "id": 5,
            "slug": None,
            "dashboard_title": "Hand-authored (no slug)",
            "created_on": fmt(now - timedelta(days=999)),
        },
    ]


def test_build_candidates_filters_to_org_llm_only() -> None:
    now = datetime(2026, 5, 6, tzinfo=timezone.utc)
    dashboards = _fake_dashboards(now)
    candidates = build_candidates(
        dashboards,
        cutoff=timedelta(days=30),
        now=now,
        fetch_charts=lambda _id: [],
        fetch_datasets=lambda _id: [],
    )
    assert {c.id for c in candidates} == {1, 2, 3}
    # 1 (60d) and 3 (45d) are over the 30d cutoff; 2 (1d) is not.
    assert {c.id for c in candidates if c.will_delete} == {1, 3}


def test_build_candidates_respects_cutoff_size() -> None:
    now = datetime(2026, 5, 6, tzinfo=timezone.utc)
    dashboards = _fake_dashboards(now)
    candidates = build_candidates(
        dashboards,
        cutoff=timedelta(hours=12),  # everything older than 12h
        now=now,
        fetch_charts=lambda _id: [],
        fetch_datasets=lambda _id: [],
    )
    # All three org-llm dashboards are now over the cutoff.
    assert {c.id for c in candidates if c.will_delete} == {1, 2, 3}


# ---------------------------------------------------------------------------
# execute_prune: order matters
# ---------------------------------------------------------------------------


def test_execute_prune_orders_chart_dashboard_dataset() -> None:
    """Charts must be deleted before dashboards before datasets so
    Superset's metadata-DB FK constraints don't fire."""
    client = MagicMock()
    call_order: list[str] = []

    client.delete_chart.side_effect = lambda cid: call_order.append(
        f"chart:{cid}"
    )
    client.delete_dashboard.side_effect = lambda did: call_order.append(
        f"dashboard:{did}"
    )
    client.delete_dataset.side_effect = lambda did: call_order.append(
        f"dataset:{did}"
    )

    cands = [
        PruneCandidate(
            id=1,
            slug="oneoff-x",
            title="x",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chart_ids=[10, 11],
            dataset_ids=[100],
            age=timedelta(days=120),
            will_delete=True,
        ),
        PruneCandidate(
            id=2,
            slug="pinned-y",
            title="y",
            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            chart_ids=[12],
            dataset_ids=[101],
            age=timedelta(days=90),
            will_delete=True,
        ),
    ]
    counts = execute_prune(client, cands)

    # Counts.
    assert counts == {"charts": 3, "dashboards": 2, "datasets": 2}

    # Order: every chart:* comes before any dashboard:*; every
    # dashboard:* comes before any dataset:*.
    chart_idxs = [i for i, x in enumerate(call_order) if x.startswith("chart:")]
    dashboard_idxs = [
        i for i, x in enumerate(call_order) if x.startswith("dashboard:")
    ]
    dataset_idxs = [
        i for i, x in enumerate(call_order) if x.startswith("dataset:")
    ]
    assert max(chart_idxs) < min(dashboard_idxs)
    assert max(dashboard_idxs) < min(dataset_idxs)


def test_execute_prune_skips_non_will_delete() -> None:
    client = MagicMock()
    cands = [
        PruneCandidate(
            id=1,
            slug="oneoff-keep",
            title="keep",
            created_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
            chart_ids=[1, 2],
            dataset_ids=[10],
            age=timedelta(days=1),
            will_delete=False,
        ),
    ]
    counts = execute_prune(client, cands)
    assert counts == {"charts": 0, "dashboards": 0, "datasets": 0}
    client.delete_chart.assert_not_called()
    client.delete_dashboard.assert_not_called()
    client.delete_dataset.assert_not_called()


def test_execute_prune_dedupes_datasets_across_dashboards() -> None:
    """Two org-llm dashboards may share registry datasets — we must
    not DELETE the same dataset twice (Superset returns 404)."""
    client = MagicMock()
    cands = [
        PruneCandidate(
            id=1, slug="oneoff-a", title="a",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chart_ids=[10], dataset_ids=[100, 101],
            age=timedelta(days=120), will_delete=True,
        ),
        PruneCandidate(
            id=2, slug="oneoff-b", title="b",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chart_ids=[11], dataset_ids=[100, 102],  # 100 is shared
            age=timedelta(days=120), will_delete=True,
        ),
    ]
    counts = execute_prune(client, cands)
    assert counts["datasets"] == 3  # 100, 101, 102 — each deleted once
    assert client.delete_dataset.call_count == 3


# ---------------------------------------------------------------------------
# CLI integration: dry-run vs apply, missing-auth, etc.
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _patch_client(monkeypatch, dashboards: list[dict[str, Any]]):
    """Replace SupersetClient.__init__ + the network methods so we
    don't make any HTTP calls during CLI tests."""
    from org_llm import superset_prune as sp

    def _no_login(self, url, auth, timeout=30.0):
        self.url = url
        self.timeout = timeout
        self.session = MagicMock()
        self._requests = MagicMock()

    monkeypatch.setattr(sp.SupersetClient, "__init__", _no_login)
    monkeypatch.setattr(
        sp.SupersetClient,
        "list_dashboards",
        lambda self: dashboards,
    )
    monkeypatch.setattr(
        sp.SupersetClient,
        "get_dashboard_charts",
        lambda self, did: [did * 10, did * 10 + 1],
    )
    monkeypatch.setattr(
        sp.SupersetClient,
        "get_dashboard_datasets",
        lambda self, did: [did * 100],
    )
    deletes: dict[str, list[int]] = {
        "charts": [], "dashboards": [], "datasets": [],
    }
    monkeypatch.setattr(
        sp.SupersetClient,
        "delete_chart",
        lambda self, cid: deletes["charts"].append(cid),
    )
    monkeypatch.setattr(
        sp.SupersetClient,
        "delete_dashboard",
        lambda self, did: deletes["dashboards"].append(did),
    )
    monkeypatch.setattr(
        sp.SupersetClient,
        "delete_dataset",
        lambda self, did: deletes["datasets"].append(did),
    )
    return deletes


def test_cli_dry_run_makes_no_deletes(
    cli_runner: CliRunner, monkeypatch
) -> None:
    from org_llm.cli import app

    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000000")  # noqa: E731
    dashboards = [
        {
            "id": 1,
            "slug": "oneoff-old",
            "dashboard_title": "x",
            "created_on": fmt(now - timedelta(days=60)),
        },
    ]
    deletes = _patch_client(monkeypatch, dashboards)

    result = cli_runner.invoke(
        app,
        ["superset", "prune", "--older-than", "30d", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert deletes == {"charts": [], "dashboards": [], "datasets": []}
    assert "would be deleted" in result.output or "dry-run" in result.output


def test_cli_apply_makes_deletes(
    cli_runner: CliRunner, monkeypatch
) -> None:
    from org_llm.cli import app

    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000000")  # noqa: E731
    dashboards = [
        {
            "id": 7,
            "slug": "oneoff-stale",
            "dashboard_title": "stale",
            "created_on": fmt(now - timedelta(days=60)),
        },
        {
            "id": 8,
            "slug": "team-keepme",  # user-authored — must be ignored
            "dashboard_title": "keep",
            "created_on": fmt(now - timedelta(days=999)),
        },
    ]
    deletes = _patch_client(monkeypatch, dashboards)

    result = cli_runner.invoke(
        app,
        ["superset", "prune", "--older-than", "30d", "--apply"],
    )
    assert result.exit_code == 0, result.output
    # Dashboard 7 deleted; user-authored dashboard 8 untouched.
    assert deletes["dashboards"] == [7]
    assert deletes["charts"] == [70, 71]  # 7*10, 7*10+1
    assert deletes["datasets"] == [700]


def test_cli_no_org_llm_dashboards(
    cli_runner: CliRunner, monkeypatch
) -> None:
    from org_llm.cli import app

    deletes = _patch_client(monkeypatch, [
        {"id": 1, "slug": "team-okrs", "dashboard_title": "u",
         "created_on": "2026-01-01T00:00:00.000000"},
    ])
    result = cli_runner.invoke(
        app,
        ["superset", "prune", "--older-than", "30d", "--apply"],
    )
    assert result.exit_code == 0, result.output
    assert deletes == {"charts": [], "dashboards": [], "datasets": []}


def test_cli_invalid_duration_clear_error(
    cli_runner: CliRunner, monkeypatch
) -> None:
    from org_llm.cli import app

    _patch_client(monkeypatch, [])
    result = cli_runner.invoke(
        app,
        ["superset", "prune", "--older-than", "forever", "--dry-run"],
    )
    assert result.exit_code == 1
    assert "invalid --older-than" in result.output


def test_cli_invalid_auth_clear_error(
    cli_runner: CliRunner, monkeypatch
) -> None:
    from org_llm.cli import app

    _patch_client(monkeypatch, [])
    result = cli_runner.invoke(
        app,
        [
            "superset", "prune",
            "--older-than", "30d",
            "--auth", "no-colon-here",
            "--dry-run",
        ],
    )
    assert result.exit_code == 1
    assert "invalid --auth" in result.output


def test_cli_auth_failure_surfaces(
    cli_runner: CliRunner, monkeypatch
) -> None:
    """If SupersetClient construction raises (e.g. wrong creds), the
    CLI exits non-zero with a clear message."""
    from org_llm import superset_prune as sp
    from org_llm.cli import app

    def _boom(self, url, auth, timeout=30.0):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(sp.SupersetClient, "__init__", _boom)
    result = cli_runner.invoke(
        app,
        ["superset", "prune", "--older-than", "30d", "--dry-run"],
    )
    assert result.exit_code == 2
    assert "auth failed" in result.output
