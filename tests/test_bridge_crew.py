"""Tests for Bridge Crew → Agor assistant materializer.

Pins the canonical 7-persona × 3-files = 21-write contract,
the dry-run / commit split, and the error paths (worktree
missing, persona unknown). Per feedback_test_before_handoff:
green here gates the report.

Per project_self_coded_tools (Phase 29 — DB-authoritative
inversion): the materializer is a deterministic verb on top
of the wiki canon, so its tests pin the file layout that
later phases will register with Agor's API.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.bridge_crew import (
    ASSISTANTS_SUBDIR,
    BRIDGE_CREW,
    PERSONA_FILES,
    get_persona,
    materialize,
    planned_writes,
)
from org_llm.cli import app


runner = CliRunner()


# ── Pure function: planned_writes ────────────────────────────────────────────

def test_planned_writes_full_crew_is_21(tmp_path):
    """Seven personas × three files = 21 records, no I/O."""
    records = planned_writes(tmp_path)
    assert len(records) == 21
    assert len(BRIDGE_CREW) == 7
    # No file actually appears on disk
    assert not (tmp_path / ASSISTANTS_SUBDIR).exists()


def test_planned_writes_paths_are_handle_namespaced(tmp_path):
    """Every path lives under <worktree>/.agor-assistants/<handle>/."""
    records = planned_writes(tmp_path)
    for r in records:
        assert r.path.parent.name == r.persona
        assert r.path.parent.parent.name == ASSISTANTS_SUBDIR
        assert r.filename in PERSONA_FILES
        assert r.written is False


# ── materialize: dry-run vs. commit ─────────────────────────────────────────

def test_materialize_dry_run_writes_nothing(tmp_path):
    """commit=False is the safe default."""
    records = materialize(tmp_path, commit=False)
    assert len(records) == 21
    assert all(r.written is False for r in records)
    assert not (tmp_path / ASSISTANTS_SUBDIR).exists()


def test_materialize_commit_creates_all_21_files(tmp_path):
    """commit=True writes the trio for every Bridge Crew member."""
    records = materialize(tmp_path, commit=True)
    assert len(records) == 21
    assert all(r.written is True for r in records)
    base = tmp_path / ASSISTANTS_SUBDIR
    assert base.is_dir()
    for p in BRIDGE_CREW:
        for fname in PERSONA_FILES:
            f = base / p.handle / fname
            assert f.is_file(), f"missing {f}"
            # Every file is non-empty
            assert f.read_text(encoding="utf-8").strip()


def test_materialize_persona_subset(tmp_path):
    """The personas= filter restricts to a subset."""
    only_picard = (get_persona("picard"),)
    records = materialize(tmp_path, commit=True, personas=only_picard)
    assert len(records) == 3
    # The picard dir exists; no other persona dir does
    assert (tmp_path / ASSISTANTS_SUBDIR / "picard").is_dir()
    other_dirs = [d for d in (tmp_path / ASSISTANTS_SUBDIR).iterdir()
                  if d.is_dir() and d.name != "picard"]
    assert other_dirs == []


# ── Error paths ─────────────────────────────────────────────────────────────

def test_materialize_missing_worktree_raises(tmp_path):
    """Refuse to create the worktree itself — Agor's responsibility."""
    nonexistent = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        materialize(nonexistent, commit=True)


def test_get_persona_unknown_handle_raises():
    """Bad handle → KeyError listing the valid handles."""
    with pytest.raises(KeyError) as exc_info:
        get_persona("kirk")
    msg = str(exc_info.value)
    # Error message lists valid handles so user can fix typos
    assert "picard" in msg
    assert "spock" in msg


# ── CLI surface ─────────────────────────────────────────────────────────────

def test_cli_help_lists_materialize():
    """`org-llm bridge-crew --help` surfaces the materialize verb."""
    result = runner.invoke(app, ["bridge-crew", "--help"])
    assert result.exit_code == 0
    assert "materialize" in result.output


def test_cli_dry_run_default(tmp_path):
    """Bare materialize is dry-run; nothing hits disk; summary printed."""
    result = runner.invoke(app, ["bridge-crew", "materialize", str(tmp_path)])
    assert result.exit_code == 0
    assert "would write" in result.output
    assert "21 files" in result.output
    assert not (tmp_path / ASSISTANTS_SUBDIR).exists()
