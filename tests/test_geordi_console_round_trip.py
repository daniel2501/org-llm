"""Round-trip test for the =@geordi= analytics-infrastructure console.

The =@geordi= console is org-llm's first persistent Layer 2
specialist dashboard — meta-analytics for the analytics pipeline
itself (not the data flowing through it). It mirrors the
Captain's Bridge round-trip contract but goes deeper on a
narrower slice: 4 charts focused on the LLM call layer + the
crew-log audit.

Template at =docs/templates/@geordi-console.org.example=:

- 4 charts, all using existing v0 registry metrics
  (=llm_max_ms=, =llm_avg_ms=, =llm_calls=, =crew_actions=).
- Each chart's SQL is byte-identical to the registry's metric
  expression so the dashboard and an =@analyst= agent answer
  with the same numbers.
- Dataset UUIDs stay byte-stable across this bundle and
  =Registry.emit_superset()= so charts stitch to the right
  datasets at import time.

These tests pin all of the above so a metric-expression drift in
either =registry.yaml= or this template fails CI before it lands
in Superset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from org_llm.metrics import Registry
from org_llm.superset_import import OrgDashboard, _stable_uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    REPO_ROOT / "docs" / "templates" / "@geordi-console.org.example"
)

# Chart slug → (registry metric, expected SQL fragment from the
# registry's metric expression). Order matches the .org file and
# defines the dashboard layout order.
EXPECTED_CHARTS: list[tuple[str, str, str]] = [
    ("tail_latency_by_model", "llm_max_ms", "MAX(duration_ms)"),
    ("mean_latency_drift", "llm_avg_ms", "AVG(duration_ms)"),
    ("embed_vs_chat_throughput", "llm_calls", "COUNT(*)"),
    ("crew_action_mix", "crew_actions", "COUNT(*)"),
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
    assert dashboard.title == "@geordi — analytics-infrastructure console"
    assert dashboard.slug == "geordi-console"
    assert dashboard.database_name == "org-llm"


def test_template_has_four_charts(dashboard: OrgDashboard) -> None:
    assert len(dashboard.charts) == 4
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
        "tail_latency_by_model": "line",
        "mean_latency_drift": "line",
        "embed_vs_chat_throughput": "line",
        "crew_action_mix": "heatmap",
    }
    by_name = {c.name: c for c in dashboard.charts}
    for name, viz in expected_viz.items():
        assert by_name[name].viz_type == viz, (
            f"{name}: expected viz_type {viz}, got {by_name[name].viz_type}"
        )


# ---------------------------- bundle shape -----------------------------------


def test_bundle_contains_exactly_four_chart_yamls(
    bundle: dict[str, bytes],
) -> None:
    chart_keys = sorted(
        k for k in bundle if k.startswith("geordi_console/charts/")
    )
    assert len(chart_keys) == 4, chart_keys
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
            bundle[f"geordi_console/charts/{slug}.yaml"]
        )
        metric = reg.metrics[metric_name]
        ds_path = (
            f"geordi_console/datasets/org_llm/"
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
            f"geordi_console/datasets/org_llm/"
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


def test_chart_sql_bodies_carry_registry_expressions(
    dashboard: OrgDashboard,
) -> None:
    """Sanity-check the Babel SQL body itself (not just the dataset
    YAML) contains the registry expression. Defends against a future
    template-author hand-rolling the SQL away from the registry."""
    for chart, (_, _metric, frag) in zip(dashboard.charts, EXPECTED_CHARTS):
        assert frag in chart.sql, (
            f"{chart.name}: expected {frag!r} in Babel SQL body, "
            f"got {chart.sql!r}"
        )


# ---------------------------- UUID byte-identity -----------------------------


def test_llm_calls_dataset_uuid_byte_identical_to_registry(
    bundle: dict[str, bytes], tmp_path: Path
) -> None:
    """The =llm_calls= dataset UUID emitted by this console's bundle
    must equal the UUID =Registry.emit_superset()= writes for the
    same source — so the console can import alongside (or after) a
    registry emit (or the Captain's Bridge) without forking dataset
    records."""
    bridge_ds = yaml.safe_load(
        bundle["geordi_console/datasets/org_llm/llm_calls.yaml"]
    )
    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_ds = yaml.safe_load(
        (out_dir / "datasets" / "org_llm" / "llm_calls.yaml").read_text()
    )
    assert bridge_ds["uuid"] == on_disk_ds["uuid"]
    assert bridge_ds["uuid"] == _stable_uuid("dataset", "llm")


def test_crew_log_dataset_uuid_byte_identical_to_registry(
    bundle: dict[str, bytes], tmp_path: Path
) -> None:
    """Same byte-identity check for the crew_log source — the
    =crew_action_mix= heatmap depends on the registry's =crew=
    dataset UUID matching what the registry itself emits."""
    bridge_ds = yaml.safe_load(
        bundle["geordi_console/datasets/org_llm/crew_log.yaml"]
    )
    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_ds = yaml.safe_load(
        (out_dir / "datasets" / "org_llm" / "crew_log.yaml").read_text()
    )
    assert bridge_ds["uuid"] == on_disk_ds["uuid"]
    assert bridge_ds["uuid"] == _stable_uuid("dataset", "crew")


def test_database_uuid_byte_identical_to_registry(
    bundle: dict[str, bytes], tmp_path: Path
) -> None:
    """Same byte-identity check at the database layer — the console
    and the registry must share one Database row in Superset."""
    bridge_db = yaml.safe_load(
        bundle["geordi_console/databases/org_llm.yaml"]
    )
    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_db = yaml.safe_load(
        (out_dir / "databases" / "org_llm.yaml").read_text()
    )
    assert bridge_db["uuid"] == on_disk_db["uuid"]


# ---------------------------- dashboard order --------------------------------


def test_dashboard_yaml_lists_all_four_charts_in_order(
    bundle: dict[str, bytes],
) -> None:
    """Single-column stack, one row per chart. Walking GRID_ID →
    ROW children → CHART must yield the four chart UUIDs in the
    order they appear in the .org file."""
    dash = yaml.safe_load(
        bundle["geordi_console/dashboards/geordi-console.yaml"]
    )
    position = dash["position"]
    grid_rows = position["GRID_ID"]["children"]
    assert len(grid_rows) == 4, (
        f"expected 4 rows in dashboard, got {len(grid_rows)}"
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
        bundle["geordi_console/dashboards/geordi-console.yaml"]
    )
    assert dash["dashboard_title"] == (
        "@geordi — analytics-infrastructure console"
    )
    assert dash["slug"] == "geordi-console"
    assert dash["uuid"] == _stable_uuid("dashboard", "geordi-console")


# ---------------------------- bundle metadata --------------------------------


def test_bundle_metadata_says_dashboard(bundle: dict[str, bytes]) -> None:
    md = yaml.safe_load(bundle["geordi_console/metadata.yaml"])
    assert md["type"] == "Dashboard"
