"""Tests for the org-llm semantic-layer registry.

Covers:
- Registry.load() validation (cross-references, schema integrity)
- compile() — group_by, where, since/until, cross-source rejection
- query() against a fixture SQLite DB
- emit_superset() — bundle layout, idempotent UUIDs, metric where-folding,
  required-field schema sanity (marshmallow validation needs Flask app
  context, so we check the structural contract by hand here)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from org_llm.metrics import (
    Registry,
    RegistryError,
    SUPERSET_DATABASE_NAME,
    SUPERSET_IMPORT_VERSION,
)


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Tiny SQLite DB matching the real schema enough for query tests."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE llm_calls(
            id INTEGER PRIMARY KEY,
            timestamp TEXT, command TEXT, model TEXT,
            duration_ms INTEGER, outcome TEXT, args TEXT,
            response TEXT, call_kind TEXT
        );
        CREATE TABLE crew_log(
            id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT,
            action TEXT, agent_from TEXT, agent_to TEXT, model TEXT,
            prompt_excerpt TEXT, result_excerpt TEXT,
            duration_ms INTEGER, outcome TEXT
        );
        CREATE TABLE history(
            id INTEGER PRIMARY KEY, timestamp TEXT, command TEXT,
            "query" TEXT, response TEXT, kind TEXT, model TEXT,
            args TEXT, duration_ms INTEGER, outcome TEXT
        );
        CREATE TABLE sensor_log(
            id INTEGER PRIMARY KEY, ts INTEGER, probe TEXT,
            value TEXT, normalized TEXT, status TEXT,
            label TEXT, message TEXT, context TEXT
        );
        """
    )
    rows = [
        ("2026-04-28T10:00:00", "chat", "phi3.5", 1000, "ok", "", "r", "chat"),
        ("2026-04-28T10:01:00", "chat", "phi3.5", 2000, "error", "", "r", "chat"),
        ("2026-04-28T10:02:00", "embed", "nomic-embed-text", 100, "ok", "", "r", "embed"),
    ]
    conn.executemany(
        "INSERT INTO llm_calls(timestamp,command,model,duration_ms,outcome,args,response,call_kind) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO crew_log(timestamp,session_id,action,agent_from,agent_to,model,"
        "prompt_excerpt,result_excerpt,duration_ms,outcome) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-05-01T10:00:00", "s1", "delegate", "manager", "researcher", "m", "", "", 100, "ok"),
            ("2026-05-01T10:01:00", "s1", "delegate", "manager", "scribe", "m", "", "", 100, "ok"),
            ("2026-05-01T10:02:00", "s1", "tool_call", "manager", "tool", "m", "", "", 50, "ok"),
        ],
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def registry(fixture_db: Path) -> Registry:
    return Registry.load(db_path=fixture_db)


# ---------- load + validate -----------------------------------------------


def test_load_validates_metric_source(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "sources": {"llm": {"table": "llm_calls", "time_col": "timestamp"}},
        "dimensions": {"model": {"source": "llm", "expr": "model"}},
        "metrics": {
            "phantom": {"source": "nonexistent", "expr": "COUNT(*)"},
        },
    }))
    with pytest.raises(RegistryError, match="unknown source"):
        Registry.load(path=bad)


def test_load_validates_dimension_source(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "sources": {"llm": {"table": "llm_calls", "time_col": "timestamp"}},
        "dimensions": {"orphan": {"source": "ghost", "expr": "x"}},
        "metrics": {},
    }))
    with pytest.raises(RegistryError, match="unknown source"):
        Registry.load(path=bad)


def test_load_real_registry(registry: Registry) -> None:
    """Sanity: the shipping registry.yaml round-trips through load()."""
    assert "llm" in registry.sources
    assert "llm_calls" in registry.metrics
    assert "model" in registry.dimensions
    assert registry.metrics["llm_error_rate"].source == "llm"


# ---------- compile() ------------------------------------------------------


def test_compile_simple_count(registry: Registry) -> None:
    sql, params = registry.compile(metric="llm_calls")
    assert "FROM llm_calls" in sql
    assert "COUNT(*)" in sql
    assert params == []


def test_compile_group_by(registry: Registry) -> None:
    sql, _ = registry.compile(metric="llm_calls", group_by=["model"])
    assert "GROUP BY model" in sql
    assert "ORDER BY llm_calls" in sql


def test_compile_where_binds_params(registry: Registry) -> None:
    sql, params = registry.compile(
        metric="llm_calls", where={"call_kind": "chat"})
    assert "call_kind = ?" in sql
    assert params == ["chat"]


def test_compile_since_until(registry: Registry) -> None:
    sql, params = registry.compile(
        metric="llm_calls", since="2026-04-01", until="2026-05-01")
    assert sql.count("?") == 2
    assert params == ["2026-04-01", "2026-05-01"]


def test_compile_metric_where_clause_folded(registry: Registry) -> None:
    """crew_delegations has where: ['action = delegate'] in YAML."""
    sql, _ = registry.compile(metric="crew_delegations")
    assert "action = 'delegate'" in sql


def test_compile_unknown_metric(registry: Registry) -> None:
    with pytest.raises(RegistryError, match="unknown metric"):
        registry.compile(metric="ghost")


def test_compile_unknown_dimension(registry: Registry) -> None:
    with pytest.raises(RegistryError, match="unknown dimension"):
        registry.compile(metric="llm_calls", group_by=["phantom"])


def test_compile_rejects_cross_source_group_by(registry: Registry) -> None:
    """metric:llm_calls (source=llm) can't group by agent_to (source=crew)."""
    with pytest.raises(RegistryError, match="from source"):
        registry.compile(metric="llm_calls", group_by=["agent_to"])


def test_compile_rejects_cross_source_where(registry: Registry) -> None:
    with pytest.raises(RegistryError, match="not in source"):
        registry.compile(metric="llm_calls", where={"agent_to": "researcher"})


# ---------- query() against fixture DB -------------------------------------


def test_query_count(registry: Registry) -> None:
    rows = registry.query(metric="llm_calls")
    assert rows == [{"llm_calls": 3}]


def test_query_group_by_outcome(registry: Registry) -> None:
    rows = registry.query(metric="llm_error_rate", group_by=["outcome"])
    by_outcome = {r["outcome"]: r["llm_error_rate"] for r in rows}
    assert by_outcome["error"] == 1.0
    assert by_outcome["ok"] == 0.0


def test_query_agent_fan_out(registry: Registry) -> None:
    rows = registry.query(metric="agent_fan_out", group_by=["agent_from"])
    fan_out = {r["agent_from"]: r["agent_fan_out"] for r in rows}
    assert fan_out["manager"] == 3  # researcher, scribe, tool


# ---------- emit_superset() ------------------------------------------------


def test_emit_superset_writes_bundle(registry: Registry, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    written = registry.emit_superset(target)
    files = {p.relative_to(target) for p in written}
    assert Path("metadata.yaml") in files
    db_dirname = SUPERSET_DATABASE_NAME.replace("-", "_")
    assert Path(f"databases/{db_dirname}.yaml") in files
    assert Path(f"datasets/{db_dirname}/llm_calls.yaml") in files
    assert Path(f"datasets/{db_dirname}/crew_log.yaml") in files


def test_emit_superset_writes_join_virtual_datasets(
    registry: Registry, tmp_path: Path
) -> None:
    """v1.1.1: each declared join becomes a Superset virtual dataset.

    The dataset's SQL projects every dim + raw column from both sides
    (with `<source>_<col>` prefix to dodge collisions like `timestamp` /
    `id`); metric expressions are rewritten to reference the prefixed
    columns; columns block lists each registry dim as a bare alias.
    """
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    db_dirname = SUPERSET_DATABASE_NAME.replace("-", "_")
    join_path = target / f"datasets/{db_dirname}/llm_x_history_event.yaml"
    assert join_path.exists()
    ds = yaml.safe_load(join_path.read_text())
    assert ds["table_name"] == "llm_x_history_event"

    # SQL has explicit JOIN with the right ON clause + filter.
    sql = ds["sql"]
    assert "FROM llm_calls AS llm" in sql
    assert "INNER JOIN history AS history" in sql
    assert "ON llm.id = history.id" in sql
    assert "history.kind = 'llm'" in sql
    # Dim exprs aliased to dim name; raw cols prefixed by source.
    assert "llm.model AS model" in sql
    assert "history.kind AS hist_kind" in sql
    assert "llm.duration_ms AS llm_duration_ms" in sql
    assert "history.duration_ms AS history_duration_ms" in sql

    # Metric expressions reference the prefixed columns (so the
    # virtual dataset's aggregations stay correct over the joined
    # row stream).
    metrics = {m["metric_name"]: m["expression"] for m in ds["metrics"]}
    assert metrics["llm_avg_ms"] == "AVG(llm_duration_ms)"
    assert metrics["hist_avg_ms"] == "AVG(history_duration_ms)"
    # COUNT(*) has no column refs so passes through unchanged.
    assert metrics["llm_calls"] == "COUNT(*)"
    # CASE-WHEN folds (from metric where-clauses) get prefixed too.
    assert "llm_outcome != 'ok'" in metrics["llm_errors"]

    # All declared joins emit a dataset.
    expected_join_files = {
        "llm_x_history_event.yaml",
        "crew_x_llm_command.yaml",
        "llm_x_sensors_temporal.yaml",
    }
    actual = {p.name for p in (target / f"datasets/{db_dirname}").iterdir()}
    assert expected_join_files.issubset(actual)


def test_emit_superset_join_dataset_main_dttm_col_picks_left_side(
    registry: Registry, tmp_path: Path
) -> None:
    """main_dttm_col (Superset's default time column) is the left
    source's prefixed time column. Charts default to using that as
    the X axis."""
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    db_dirname = SUPERSET_DATABASE_NAME.replace("-", "_")
    ds = yaml.safe_load(
        (target / f"datasets/{db_dirname}/llm_x_sensors_temporal.yaml").read_text()
    )
    # llm side comes first in the join's `sources: [llm, sensors]`.
    assert ds["main_dttm_col"] == "llm_timestamp"
    # Both sides' time columns appear as is_dttm columns.
    dttm_cols = {c["column_name"] for c in ds["columns"] if c.get("is_dttm")}
    assert {"llm_timestamp", "sensors_ts"} == dttm_cols


def test_emit_superset_idempotent_uuids(registry: Registry, tmp_path: Path) -> None:
    """Two emits must produce byte-identical YAML for stable round-trip."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    registry.emit_superset(a)
    registry.emit_superset(b)
    for ap in a.rglob("*.yaml"):
        bp = b / ap.relative_to(a)
        assert ap.read_text() == bp.read_text(), f"emit not idempotent: {ap.name}"


def test_emit_superset_metadata_required_fields(registry: Registry, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    md = yaml.safe_load((target / "metadata.yaml").read_text())
    assert md["version"] == SUPERSET_IMPORT_VERSION
    assert "type" in md
    assert "timestamp" in md


def test_emit_superset_database_required_fields(registry: Registry, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    db_dir = target / "databases"
    db_yaml = next(db_dir.glob("*.yaml"))
    db = yaml.safe_load(db_yaml.read_text())
    for field in ("database_name", "sqlalchemy_uri", "uuid", "version"):
        assert field in db, f"databases yaml missing {field}"
    assert db["version"] == SUPERSET_IMPORT_VERSION


def test_emit_superset_dataset_required_fields(registry: Registry, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    for ds_path in (target / "datasets").rglob("*.yaml"):
        ds = yaml.safe_load(ds_path.read_text())
        for field in ("table_name", "uuid", "version", "database_uuid",
                      "metrics", "columns"):
            assert field in ds, f"{ds_path.name} missing {field}"
        for m in ds["metrics"]:
            assert "metric_name" in m and "expression" in m
        for c in ds["columns"]:
            assert "column_name" in c


def test_emit_superset_folds_metric_where_clause(registry: Registry, tmp_path: Path) -> None:
    """crew_delegations should appear with CASE WHEN in the metric expression
    even though the registry stores expr+where separately."""
    target = tmp_path / "bundle"
    registry.emit_superset(target)
    crew_yaml = next((target / "datasets").rglob("crew_log.yaml"))
    ds = yaml.safe_load(crew_yaml.read_text())
    metric_exprs = {m["metric_name"]: m["expression"] for m in ds["metrics"]}
    assert "CASE WHEN" in metric_exprs["crew_delegations"]
    assert "delegate" in metric_exprs["crew_delegations"]


# ---------- cross-source v1 — named joins ---------------------------------


def test_join_block_loaded(registry: Registry) -> None:
    """The shipping registry.yaml should expose the v1 + v1.1 joins."""
    assert "llm_x_history_event" in registry.joins
    assert "crew_x_llm_command" in registry.joins
    assert "llm_x_sensors_temporal" in registry.joins  # v1.1
    j = registry.joins["llm_x_history_event"]
    assert j.sources == ("llm", "history")
    assert j.kind == "inner"
    assert j.on_left == "id" and j.on_right == "id"
    assert j.filter == "right.kind = 'llm'"
    tb = registry.joins["llm_x_sensors_temporal"]
    assert tb.kind == "time_bucket"
    assert tb.sources == ("llm", "sensors")


def test_join_llm_x_history_event(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive: inner join with on.filter, dim from the right side."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    sql, params = registry.compile(
        metric="llm_avg_ms",
        group_by=["hist_kind"],
        join="llm_x_history_event",
    )
    # source-aliased FROM
    assert "FROM llm_calls AS llm" in sql
    # explicit INNER JOIN to history with alias
    assert "INNER JOIN history AS history" in sql
    # on-clause with both sides qualified + filter rewritten from `right.kind`
    assert "ON llm.id = history.id" in sql
    assert "history.kind = 'llm'" in sql
    # SELECT cols are source-qualified
    assert "history.kind AS hist_kind" in sql
    assert "AVG(llm.duration_ms)" in sql
    assert "AS llm_avg_ms" in sql
    # group-by + order-by use the metric / dim names (aliases), not raw exprs
    assert "GROUP BY hist_kind" in sql
    assert "ORDER BY llm_avg_ms DESC" in sql
    assert params == []


def test_join_left_kind_crew_x_llm_command(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    sql, _ = registry.compile(
        metric="crew_delegations",
        group_by=["command"],
        join="crew_x_llm_command",
    )
    assert "FROM crew_log AS crew" in sql
    assert "LEFT JOIN llm_calls AS llm" in sql
    assert "ON crew.agent_to = llm.command" in sql
    # the metric's where: ["action = 'delegate'"] must be qualified to crew
    assert "crew.action = 'delegate'" in sql
    # group-by dim from llm side qualifies the right way
    assert "llm.command AS command" in sql


def test_join_with_since_qualifies_time_col(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    sql, params = registry.compile(
        metric="llm_avg_ms",
        group_by=["hist_kind"],
        join="llm_x_history_event",
        since="2026-04-01",
    )
    assert "llm.timestamp >= ?" in sql
    assert params == ["2026-04-01"]


def test_join_unknown_name(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    with pytest.raises(RegistryError, match="unknown join 'nope'"):
        registry.compile(metric="llm_calls", join="nope")


def test_join_metric_source_mismatch(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Metric on `sensors` against a join covering [llm, history] must raise."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    with pytest.raises(RegistryError, match="requires source 'sensors'"):
        registry.compile(
            metric="sensor_readings",
            join="llm_x_history_event",
        )


def test_join_dim_not_covered(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dim from a third source must raise even with a valid metric+join."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    with pytest.raises(RegistryError, match="not covered by"):
        registry.compile(
            metric="llm_avg_ms",
            group_by=["probe"],  # sensors — outside [llm, history]
            join="llm_x_history_event",
        )


def test_join_time_bucket_compiles(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.1: time_bucket joins compile to INNER JOIN with both `on.left`
    and `on.right` already cast to a comparable temporal grain. This
    powers the chat-burst × memory-pressure correlation chart."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    sql, params = registry.compile(
        metric="llm_avg_ms",
        group_by=["probe"],
        join="llm_x_sensors_temporal",
    )
    assert "FROM llm_calls AS llm" in sql
    # time_bucket renders as INNER JOIN — same SQL shape as a `kind: inner`
    # join; the `time_bucket` label is documentation, not a different operator.
    assert "INNER JOIN sensor_log AS sensors" in sql
    # ON clause uses the user-supplied time-cast expressions, qualified to
    # each side's alias.
    assert "ON substr(llm.timestamp, 1, 10) = date(sensors.ts, 'unixepoch')" in sql
    # Dim from sensors side is qualified.
    assert "sensors.probe AS probe" in sql
    # Metric expr qualified to its source.
    assert "AVG(llm.duration_ms)" in sql
    assert "GROUP BY probe" in sql
    assert params == []


def test_join_unknown_kind_rejected(tmp_path: Path) -> None:
    """Defensive: any kind outside {inner, left, time_bucket} fails loud
    with a message naming the supported set so authors don't bikeshed."""
    bad = tmp_path / "bad-kind.yaml"
    bad.write_text(yaml.safe_dump({
        "sources": {
            "llm": {"table": "llm_calls", "time_col": "timestamp"},
            "history": {"table": "history", "time_col": "timestamp"},
        },
        "dimensions": {
            "model": {"source": "llm", "expr": "model"},
            "hist_kind": {"source": "history", "expr": "kind"},
        },
        "metrics": {
            "llm_avg_ms": {"source": "llm", "expr": "AVG(duration_ms)"},
        },
        "joins": {
            "bad_join": {
                "sources": ["llm", "history"],
                "kind": "asof",  # not yet supported — must fail loud
                "on": {"left": "id", "right": "id"},
            },
        },
    }))
    reg = Registry.load(path=bad)
    import os as _os
    _os.environ["ORG_LLM_REGISTRY_V1_JOINS"] = "1"
    try:
        with pytest.raises(RegistryError, match="kind 'asof' not supported"):
            reg.compile(
                metric="llm_avg_ms",
                group_by=["hist_kind"],
                join="bad_join",
            )
    finally:
        _os.environ.pop("ORG_LLM_REGISTRY_V1_JOINS", None)


def test_v0_rejection_unchanged_without_flag(registry: Registry) -> None:
    """Without the env flag, v0 rejection message stays byte-identical."""
    # No hint should appear; the existing message ends at "from source 'llm'"
    with pytest.raises(RegistryError) as exc:
        registry.compile(metric="llm_calls", group_by=["agent_to"])
    msg = str(exc.value)
    assert "hint:" not in msg


def test_v0_rejection_has_hint_with_flag(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag set but no join= passed, rejection message points the
    caller at the named join that would unlock this query."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    with pytest.raises(RegistryError) as exc:
        registry.compile(metric="llm_avg_ms", group_by=["hist_kind"])
    assert "hint:" in str(exc.value)
    assert "llm_x_history_event" in str(exc.value)


def test_join_query_executes(
    registry: Registry, fixture_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: with a tiny matched history row, the inner join returns
    only events flagged kind='llm'."""
    monkeypatch.setenv("ORG_LLM_REGISTRY_V1_JOINS", "1")
    conn = sqlite3.connect(fixture_db)
    conn.executemany(
        "INSERT INTO history(id,timestamp,command,kind,duration_ms,outcome) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1, "2026-04-28T10:00:00", "chat", "llm", 1100, "ok"),
            (2, "2026-04-28T10:01:00", "chat", "llm", 2100, "error"),
            (3, "2026-04-28T10:02:00", "embed", "shell", 50, "ok"),
        ],
    )
    conn.commit()
    conn.close()
    rows = registry.query(
        metric="llm_avg_ms",
        group_by=["hist_kind"],
        join="llm_x_history_event",
    )
    # Inner join + filter kind='llm' restricts to ids {1,2}; rows 1100ms+2000ms
    by_kind = {r["hist_kind"]: r["llm_avg_ms"] for r in rows}
    assert "llm" in by_kind
    assert by_kind["llm"] == 1500.0  # (1000 + 2000) / 2 from llm_calls.duration_ms
