"""Tests for org_llm.extract — verdict parsing + extraction prep."""
from __future__ import annotations

from pathlib import Path

import pytest

from org_llm import extract


_FIXTURE_VERDICT = """
* Components

*** Walk graph traversal — pre-classified ADDABLE
- *Decision:* SPLIT (2026-05-05) — track-selection logic (~140 LoC: orphan +
  recent + untagged scoring) SUBSUMED per scan; replaces with one SQL view.
- *Where it lives:* =org_llm/walk.py=
- *What it does:* Traverses link-graph between nodes for narrative walkthroughs.
- *What depends on it:* link_graph_walk recipe; specific narrative recipes.
- *Cut-criterion test:* β ships without.
- *Walkthrough notes:* track-selection follows.

*** Avatars — pre-classified ADDABLE
- *Decision:* COMMUNITY IDEA (2026-05-05) — pure aesthetic.
- *Where it lives:* =org_llm/avatars.py=
- *What it does:* Theme × metadata → image-gen pipeline.
- *What depends on it:* TUI rendering.
- *Cut-criterion test:* β ships without.

*** Bridge crew — pre-classified CORE
- *Decision:* CORE — locked agent roster.
- *Where it lives:* =org_llm/bridge_crew.py=
- *What it does:* Materializes Bridge Crew personas.
- *What depends on it:* Agor session spawn.
- *Cut-criterion test:* β requires this.

*** Insights — pre-classified ADDABLE
- *Decision:* RETIRE (2026-05-05) — fully subsumed by recipes.
- *Where it lives:* =org_llm/insights.py=
- *What it does:* Generates insight cards.
- *What depends on it:* opencode TUI plugin.
- *Cut-criterion test:* β ships without.

*** Doom intro — pre-classified ADDABLE
- *Decision:* CORE_DISABLED_BY_DEFAULT — Doom-specific.
- *Where it lives:* =org_llm/doom_introspect.py=
- *What it does:* Reads Doom config.
- *What depends on it:* @doom agent.
- *Cut-criterion test:* β ships without.
"""


@pytest.fixture
def verdict_file(tmp_path: Path) -> Path:
    p = tmp_path / "cull.org"
    p.write_text(_FIXTURE_VERDICT)
    return p


def test_parse_verdict_split(verdict_file: Path):
    v = extract.parse_verdict(verdict_file, "org_llm/walk.py")
    assert v.decision_keyword == "SPLIT"
    assert "track-selection" in v.decision_full
    assert "org_llm/walk.py" in v.where_lives
    assert "narrative walkthroughs" in v.what_does


def test_parse_verdict_match_by_basename(verdict_file: Path):
    """Matching by =walk.py= alone should work, not just full path."""
    v = extract.parse_verdict(verdict_file, "walk.py")
    assert v.decision_keyword == "SPLIT"


def test_parse_verdict_retire(verdict_file: Path):
    v = extract.parse_verdict(verdict_file, "insights.py")
    assert v.decision_keyword == "RETIRE"


def test_keep_decision_rejected(verdict_file: Path):
    with pytest.raises(extract.ExtractError) as exc:
        extract.parse_verdict(verdict_file, "org_llm/bridge_crew.py")
    assert "not eligible" in str(exc.value)


def test_community_decision_rejected(verdict_file: Path):
    with pytest.raises(extract.ExtractError) as exc:
        extract.parse_verdict(verdict_file, "org_llm/avatars.py")
    assert "not eligible" in str(exc.value)
    assert "COMMUNITY" in str(exc.value)


def test_core_disabled_rejected(verdict_file: Path):
    """CORE_DISABLED_BY_DEFAULT must NOT be confused with CORE
    (longer keyword wins) — and both are rejected."""
    with pytest.raises(extract.ExtractError) as exc:
        extract.parse_verdict(verdict_file, "doom_introspect.py")
    msg = str(exc.value)
    assert "CORE_DISABLED" in msg


def test_missing_verdict_raises(verdict_file: Path):
    with pytest.raises(extract.ExtractError) as exc:
        extract.parse_verdict(verdict_file, "org_llm/no_such_thing.py")
    assert "no verdict block found" in str(exc.value)


def test_build_prompt_includes_verdict(verdict_file: Path):
    v = extract.parse_verdict(verdict_file, "walk.py")
    prompt = extract.build_prompt(v, "def hello():\n    pass\n")
    assert "SPLIT" in prompt
    assert "def hello()" in prompt
    assert "unified diff" in prompt
    assert "track-selection" in prompt    # raw_block included


def test_classify_decision_ordering():
    """CORE_DISABLED_BY_DEFAULT must classify before plain CORE."""
    assert extract._classify_decision(
        "CORE_DISABLED_BY_DEFAULT — Doom-specific"
    ) == "CORE_DISABLED_BY_DEFAULT"
    assert extract._classify_decision(
        "SUBSUMED per scan"
    ) == "SUBSUMED"
    assert extract._classify_decision(
        "RESOLVED — entry was conflated"
    ) == "RESOLVED"
