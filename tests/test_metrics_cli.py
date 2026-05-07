"""Tests for `org-llm metrics import` + `org-llm metrics emit --overwrite`.

These verbs shell out to apache-superset's `superset import-directory`
binary. Mocking subprocess.run is enough to assert the right argv is
built; we don't need a live Superset.

Verified against apache-superset 4.1.1 source
(superset/cli/importexport.py): `import-directory` accepts
--overwrite/-o directly, contrary to the wiki tier-1 probe note. Tests
pin that flag-shape so a future upstream rename fails loud here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from org_llm.cli import app, _superset_import_argv


runner = CliRunner()


# ---- pure argv builder -------------------------------------------------------


class TestSupersetImportArgv:
    def test_no_overwrite(self):
        argv = _superset_import_argv("/usr/bin/superset", Path("docs/superset"), False)
        assert argv == ["/usr/bin/superset", "import-directory", "docs/superset"]

    def test_overwrite_appends_flag(self):
        argv = _superset_import_argv("/usr/bin/superset", Path("docs/superset"), True)
        assert argv == [
            "/usr/bin/superset",
            "import-directory",
            "docs/superset",
            "--overwrite",
        ]

    def test_directory_passed_as_str(self):
        argv = _superset_import_argv("superset", Path("/abs/path"), False)
        assert argv[2] == "/abs/path"


# ---- `metrics import` end-to-end (mocked subprocess) ------------------------


@pytest.fixture
def fake_bundle(tmp_path: Path) -> Path:
    """Minimal directory that passes the existence check."""
    d = tmp_path / "superset_bundle"
    d.mkdir()
    (d / "metadata.yaml").write_text("version: 1.0.0\n")
    return d


class TestMetricsImportCmd:
    def test_no_overwrite_argv(self, fake_bundle: Path):
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(
                app, ["metrics", "import", "--out", str(fake_bundle)]
            )
        assert result.exit_code == 0, result.output
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv == [
            "/fake/superset",
            "import-directory",
            str(fake_bundle),
        ]

    def test_overwrite_argv(self, fake_bundle: Path):
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(
                app,
                ["metrics", "import", "--out", str(fake_bundle), "--overwrite"],
            )
        assert result.exit_code == 0, result.output
        argv = run.call_args.args[0]
        assert argv[-1] == "--overwrite"
        assert argv[1] == "import-directory"

    def test_short_overwrite_flag(self, fake_bundle: Path):
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(
                app, ["metrics", "import", "-d", str(fake_bundle), "-o"]
            )
        assert result.exit_code == 0, result.output
        argv = run.call_args.args[0]
        assert "--overwrite" in argv

    def test_missing_binary_exits_2(self, fake_bundle: Path):
        with patch("shutil.which", return_value=None):
            result = runner.invoke(
                app, ["metrics", "import", "--out", str(fake_bundle)]
            )
        assert result.exit_code == 2
        assert "superset CLI not found" in result.output

    def test_missing_directory_exits_2(self, tmp_path: Path):
        nope = tmp_path / "does-not-exist"
        with patch("shutil.which", return_value="/fake/superset"):
            result = runner.invoke(
                app, ["metrics", "import", "--out", str(nope)]
            )
        assert result.exit_code == 2
        assert "bundle directory not found" in result.output

    def test_subprocess_failure_propagates(self, fake_bundle: Path):
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 7)
            result = runner.invoke(
                app, ["metrics", "import", "--out", str(fake_bundle)]
            )
        assert result.exit_code == 7

    def test_env_passed_through(self, fake_bundle: Path, monkeypatch):
        """SUPERSET_CONFIG_PATH must reach the subprocess env."""
        monkeypatch.setenv("SUPERSET_CONFIG_PATH", "/etc/superset/cfg.py")
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            runner.invoke(app, ["metrics", "import", "--out", str(fake_bundle)])
        env = run.call_args.kwargs["env"]
        assert env.get("SUPERSET_CONFIG_PATH") == "/etc/superset/cfg.py"


# ---- `metrics emit --overwrite` chains emit + import ------------------------


class TestMetricsEmitOverwrite:
    def test_emit_no_overwrite_does_not_call_subprocess(self, tmp_path: Path):
        out = tmp_path / "bundle"
        with patch("subprocess.run") as run:
            result = runner.invoke(
                app, ["metrics", "emit", "--out", str(out)]
            )
        assert result.exit_code == 0, result.output
        assert run.call_count == 0
        # emit_superset should still have written files
        assert (out / "metadata.yaml").exists()

    def test_emit_with_overwrite_calls_import(self, tmp_path: Path):
        out = tmp_path / "bundle"
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(
                app, ["metrics", "emit", "--out", str(out), "--overwrite"]
            )
        assert result.exit_code == 0, result.output
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[1] == "import-directory"
        assert argv[-1] == "--overwrite"
        assert str(out) in argv

    def test_emit_overwrite_propagates_subprocess_failure(self, tmp_path: Path):
        out = tmp_path / "bundle"
        with (
            patch("shutil.which", return_value="/fake/superset"),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 5)
            result = runner.invoke(
                app, ["metrics", "emit", "--out", str(out), "--overwrite"]
            )
        assert result.exit_code == 5
