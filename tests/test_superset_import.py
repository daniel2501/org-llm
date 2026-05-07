"""Tests for the Org-as-source Superset preprocessor.

The headline test is a 30-line .org → zip → re-read round-trip that
asserts the bundle has the expected shape and that the chart's SQL /
metric expression match the registry's `metric:llm_avg_ms`. That's
the success metric called out in
docs/wiki/2026-05-06-superset-fork-survey.org.

We also unit-test the Babel-arg parser, the headline → chart
attachment, and the dataset_uuid byte-identity with
`Registry.emit_superset` (since that's the load-bearing seam between
the existing semantic-layer emitter and the new dashboard preprocessor).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from org_llm.metrics import (
    Registry,
    SUPERSET_DATABASE_NAME,
    SUPERSET_IMPORT_VERSION,
)
from org_llm.superset_import import (
    OrgChart,
    OrgDashboard,
    _parse_babel_args,
    _parse_list_arg,
    _stable_uuid,
)


THIRTY_LINE_ORG = """\
#+TITLE: LLM Health
#+SUPERSET_DASHBOARD: llm-health
#+SUPERSET_DATABASE: org-llm
#+OPTIONS: toc:nil num:nil

* Average call duration by kind
:PROPERTIES:
:VIZ_TYPE: bar
:END:

Mean wall-clock duration of LLM calls bucketed by call_kind.
Sourced from the semantic layer so this chart agrees byte-for-byte
with metric:llm_avg_ms group_by:[call_kind].

#+BEGIN_SRC sql :name avg_ms_by_kind :metric llm_avg_ms :group_by call_kind
SELECT call_kind, AVG(duration_ms) AS llm_avg_ms
FROM llm_calls
GROUP BY call_kind
#+END_SRC
"""


@pytest.fixture
def org_path(tmp_path: Path) -> Path:
    p = tmp_path / "llm-health.org"
    p.write_text(THIRTY_LINE_ORG)
    return p


# -------------------------- parser unit tests --------------------------------


def test_parse_babel_args_simple() -> None:
    args = _parse_babel_args(":name foo :metric llm_avg_ms")
    assert args == {"name": "foo", "metric": "llm_avg_ms"}


def test_parse_babel_args_handles_list_literal() -> None:
    args = _parse_babel_args(":name foo :group_by [a, b, c]")
    assert args["group_by"] == "[a, b, c]"
    assert _parse_list_arg(args["group_by"]) == ["a", "b", "c"]


def test_parse_list_arg_singleton() -> None:
    assert _parse_list_arg("call_kind") == ["call_kind"]


def test_parse_list_arg_empty() -> None:
    assert _parse_list_arg("") == []


# -------------------------- from_org parsing ---------------------------------


def test_from_org_extracts_headers(org_path: Path) -> None:
    od = OrgDashboard.from_org(org_path)
    assert od.title == "LLM Health"
    assert od.slug == "llm-health"
    assert od.database_name == "org-llm"
    assert od.source_path == org_path


def test_from_org_extracts_one_chart(org_path: Path) -> None:
    od = OrgDashboard.from_org(org_path)
    assert len(od.charts) == 1
    chart = od.charts[0]
    assert chart.name == "avg_ms_by_kind"
    assert chart.metric_name == "llm_avg_ms"
    assert chart.group_by == ["call_kind"]
    assert chart.viz_type == "bar"  # from the PROPERTIES drawer
    assert chart.title == "Average call duration by kind"
    assert "AVG(duration_ms)" in chart.sql


def test_from_org_falls_back_to_filename_title(tmp_path: Path) -> None:
    p = tmp_path / "untitled.org"
    p.write_text("#+BEGIN_SRC sql :name x :metric llm_calls\nSELECT 1\n#+END_SRC\n")
    od = OrgDashboard.from_org(p)
    assert od.title == "untitled"
    assert od.slug == "untitled"  # slugified from title


# -------------------------- bundle round-trip --------------------------------


def test_to_bundle_has_expected_shape(org_path: Path) -> None:
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    files = set(bundle.keys())
    # Bundle root mirrors the slug with hyphens → underscores.
    assert "llm_health/metadata.yaml" in files
    assert "llm_health/databases/org_llm.yaml" in files
    assert "llm_health/datasets/org_llm/llm_calls.yaml" in files
    assert "llm_health/charts/avg_ms_by_kind.yaml" in files
    assert "llm_health/dashboards/llm-health.yaml" in files


def test_metadata_says_dashboard_not_database(org_path: Path) -> None:
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    md = yaml.safe_load(bundle["llm_health/metadata.yaml"])
    assert md["type"] == "Dashboard"
    assert md["version"] == SUPERSET_IMPORT_VERSION


def test_chart_yaml_carries_registry_metric_expression(org_path: Path) -> None:
    """The merge-bar test from the survey: the chart in the bundle must
    name the registry's metric so its expression equals
    `Registry._superset_metric_expression(llm_avg_ms)`."""
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    chart = yaml.safe_load(bundle["llm_health/charts/avg_ms_by_kind.yaml"])
    assert chart["slice_name"] == "Average call duration by kind"
    # Preprocessor canonicalizes legacy viz types to their modern
    # equivalents so Superset's import-time auto-migrator skips
    # (avoids the form_data-stringification side effect).
    assert chart["viz_type"] == "echarts_timeseries_bar"
    assert chart["params"]["metrics"] == ["llm_avg_ms"]
    # echarts_timeseries_* requires an x_axis; preprocessor promotes
    # the last :group_by column. With a single-element :group_by, that
    # leaves groupby empty and call_kind on x_axis.
    assert chart["params"]["groupby"] == []
    assert chart["params"]["x_axis"] == "call_kind"

    # Walk to the dataset YAML and confirm the metric expression matches
    # what the registry would emit. This is the load-bearing assertion —
    # if these drift the dashboard and the agent disagree on numbers.
    ds = yaml.safe_load(bundle["llm_health/datasets/org_llm/llm_calls.yaml"])
    metric_expr = {
        m["metric_name"]: m["expression"] for m in ds["metrics"]
    }
    reg = Registry.load()
    expected = reg._superset_metric_expression(reg.metrics["llm_avg_ms"])
    assert metric_expr["llm_avg_ms"] == expected
    # Sanity: the AVG(duration_ms) shape should literally appear.
    assert "AVG(duration_ms)" in metric_expr["llm_avg_ms"]


def test_chart_query_context_is_populated(org_path: Path) -> None:
    """Without query_context, GET /api/v1/chart/<id>/data fails with
    "Chart has no query context saved" until the user clicks the chart
    in the UI once. The preprocessor synthesizes one from form_data so
    the data API works on first request after import."""
    import json as _json
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    chart = yaml.safe_load(bundle["llm_health/charts/avg_ms_by_kind.yaml"])
    qc_str = chart["query_context"]
    assert qc_str is not None and isinstance(qc_str, str), (
        "query_context must be a JSON string for Superset to translate "
        "datasource refs at import time"
    )
    qc = _json.loads(qc_str)
    # Datasource id is a placeholder that Superset rewrites at import.
    assert qc["datasource"] == {"id": 0, "type": "table"}
    # Single-query shape with the chart's metric + group_by reflected.
    assert len(qc["queries"]) == 1
    q = qc["queries"][0]
    assert q["metrics"] == ["llm_avg_ms"]
    assert q["columns"] == ["call_kind"]
    # form_data must equal the chart's params so Superset's
    # update_chart_config_dataset can swap the datasource ref atomically.
    assert qc["form_data"] == chart["params"]


def test_chart_dataset_uuid_matches_registry_uuid(org_path: Path) -> None:
    """The chart's dataset_uuid must equal the uuid the registry emits
    for the same source — that's how Superset stitches charts to
    datasets at import time."""
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    chart = yaml.safe_load(bundle["llm_health/charts/avg_ms_by_kind.yaml"])
    ds = yaml.safe_load(bundle["llm_health/datasets/org_llm/llm_calls.yaml"])
    assert chart["dataset_uuid"] == ds["uuid"]
    assert chart["dataset_uuid"] == _stable_uuid("dataset", "llm")


def test_database_uuid_byte_identical_to_registry(
    tmp_path: Path, org_path: Path
) -> None:
    """`Registry.emit_superset` writes a databases/org_llm.yaml on
    disk; the preprocessor's bundle must carry the same UUID under the
    same filename so re-emitting one vs the other is a no-op upgrade."""
    od = OrgDashboard.from_org(org_path)
    bundle = od.to_bundle()
    bundle_db = yaml.safe_load(bundle["llm_health/databases/org_llm.yaml"])

    out_dir = tmp_path / "registry-emit"
    Registry.load().emit_superset(out_dir)
    on_disk_db = yaml.safe_load((out_dir / "databases" / "org_llm.yaml").read_text())
    assert bundle_db["uuid"] == on_disk_db["uuid"]
    assert bundle_db["database_name"] == SUPERSET_DATABASE_NAME


# -------------------------- write_zip round-trip -----------------------------


def test_write_zip_roundtrip(org_path: Path, tmp_path: Path) -> None:
    """Headline test: write a zip, read it back, assert one chart whose
    metric expression contains the registry's `llm_avg_ms` shape."""
    od = OrgDashboard.from_org(org_path)
    out = tmp_path / "out.zip"
    od.write_zip(out)
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "llm_health/metadata.yaml" in names
        assert "llm_health/charts/avg_ms_by_kind.yaml" in names
        assert "llm_health/dashboards/llm-health.yaml" in names
        chart_yaml = zf.read("llm_health/charts/avg_ms_by_kind.yaml")
        ds_yaml = zf.read("llm_health/datasets/org_llm/llm_calls.yaml")
    chart = yaml.safe_load(chart_yaml)
    ds = yaml.safe_load(ds_yaml)
    metric_exprs = {m["metric_name"]: m["expression"] for m in ds["metrics"]}
    assert chart["params"]["metrics"] == ["llm_avg_ms"]
    assert "AVG(duration_ms)" in metric_exprs["llm_avg_ms"]


def test_to_zip_bytes_is_valid_zip(org_path: Path) -> None:
    od = OrgDashboard.from_org(org_path)
    blob = od.to_zip_bytes()
    assert blob[:2] == b"PK"  # zip magic
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert "llm_health/metadata.yaml" in zf.namelist()


def test_bundle_idempotent(org_path: Path) -> None:
    """Two bundles from the same org must be byte-identical so re-runs
    don't churn the import."""
    a = OrgDashboard.from_org(org_path).to_bundle()
    b = OrgDashboard.from_org(org_path).to_bundle()
    assert a == b


# -------------------------- live-Superset POST (skipped) ---------------------


@pytest.mark.skip(reason="requires a running Superset; mark live in CI")
def test_post_to_live_superset(org_path: Path) -> None:  # pragma: no cover
    od = OrgDashboard.from_org(org_path)
    resp = od.post("http://localhost:8088", auth=("admin", "admin"))
    assert "message" in resp or "status" in resp


# -------------------------- viz-type fallback --------------------------------


def test_chart_viz_type_defaults_to_table(tmp_path: Path) -> None:
    """No PROPERTIES drawer on the headline → viz_type=table."""
    org = """\
#+TITLE: t
* untyped
#+BEGIN_SRC sql :name x :metric llm_calls
SELECT COUNT(*) FROM llm_calls
#+END_SRC
"""
    p = tmp_path / "t.org"
    p.write_text(org)
    od = OrgDashboard.from_org(p)
    assert od.charts[0].viz_type == "table"


def test_unknown_metric_still_emits_chart(tmp_path: Path) -> None:
    """A chart referencing a metric that's not in the registry should
    still produce a chart YAML (with a synthetic dataset_uuid) — the
    bundle goes through and Superset surfaces the error inline."""
    org = """\
#+TITLE: t
* something
#+BEGIN_SRC sql :name x :metric ghost_metric
SELECT 1
#+END_SRC
"""
    p = tmp_path / "ghost.org"
    p.write_text(org)
    od = OrgDashboard.from_org(p)
    bundle = od.to_bundle()
    # Chart written, dataset folder empty (no resolved source).
    chart_keys = [k for k in bundle if k.startswith("t/charts/")]
    dataset_keys = [k for k in bundle if k.startswith("t/datasets/")]
    assert chart_keys == ["t/charts/x.yaml"]
    assert dataset_keys == []
