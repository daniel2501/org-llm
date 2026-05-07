"""@sidecar v0 — capture path + extension persona registration.

Per docs/wiki/sidecar-agent-design.org: the headline contract is
sub-500ms silent capture during deep work, with enrichment
deferred. These tests pin that contract, the file shape, and the
register/dispatch path through the agents registry.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib  import Path

import pytest


# ── 1. latency budget ───────────────────────────────────────────────
def test_capture_completes_well_under_latency_budget(tmp_path):
    """Headline contract: capture finishes in <500ms wall clock.

    Measured at the API boundary — what the Emacs / proxy caller
    sees. We assert on the actual elapsed time, not just the
    config value, because the slow-path notice is non-fatal and
    we want a real budget breach to fail the test.
    """
    from org_llm.sidecar import SidecarConfig, capture
    cfg = SidecarConfig(output_file=tmp_path / "parks.org")
    start = time.perf_counter()
    eid = capture("revisit the proxy log format", config=cfg)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert eid and len(eid) == 36           # uuid4 string shape
    assert elapsed_ms < 500, (
        f"capture exceeded latency budget: {elapsed_ms:.1f}ms"
    )


# ── 2. output-file write shape ──────────────────────────────────────
def test_capture_writes_minimal_org_entry(tmp_path):
    """The on-disk shape is the contract @journalist / @boothby /
    @riker downstream readers parse. Pin it: heading + ID +
    CREATED + STATUS + body."""
    from org_llm.sidecar import SidecarConfig, capture
    out = tmp_path / "parks.org"
    cfg = SidecarConfig(output_file=out)
    eid = capture("the A/B harness should run on PR-open",
                   context_hint="active project: org-llm",
                   config=cfg)

    text = out.read_text(encoding="utf-8")
    # File preamble lands once.
    assert "#+TITLE: Sidecar parks" in text
    # Heading + tags.
    assert ":sidecar:park:" in text
    # Body content present verbatim.
    assert "the A/B harness should run on PR-open" in text
    # Property drawer carries the returned ID.
    assert f":ID:       {eid}" in text
    # Context hint stored on the entry.
    assert "active project: org-llm" in text
    # Status is parked (entry not yet reviewed).
    assert ":STATUS:   parked" in text


# ── 3. enrichment-deferral inline fallback ──────────────────────────
def test_enrich_async_falls_back_to_inline(tmp_path):
    """delegate.fork is filed-but-unbuilt and Agor isn't reachable
    in CI. Enrichment must still apply some best-effort tags via
    the inline fallback rather than silently doing nothing."""
    from org_llm.sidecar import SidecarConfig, capture, enrich_async
    out = tmp_path / "parks.org"
    cfg = SidecarConfig(output_file=out, enrich_mode="inline")
    eid = capture(
        "thought: the org-llm proxy log format is hard to grep",
        config=cfg)

    t = enrich_async(eid, config=cfg)
    t.join(timeout=2.0)
    assert not t.is_alive(), "enrichment thread did not complete"

    text = out.read_text(encoding="utf-8")
    # Inline tagger should have spotted "org-llm" → :org-llm: tag.
    assert ":org-llm:" in text, (
        f"inline enrichment did not splice :org-llm:; file:\n{text}"
    )


# ── 4. private-mode suppresses context-tagging ──────────────────────
def test_private_mode_suppresses_context_hint_and_inline_tags(tmp_path):
    """Per the design doc's privacy section, :private: is
    load-bearing: no project tags, no context_hint serialization,
    no inferred tags from inline enrichment."""
    from org_llm.sidecar import SidecarConfig, capture, enrich_async
    out = tmp_path / "parks.org"
    cfg = SidecarConfig(output_file=out,
                        enrich_mode="inline",
                        private=True)
    eid = capture("feeling I should leave the relationship — call mom",
                   context_hint="active project: org-llm",
                   config=cfg)
    t = enrich_async(eid, config=cfg)
    t.join(timeout=2.0)

    text = out.read_text(encoding="utf-8")
    assert ":private:" in text
    # context_hint must NOT have been serialized.
    assert "active project: org-llm" not in text
    # Inline enrichment must NOT have added :people: / :org-llm:.
    assert ":people:"  not in text
    assert ":org-llm:" not in text


# ── 5. registers + dispatches via agents registry ───────────────────
def test_sidecar_persona_registered_in_agents_registry():
    """@sidecar lands as an extension persona via _builtins.py.
    Resolves through both the canonical birth_name (`morn`) and
    the alias `sidecar`; pack = "extension"; not part of Bridge
    Crew."""
    from org_llm.agents import get_builtins, resolve_alias
    agents = {a.birth_name: a for a in get_builtins()}

    assert "morn" in agents, "extension persona @morn missing"
    morn = agents["morn"]
    assert "sidecar" in morn.aliases
    assert morn.pack == "extension"
    # Persona text and description came from the sidecar module.
    assert "silent stenographer" in morn.persona
    assert "side-thought" in morn.description.lower()
    # Trigger list is non-empty and includes the canonical
    # 'park:' anchor.
    assert "park:" in morn.triggers

    # Both names route to canonical birth_name `morn`.
    assert resolve_alias("@sidecar".lstrip("@")) == "morn"
    assert resolve_alias("morn") == "morn"
    assert resolve_alias("sidecar") == "morn"


# ── 6. registration is purely additive — Bridge Crew untouched ──────
def test_sidecar_registration_does_not_perturb_bridge_crew():
    """Hard isolation: existing 7 personas keep their birth-names,
    aliases, packs, and capabilities. Catches accidental
    collisions on the @-prefix seam."""
    from org_llm.agents import get_builtins
    by_name = {a.birth_name: a for a in get_builtins()}
    bridge_crew = ("picard", "spock", "data", "boothby",
                    "geordi", "atoz", "riker")
    for name in bridge_crew:
        assert name in by_name, f"Bridge Crew agent {name} missing"
        assert by_name[name].pack == "starfleet-core", (
            f"{name} pack drifted from starfleet-core"
        )
    # @sidecar must not have hijacked any existing alias.
    aliases = {alias for a in by_name.values() for alias in a.aliases}
    morn = by_name["morn"]
    # Morn's only alias is 'sidecar'; nothing else.
    assert set(morn.aliases) == {"sidecar"}
    # sidecar/morn are not aliases for any other agent.
    for a in by_name.values():
        if a.birth_name == "morn":
            continue
        assert "morn"    not in a.aliases
        assert "sidecar" not in a.aliases


# ── 7. extract_park_text trigger heuristics ─────────────────────────
def test_extract_park_text_anchors_to_start_and_supports_trailing(tmp_path):
    """Anti-trigger contract: mid-sentence 'park:' is NOT a
    capture; only start-of-message / start-of-line. Trailing
    `(park)` / `(side)` markers ARE captures (text precedes the
    marker)."""
    from org_llm.sidecar import extract_park_text

    # Canonical leading triggers.
    assert extract_park_text("park: revisit Y") == "revisit Y"
    assert extract_park_text("side: A/B harness on PR-open") \
        == "A/B harness on PR-open"
    assert extract_park_text("remember: that paper Gwern linked") \
        == "that paper Gwern linked"

    # Trailing-marker shape.
    assert extract_park_text("we should refactor auth (side)") \
        == "we should refactor auth"
    assert extract_park_text("try Forgejo for git remote (park)") \
        == "try Forgejo for git remote"

    # Mid-sentence trigger must NOT fire (anti-trigger anchor).
    assert extract_park_text("can you remember: where the keys are") is None
    assert extract_park_text("let's park: the X discussion later") is None
    # Empty / non-matching strings.
    assert extract_park_text("") is None
    assert extract_park_text("hello world") is None


# ── 8. capture refuses empty thoughts ───────────────────────────────
def test_capture_refuses_empty_thought(tmp_path):
    """API-boundary guard: empty park-text would just clutter the
    file. Surface as ValueError so the persona/proxy layer has a
    clear failure mode."""
    from org_llm.sidecar import SidecarConfig, capture
    cfg = SidecarConfig(output_file=tmp_path / "parks.org")
    with pytest.raises(ValueError):
        capture("   ", config=cfg)
    with pytest.raises(ValueError):
        capture("", config=cfg)
