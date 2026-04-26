# [[file:../../../org/20260425230731-org_llm.org::*tests/test_performance.py][test_performance.py:1]]
"""Tests for org_llm.performance — hardware probe + recommend()."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from org_llm.cli import _normalize_tag, _is_pulled, app, _strip_code_fences
from org_llm.performance import (
    BenchmarkResult,
    HardwareSnapshot,
    probe_hardware,
    recommend,
)

runner = CliRunner()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "perf.db"))
    runner.invoke(app, ["init"])
    return tmp_path / "perf.db"


# ── Tag normalization ───────────────────────────────────────────────────────

class TestTagNormalization:
    def test_strips_latest_suffix(self):
        assert _normalize_tag("llama3.3:latest") == "llama3.3"
        assert _normalize_tag("nomic-embed-text:LATEST") == "nomic-embed-text"

    def test_lowercases(self):
        assert _normalize_tag("Llama3.3") == "llama3.3"

    def test_keeps_specific_variant(self):
        assert _normalize_tag("deepseek-r1:7b") == "deepseek-r1:7b"

    def test_handles_empty(self):
        assert _normalize_tag("") == ""
        assert _normalize_tag(None) == ""

    def test_pulled_set_match(self):
        pulled = {_normalize_tag("llama3.3:latest"),
                  _normalize_tag("nomic-embed-text:latest"),
                  _normalize_tag("deepseek-r1:7b")}
        # User's chat_model is "llama3.3" (no tag) — should be considered pulled
        assert _is_pulled("llama3.3", pulled)
        # User's catalog tag "llama3.3:70b" should also match (stem-match)
        assert _is_pulled("llama3.3:70b", pulled)
        # Different stem must NOT match
        assert not _is_pulled("llama3.2", pulled)
        # Empty string is never pulled
        assert not _is_pulled("", pulled)


# ── Code fence stripping ────────────────────────────────────────────────────

class TestStripCodeFences:
    def test_strips_lang_fences(self):
        assert _strip_code_fences("```elisp\n(message \"hi\")\n```") == "(message \"hi\")"

    def test_strips_plain_fences(self):
        assert _strip_code_fences("```\necho hi\n```") == "echo hi"

    def test_no_fences_unchanged(self):
        assert _strip_code_fences("(message \"hi\")") == "(message \"hi\")"

    def test_preserves_internal_fences(self):
        # If output happens to contain fenced sub-blocks, only outer pair is stripped
        out = _strip_code_fences("```python\ncode\n```\nmore stuff")
        # The trailing ``` matches; remaining content should keep the code line
        assert "code" in out


# ── probe_hardware ──────────────────────────────────────────────────────────

class TestProbeHardware:
    def test_returns_snapshot(self):
        hw = probe_hardware()
        assert isinstance(hw, HardwareSnapshot)
        assert hw.cpu_count >= 1
        # On a real Linux box, ram_total > 0
        assert hw.ram_total_gb > 0
        # ram_free is bounded by ram_total
        assert 0 <= hw.ram_free_gb <= hw.ram_total_gb + 0.5

    def test_disk_free_positive(self):
        hw = probe_hardware()
        assert hw.disk_free_gb > 0


# ── recommend() — pure logic ────────────────────────────────────────────────

class TestRecommend:
    def _hw(self, ram_free_gb=8.0, vram_free_gb=None):
        return HardwareSnapshot(
            cpu_model="test", cpu_count=4,
            ram_total_gb=16.0, ram_free_gb=ram_free_gb,
            vram_total_gb=None if vram_free_gb is None else vram_free_gb + 1,
            vram_free_gb=vram_free_gb,
            disk_free_gb=100.0,
        )

    def test_oversized_chat_gets_downgrade(self):
        # 2 GB free, llama3.3 needs ~48 GB → downgrade
        hw = self._hw(ram_free_gb=2.0)
        recs = recommend(
            hw,
            role_assignments={"chat": "llama3.3"},
            benchmarks={},
            pulled=set(),
            cloud_configured=False,
        )
        chat_rec = next((r for r in recs if r.role == "chat"), None)
        assert chat_rec is not None
        assert chat_rec.severity == "downgrade"
        assert "needs" in chat_rec.reason

    def test_missing_role_flagged(self):
        hw = self._hw(ram_free_gb=8.0)
        recs = recommend(
            hw,
            role_assignments={"chat": ""},
            benchmarks={},
            pulled=set(),
            cloud_configured=False,
        )
        chat_rec = next((r for r in recs if r.role == "chat"), None)
        assert chat_rec is not None
        assert chat_rec.severity == "missing"

    def test_optimal_assignment_marked_fit(self):
        # 8 GB free, llama3.2:3b is ~3 GB → fits
        hw = self._hw(ram_free_gb=8.0)
        recs = recommend(
            hw,
            role_assignments={"chat": "llama3.2:3b"},
            benchmarks={},
            pulled={"llama3.2:3b"},
            cloud_configured=False,
        )
        chat_rec = next((r for r in recs if r.role == "chat"), None)
        # Could be fit OR upgrade depending on whether a higher-quality model
        # also fits — both are acceptable
        assert chat_rec is not None
        assert chat_rec.severity in ("fit", "upgrade")

    def test_benchmark_error_forces_downgrade(self):
        # Plenty of RAM — model fits — but benchmark hit OOM at runtime.
        # That's exactly the case --benchmark is supposed to catch.
        hw = self._hw(ram_free_gb=32.0)
        bench = {
            "qwen2.5-coder:7b": BenchmarkResult(
                model="qwen2.5-coder:7b", role="chat",
                latency_ms=0, tokens_per_sec=0, output_tokens=0,
                error="model requires more system memory",
            )
        }
        recs = recommend(
            hw,
            role_assignments={"code": "qwen2.5-coder:7b"},
            benchmarks=bench,
            pulled={"qwen2.5-coder:7b"},
            cloud_configured=False,
        )
        code_rec = next((r for r in recs if r.role == "code"), None)
        assert code_rec is not None
        assert code_rec.severity == "downgrade"
        assert "errored" in code_rec.reason


# ── CLI integration ─────────────────────────────────────────────────────────

class TestPerformanceCLI:
    def test_quick_runs_without_ollama(self, cli_db):
        r = runner.invoke(app, ["performance", "--quick"])
        assert r.exit_code == 0
        assert "Hardware" in r.output
        assert "RAM" in r.output

    def test_full_runs(self, cli_db):
        # Won't actually benchmark; just exercises the recommendation path
        r = runner.invoke(app, ["performance"])
        # exit_code may be non-zero if Ollama is unreachable AND no cloud — accept both
        assert r.exit_code in (0, 1)
        assert "Hardware" in r.output

    def test_apply_help(self, cli_db):
        r = runner.invoke(app, ["performance", "--help"])
        assert r.exit_code == 0
        assert "--apply" in r.output
        assert "--benchmark" in r.output
        assert "--quick" in r.output

    def test_prefix_matches_perf(self, cli_db):
        # Shortest unique prefix for `performance` is `perf`
        r = runner.invoke(app, ["perf", "--quick"])
        assert r.exit_code == 0
        assert "Hardware" in r.output
# test_performance.py:1 ends here
