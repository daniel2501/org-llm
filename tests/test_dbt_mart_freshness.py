"""Regression guard for the 2026-05-06 timestamp-drift bug.

The Superset tier-1 probe found that `llm_calls.timestamp` and
`history.timestamp` for the same 1000 events disagreed by ~6 days. Root
cause: dbt marts were materialized as `table` and snapshotted at the
last `dbt build` while `history` kept growing. The fix in
`org_llm/dbt_templates/dbt_project.yml` flips the four event-stream
marts (llm_calls, cli_invocations, recent_activity, recent_nodes) to
`view`, so they always reflect live `history`.

This test pins the materialization to prevent a casual revert.
"""

from pathlib import Path

import yaml

DBT_PROJECT = Path(__file__).parent.parent / "org_llm" / "dbt_templates" / "dbt_project.yml"

EVENT_STREAM_MARTS = ("llm_calls", "cli_invocations", "recent_activity", "recent_nodes")


def test_event_stream_marts_are_views() -> None:
    cfg = yaml.safe_load(DBT_PROJECT.read_text())
    marts = cfg["models"]["org_llm"]["marts"]
    assert marts["+materialized"] == "table", "default mart materialization should remain table"
    for mart in EVENT_STREAM_MARTS:
        assert mart in marts, f"missing mart override for {mart!r}"
        assert marts[mart]["+materialized"] == "view", (
            f"{mart} must stay a view — materializing as table reintroduces the "
            f"2026-05-06 timestamp drift bug (see docs/wiki/2026-05-06-superset-tier1-probe.org)"
        )
