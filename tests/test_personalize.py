"""Tests for org_llm.personalize — auto-theme generation from real content."""
from __future__ import annotations

import time

import pytest

from org_llm.db import File, Node, get_session, init_db, make_engine
from org_llm.personalize import (
    ThemeProposal, _BORING_TAGS, _normalize, _template_messages,
    detect_themes, generate_messages, proposals_to_knobs,
)


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "p.db"
    engine = make_engine(db_path)
    init_db(engine)
    now = time.time()
    with get_session(engine) as s:
        f = File(path="/v/n.org", indexed_at="now", node_count=10, mtime=now)
        s.add(f); s.flush()
        # 6 nodes tagged "synthwave" → above the (strict) deterministic
        # threshold of 5 single-word tag occurrences
        for i in range(6):
            s.add(Node(file_id=f.id, node_id=f"sw-{i}",
                        title=f"Synth riff {i}",
                        body="content",
                        tags="synthwave music",
                        mtime=now))
        # 5 nodes tagged "homelab" — also above threshold
        for i in range(5):
            s.add(Node(file_id=f.id, node_id=f"hl-{i}",
                        title=f"Homelab note {i}",
                        body="content",
                        tags="homelab",
                        mtime=now))
        # Boring + code tags should not become proposals
        s.add(Node(file_id=f.id, node_id="b-1",
                    title="todo dump", body="x",
                    tags="todo done draft",
                    mtime=now))
        for i in range(6):
            s.add(Node(file_id=f.id, node_id=f"c-{i}",
                        title=f"some code {i}", body="x",
                        tags="code code:python",
                        mtime=now))
        s.commit()
    yield db_path
    engine.dispose()


class TestNormalize:
    def test_lowercases_and_dashes(self):
        assert _normalize("My Cool Tag") == "my-cool-tag"

    def test_strips_punctuation(self):
        assert _normalize("foo!! @bar??") == "foo-bar"

    def test_handles_empty(self):
        assert _normalize("") == ""


class TestDetectThemes:
    """Tests run against the deterministic fallback (use_llm=False)."""

    def test_picks_up_real_tags(self, seeded_db):
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            proposals = detect_themes(s, use_llm=False)
        names = [p.name for p in proposals]
        assert "synthwave" in names
        assert "homelab" in names

    def test_skips_boring_tags(self, seeded_db):
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            proposals = detect_themes(s, use_llm=False)
        names = [p.name for p in proposals]
        for boring in ("todo", "done", "draft"):
            assert boring not in names

    def test_skips_code_prefix_tags(self, seeded_db):
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            proposals = detect_themes(s, use_llm=False)
        names = [p.name for p in proposals]
        for c in ("code", "code:python"):
            assert c not in names

    def test_skips_identifier_shaped_tags(self, seeded_db):
        # Add an identifier-y tag and confirm it's filtered
        from org_llm.db import File, Node
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            f = s.query(File).first()
            for i in range(8):
                s.add(Node(file_id=f.id, node_id=f"id-{i}",
                            title=f"identifier note {i}", body="x",
                            tags="bh-gh-spcs-internal-thing",
                            mtime=time.time()))
            s.commit()
            proposals = detect_themes(s, use_llm=False)
        names = [p.name for p in proposals]
        assert "bh-gh-spcs-internal-thing" not in names

    def test_max_themes_caps(self, seeded_db):
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            proposals = detect_themes(s, max_themes=1, use_llm=False)
        assert len(proposals) <= 1


class TestTemplateMessages:
    def test_returns_list_of_text_style_pairs(self):
        msgs = _template_messages("synthwave", ["Synth riff 1"])
        assert msgs and isinstance(msgs, list)
        for entry in msgs:
            assert isinstance(entry, list) and len(entry) == 2
            text, style = entry
            assert isinstance(text, str) and text
            assert isinstance(style, str) and style

    def test_includes_theme_name_in_messages(self):
        msgs = _template_messages("homelab", [])
        joined = " ".join(m[0].lower() for m in msgs)
        assert "homelab" in joined


class TestGenerateMessages:
    def test_falls_back_to_templates_without_model(self):
        p = ThemeProposal(name="kombucha", score=10,
                           sample_titles=[], source="vault-tag")
        msgs = generate_messages(p, model="", base_url="", use_llm=False)
        assert msgs and len(msgs) >= 4

    def test_no_llm_flag_disables_chat(self):
        p = ThemeProposal(name="kombucha", score=10,
                           sample_titles=[], source="vault-tag")
        # Even with model+url given, --no-llm should still return templates
        msgs = generate_messages(p, model="fake", base_url="http://localhost:0",
                                  use_llm=False)
        assert msgs and len(msgs) >= 4


class TestProposalsToKnobs:
    def test_serialises_messages(self, seeded_db):
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            proposals = detect_themes(s)
        knobs = proposals_to_knobs(proposals, use_llm=False)
        assert knobs
        for k in knobs:
            assert "name" in k
            assert "default_level" in k
            assert "messages" in k
            assert k["messages"]
            for entry in k["messages"]:
                assert isinstance(entry, list) and len(entry) == 2


class TestIdentifierFilter:
    """Spot-check `_looks_like_identifier`."""
    def test_rejects_long_underscored(self):
        from org_llm.personalize import _looks_like_identifier
        assert _looks_like_identifier("tableau_associate_architect_partner_exam")
        assert _looks_like_identifier("beanhub-sync-to-github")
        assert _looks_like_identifier("bh-gh-spcs")

    def test_accepts_evocative_themes(self):
        from org_llm.personalize import _looks_like_identifier
        for ok in ("synthwave", "homelab", "espresso", "dark-academia",
                    "solarpunk", "cottage-witch"):
            assert not _looks_like_identifier(ok), f"{ok!r} should pass"

    def test_rejects_versions_and_codes(self):
        from org_llm.personalize import _looks_like_identifier
        assert _looks_like_identifier("v1.5")
        assert _looks_like_identifier("python3")


class TestPersonalizeCommand:
    def test_dry_run_does_not_write(self, seeded_db, monkeypatch):
        from typer.testing import CliRunner
        from org_llm.cli import app
        monkeypatch.setenv("ORG_LLM_DB", str(seeded_db))
        r = CliRunner().invoke(app, ["personalize", "--no-llm"])
        assert r.exit_code == 0
        assert "Dry-run" in r.output
        # No knobs should be persisted
        from org_llm.db import Config
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            assert s.get(Config, "user_theme_knobs") is None

    def test_apply_writes_knobs(self, seeded_db, monkeypatch):
        from typer.testing import CliRunner
        from org_llm.cli import app
        monkeypatch.setenv("ORG_LLM_DB", str(seeded_db))
        r = CliRunner().invoke(app, ["personalize", "--apply", "--no-llm"])
        assert r.exit_code == 0, r.output
        from org_llm.db import Config
        import json as _json
        engine = make_engine(seeded_db)
        with get_session(engine) as s:
            row = s.get(Config, "user_theme_knobs")
        assert row and row.value
        knobs = _json.loads(row.value)
        names = {k["name"] for k in knobs}
        assert "synthwave" in names or "homelab" in names
