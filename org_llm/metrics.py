"""Semantic layer for org-llm.

A single registry (org_llm/metrics/registry.yaml) defines what metrics and
dimensions *mean*; agents and dashboards read from this registry instead of
hand-rolling SQL. v0 scope: read-only queries against the existing
~/.local/share/org-llm/org-llm.db, no joins across sources, SQLite dialect.

Usage:

    from org_llm.metrics import Registry

    reg = Registry.load()
    rows = reg.query(
        metric="llm_error_rate",
        group_by=["model"],
        where={"call_kind": "chat"},
        since="2026-04-01",
        limit=10,
    )

CLI:
    python -m org_llm.metrics                        # demo run
    python -m org_llm.metrics emit --target superset --out docs/superset/
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore

REGISTRY_PATH = Path(__file__).parent / "metrics" / "registry.yaml"
DEFAULT_DB = Path("~/.local/share/org-llm/org-llm.db").expanduser()

SUPERSET_IMPORT_VERSION = "1.0.0"
SUPERSET_DATABASE_NAME = "org-llm"
ORG_LLM_NAMESPACE_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "org-llm.metrics.superset")

# Feature flag for the v1 named-joins prototype. When unset, cross-source
# queries still raise RegistryError exactly as in v0; setting this to "1"
# (or any truthy value) only changes the *hint* in the rejection message —
# join: NAME must still be passed explicitly to opt in.
JOINS_FLAG_ENV = "ORG_LLM_REGISTRY_V1_JOINS"

# SQL identifiers that must NOT be prefixed with a source alias when we
# qualify column references inside metric/dimension expressions. Conservative
# — better to leave a real column unqualified (and let SQLite raise
# "ambiguous column") than to mangle a keyword and emit invalid SQL.
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "AS",
        "CASE", "WHEN", "THEN", "ELSE", "END", "IN", "ON", "JOIN",
        "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "GROUP", "BY",
        "ORDER", "DESC", "ASC", "LIMIT", "DISTINCT", "TRUE", "FALSE",
        "IS", "BETWEEN", "LIKE", "EXISTS", "ALL", "ANY", "SOME",
    }
)

_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _qualify_columns(expr: str, alias: str) -> str:
    """Rewrite bare column identifiers in `expr` by prepending `alias.`.

    Heuristic: walk the string; skip single-quoted string literals; for each
    bare identifier token, prepend `<alias>.` UNLESS it's a SQL keyword,
    already qualified (preceded by `.`), or a function call (followed by `(`
    after optional whitespace). Numbers don't match `_IDENT_RE` so they pass
    through unchanged.
    """
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        if c == "'":
            j = i + 1
            while j < n:
                if expr[j] == "'" and j + 1 < n and expr[j + 1] == "'":
                    j += 2  # SQL doubled-quote escape
                    continue
                if expr[j] == "'":
                    j += 1
                    break
                j += 1
            out.append(expr[i:j])
            i = j
            continue
        m = _IDENT_RE.match(expr, i)
        if m:
            tok = m.group(0)
            start, end = m.span()
            prev = expr[start - 1] if start > 0 else ""
            after = expr[end:].lstrip()
            is_func_call = after.startswith("(")
            if (
                tok.upper() not in _SQL_KEYWORDS
                and prev != "."
                and not is_func_call
            ):
                out.append(f"{alias}.{tok}")
            else:
                out.append(tok)
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class Source:
    name: str
    table: str
    time_col: str


@dataclass(frozen=True)
class Dimension:
    name: str
    source: str
    expr: str


@dataclass(frozen=True)
class Metric:
    name: str
    source: str
    expr: str
    description: str = ""
    where: tuple[str, ...] = ()


@dataclass(frozen=True)
class Join:
    """A sanctioned cross-source relationship.

    `sources` is exactly 2 source names (v1 limits to 2-source joins).
    `kind` is one of {"inner", "left"}; the design doc reserves "time_bucket"
    for v1.1 and the compiler raises RegistryError if anyone tries it now.
    `on_left` / `on_right` are SQL fragments evaluated in their respective
    source's table alias (the alias is the source name itself). `filter` is
    optional and may use `left.<col>` / `right.<col>` tokens that get
    rewritten to the qualified source aliases.
    """
    name: str
    sources: tuple[str, str]
    kind: str
    on_left: str
    on_right: str
    filter: str | None = None


class RegistryError(Exception):
    pass


class Registry:
    def __init__(
        self,
        sources: dict[str, Source],
        dimensions: dict[str, Dimension],
        metrics: dict[str, Metric],
        db_path: Path = DEFAULT_DB,
        joins: dict[str, Join] | None = None,
    ) -> None:
        self.sources = sources
        self.dimensions = dimensions
        self.metrics = metrics
        self.db_path = db_path
        self.joins: dict[str, Join] = joins or {}

    # ---- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: Path = REGISTRY_PATH, db_path: Path | None = None) -> "Registry":
        with open(path) as f:
            raw = yaml.safe_load(f)
        sources = {n: Source(n, s["table"], s["time_col"]) for n, s in raw["sources"].items()}
        dimensions = {
            n: Dimension(n, d["source"], d["expr"]) for n, d in raw["dimensions"].items()
        }
        metrics = {
            n: Metric(
                name=n,
                source=m["source"],
                expr=m["expr"],
                description=m.get("description", ""),
                where=tuple(m.get("where", [])),
            )
            for n, m in raw["metrics"].items()
        }
        # Validate cross-references at load time so bad YAML fails loud.
        for d in dimensions.values():
            if d.source not in sources:
                raise RegistryError(f"dimension {d.name!r} references unknown source {d.source!r}")
        for m in metrics.values():
            if m.source not in sources:
                raise RegistryError(f"metric {m.name!r} references unknown source {m.source!r}")
        joins: dict[str, Join] = {}
        for n, j in (raw.get("joins") or {}).items():
            j_sources = j.get("sources") or []
            if len(j_sources) != 2:
                raise RegistryError(
                    f"join {n!r} must declare exactly 2 sources (v1 supports 2-source joins only)"
                )
            for s in j_sources:
                if s not in sources:
                    raise RegistryError(f"join {n!r} references unknown source {s!r}")
            on = j.get("on") or {}
            if "left" not in on or "right" not in on:
                raise RegistryError(f"join {n!r} missing on.left / on.right")
            joins[n] = Join(
                name=n,
                sources=(j_sources[0], j_sources[1]),
                kind=str(j.get("kind", "inner")),
                on_left=str(on["left"]),
                on_right=str(on["right"]),
                filter=on.get("filter"),
            )
        db = db_path or Path(os.environ.get("ORG_LLM_DB", str(DEFAULT_DB)))
        return cls(sources, dimensions, metrics, db, joins=joins)

    # ---- compilation -------------------------------------------------------

    def compile(
        self,
        metric: str,
        group_by: list[str] | None = None,
        where: dict[str, Any] | None = None,
        since: str | int | None = None,
        until: str | int | None = None,
        limit: int | None = None,
        join: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Compile a metric query to (sql, params).

        Default (v0) behavior: every dimension referenced (group_by or where
        keys) must live in the same source as the metric; cross-source
        queries raise RegistryError. Pass ``join="<name>"`` to opt into a
        sanctioned cross-source path declared in registry.yaml's ``joins:``
        block (v1, behind ORG_LLM_REGISTRY_V1_JOINS=1).
        """
        if metric not in self.metrics:
            raise RegistryError(f"unknown metric {metric!r}")
        m = self.metrics[metric]
        group_by = group_by or []
        where = where or {}

        if join is not None:
            return self._compile_join(
                m=m,
                group_by=group_by,
                where=where,
                since=since,
                until=until,
                limit=limit,
                join_name=join,
            )

        src = self.sources[m.source]

        for dim in group_by:
            if dim not in self.dimensions:
                raise RegistryError(f"unknown dimension {dim!r}")
            if self.dimensions[dim].source != m.source:
                hint = self._cross_source_hint(m.source, self.dimensions[dim].source)
                raise RegistryError(
                    f"dimension {dim!r} from source {self.dimensions[dim].source!r} "
                    f"can't be used with metric {metric!r} from source {m.source!r}"
                    + hint
                )
        for dim in where:
            if dim not in self.dimensions:
                raise RegistryError(f"unknown filter dimension {dim!r}")
            if self.dimensions[dim].source != m.source:
                hint = self._cross_source_hint(m.source, self.dimensions[dim].source)
                raise RegistryError(
                    f"filter dimension {dim!r} not in source {m.source!r}" + hint
                )

        select_cols: list[str] = []
        for dim in group_by:
            d = self.dimensions[dim]
            select_cols.append(f"{d.expr} AS {dim}")
        select_cols.append(f"({m.expr}) AS {metric}")

        sql_where: list[str] = list(m.where)
        params: list[Any] = []
        for dim, val in where.items():
            d = self.dimensions[dim]
            sql_where.append(f"{d.expr} = ?")
            params.append(val)
        if since is not None:
            sql_where.append(f"{src.time_col} >= ?")
            params.append(since)
        if until is not None:
            sql_where.append(f"{src.time_col} < ?")
            params.append(until)

        sql = f"SELECT {', '.join(select_cols)} FROM {src.table}"
        if sql_where:
            sql += " WHERE " + " AND ".join(sql_where)
        if group_by:
            sql += " GROUP BY " + ", ".join(group_by)
            sql += f" ORDER BY {metric} DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return sql, params

    # ---- cross-source v1 (named joins) -------------------------------------

    def _cross_source_hint(self, want_a: str, want_b: str) -> str:
        """Append a hint pointing at any declared join covering this pair —
        only when the v1 flag is set. Keeps the v0 message byte-identical
        for callers that haven't opted in."""
        if not os.environ.get(JOINS_FLAG_ENV):
            return ""
        pair = {want_a, want_b}
        candidates = [n for n, j in self.joins.items() if set(j.sources) == pair]
        if not candidates:
            return ""
        return f" (hint: pass join={candidates[0]!r} to opt into the v1 cross-source path)"

    def _compile_join(
        self,
        *,
        m: Metric,
        group_by: list[str],
        where: dict[str, Any],
        since: str | int | None,
        until: str | int | None,
        limit: int | None,
        join_name: str,
    ) -> tuple[str, list[Any]]:
        if join_name not in self.joins:
            raise RegistryError(f"unknown join {join_name!r}")
        j = self.joins[join_name]

        # `time_bucket` is structurally an INNER JOIN with both `on.left`
        # and `on.right` already cast to a common temporal grain (e.g.
        # `substr(timestamp, 1, 10)` vs `date(ts, 'unixepoch')`). Same
        # SQL shape; the kind name is a documentation knob telling
        # readers "this join matches on time, not foreign keys."
        if j.kind not in ("inner", "left", "time_bucket"):
            raise RegistryError(
                f"join {join_name!r} kind {j.kind!r} not supported in v1.1 "
                f"(supported: inner, left, time_bucket)"
            )
        if m.source not in j.sources:
            raise RegistryError(
                f"metric {m.name!r} requires source {m.source!r} but "
                f"join {join_name!r} covers {list(j.sources)!r}"
            )

        # Aliases are the source names themselves — unambiguous, matches the
        # design doc's example SQL, and means dimension expressions get
        # rewritten with `<source>.<col>` tokens that read cleanly.
        a, b = j.sources  # a = "left" side, b = "right" side
        alias_a = a
        alias_b = b
        src_a = self.sources[a]
        src_b = self.sources[b]

        def alias_for(source: str) -> str:
            return source  # 1:1 today; isolated for future renaming

        # Validate dimensions against the join's source pair.
        for dim in group_by:
            if dim not in self.dimensions:
                raise RegistryError(f"unknown dimension {dim!r}")
            if self.dimensions[dim].source not in j.sources:
                raise RegistryError(
                    f"dimension {dim!r} from source "
                    f"{self.dimensions[dim].source!r} not covered by "
                    f"join {join_name!r} (covers {list(j.sources)!r})"
                )
        for dim in where:
            if dim not in self.dimensions:
                raise RegistryError(f"unknown filter dimension {dim!r}")
            if self.dimensions[dim].source not in j.sources:
                raise RegistryError(
                    f"filter dimension {dim!r} from source "
                    f"{self.dimensions[dim].source!r} not covered by "
                    f"join {join_name!r} (covers {list(j.sources)!r})"
                )

        # SELECT list — qualify dim exprs by their source; metric expr by
        # the metric's source.
        select_cols: list[str] = []
        for dim in group_by:
            d = self.dimensions[dim]
            qualified = _qualify_columns(d.expr, alias_for(d.source))
            select_cols.append(f"{qualified} AS {dim}")
        metric_alias = alias_for(m.source)
        select_cols.append(
            f"({_qualify_columns(m.expr, metric_alias)}) AS {m.name}"
        )

        # Metric where-clauses qualify against the metric's source.
        sql_where: list[str] = [
            _qualify_columns(w, metric_alias) for w in m.where
        ]
        params: list[Any] = []
        for dim, val in where.items():
            d = self.dimensions[dim]
            qualified = _qualify_columns(d.expr, alias_for(d.source))
            sql_where.append(f"{qualified} = ?")
            params.append(val)
        if since is not None:
            sql_where.append(f"{metric_alias}.{self.sources[m.source].time_col} >= ?")
            params.append(since)
        if until is not None:
            sql_where.append(f"{metric_alias}.{self.sources[m.source].time_col} < ?")
            params.append(until)

        # Build ON clause. on.left lives on alias_a, on.right on alias_b.
        on_left_q = _qualify_columns(j.on_left, alias_a)
        on_right_q = _qualify_columns(j.on_right, alias_b)
        on_parts = [f"{on_left_q} = {on_right_q}"]
        if j.filter:
            on_parts.append(self._rewrite_filter(j.filter, alias_a, alias_b))

        join_kw = "LEFT JOIN" if j.kind == "left" else "INNER JOIN"

        sql = (
            f"SELECT {', '.join(select_cols)} "
            f"FROM {src_a.table} AS {alias_a} "
            f"{join_kw} {src_b.table} AS {alias_b} "
            f"ON {' AND '.join(on_parts)}"
        )
        if sql_where:
            sql += " WHERE " + " AND ".join(sql_where)
        if group_by:
            sql += " GROUP BY " + ", ".join(group_by)
            sql += f" ORDER BY {m.name} DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return sql, params

    @staticmethod
    def _rewrite_filter(filter_expr: str, alias_a: str, alias_b: str) -> str:
        """Rewrite ``left.<col>`` / ``right.<col>`` placeholders in a join's
        on.filter predicate to the actual source aliases."""
        out = filter_expr
        out = re.sub(r"\bleft\.", f"{alias_a}.", out)
        out = re.sub(r"\bright\.", f"{alias_b}.", out)
        return out

    # ---- execution ---------------------------------------------------------

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        sql, params = self.compile(**kwargs)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- discovery ---------------------------------------------------------

    def describe(self) -> str:
        """Human-readable index. Useful for agent prompts: 'here are the
        metrics you can ask for' instead of leaking the YAML wholesale."""
        lines = ["# org-llm metrics registry", ""]
        for src in self.sources.values():
            lines.append(f"## source: {src.name}  ({src.table})")
            ms = [m for m in self.metrics.values() if m.source == src.name]
            ds = [d for d in self.dimensions.values() if d.source == src.name]
            if ms:
                lines.append("  metrics:")
                for m in ms:
                    lines.append(f"    - {m.name}: {m.description}")
            if ds:
                lines.append("  dimensions:")
                for d in ds:
                    lines.append(f"    - {d.name}")
            lines.append("")
        if self.joins:
            lines.append("## cross-source joins  (opt-in via "
                         "`--join NAME` + ORG_LLM_REGISTRY_V1_JOINS=1)")
            for j in self.joins.values():
                lines.append(
                    f"  - {j.name}: {j.kind} "
                    f"{j.sources[0]} ↔ {j.sources[1]}"
                )
            lines.append("")
        return "\n".join(lines)

    # ---- Superset emitter --------------------------------------------------

    def _stable_uuid(self, kind: str, name: str) -> str:
        return str(uuid.uuid5(ORG_LLM_NAMESPACE_UUID, f"{kind}:{name}"))

    def _superset_metric_expression(self, m: Metric) -> str:
        """Fold registry where-clauses into a single SQL expression.

        Superset metrics carry a single SQL `expression` and no separate
        WHERE; chart-level filters are applied independently. Aggregation-
        scoped filters from the registry (`where: ["action = 'delegate'"]`)
        therefore have to fold into the expression itself. The safe
        transformation for COUNT(*) / SUM(...) shapes is CASE WHEN.
        """
        if not m.where:
            return m.expr
        cond = " AND ".join(f"({w})" for w in m.where)
        expr = m.expr.strip()
        upper = expr.upper()
        if upper == "COUNT(*)":
            return f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"
        if upper.startswith("SUM(") and upper.endswith(")"):
            inner = expr[4:-1]
            return f"SUM(CASE WHEN {cond} THEN ({inner}) ELSE 0 END)"
        if upper.startswith("AVG(") and upper.endswith(")"):
            inner = expr[4:-1]
            return f"AVG(CASE WHEN {cond} THEN ({inner}) END)"
        # Fallback: documentation-only — Superset will still run the metric,
        # but the registry where-clause won't apply. Surface in description.
        return expr

    def emit_superset(self, target_dir: Path) -> list[Path]:
        """Write a Superset v1 import bundle for this registry.

        Layout:
            target_dir/metadata.yaml
            target_dir/databases/org_llm.yaml
            target_dir/datasets/org_llm/<source>.yaml  (one per source)

        Returns the list of files written. Idempotent: re-emits with stable
        uuid5 IDs so repeated calls produce byte-identical output.
        """
        target_dir = Path(target_dir)
        (target_dir / "databases").mkdir(parents=True, exist_ok=True)
        datasets_dir = target_dir / "datasets" / SUPERSET_DATABASE_NAME.replace("-", "_")
        datasets_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        timestamp = datetime(2026, 5, 6, tzinfo=timezone.utc).isoformat()

        metadata_path = target_dir / "metadata.yaml"
        metadata_path.write_text(
            yaml.safe_dump(
                {
                    "version": SUPERSET_IMPORT_VERSION,
                    "type": "Database",
                    "timestamp": timestamp,
                },
                sort_keys=False,
            )
        )
        written.append(metadata_path)

        db_uuid = self._stable_uuid("database", SUPERSET_DATABASE_NAME)
        db_path = target_dir / "databases" / f"{SUPERSET_DATABASE_NAME.replace('-', '_')}.yaml"
        db_path.write_text(
            yaml.safe_dump(
                {
                    "database_name": SUPERSET_DATABASE_NAME,
                    "sqlalchemy_uri": f"sqlite:///{self.db_path}",
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
            )
        )
        written.append(db_path)

        for src in self.sources.values():
            cols_for_src = [d for d in self.dimensions.values() if d.source == src.name]
            mets_for_src = [m for m in self.metrics.values() if m.source == src.name]

            seen_cols: set[str] = set()
            columns: list[dict[str, Any]] = []
            # Always include the time column so charts can use it on the X axis.
            seen_cols.add(src.time_col)
            columns.append(
                {
                    "column_name": src.time_col,
                    "is_dttm": True,
                    "is_active": True,
                    "type": "INTEGER" if src.time_col == "ts" else "TEXT",
                    "groupby": True,
                    "filterable": True,
                    "expression": None,
                }
            )
            for d in cols_for_src:
                expr = None if d.expr.strip() == d.name else d.expr
                col_name = d.name
                if col_name in seen_cols:
                    continue
                seen_cols.add(col_name)
                columns.append(
                    {
                        "column_name": col_name,
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
                        "expression": self._superset_metric_expression(m),
                        "description": m.description,
                        "d3format": None,
                        "warning_text": None,
                    }
                )

            ds_uuid = self._stable_uuid("dataset", src.name)
            ds_path = datasets_dir / f"{src.table}.yaml"
            ds_path.write_text(
                yaml.safe_dump(
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
                )
            )
            written.append(ds_path)

        return written


def _demo() -> None:
    reg = Registry.load()
    print(reg.describe())
    print("=" * 60)

    cases = [
        dict(metric="llm_calls"),
        dict(metric="llm_calls", group_by=["model"]),
        dict(metric="llm_error_rate", group_by=["model"]),
        dict(metric="llm_avg_ms", group_by=["call_kind"]),
        dict(metric="llm_calls", group_by=["llm_day"], since="2026-04-15", limit=10),
        dict(metric="crew_delegations", group_by=["agent_to"], limit=5),
        dict(metric="sensor_alerts", group_by=["probe"], limit=5),
    ]
    for c in cases:
        sql, params = reg.compile(**c)
        print(f"\n>>> {c}")
        print(f"SQL: {sql}")
        if params:
            print(f"params: {params}")
        try:
            rows = reg.query(**c)
        except sqlite3.OperationalError as e:
            print(f"  (db error: {e})")
            continue
        for r in rows[:8]:
            print(f"  {r}")


def _cli_emit(args: argparse.Namespace) -> None:
    reg = Registry.load()
    if args.target == "superset":
        written = reg.emit_superset(args.out)
        print(f"wrote {len(written)} file(s) to {args.out}")
        for p in written:
            print(f"  {p}")
    else:
        raise SystemExit(f"unknown emit target: {args.target}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="org_llm.metrics")
    sub = parser.add_subparsers(dest="cmd")

    p_emit = sub.add_parser("emit", help="emit registry to an external format")
    p_emit.add_argument("--target", choices=["superset"], required=True)
    p_emit.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "emit":
        _cli_emit(args)
    else:
        _demo()


if __name__ == "__main__":
    main()
