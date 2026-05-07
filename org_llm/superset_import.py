"""Org-as-source preprocessor for Apache Superset.

Recommended by the 2026-05-06 fork-spike survey
(`docs/wiki/2026-05-06-superset-fork-survey.org`) as the only fork-shaped
tier-7 candidate worth pursuing — and the survey concluded the cheapest
shape is *zero-fork*: parse `.org` files in org-llm and emit Superset's
existing v1 import-bundle zip (metadata.yaml + databases/ + datasets/ +
charts/ + dashboards/), then POST it to /api/v1/dashboard/import/.

This module is the preprocessor. It does not patch Superset.

Usage:

    from org_llm.superset_import import OrgDashboard

    od = OrgDashboard.from_org("dashboards/llm-health.org")
    bundle = od.to_bundle()             # {filename: bytes}
    od.write_zip("out.zip")
    od.post("http://localhost:8088", auth=("admin", "admin"))

Org file shape (see docs/templates/org-llm-superset-dashboard.org.example):

    #+TITLE: LLM Health
    #+SUPERSET_DASHBOARD: llm-health
    #+SUPERSET_DATABASE: org-llm

    * Average call duration by kind
      :PROPERTIES:
      :VIZ_TYPE: bar
      :END:

    #+BEGIN_SRC sql :name avg_ms_by_kind :metric llm_avg_ms :group_by call_kind
    SELECT call_kind, AVG(duration_ms) AS llm_avg_ms FROM llm_calls GROUP BY call_kind
    #+END_SRC

The block's `:metric` arg names a registered metric in the semantic-layer
(`org_llm/metrics/registry.yaml`); the SQL is what Superset actually runs
when previewing the chart in SQL-Lab. The two should agree — the registry
metric is the authoritative numerator.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore

from .metrics import (
    ORG_LLM_NAMESPACE_UUID,
    SUPERSET_DATABASE_NAME,
    SUPERSET_IMPORT_VERSION,
    Registry,
)

# Re-use the same uuid5 namespace + import-version constants as
# `Registry.emit_superset` so dataset/database UUIDs stay byte-stable
# across the registry emitter and the dashboard preprocessor. That's
# load-bearing — Superset matches charts to datasets via UUID.

# Pre-translate legacy viz_type names to their modern equivalents so the
# import path skips Superset's auto-migrator. The migrator's side effect
# (superset/commands/chart/importers/v1/utils.py:115,126) stringifies
# `query_context.form_data`, which then breaks GET /api/v1/chart/<id>/data
# with `'str' object has no attribute 'get'`. Templates keep friendly
# names (`:viz bar`); we canonicalize on emit. Mapping is verbatim from
# superset/migrations/shared/migrate_viz/processors.py.
_VIZ_TYPE_MIGRATION = {
    "bar": "echarts_timeseries_bar",
    "dist_bar": "echarts_timeseries_bar",
    "line": "echarts_timeseries_line",
    "area": "echarts_area",
    "heatmap": "heatmap_v2",
    "histogram": "histogram_v2",
    "treemap": "treemap_v2",
    "pivot_table": "pivot_table_v2",
    "sunburst": "sunburst_v2",
    "bubble": "bubble_v2",
    "sankey": "sankey_v2",
    "dual_line": "mixed_timeseries",
}


_HEADER_RE = re.compile(r"^#\+(\w+):\s*(.*?)\s*$", re.MULTILINE)
_BABEL_BLOCK_RE = re.compile(
    r"#\+BEGIN_SRC\s+sql\s*([^\n]*)\n(.*?)#\+END_SRC",
    re.DOTALL | re.IGNORECASE,
)


def _stable_uuid(kind: str, name: str) -> str:
    """Mirror `Registry._stable_uuid`. Exposed at module scope so tests
    can compute expected UUIDs without instantiating a Registry."""
    return str(uuid.uuid5(ORG_LLM_NAMESPACE_UUID, f"{kind}:{name}"))


def _parse_babel_args(arg_line: str) -> dict[str, str]:
    """Parse `:name foo :metric llm_avg_ms :group_by call_kind` →
    {'name': 'foo', 'metric': 'llm_avg_ms', 'group_by': 'call_kind'}.

    Org Babel header args are space-separated `:KEY VALUE` pairs;
    values may themselves contain spaces, but in practice for our
    SQL-block contract they don't. Keep the parser deliberately
    minimal so error-modes are obvious — no quoting, no list syntax,
    one value per key. If we need lists we accept `[a, b, c]` literals
    and re-stitch them post-tokenization.
    """
    args: dict[str, str] = {}
    tokens = arg_line.strip().split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith(":") and i + 1 < len(tokens):
            key = tok[1:]
            val = tokens[i + 1]
            if val.startswith("[") and not val.endswith("]"):
                j = i + 1
                while j < len(tokens) and not tokens[j].endswith("]"):
                    j += 1
                if j < len(tokens):
                    val = " ".join(tokens[i + 1 : j + 1])
                    i = j + 1
                else:
                    i += 2
            else:
                i += 2
            args[key] = val
        else:
            i += 1
    return args


def _parse_list_arg(val: str) -> list[str]:
    """Turn `[a, b]` or `a` into `['a', 'b']` / `['a']`."""
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1]
        return [x.strip() for x in inner.split(",") if x.strip()]
    return [val] if val else []


@dataclass
class OrgChart:
    """One Superset chart parsed out of one #+BEGIN_SRC sql block."""

    name: str  # Babel :name — also the chart slug
    sql: str
    metric_name: str  # Babel :metric, or fallback
    group_by: list[str] = field(default_factory=list)
    viz_type: str = "table"  # PROPERTIES drawer override
    title: str | None = None  # parent headline if present


@dataclass
class OrgDashboard:
    """An entire org file projected onto Superset's import bundle shape."""

    title: str
    slug: str
    database_name: str
    charts: list[OrgChart]
    source_path: Path | None = None

    # ---- parsing ---------------------------------------------------------

    @classmethod
    def from_org(cls, path: str | Path) -> "OrgDashboard":
        """Parse an .org file into an OrgDashboard.

        Uses orgparse where it earns its keep (headline + properties +
        per-headline body), but falls back to two regexes for the
        file-level header lines (#+TITLE etc.) and the Babel SQL blocks
        — orgparse exposes those awkwardly and the regexes are tighter
        than asking it to round-trip. If we ever need full org Babel
        semantics we can switch.
        """
        import orgparse

        path = Path(path)
        text = path.read_text()
        org = orgparse.load(str(path))

        headers: dict[str, str] = {}
        for m in _HEADER_RE.finditer(text):
            key, val = m.group(1).upper(), m.group(2)
            headers[key] = val

        title = headers.get("TITLE", path.stem)
        slug = headers.get("SUPERSET_DASHBOARD", _slugify(title))
        database_name = headers.get(
            "SUPERSET_DATABASE", SUPERSET_DATABASE_NAME
        )

        # Walk headlines so we can attach a chart to the headline that
        # contains its src block. orgparse's linenumber is 1-indexed.
        heading_spans: list[tuple[int, Any]] = []
        for h in org[1:]:  # skip file-level node
            heading_spans.append((h.linenumber, h))
        heading_spans.sort(key=lambda x: x[0])

        charts: list[OrgChart] = []
        for m in _BABEL_BLOCK_RE.finditer(text):
            arg_line = m.group(1) or ""
            sql_body = m.group(2).strip()
            args = _parse_babel_args(arg_line)
            name = args.get("name") or f"chart_{len(charts) + 1}"
            metric = args.get("metric") or name
            group_by = _parse_list_arg(args.get("group_by", ""))
            block_line = text[: m.start()].count("\n") + 1
            heading = _enclosing_heading(heading_spans, block_line)
            viz_type = "table"
            chart_title: str | None = None
            if heading is not None:
                chart_title = heading.heading
                viz_type = (
                    heading.get_property("VIZ_TYPE")
                    or heading.get_property("SUPERSET_VIZ")
                    or "table"
                )
            charts.append(
                OrgChart(
                    name=name,
                    sql=sql_body,
                    metric_name=metric,
                    group_by=group_by,
                    viz_type=viz_type,
                    title=chart_title,
                )
            )

        return cls(
            title=title,
            slug=slug,
            database_name=database_name,
            charts=charts,
            source_path=path,
        )

    # ---- bundle emission -------------------------------------------------

    def to_bundle(self, registry: Registry | None = None) -> dict[str, bytes]:
        """Build the in-memory Superset import bundle.

        Returns `{filename: bytes}` keyed on archive-relative paths
        ready to feed straight to `zipfile.ZipFile.writestr`. Layout
        matches what Superset's `get_contents_from_bundle` expects:

            <bundle>/metadata.yaml
            <bundle>/databases/<db>.yaml
            <bundle>/datasets/<db>/<table>.yaml
            <bundle>/charts/<slug>.yaml
            <bundle>/dashboards/<slug>.yaml

        We delegate database+dataset YAML emission to a helper that
        mirrors `Registry.emit_superset`'s shape so dataset UUIDs match
        the ones agents already cite — that's how charts here resolve
        their `dataset_uuid`.
        """
        registry = registry or Registry.load()
        bundle: dict[str, bytes] = {}
        bundle_root = self.slug.replace("-", "_")
        timestamp = "2026-05-06T00:00:00+00:00"

        # metadata.yaml — `type: Dashboard` since the bundle's top-level
        # intent is a dashboard import (vs. registry's `type: Database`).
        bundle[f"{bundle_root}/metadata.yaml"] = yaml.safe_dump(
            {
                "version": SUPERSET_IMPORT_VERSION,
                "type": "Dashboard",
                "timestamp": timestamp,
            },
            sort_keys=False,
        ).encode()

        db_uuid = _stable_uuid("database", self.database_name)
        db_dirname = self.database_name.replace("-", "_")
        bundle[f"{bundle_root}/databases/{db_dirname}.yaml"] = yaml.safe_dump(
            {
                "database_name": self.database_name,
                "sqlalchemy_uri": f"sqlite:///{registry.db_path}",
                "cache_timeout": None,
                "expose_in_sqllab": True,
                "allow_run_async": False,
                "allow_ctas": False,
                "allow_cvas": False,
                "allow_dml": False,
                "allow_csv_upload": False,
                "extra": {"engine_params": {}, "metadata_params": {}},
                "uuid": db_uuid,
                "version": SUPERSET_IMPORT_VERSION,
            },
            sort_keys=False,
        ).encode()

        # One dataset YAML per source referenced by any chart's metric.
        sources_used: dict[str, str] = {}  # source_name -> table
        for chart in self.charts:
            m = registry.metrics.get(chart.metric_name)
            if m is not None:
                src = registry.sources[m.source]
                sources_used[src.name] = src.table

        for src_name in sources_used:
            ds_yaml = _emit_dataset_yaml(registry, src_name, db_uuid)
            src = registry.sources[src_name]
            bundle[
                f"{bundle_root}/datasets/{db_dirname}/{src.table}.yaml"
            ] = ds_yaml

        # charts/<slug>.yaml — one per Babel block.
        for chart in self.charts:
            chart_yaml, _chart_uuid = _emit_chart_yaml(
                chart, registry
            )
            bundle[f"{bundle_root}/charts/{chart.name}.yaml"] = chart_yaml

        # dashboards/<slug>.yaml — single file referencing all charts.
        bundle[f"{bundle_root}/dashboards/{self.slug}.yaml"] = (
            _emit_dashboard_yaml(self).encode()
        )

        return bundle

    # ---- writers ---------------------------------------------------------

    def write_zip(self, path: str | Path) -> Path:
        """Write a zip to disk. Returns the resolved path."""
        path = Path(path)
        bundle = self.to_bundle()
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in sorted(bundle.items()):
                zf.writestr(name, data)
        return path

    def to_zip_bytes(self) -> bytes:
        """Return the zip as in-memory bytes (for HTTP POST)."""
        bundle = self.to_bundle()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in sorted(bundle.items()):
                zf.writestr(name, data)
        return buf.getvalue()

    # ---- POST to live Superset ------------------------------------------

    def post(
        self,
        superset_url: str,
        auth: tuple[str, str] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST the bundle to Superset's /api/v1/dashboard/import/.

        Superset expects: a JWT bearer (the `/api/v1/security/login`
        flow), a CSRF token, then a multipart upload with field
        `formData=<bundle.zip>` plus `overwrite=true`. We do the
        minimum here — pluggable auth tuple, no SSO. Richer deployments
        can construct the request themselves; this path is for the
        round-trip dev loop.

        Returns the parsed JSON response body. Raises on HTTP errors.
        """
        try:
            import requests
        except ImportError as e:  # pragma: no cover — requests is a dep
            raise RuntimeError(
                "requests is required for OrgDashboard.post; "
                "install via `pip install requests`"
            ) from e

        sess = requests.Session()
        if auth is not None:
            login = sess.post(
                f"{superset_url}/api/v1/security/login",
                json={
                    "username": auth[0],
                    "password": auth[1],
                    "provider": "db",
                    "refresh": True,
                },
                timeout=timeout,
            )
            login.raise_for_status()
            token = login.json()["access_token"]
            sess.headers["Authorization"] = f"Bearer {token}"

            csrf = sess.get(
                f"{superset_url}/api/v1/security/csrf_token/",
                timeout=timeout,
            )
            csrf.raise_for_status()
            sess.headers["X-CSRFToken"] = csrf.json()["result"]

        files = {
            "formData": (
                f"{self.slug}.zip",
                self.to_zip_bytes(),
                "application/zip",
            ),
        }
        data = {"overwrite": "true"}
        resp = sess.post(
            f"{superset_url}/api/v1/dashboard/import/",
            files=files,
            data=data,
            timeout=timeout,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"status": resp.status_code, "text": resp.text}


# ----------------------------------------------------------------------------
# Helpers — module-private. Kept out of OrgDashboard so the data-class stays
# a thin record and the YAML-shape gore is easy to find when Superset bumps.
# ----------------------------------------------------------------------------


def _slugify(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out or "dashboard"


def _enclosing_heading(
    spans: list[tuple[int, Any]], line: int
) -> Any | None:
    """Return the most-recent headline at or before `line`, if any.

    Linear scan is fine — typical org files have <100 headlines and
    <20 charts. If this ever shows up in a profile we can switch to a
    sorted-bisect.
    """
    candidate = None
    for start, h in spans:
        if start <= line:
            candidate = h
        else:
            break
    return candidate


def _emit_dataset_yaml(
    registry: Registry, source_name: str, db_uuid: str
) -> bytes:
    """Build the per-dataset YAML inline.

    Mirrors `Registry.emit_superset`'s per-dataset path so we can
    produce an in-memory bundle without going through a tmpdir. The
    on-disk emitter is still authoritative — copy-paste with one
    dispatch surface is the simpler trade than refactoring the
    registry method right now.
    """
    src = registry.sources[source_name]
    ds_uuid = _stable_uuid("dataset", source_name)

    cols_for_src = [
        d for d in registry.dimensions.values() if d.source == source_name
    ]
    mets_for_src = [
        m for m in registry.metrics.values() if m.source == source_name
    ]

    seen_cols: set[str] = {src.time_col}
    columns: list[dict[str, Any]] = [
        {
            "column_name": src.time_col,
            "is_dttm": True,
            "is_active": True,
            "type": "INTEGER" if src.time_col == "ts" else "TEXT",
            "groupby": True,
            "filterable": True,
            "expression": None,
        }
    ]
    for d in cols_for_src:
        if d.name in seen_cols:
            continue
        seen_cols.add(d.name)
        expr = None if d.expr.strip() == d.name else d.expr
        columns.append(
            {
                "column_name": d.name,
                "is_dttm": False,
                "is_active": True,
                "type": "TEXT",
                "groupby": True,
                "filterable": True,
                "expression": expr,
            }
        )

    metrics_yaml = []
    for m in mets_for_src:
        metrics_yaml.append(
            {
                "metric_name": m.name,
                "verbose_name": m.name,
                "metric_type": None,
                "expression": registry._superset_metric_expression(m),
                "description": m.description,
                "d3format": None,
                "warning_text": None,
            }
        )

    return yaml.safe_dump(
        {
            "table_name": src.table,
            "main_dttm_col": src.time_col,
            "description": f"org-llm source: {src.name}",
            "default_endpoint": None,
            "offset": 0,
            "cache_timeout": None,
            "schema": None,
            "sql": None,
            "params": None,
            "template_params": None,
            "filter_select_enabled": True,
            "fetch_values_predicate": None,
            "extra": None,
            "uuid": ds_uuid,
            "metrics": metrics_yaml,
            "columns": columns,
            "version": SUPERSET_IMPORT_VERSION,
            "database_uuid": db_uuid,
        },
        sort_keys=False,
    ).encode()


def _emit_chart_yaml(
    chart: OrgChart, registry: Registry
) -> tuple[bytes, str]:
    """Build a Superset chart YAML for one Babel SQL block.

    The chart binds to a dataset by `dataset_uuid`. If the chart names
    a registered metric we link to that metric's dataset; otherwise we
    fall back to a synthetic UUID derived from the metric name so
    import doesn't crash even when the metric is unknown (Superset
    will refuse the chart, but the rest of the bundle goes through
    and the user gets a useful error).
    """
    metric = registry.metrics.get(chart.metric_name)
    if metric is not None:
        dataset_uuid = _stable_uuid("dataset", metric.source)
    else:
        dataset_uuid = _stable_uuid(
            "dataset", f"unresolved:{chart.metric_name}"
        )

    chart_uuid = _stable_uuid("chart", chart.name)

    # Canonicalize viz_type up-front so Superset's import-time
    # auto-migrator doesn't run (which would corrupt query_context —
    # see comment on _VIZ_TYPE_MIGRATION above).
    viz_type = _VIZ_TYPE_MIGRATION.get(chart.viz_type, chart.viz_type)

    # `params` is a free-form JSON-blob dict matching whatever the
    # viz_type's controlPanel expects. For agent-shaped use the table
    # viz is the safe default; the Babel block's :metric arg lands in
    # the metrics array, and :group_by populates groupby.
    params = {
        "datasource": f"{dataset_uuid}__table",
        "viz_type": viz_type,
        "groupby": chart.group_by,
        "metrics": [chart.metric_name],
        "adhoc_filters": [],
        "row_limit": 1000,
    }

    # Without a populated query_context, GET /api/v1/chart/<id>/data
    # fails with "Chart has no query context saved" until the user
    # opens the chart in the explore UI once. Synthesize one from the
    # form_data so the data API works on first request after import.
    # `datasource.id = 0` is a placeholder; Superset's
    # `update_chart_config_dataset` rewrites it to the real id at
    # import time (see superset/commands/utils.py:191).
    query_context = {
        "datasource": {"id": 0, "type": "table"},
        "force": False,
        "queries": [
            {
                "filters": [],
                "extras": {"having": "", "where": ""},
                "applied_time_extras": {},
                "columns": chart.group_by,
                "metrics": [chart.metric_name],
                "row_limit": params["row_limit"],
                "timeseries_limit": 0,
                "order_desc": True,
                "url_params": {},
                "custom_params": {},
                "custom_form_data": {},
                "annotation_layers": [],
            }
        ],
        "form_data": params,
        "result_format": "json",
        "result_type": "full",
    }

    payload = {
        "slice_name": chart.title or chart.name,
        "description": (
            f"Generated from {chart.name} (metric: {chart.metric_name})"
        ),
        "certified_by": None,
        "certification_details": None,
        "viz_type": viz_type,
        "params": params,
        "query_context": json.dumps(query_context),
        "cache_timeout": None,
        "uuid": chart_uuid,
        "version": SUPERSET_IMPORT_VERSION,
        "dataset_uuid": dataset_uuid,
    }
    return yaml.safe_dump(payload, sort_keys=False).encode(), chart_uuid


def _emit_dashboard_yaml(dash: OrgDashboard) -> str:
    """Build a minimal-but-valid dashboard YAML.

    The `position` field is what Superset's frontend uses to lay out
    the dashboard grid. We emit a single-column stack — one row per
    chart, full-width — which round-trips cleanly through the
    importer's marshmallow schema and renders as expected. Custom
    multi-chart layouts are deferred (see module docstring TODO).
    """
    rows: list[str] = []
    position: dict[str, Any] = {
        "ROOT_ID": {
            "children": ["GRID_ID"],
            "id": "ROOT_ID",
            "type": "ROOT",
        },
        "GRID_ID": {"children": [], "id": "GRID_ID", "type": "GRID"},
        "DASHBOARD_VERSION_KEY": "v2",
    }
    for chart in dash.charts:
        chart_uuid = _stable_uuid("chart", chart.name)
        # First 10 hex chars of the UUID — stable + collision-safe for
        # typical fan-out (UUIDs are uuid5-derived from chart name).
        short = chart_uuid.replace("-", "")[:10]
        chart_key = f"CHART-{short}"
        row_key = f"ROW-{short}"
        rows.append(row_key)
        position[chart_key] = {
            "children": [],
            "id": chart_key,
            "meta": {
                "chartId": 0,  # filled in on import
                "height": 50,
                "sliceName": chart.title or chart.name,
                "width": 12,
                "uuid": chart_uuid,
            },
            "type": "CHART",
        }
        position[row_key] = {
            "children": [chart_key],
            "id": row_key,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "type": "ROW",
        }
    position["GRID_ID"]["children"] = rows

    payload = {
        "dashboard_title": dash.title,
        "description": (
            f"Generated from {dash.source_path or dash.slug}"
        ),
        "css": None,
        "slug": dash.slug,
        "uuid": _stable_uuid("dashboard", dash.slug),
        "position": position,
        "metadata": {},
        "version": SUPERSET_IMPORT_VERSION,
    }
    return yaml.safe_dump(payload, sort_keys=False)


__all__ = [
    "OrgChart",
    "OrgDashboard",
    "_stable_uuid",
]
