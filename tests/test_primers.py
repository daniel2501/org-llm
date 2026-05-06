"""Tests for org_llm.primers — Phase 24.5 orientation primer registry.

The primers are deterministic resolvers that read authoritative
source files at call time (always-fresh rule from
docs/wiki/lazy-loading.org). These tests pin three properties
per primer: the namespace is registered, the output is non-empty
and mentions its source path, and the primer round-trips through
the public ``primer(name)`` entry-point.
"""

from __future__ import annotations

import pytest

from org_llm.primers import (
    _REGISTRY,
    list_namespaces,
    manifest,
    primer,
)


_EXPECTED = {
    "wiki-authorship": ("docs/wiki/wiki-conventions.org", "Rule 2b"),
    "agent-report":    ("docs/templates/agent-report.org", "Objective"),
    "dev-tracker-entry": ("docs/wiki/dev-tracker.org", ":PRIORITY:"),
    "new-agent":       ("docs/wiki/agentsmith.org", "seven design steps"),
}


def test_list_namespaces_matches_registry():
    assert list_namespaces() == sorted(_REGISTRY)
    assert set(list_namespaces()) == set(_EXPECTED)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_primer_returns_non_empty_string(name):
    out = primer(name)
    assert isinstance(out, str)
    assert len(out) > 200, "primer should be substantive, not a one-liner"


@pytest.mark.parametrize("name,expected", sorted(_EXPECTED.items()))
def test_primer_mentions_source_and_landmark(name, expected):
    source, landmark = expected
    out = primer(name)
    assert source in out, f"primer({name!r}) must name its source file {source}"
    assert landmark in out, (
        f"primer({name!r}) must include landmark text {landmark!r} "
        f"from its source"
    )


def test_primer_unknown_namespace_lists_valid_names():
    with pytest.raises(KeyError) as exc:
        primer("does-not-exist")
    msg = str(exc.value)
    for ns in list_namespaces():
        assert ns in msg, f"error message should list valid namespace {ns}"


def test_manifest_is_short_and_names_each_steward():
    out = manifest()
    line_count = len(out.splitlines())
    assert line_count <= 15, (
        f"manifest is {line_count} lines; contract caps it at 15"
    )
    for steward in ("@atoz", "@riker", "@agentsmith"):
        assert steward in out, f"manifest must name {steward}"
    for namespace in list_namespaces():
        assert namespace in out, (
            f"manifest must mention each registered namespace; missing {namespace}"
        )


def test_primer_is_fresh_on_each_call(tmp_path, monkeypatch):
    """The always-fresh rule: a source-file edit propagates to the
    next primer() call without any cache invalidation step."""
    from org_llm.primers import _repo, agent_report

    fake_root = tmp_path
    template_dir = fake_root / "docs" / "templates"
    template_dir.mkdir(parents=True)
    template = template_dir / "agent-report.org"

    template.write_text("FIRST VERSION\n", encoding="utf-8")
    monkeypatch.setattr(_repo, "_REPO_ROOT", fake_root)
    out1 = agent_report.render()
    assert "FIRST VERSION" in out1

    template.write_text("SECOND VERSION\n", encoding="utf-8")
    out2 = agent_report.render()
    assert "SECOND VERSION" in out2
    assert "FIRST VERSION" not in out2
