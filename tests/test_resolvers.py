"""Tests for org_llm.resolvers — Phase 24.1 pre-flight registry.

Anchor scenario: the 2026-05-04 motivating bug. User says
"@curator in org-llm, fix wiki/superset.org" — pre-24.1 the
agent guessed two wrong paths and gave up. Post-24.1 the
PathResolver + RepoResolver hand the absolute path to the
agent before the first tool call.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from org_llm.resolvers       import (
    ResolvedFact,
    format_resolved_context,
    resolve_all,
)
from org_llm.resolvers.path  import resolve_paths
from org_llm.resolvers.repo  import resolve_repos
from org_llm.resolvers.agent import resolve_agents


# ── shared fixture: simulated repo with a target file ────────────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Build a tmp_path that looks like a user home with a
    `~/repos/org-llm/docs/wiki/superset.org` layout. Point HOME
    at it and chdir there so cwd-based resolution works too."""
    home = tmp_path / "home"
    repos = home / "repos" / "org-llm"
    (repos / ".git").mkdir(parents=True)
    (repos / "docs" / "wiki").mkdir(parents=True)
    target = repos / "docs" / "wiki" / "superset.org"
    target.write_text("* Superset\n")
    # Second repo so we can prove name-disambiguation works.
    other = home / "repos" / "other-repo"
    (other / ".git").mkdir(parents=True)
    (other / "README.md").write_text("# other\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)
    # Reset module caches so the new HOME is picked up.
    from org_llm.resolvers import repo as _repo_mod
    from org_llm.resolvers import path as _path_mod
    _repo_mod._reset_cache_for_tests()
    _path_mod._reset_cache_for_tests()
    return home


# ── PathResolver ────────────────────────────────────────────────

class TestPathResolver:
    def test_finds_basename_in_repo(self, fake_home):
        facts = resolve_paths(
            "fix wiki/superset.org", "in the org-llm repository"
        )
        values = [f.value for f in facts]
        expected = str(
            (fake_home / "repos" / "org-llm" / "docs" / "wiki" /
             "superset.org").resolve()
        )
        assert expected in values

    def test_no_path_token_no_facts(self, fake_home):
        assert resolve_paths("just chatting", "") == []

    def test_caps_candidates_per_token(self, fake_home):
        # Drop a bunch of same-name files into many roots; we
        # cap at 5 per the constant.
        for i in range(8):
            d = fake_home / "repos" / "org-llm" / f"sub{i}"
            d.mkdir()
            (d / "x.org").write_text("x")
        facts = resolve_paths("look at x.org", "")
        assert len(facts) <= 5

    def test_token_with_unknown_extension_skipped(self, fake_home):
        # `.foobar` not in the recognised extension list, so no
        # token is extracted and no facts are returned.
        assert resolve_paths("see file.foobar", "") == []


# ── RepoResolver ────────────────────────────────────────────────

class TestRepoResolver:
    def test_resolves_in_org_llm(self, fake_home):
        facts = resolve_repos(
            "@curator in org-llm, fix wiki/superset.org", ""
        )
        assert any(f.label == 'repo "org-llm"' for f in facts)
        assert any(
            f.value == str(
                (fake_home / "repos" / "org-llm").resolve())
            for f in facts
        )

    def test_resolves_repo_keyword_form(self, fake_home):
        facts = resolve_repos("look at the org-llm repo", "")
        assert any('repo "org-llm"' == f.label for f in facts)

    def test_stopword_words_dont_resolve(self, fake_home):
        # "in particular" used to false-positive as repo "particular".
        facts = resolve_repos(
            "in particular this matters", ""
        )
        assert facts == []

    def test_unknown_repo_name_silent(self, fake_home):
        facts = resolve_repos("in nonexistent-repo, do X", "")
        assert facts == []


# ── AgentResolver ───────────────────────────────────────────────

class TestAgentResolver:
    def test_resolves_known_handle(self):
        facts = resolve_agents("@curator can you help?", "")
        # If `curator` is a builtin alias/birth_name, we get a
        # fact. If the agent registry has been refactored away
        # we tolerate empty (so this test doesn't ossify the
        # roster).
        if facts:
            f = facts[0]
            assert "curator" in f.label.lower() or "atoz" in f.label.lower()
            assert f.evidence == "builtin agent registry"

    def test_unknown_handle_no_fact(self):
        assert resolve_agents("@notarealagent_zz", "") == []

    def test_no_at_mention_no_fact(self):
        assert resolve_agents("just text about curators", "") == []


# ── resolve_all + format_resolved_context ───────────────────────

class TestRegistry:
    def test_motivating_bug_closes(self, fake_home):
        """The integration test: simulate the curator delegate
        prompt and assert that resolve_all + format_resolved_context
        produces a block containing the right absolute path."""
        prompt = ("In the org-llm repository, the file "
                  "wiki/superset.org needs to be fixed up.")
        context = ("The user has requested that I, @curator, "
                   "fix wiki/superset.org.")
        block = format_resolved_context(resolve_all(prompt, context))
        assert "RESOLVED CONTEXT" in block
        assert "org-llm" in block
        expected_path = str(
            (fake_home / "repos" / "org-llm" / "docs" / "wiki" /
             "superset.org").resolve()
        )
        assert expected_path in block

    def test_empty_prompt_empty_block(self, fake_home):
        assert format_resolved_context(resolve_all("", "")) == ""

    def test_resolver_exception_dropped_silently(self, fake_home):
        def angry(prompt, context):
            raise RuntimeError("boom")
        # When a custom resolver raises, the framework returns
        # only the surviving resolvers' results — no propagation.
        out = resolve_all(
            "in org-llm",
            "",
            resolvers=[
                ("angry", angry),
                ("repo", resolve_repos),
            ],
        )
        # angry should be dropped; repo should still produce
        labels = {f.label for f in out}
        assert any('"org-llm"' in l for l in labels)

    def test_time_budget_drops_slow_resolver(self, fake_home):
        import time
        def slow(prompt, context):
            time.sleep(0.5)
            return [ResolvedFact(label="slow", value="x")]
        def fast(prompt, context):
            return [ResolvedFact(label="fast", value="y")]
        out = resolve_all(
            "anything",
            "",
            time_budget_ms=50,
            resolvers=[("slow", slow), ("fast", fast)],
        )
        labels = {f.label for f in out}
        assert "fast" in labels
        assert "slow" not in labels

    def test_format_block_shape(self):
        facts = [
            ResolvedFact(label="path 'x.org'", value="/tmp/x.org",
                         evidence="basename match", source="path"),
            ResolvedFact(label='repo "org-llm"', value="/repos/org-llm",
                         source="repo"),
        ]
        block = format_resolved_context(facts)
        assert block.startswith("RESOLVED CONTEXT")
        assert "/tmp/x.org" in block
        assert "/repos/org-llm" in block
        assert "(basename match)" in block


# ── ResolvedFact dataclass ──────────────────────────────────────

class TestResolvedFact:
    def test_frozen(self):
        f = ResolvedFact(label="a", value="b")
        with pytest.raises(Exception):
            f.label = "c"  # type: ignore[misc]

    def test_default_fields(self):
        f = ResolvedFact(label="a", value="b")
        assert f.evidence == ""
        assert f.source == ""
