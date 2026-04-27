"""Tests for org_llm.context — tangle parser, fact writer, stale detection."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from org_llm.db import File, Node, get_session, init_db, make_engine
from org_llm.context import (
    _parse_tangle_blocks, add_fact,
    apply_stale_tags, context_org_path, context_tangle_path,
    count_unreviewed_stale_candidates, ensure_context_file_exists,
    find_stale_candidates, read_context_for_prompt, render_context_block,
    tangle,
)


@pytest.fixture
def ctx_env(tmp_path, monkeypatch):
    """Isolate the context paths to tmp_path."""
    org_dir   = tmp_path / "org"
    org_dir.mkdir()
    monkeypatch.setenv("ORG_LLM_ORG_DIR",        str(org_dir))
    monkeypatch.setenv("ORG_LLM_CONTEXT_FILE",   str(org_dir / "ctx.org"))
    monkeypatch.setenv("ORG_LLM_CONTEXT_TANGLE", str(tmp_path / "ctx.txt"))
    monkeypatch.setenv("ORG_LLM_DB",             str(tmp_path / "ctx.db"))
    return tmp_path


# ── Tangle parser ─────────────────────────────────────────────────────────

class TestTangleParser:
    def test_extracts_explicit_target(self, tmp_path):
        text = (
            "* Heading\n"
            "#+name: foo\n"
            f"#+begin_src text :tangle {tmp_path / 'out.txt'}\n"
            "first line\nsecond line\n"
            "#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "default.txt")
        assert len(blocks) == 1
        assert blocks[0].target == tmp_path / "out.txt"
        assert "first line" in blocks[0].body
        assert blocks[0].name == "foo"

    def test_falls_back_to_default_target(self, tmp_path):
        text = (
            "#+begin_src text\n"
            "no tangle directive\n"
            "#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "default.txt")
        assert len(blocks) == 1
        assert blocks[0].target == tmp_path / "default.txt"

    def test_skips_tangle_no(self, tmp_path):
        text = (
            "#+begin_src text :tangle no\n"
            "do not tangle\n"
            "#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "default.txt")
        assert blocks == []

    def test_multiple_blocks_to_same_target_concatenate(self, tmp_path):
        target = tmp_path / "stacked.txt"
        text = (
            f"#+begin_src text :tangle {target}\nfirst\n#+end_src\n"
            f"#+begin_src text :tangle {target}\nsecond\n#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert len(blocks) == 2
        assert blocks[0].target == blocks[1].target == target

    def test_multiple_blocks_to_different_targets(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        text = (
            f"#+begin_src text :tangle {a}\nA\n#+end_src\n"
            f"#+begin_src text :tangle {b}\nB\n#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        targets = {b.target for b in blocks}
        assert targets == {a, b}

    def test_tilde_in_target_expands(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        text = "#+begin_src text :tangle ~/extracted.txt\nhi\n#+end_src\n"
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert len(blocks) == 1
        assert blocks[0].target == tmp_path / "extracted.txt"

    def test_indented_block_lines_preserved(self, tmp_path):
        # org-mode usually allows indented :tangle blocks under heading
        text = (
            f"#+begin_src text :tangle {tmp_path / 'i.txt'}\n"
            "    indented line one\n"
            "        deeper indent\n"
            "#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert len(blocks) == 1
        assert "    indented line one" in blocks[0].body
        assert "        deeper indent" in blocks[0].body

    def test_empty_block_still_parsed(self, tmp_path):
        text = f"#+begin_src text :tangle {tmp_path / 'e.txt'}\n#+end_src\n"
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert len(blocks) == 1
        assert blocks[0].body == ""

    def test_other_header_args_ignored(self, tmp_path):
        # :results, :exports, :noweb shouldn't break tangle path parsing
        target = tmp_path / "headers.txt"
        text = (
            f"#+begin_src text :results none :exports both :tangle {target} :noweb yes\n"
            "ok\n#+end_src\n"
        )
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert len(blocks) == 1
        assert blocks[0].target == target

    def test_unclosed_block_does_not_emit(self, tmp_path):
        text = f"#+begin_src text :tangle {tmp_path / 'u.txt'}\nbody never closed\n"
        blocks = _parse_tangle_blocks(text, default_target=tmp_path / "x.txt")
        assert blocks == []   # unclosed → silently skipped


class TestTangleEnd2End:
    def test_writes_target_file(self, ctx_env):
        org = context_org_path()
        tgt = context_tangle_path()
        org.write_text(
            f"* Facts\n#+begin_src text :tangle {tgt}\n"
            "I work at Idexx since 2026-04.\n#+end_src\n"
        )
        written = tangle()
        assert tgt in written
        assert "Idexx" in tgt.read_text()

    def test_extracts_llm_context_sections_when_no_tangle_blocks(self, ctx_env):
        org = context_org_path()
        org.write_text(
            "* Active facts\n"
            ":PROPERTIES:\n:LLM_CONTEXT: facts\n:END:\n\n"
            "I work at Idexx now.\n"
            "I live in Portland.\n"
        )
        written = tangle()
        tgt = context_tangle_path()
        assert tgt in written
        body = tgt.read_text()
        assert "Idexx" in body
        assert "Portland" in body


# ── Fact writer ───────────────────────────────────────────────────────────

class TestAddFact:
    def test_creates_file_from_template(self, ctx_env):
        p = context_org_path()
        assert not p.exists()
        add_fact("I work at Idexx now.")
        assert p.exists()
        assert "Idexx" in p.read_text()

    def test_appends_to_active_block(self, ctx_env):
        add_fact("First fact.")
        add_fact("Second fact.")
        body = context_org_path().read_text()
        assert "First fact." in body
        assert "Second fact." in body
        assert body.count("- First fact.") == 1   # not duplicated

    def test_logs_history(self, ctx_env):
        add_fact("Some fact.")
        body = context_org_path().read_text()
        # History contains the date
        assert "Context history" in body
        assert "Some fact." in body.split("Context history")[1]

    def test_retangles_after_add(self, ctx_env):
        add_fact("I live in Portland, Maine.")
        tgt = context_tangle_path()
        assert tgt.exists()
        assert "Portland" in tgt.read_text()


# ── Read for prompt ───────────────────────────────────────────────────────

class TestReadForPrompt:
    def test_empty_when_no_file(self, ctx_env):
        assert read_context_for_prompt() == ""
        assert render_context_block() == ""

    def test_returns_body_after_add(self, ctx_env):
        add_fact("I work at Idexx.")
        body = read_context_for_prompt()
        assert "Idexx" in body

    def test_render_block_includes_header(self, ctx_env):
        add_fact("I live in Portland.")
        block = render_context_block()
        assert "USER CONTEXT" in block
        assert "Portland" in block

    def test_truncates_long_body(self, ctx_env):
        # Write a single huge fact
        for i in range(200):
            add_fact(f"Filler fact {i} with extra padding to make it long " * 5)
        body = read_context_for_prompt(max_chars=2000)
        assert len(body) <= 2100
        assert "(truncated)" in body or len(body) < 2000


# ── Stale candidate detection ─────────────────────────────────────────────

@pytest.fixture
def db_with_unum_notes(ctx_env):
    db_path = Path(ctx_env / "ctx.db")
    engine = make_engine(db_path)
    init_db(engine)
    now = time.time()
    with get_session(engine) as s:
        f = File(path="/v/n.org", indexed_at="now",
                 node_count=5, mtime=now)
        s.add(f); s.flush()
        s.add(Node(file_id=f.id, node_id="id-unum-1",
                   title="Notes from Unum standup",
                   body="Discussed Q3 OKRs at Unum today.",
                   tags="work meeting", mtime=now))
        s.add(Node(file_id=f.id, node_id="id-unum-2",
                   title="Idea for Unum claims pipeline",
                   body="Could refactor the claims module.",
                   tags="work idea", mtime=now))
        s.add(Node(file_id=f.id, node_id="id-other",
                   title="Reading list",
                   body="Books to read this year.",
                   tags="personal", mtime=now))
        s.add(Node(file_id=f.id, node_id="id-already-stale",
                   title="Old Unum decision",
                   body="Decided X at Unum.",
                   tags="stale work", mtime=now - 10*86400))
        s.commit()
    yield engine


class TestFindStaleCandidates:
    def test_finds_keyword_in_title_and_body(self, db_with_unum_notes):
        with get_session(db_with_unum_notes) as s:
            cands = find_stale_candidates(s, ["Unum"])
        ids = {n.node_id for n, _ in cands}
        assert "id-unum-1" in ids
        assert "id-unum-2" in ids
        assert "id-other" not in ids

    def test_skips_already_stale(self, db_with_unum_notes):
        with get_session(db_with_unum_notes) as s:
            cands = find_stale_candidates(s, ["Unum"])
        ids = {n.node_id for n, _ in cands}
        assert "id-already-stale" not in ids

    def test_short_keywords_ignored(self, db_with_unum_notes):
        with get_session(db_with_unum_notes) as s:
            # 2-letter keyword should be filtered
            cands = find_stale_candidates(s, ["a"])
        assert cands == []


class TestApplyStaleTags:
    def test_adds_stale_tag(self, db_with_unum_notes):
        with get_session(db_with_unum_notes) as s:
            cands = find_stale_candidates(s, ["Unum"])
            n_updated = apply_stale_tags(s, cands, topic="employment")
        assert n_updated >= 2
        with get_session(db_with_unum_notes) as s:
            n = s.query(Node).filter_by(node_id="id-unum-1").first()
            assert "stale" in (n.tags or "").split()
            assert "re:employment" in (n.tags or "").split()

    def test_does_not_double_tag(self, db_with_unum_notes):
        with get_session(db_with_unum_notes) as s:
            cands = find_stale_candidates(s, ["Unum"])
            apply_stale_tags(s, cands, topic="employment")
        with get_session(db_with_unum_notes) as s:
            cands2 = find_stale_candidates(s, ["Unum"])
            n2 = apply_stale_tags(s, cands2, topic="employment")
        # First pass tagged everything; second should find nothing un-tagged.
        assert n2 == 0


class TestLlmJsonCall:
    """Shared LLM-JSON helper with retry-on-parse-failure."""

    def test_parses_clean_json(self, monkeypatch):
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        # Stub chat to return clean JSON
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system: '{"x": 1}')
        out = _ctx._llm_json_call("hi", "sys", model="m",
                                    base_url="http://x", timeout=2.0)
        assert out == {"x": 1}

    def test_strips_markdown_fences(self, monkeypatch):
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system:
                              '```json\n{"x": 2}\n```')
        out = _ctx._llm_json_call("hi", "sys", model="m",
                                    base_url="http://x", timeout=2.0)
        assert out == {"x": 2}

    def test_retries_on_parse_failure(self, monkeypatch):
        """First call returns prose; retry returns valid JSON."""
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        responses = iter([
            "Sure, here's the data: {x: 'unparseable'}",
            '{"x": "retry-worked"}',
        ])
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system:
                              next(responses))
        out = _ctx._llm_json_call("hi", "sys", model="m",
                                    base_url="http://x", timeout=2.0)
        assert out == {"x": "retry-worked"}

    def test_returns_none_after_two_failures(self, monkeypatch):
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda p, model, base_url, system:
                              "I refuse to output JSON")
        assert _ctx._llm_json_call("hi", "sys", model="m",
                                     base_url="http://x", timeout=2.0) is None

    def test_returns_none_on_chat_exception(self, monkeypatch):
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        def _boom(*a, **kw):
            raise ConnectionError("ollama is down")
        monkeypatch.setattr(_llm, "chat", _boom)
        assert _ctx._llm_json_call("hi", "sys", model="m",
                                     base_url="http://x", timeout=2.0) is None

    def test_no_retry_when_disabled(self, monkeypatch):
        """retry=False — first parse failure returns None immediately."""
        from org_llm import context as _ctx
        import org_llm.llm as _llm
        call_count = [0]
        def _chat(p, model, base_url, system):
            call_count[0] += 1
            return "not-json"
        monkeypatch.setattr(_llm, "chat", _chat)
        assert _ctx._llm_json_call("hi", "sys", model="m",
                                     base_url="http://x", timeout=2.0,
                                     retry=False) is None
        assert call_count[0] == 1


class TestCountUnreviewed:
    def test_zero_when_no_context(self, ctx_env, db_with_unum_notes):
        # No context file exists yet
        with get_session(db_with_unum_notes) as s:
            assert count_unreviewed_stale_candidates(s) == 0

    def test_counts_keyword_matches_after_context_add(self, ctx_env, db_with_unum_notes):
        add_fact("Works at Idexx now (formerly Unum).")
        with get_session(db_with_unum_notes) as s:
            n = count_unreviewed_stale_candidates(s)
        assert n >= 2     # both Unum notes should match
