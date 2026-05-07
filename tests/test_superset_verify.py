"""Tests for org_llm.superset_verify — headless-render smoke check.

The unit tests mock the subprocess that runs Playwright inside the
Superset venv, so they don't need Chromium or a running Superset.
A live test marked `pytest.mark.live` exercises the full path
against a real Superset (skipped by default; opt in with
ORG_LLM_TEST_LIVE_SUPERSET=1).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from org_llm.superset_verify import (
    ChartResult,
    ERROR_MARKERS,
    SupersetVenvNotFound,
    superset_venv_present,
    verify,
)


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_chart_result_ok_truthiness() -> None:
    assert ChartResult(1, "ok", "").ok is True
    assert ChartResult(2, "broken", "Data Error").ok is False


def test_error_markers_cover_known_classes() -> None:
    """Pin the error markers we've actually seen in the wild so this
    test fails loud if someone removes one without replacing it."""
    must_have = {
        "Data Error",
        "Add required control values",  # heatmap_v2 missing fields
        "Cannot read properties of undefined",  # echarts crash
        "Datetime column not provided",  # x_axis missing
    }
    assert must_have.issubset(set(ERROR_MARKERS))


def test_superset_venv_not_found_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope-venv"
    with pytest.raises(SupersetVenvNotFound):
        verify(venv=nonexistent)


def test_verify_parses_runner_output(tmp_path: Path) -> None:
    """When the subprocess exits 0 with valid JSON, verify() returns
    one ChartResult per chart with the right fields decoded."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()

    payload = json.dumps([
        {"chart_id": 1, "name": "Heatmap", "error": "", "screenshot_path": None},
        {"chart_id": 2, "name": "Bar", "error": "Data Error",
         "screenshot_path": "/tmp/2.png"},
    ])
    with patch.object(subprocess, "run",
                      return_value=_fake_completed(payload)) as mrun:
        results = verify(venv=venv)
    assert len(results) == 2
    assert results[0].chart_id == 1 and results[0].ok
    assert results[1].chart_id == 2 and not results[1].ok
    assert results[1].error == "Data Error"
    assert results[1].screenshot_path == "/tmp/2.png"
    # Confirm the runner gets all the args it needs
    args = mrun.call_args[0][0]
    assert str(venv / "bin" / "python") in args[0]
    assert args[1] == "-c"
    # url/user/pass at fixed positions
    assert args[3] == "http://localhost:8088"
    assert args[4] == "admin"


def test_verify_passes_dashboard_and_screenshot_args(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    shots = tmp_path / "shots"

    with patch.object(subprocess, "run",
                      return_value=_fake_completed("[]")) as mrun:
        verify(
            venv=venv,
            dashboard_slug="captains-bridge",
            screenshot_dir=shots,
            timeout_ms=15000,
        )
    args = mrun.call_args[0][0]
    # argv positions per _runner_script(): url, user, pass, dash, dir, ms, markers
    assert args[6] == "captains-bridge"
    assert args[7] == str(shots)
    assert args[8] == "15000"
    assert json.loads(args[9]) == list(ERROR_MARKERS)


def test_verify_runner_failure_surfaces_stderr(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()

    with patch.object(
        subprocess, "run",
        return_value=_fake_completed("", returncode=1, stderr="auth failed: 401"),
    ):
        with pytest.raises(RuntimeError, match="auth failed: 401"):
            verify(venv=venv)


def test_superset_venv_present_resolves_default_path(tmp_path: Path) -> None:
    venv = tmp_path / "ok-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    assert superset_venv_present(venv=venv) is True
    assert superset_venv_present(venv=tmp_path / "missing") is False


@pytest.mark.skipif(
    not os.environ.get("ORG_LLM_TEST_LIVE_SUPERSET"),
    reason="set ORG_LLM_TEST_LIVE_SUPERSET=1 to run against a real Superset",
)
def test_verify_against_live_superset() -> None:
    """End-to-end: actually render every chart in the local Superset.

    Run with:
        ORG_LLM_TEST_LIVE_SUPERSET=1 uv run pytest \\
            tests/test_superset_verify.py::test_verify_against_live_superset
    """
    results = verify()
    assert results, "expected at least one chart in the workspace"
    failures = [r for r in results if not r.ok]
    assert not failures, (
        f"{len(failures)} chart(s) failed render: "
        + ", ".join(f"#{r.chart_id} {r.name!r}: {r.error}" for r in failures)
    )
