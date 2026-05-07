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
    COMPOSED_FILENAME,
    PERSONA_FILES,
    compose_system_prompt,
    create_session_with_persona,
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


# ── BUG-5 workaround: SOUL → behavior wiring ────────────────────────────────
#
# Agor v0.17.3 doesn't auto-load .agor-assistants/<handle>/. The
# workaround composes the trio at session-create time + bakes
# the result into the initial prompt body. These tests pin:
#  - exact composed format (so future Agor field-name swaps stay
#    a pure string-replace at the wire layer)
#  - graceful USER.md handling (vault dirs may have been hand-edited)
#  - the materializer also drops a `composed.md` next to the trio
#    for any future Agor auto-load hook.

def test_compose_system_prompt_picard(tmp_path):
    """compose_system_prompt(@picard) returns SOUL + IDENTITY + USER
    in that exact order with the canonical separators.
    """
    materialize(tmp_path, commit=True, personas=(get_persona("picard"),))
    out = compose_system_prompt("picard", tmp_path)
    picard = get_persona("picard")
    # Order is non-negotiable: SOUL first, then the literal markers.
    assert out.startswith(picard.soul.rstrip())
    assert "\n--- IDENTITY ---\n" in out
    assert "\n--- USER PREFERENCES ---\n" in out
    # SOUL appears before IDENTITY appears before USER PREFERENCES.
    soul_idx = out.index(picard.soul.rstrip())
    id_idx   = out.index("--- IDENTITY ---")
    user_idx = out.index("--- USER PREFERENCES ---")
    assert soul_idx < id_idx < user_idx
    # IDENTITY content lands in the middle band; USER content at the end.
    assert picard.identity.rstrip() in out
    assert picard.user.rstrip() in out
    # Trailing newline so concatenation w/ a downstream task is clean.
    assert out.endswith("\n")


def test_compose_system_prompt_with_base_override(tmp_path):
    """`base_system_prompt` is appended AFTER the trio so caller
    overrides win the last word.
    """
    materialize(tmp_path, commit=True, personas=(get_persona("spock"),))
    base = "You are running in CI; be terse."
    out = compose_system_prompt("spock", tmp_path, base_system_prompt=base)
    # The trio still appears…
    assert "--- IDENTITY ---" in out
    assert "--- USER PREFERENCES ---" in out
    # …but the BASE block lands AFTER USER PREFERENCES.
    assert "--- BASE ---" in out
    assert out.index("--- USER PREFERENCES ---") < out.index("--- BASE ---")
    assert base in out
    # And BASE is the last non-blank section.
    assert out.rstrip().endswith(base)


def test_compose_system_prompt_missing_handle_raises(tmp_path):
    """Unknown handle → KeyError (re-raised from get_persona)."""
    with pytest.raises(KeyError):
        compose_system_prompt("kirk", tmp_path)


def test_create_session_with_persona_includes_composed_prompt(tmp_path):
    """`create_session_with_persona` POSTs a body whose
    description/prompt contains the composed SOUL/IDENTITY/USER
    text. Mocks the POST via the `_post` injection seam.
    """
    materialize(tmp_path, commit=True, personas=(get_persona("data"),))

    captured: dict = {}

    def fake_post(url, *, token_file, body, timeout):
        captured["url"] = url
        captured["body"] = body
        return {"session_id": "sess-123", "mcp_token": "tok-abc"}

    resp = create_session_with_persona(
        "data", tmp_path,
        prompt="Capture: I had coffee at 8am.",
        worktree_id="wt-uuid-1",
        _post=fake_post,
    )
    assert resp["session_id"] == "sess-123"
    assert captured["url"].endswith("/sessions")
    body = captured["body"]
    # Composed persona prompt is in the wire body…
    data = get_persona("data")
    assert data.soul.rstrip() in body["description"]
    assert "--- IDENTITY ---" in body["description"]
    # …user task is appended below a TASK separator.
    assert "--- TASK ---" in body["description"]
    assert "Capture: I had coffee at 8am." in body["description"]
    # Worktree ID + tool default land on the wire.
    assert body["worktree_id"] == "wt-uuid-1"
    assert body["agentic_tool"] == "claude-code"


def test_compose_handles_missing_user_md_gracefully(tmp_path):
    """USER.md is optional; SOUL + IDENTITY alone produces a
    valid composed prompt with the USER PREFERENCES section
    omitted.
    """
    materialize(tmp_path, commit=True, personas=(get_persona("riker"),))
    user_path = tmp_path / ASSISTANTS_SUBDIR / "riker" / "USER.md"
    user_path.unlink()
    out = compose_system_prompt("riker", tmp_path)
    riker = get_persona("riker")
    assert riker.soul.rstrip() in out
    assert "--- IDENTITY ---" in out
    assert riker.identity.rstrip() in out
    # USER PREFERENCES section is fully omitted (not just empty).
    assert "--- USER PREFERENCES ---" not in out


def test_materializer_writes_composed_md(tmp_path):
    """`materialize(commit=True)` drops a `composed.md` next to
    the SOUL/IDENTITY/USER trio for every persona — the single
    file Agor can consume if upstream lands an auto-load hook.
    """
    materialize(tmp_path, commit=True, personas=(get_persona("atoz"),))
    composed = tmp_path / ASSISTANTS_SUBDIR / "atoz" / COMPOSED_FILENAME
    assert composed.is_file(), f"missing composed.md at {composed}"
    text = composed.read_text(encoding="utf-8")
    atoz = get_persona("atoz")
    # composed.md content equals the in-memory compose_system_prompt result.
    expected = compose_system_prompt("atoz", tmp_path)
    assert text == expected
    # Sanity: SOUL content is present.
    assert atoz.soul.rstrip() in text
