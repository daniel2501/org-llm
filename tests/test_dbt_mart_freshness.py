"""Regression guard for the 2026-05-06 timestamp-drift bug.

The Superset tier-1 probe found that `llm_calls.timestamp` and
`history.timestamp` for the same 1000 events disagreed by ~6 days. Root
cause: dbt marts were materialized as `table` and snapshotted at the
last `dbt build` while `history` kept growing. The fix in
`org_llm/dbt_templates/dbt_project.yml` flips the four event-stream
marts (llm_calls, cli_invocations, recent_activity, recent_nodes) to
`view`, so they always reflect live `history`.

Two layers of defence:

1. ``test_event_stream_marts_are_views`` — fast YAML-parse canary.
   Catches a casual revert of the override.
2. ``test_dbt_actually_resolves_marts_as_views`` — invokes ``dbt parse``
   on a temp copy of the bundled template and inspects the resulting
   ``manifest.json``. This is the *real* regression guard: the
   2026-05-06 follow-up bug (commit ``deeb3f3`` → ``a360ad0``) shipped
   flow-style YAML — ``llm_calls: { +materialized: view }`` — that
   ``yaml.safe_load`` parsed identically to block-style but ``dbt``
   silently ignored, leaving the marts as tables. Only inspecting dbt's
   own resolved config catches that class of bug.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DBT_TEMPLATE_DIR = (
    Path(__file__).parent.parent / "org_llm" / "dbt_templates"
)
DBT_PROJECT = DBT_TEMPLATE_DIR / "dbt_project.yml"

EVENT_STREAM_MARTS = ("llm_calls", "cli_invocations", "recent_activity", "recent_nodes")


def test_event_stream_marts_are_views() -> None:
    """Fast canary: YAML structurally declares the four marts as views.

    Cheap (no subprocess) so it stays in the default test loop. Does NOT
    catch the flow-style-vs-block-style bug — see the dbt-parse test
    below for that.
    """
    cfg = yaml.safe_load(DBT_PROJECT.read_text())
    marts = cfg["models"]["org_llm"]["marts"]
    assert marts["+materialized"] == "table", "default mart materialization should remain table"
    for mart in EVENT_STREAM_MARTS:
        assert mart in marts, f"missing mart override for {mart!r}"
        assert marts[mart]["+materialized"] == "view", (
            f"{mart} must stay a view — materializing as table reintroduces the "
            f"2026-05-06 timestamp drift bug (see docs/wiki/2026-05-06-superset-tier1-probe.org)"
        )


def _dbt_bin() -> str | None:
    """Locate the dbt executable. Mirrors org_llm.cli._dbt_bin so this
    test works under `uv run pytest`, `pipx`-installed dbt, or a plain
    venv. Returns None if dbt isn't reachable so the test can skip
    cleanly rather than error."""
    found = shutil.which("dbt")
    if found:
        return found
    sibling = Path(sys.executable).parent / "dbt"
    if sibling.exists():
        return str(sibling)
    return None


def test_dbt_actually_resolves_marts_as_views(tmp_path: Path) -> None:
    """Invoke `dbt parse` on a temp copy of the bundled template and
    assert dbt itself (not just yaml.safe_load) resolves each event-stream
    mart as a view.

    Why this exists: on 2026-05-06 the four overrides were written in
    flow-style YAML — `llm_calls: { +materialized: view }`. PyYAML parses
    that identically to block-style, so the YAML-only canary above
    passed, but dbt's own loader silently dropped the override and
    materialized the marts as tables. The bug shipped to trunk and only
    surfaced in `dbt build`. The fix (commit a360ad0) switched to
    block-style. This test pins dbt's *resolved* materialization so the
    regression cannot recur regardless of YAML style choices.
    """
    dbt = _dbt_bin()
    if dbt is None:
        pytest.skip("dbt not available on PATH/venv")

    # Stage a self-contained dbt project: project file, models, profiles.
    shutil.copy(DBT_PROJECT, tmp_path / "dbt_project.yml")
    shutil.copytree(DBT_TEMPLATE_DIR / "models", tmp_path / "models")
    shutil.copy(DBT_TEMPLATE_DIR / "profiles.yml", tmp_path / "profiles.yml")

    # profiles.yml resolves ORG_LLM_DB at parse time. dbt-sqlite needs the
    # attached DB file to exist, so create an empty SQLite db. Parse
    # doesn't execute SQL, so empty-schema is fine.
    db_path = tmp_path / "org-llm.db"
    sqlite3.connect(db_path).close()

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(tmp_path),  # isolate from the user's ~/.dbt cache
        "ORG_LLM_DB": str(db_path),
        "ORG_LLM_DB_DIR": str(tmp_path),
        "DBT_PROFILES_DIR": str(tmp_path),
    }

    proc = subprocess.run(
        [
            dbt,
            "parse",
            "--project-dir", str(tmp_path),
            "--profiles-dir", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"dbt parse failed (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

    manifest_path = tmp_path / "target" / "manifest.json"
    assert manifest_path.exists(), (
        f"dbt parse did not produce manifest.json. stdout: {proc.stdout}"
    )
    manifest = json.loads(manifest_path.read_text())

    # manifest.nodes keys look like "model.org_llm.<mart_name>". Index by
    # the trailing component so the assertion is style-agnostic.
    nodes_by_name = {
        node["name"]: node
        for node in manifest["nodes"].values()
        if node.get("resource_type") == "model"
    }

    for mart in EVENT_STREAM_MARTS:
        assert mart in nodes_by_name, (
            f"mart {mart!r} missing from dbt manifest; "
            f"got {sorted(nodes_by_name)}"
        )
        resolved = nodes_by_name[mart]["config"]["materialized"]
        assert resolved == "view", (
            f"dbt resolved {mart!r} as {resolved!r}, expected 'view'. "
            f"This is the 2026-05-06 flow-style YAML bug recurring — "
            f"check that dbt_project.yml uses BLOCK-style overrides "
            f"(`{mart}:\\n  +materialized: view`), not flow-style "
            f"(`{mart}: {{ +materialized: view }}`). See commits "
            f"deeb3f3 (broken) → a360ad0 (fixed)."
        )
