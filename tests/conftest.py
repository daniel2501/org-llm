# [[file:../../../org/20260425230731-org_llm.org::*conftest.py][conftest.py:1]]
from __future__ import annotations

import pytest
from pathlib import Path

from org_llm.skills import Skill          # registers Skill table on Base
from org_llm.db import init_db, get_session, make_engine


@pytest.fixture
def db_engine(tmp_path):
    """Fresh in-file SQLite DB with all tables and default config."""
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """Fresh isolated DB wired in via ORG_LLM_DB so CLI commands hit it
    instead of the user's real ~/.local/share/org-llm/.

    Lifted from test_cli.py for cross-file reuse — test_knobs.py uses
    it for the knob CLI tests too.
    """
    from typer.testing import CliRunner
    from org_llm.cli import app
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("ORG_LLM_DB", str(db_path))
    CliRunner().invoke(app, ["init"])
    return db_path


@pytest.fixture
def session(db_engine):
    with get_session(db_engine) as s:
        yield s


@pytest.fixture
def tmp_org_dir(tmp_path):
    """Minimal org directory covering common edge cases."""
    org = tmp_path / "org"
    org.mkdir()

    # standard roam file: file-level ID + sub-heading with its own ID
    (org / "note1.org").write_text(
        ":PROPERTIES:\n:ID: id-001\n:END:\n"
        "#+title: First Note\n\n"
        "Body of the first note.\n\n"
        "* Sub-heading\n"
        ":PROPERTIES:\n:ID: id-002\n:END:\n\n"
        "Sub-heading body.\n"
    )
    # file with no #+title
    (org / "no_title.org").write_text("* Just a heading\n\nSome text.\n")

    # file with duplicate #+TITLE lines
    (org / "dup_title.org").write_text(
        "#+TITLE: Real Title\n#+TITLE: Ignored\n\nBody.\n"
    )
    # same :ID: in root properties AND first heading (real-world org-roam pattern)
    (org / "shared_id.org").write_text(
        ":PROPERTIES:\n:ID: shared-xyz\n:END:\n"
        "#+title: Shared ID\n\n"
        "* First heading\n:PROPERTIES:\n:ID: shared-xyz\n:END:\n\nBody.\n"
    )
    # daily note
    daily = org / "daily"
    daily.mkdir()
    (daily / "2026-01-01.org").write_text(
        "#+title: 2026-01-01\n\n* Tasks\n- Did stuff.\n"
    )
    return org


@pytest.fixture
def skill_file(tmp_path):
    """Org file with a Python :skill: block."""
    p = tmp_path / "skills.org"
    p.write_text(
        "* Echo skill                           :skill:\n"
        ":PROPERTIES:\n"
        ":SKILL_NAME: echo_test\n"
        ":SKILL_LANG: python\n"
        ":SKILL_MODEL: text_model\n"
        ":END:\n\n"
        "An echo skill.\n\n"
        "#+begin_src python\n"
        "print('{{input}}')\n"
        "#+end_src\n"
    )
    return p


@pytest.fixture
def shell_skill_file(tmp_path):
    """Org file with a shell :skill: block."""
    p = tmp_path / "shell_skill.org"
    p.write_text(
        "* Shell skill                          :skill:\n"
        ":PROPERTIES:\n"
        ":SKILL_NAME: shell_test\n"
        ":SKILL_LANG: sh\n"
        ":END:\n\n"
        "#+begin_src sh\n"
        "echo 'queer and free'\n"
        "#+end_src\n"
    )
    return p
# conftest.py:1 ends here
