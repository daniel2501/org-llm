"""Round-trip test for the Captain's Bridge dashboard template.

The Captain's Bridge is the first persistent Superset dashboard in
org-llm — owned by =@picard=, opened daily for a 30-second pulse
check. The template at
=docs/templates/org-llm-captains-bridge.org.example= is the
authoritative source: six charts, one dashboard, single-column
layout. Each chart binds to a registry metric so the dashboard and
the =@analyst= agent answer with the same numbers.

These tests assert the contract:

- The =.org= file parses cleanly via =OrgDashboard.from_org()=.
- The bundle contains exactly six chart YAMLs.
- Each chart's referenced metric expression (via its dataset) is
  byte-identical to what the registry would emit — i.e. chart 2's
  =llm_avg_ms= dataset metric contains =AVG(duration_ms)=.
- The dataset UUIDs in the bundle match what =Registry.emit_superset()=
  emits for the same source — load-bearing, since Superset stitches
  charts to datasets by UUID at import time.
- The dashboard YAML lists all six charts in template order.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from org_llm.metrics import Registry
from org_llm.superset_import import OrgDashboard, _stable_uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    REPO_ROOT / "docs" / "templates" / "org-llm-captains-bridge.org.example"
)

# Chart slug → (registry metric, expected SQL fragment from the
# registry's metric expression). Order matches the .org file and
# defines the dashboard layout order.
EXPECTED_CHARTS: list[tuple[str, str, str]] = [
    ("agent_delegation_heatmap", "crew_actions", "COUNT(*)"),
    ("model_cost_latency", "llm_avg_ms", "AVG(duration_ms)"),
    # Redesigned 2026-05-07 by @geordi: error_rate single bar lacked
    # sample-size context — replaced with stacked outcome bars per model.
    ("model_usage_by_outcome", "llm_calls", "COUNT(*)"),
    ("captains_log_heatmap", "hist_events", "COUNT(*)"),
    # Redesigned 2026-05-07 by @geordi: probe_alert_rate averaged
    # away spikes — replaced with sensor_alerts heatmap per probe.
    (
        "probe_alert_pattern",
        "sensor_alerts",
        "SUM(CASE WHEN status IN",
    ),
    # Redesigned 2026-05-07 by @geordi: hist_kind_count flat-line
    # → hist_events stacked area for actual workload composition.
    (
        "history_workload_composition",
        "hist_events",
        "COUNT(*)",
    ),
]


@pytest.fixture(scope="module")
def dashboard() -> OrgDashboard:
    assert TEMPLATE_PATH.exists(), f"missing template: {TEMPLATE_PATH}"
    return OrgDashboard.from_org(TEMPLATE_PATH)


@pytest.fixture(scope="module")
def bundle(dashboard: OrgDashboard) -> dict[str, bytes]:
    return dashboard.to_bundle()


# ---------------------------- header parse -----------------------------------


def test_template_parses_cleanly(dashboard: OrgDashboard) -> None:
    assert dashboard.title == "Captain's Bridge — org-llm vital signs"
    assert dashboard.slug == "captains-bridge"
    assert dashboard.database_name == "org-llm"


def test_template_has_six_charts(dashboard: OrgDashboard) -> None:
    assert len(dashboard.charts) == 6
    actual_names = [c.name for c in dashboard.charts]
    expected_names = [name for name, _m, _frag in EXPECTED_CHARTS]
    assert actual_names == expected_names


def test_charts_bind_registered_metrics(dashboard: OrgDashboard) -> None:
    reg = Registry.load()
    for chart, (_, expected_metric, _) in zip(
        dashboard.charts, EXPECTED_CHARTS
    ):
        assert chart.metric_name == expected_metric, (
            f"{chart.name}: expected metric {expected_metric}, "
            f"got {chart.metric_name}"
        )
        assert chart.metric_name in reg.metrics, (
            f"{chart.name} cites unknown registry metric "
            f"{chart.metric_name!r}"
        )


def test_charts_carry_viz_types(dashboard: OrgDashboard) -> None:
    expected_viz = {
        "agent_delegation_heatmap": "heatmap",
        "model_cost_latency": "bar",
        "model_usage_by_outcome": "dist_bar",
        "captains_log_heatmap": "heatmap",
        "probe_alert_pattern": "heatmap",
        "history_workload_composition": "area",
    }
    by_name = {c.name: c for c in dashboard.charts}
    for name, viz in expected_viz.items():
        assert by_name[name].viz_type == viz, (
            f"{name}: expected viz_type {viz}, got {by_name[name].viz_type}"
        )


# ---------------------------- bundle shape -----------------------------------


def test_bundle_contains_exactly_six_chart_yamls(
    bundle: dict[str, bytes],
) -> None:
    chart_keys = sorted(
        k for k in bundle if k.startswith("captains_bridge/charts/")
    )
    assert len(chart_keys) == 6, chart_keys
    for chart_key in chart_keys:
        assert chart_key.endswith(".yaml")


def test_each_chart_resolves_to_registry_dataset(
    bundle: dict[str, bytes],
) -> None:
    """Every chart names a registered metric; that metric's source
    must produce a dataset YAML in the bundle, and the chart's
    dataset_uuid must equal the dataset's uuid."""
    reg = Registry.load()
    for slug, metric_name, _frag in EXPECTED_CHARTS:
        chart_yaml = yaml.safe_load(
            bundle[f"captains_bridge/charts/{slug}.yaml"]
        )
        metric = reg.metrics[metric_name]
        ds_path = (
            f"captains_bridge/datasets/org_llm/"
            f"{reg.sources[metric.source].table}.yaml"
        )
        assert ds_path in bundle, (
            f"{slug}: dataset YAML missing for source {metric.source}"
        )
        ds_yaml = yaml.safe_load(bundle[ds_path])
        assert chart_yaml["dataset_uuid"] == ds_yaml["uuid"], (
            f"{slug}: chart.dataset_uuid != dataset.uuid"
        )


def test_chart_metric_expressions_carry_registry_sql(
    bundle: dict[str, bytes],
) -> None:
    """For each chart, walk to its dataset YAML and confirm the
    registry's metric expression appears verbatim — that's the merge
    bar from the survey: dashboard SQL must equal registry SQL."""
    reg = Registry.load()
    for slug, metric_name, frag in EXPECTED_CHARTS:
        metric = reg.metrics[metric_name]
        ds_path = (
            f"captains_bridge/datasets/org_llm/"
            f"{reg.sources[metric.source].table}.yaml"
        )
        ds_yaml = yaml.safe_load(bundle[ds_path])
        metric_exprs = {
            m["metric_name"]: m["expression"] for m in ds_yaml["metrics"]
        }
        expected = reg._superset_metric_expression(metric)
        assert metric_exprs[metric_name] == expected, (
            f"{slug}: dataset metric expr drift for {metric_name}"
        )
        assert frag in metric_exprs[metric_name], (
            f"{slug}: expected {frag!r} in metric expression "
            f"{metric_exprs[metric_name]!r}"
        )


# ---------------------------- UUID byte-identity -----------------------------


def test_llm_calls_dataset_uuid_byte_identical_to_registry(
    bundle: dict[str, bytes], tmp_path: Path
) -> None:
    """The =llm_calls= dataset UUID emitted by the Captain's Bridge
    bundle must equal the UUID =Registry.emit_superset()= writes for
    the same source — so the bridge can import alongside (or after)
    a registry emit without forking dataset records."""
    bridge_ds = yaml.safe_load(
        bundle["captains_bridge/datasets/org_llm/llm_calls.yaml"]
    )
    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_ds = yaml.safe_load(
        (out_dir / "datasets" / "org_llm" / "llm_calls.yaml").read_text()
    )
    assert bridge_ds["uuid"] == on_disk_ds["uuid"]
    assert bridge_ds["uuid"] == _stable_uuid("dataset", "llm")


def test_database_uuid_byte_identical_to_registry(
    bundle: dict[str, bytes], tmp_path: Path
) -> None:
    """Same byte-identity check at the database layer — the bridge
    and the registry must share one Database row in Superset."""
    bridge_db = yaml.safe_load(
        bundle["captains_bridge/databases/org_llm.yaml"]
    )
    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_db = yaml.safe_load(
        (out_dir / "databases" / "org_llm.yaml").read_text()
    )
    assert bridge_db["uuid"] == on_disk_db["uuid"]


# ---------------------------- dashboard order --------------------------------


def test_dashboard_yaml_lists_all_six_charts_in_order(
    bundle: dict[str, bytes],
) -> None:
    """The position layout is a single-column stack — one row per
    chart in template order. Walking GRID_ID → ROW children → CHART
    must yield the six chart UUIDs in the order they appear in the
    .org file."""
    dash = yaml.safe_load(
        bundle["captains_bridge/dashboards/captains-bridge.yaml"]
    )
    position = dash["position"]
    grid_rows = position["GRID_ID"]["children"]
    assert len(grid_rows) == 6, (
        f"expected 6 rows in dashboard, got {len(grid_rows)}"
    )

    actual_chart_uuids: list[str] = []
    for row_key in grid_rows:
        row = position[row_key]
        assert row["type"] == "ROW"
        children = row["children"]
        assert len(children) == 1, (
            f"{row_key}: expected 1 chart per row, got {len(children)}"
        )
        chart_node = position[children[0]]
        assert chart_node["type"] == "CHART"
        actual_chart_uuids.append(chart_node["meta"]["uuid"])

    expected_chart_uuids = [
        _stable_uuid("chart", slug) for slug, _m, _f in EXPECTED_CHARTS
    ]
    assert actual_chart_uuids == expected_chart_uuids


def test_dashboard_carries_correct_title_and_slug(
    bundle: dict[str, bytes],
) -> None:
    dash = yaml.safe_load(
        bundle["captains_bridge/dashboards/captains-bridge.yaml"]
    )
    assert dash["dashboard_title"] == "Captain's Bridge — org-llm vital signs"
    assert dash["slug"] == "captains-bridge"
    assert dash["uuid"] == _stable_uuid("dashboard", "captains-bridge")


# ---------------------------- bundle metadata --------------------------------


def test_bundle_metadata_says_dashboard(bundle: dict[str, bytes]) -> None:
    md = yaml.safe_load(bundle["captains_bridge/metadata.yaml"])
    assert md["type"] == "Dashboard"
