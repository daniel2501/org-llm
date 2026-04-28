"""Tests for org_llm.theme_studio — LLM-driven theming with quality gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_llm import theme_studio as ts


# ── gate ──────────────────────────────────────────────────────────────────────

class TestGate:
    """Quality gate that decides which LLM-generated variants make it
    into the cache. The gate is the trust boundary — if it accepts garbage
    we render garbage, if it rejects everything good we never theme."""

    def _surf(self, **kw):
        defaults = dict(key="t", description="x",
                          len_min=8, len_max=80, n_variants=4, default="d")
        defaults.update(kw)
        return ts.Surface(**defaults)

    def test_accepts_clean_variant(self):
        g = ts.gate("Hailing frequencies open. Org-llm operator standing by.",
                     self._surf(len_min=20, len_max=80),
                     keyword_pool=["hailing", "warp", "stardate"])
        assert g.accepted, g.reason

    def test_rejects_too_short(self):
        g = ts.gate("hi", self._surf(len_min=10), keyword_pool=[])
        assert not g.accepted
        assert "too short" in g.reason

    def test_rejects_too_long(self):
        g = ts.gate("x" * 200, self._surf(len_max=80), keyword_pool=[])
        assert not g.accepted
        assert "too long" in g.reason

    def test_rejects_forbidden_phrase(self):
        g = ts.gate("As an AI language model I cannot help with that.",
                     self._surf(len_min=8, len_max=120), keyword_pool=[])
        assert not g.accepted
        assert "forbidden" in g.reason

    def test_rejects_newline_in_single_line_surface(self):
        g = ts.gate("first line\nsecond line",
                     self._surf(len_min=8, len_max=80), keyword_pool=[])
        assert not g.accepted
        assert "newline" in g.reason

    def test_rejects_unbalanced_markup(self):
        g = ts.gate("[bold lcars1]half-tagged but never closed",
                     self._surf(len_min=8, len_max=80), keyword_pool=[])
        assert not g.accepted
        assert "markup" in g.reason

    def test_accepts_balanced_markup(self):
        g = ts.gate("[bold]Engage warp drive[/bold] now",
                     self._surf(len_min=8, len_max=80), keyword_pool=[])
        assert g.accepted, g.reason

    def test_rejects_no_theme_keyword_when_pool_active(self):
        """Active dial means the variant MUST mention at least one
        theme-pool word. A generic 'your second brain' line passes
        length but should fail the keyword gate when trek is up."""
        g = ts.gate("your second brain, scripted",
                     self._surf(len_min=8, len_max=80),
                     keyword_pool=["warp", "stardate", "federation"])
        assert not g.accepted
        assert "no theme keyword" in g.reason

    def test_accepts_when_pool_empty_neutral_mode(self):
        """At all-neutral dials there's no keyword pool, so a generic
        line should pass."""
        g = ts.gate("your second brain, scripted",
                     self._surf(len_min=8, len_max=80), keyword_pool=[])
        assert g.accepted, g.reason


# ── keyword pool ──────────────────────────────────────────────────────────────

class TestKeywordPool:
    """`_active_keyword_pool` defers to the pluggable knob registry —
    no theme name is hardcoded here or in theme_studio. The registry
    knows what 'trek'/'commie'/'queer' mean by way of the BUILTIN_KNOBS
    seed (knobs.py). Adding a new knob in config makes it participate
    automatically — see TestPluggability below."""

    def test_neutral_levels_yield_empty_pool(self):
        pool = ts._active_keyword_pool({"trek": 0, "commie": 0, "queer": 0})
        assert pool == []

    def test_high_trek_includes_federation_words(self):
        pool = ts._active_keyword_pool({"trek": 3, "commie": 0, "queer": 0})
        joined = " ".join(pool).lower()
        # Cumulative pool — level 3 should INCLUDE level 1+2 words too,
        # plus the level-3 specific ones.
        assert "warp" in joined        # level 1 carries up
        assert "engage" in joined      # level 2 carries up
        assert "federation" in joined or "make it so" in joined  # level 3

    def test_high_queer_includes_queer_words(self):
        pool = ts._active_keyword_pool({"trek": 0, "commie": 0, "queer": 3})
        assert any("queer" in w.lower() for w in pool), pool

    def test_combined_levels_combine_pools(self):
        pool = ts._active_keyword_pool({"trek": 2, "commie": 2, "queer": 0})
        joined = " ".join(pool).lower()
        assert "warp" in joined or "lcars" in joined
        assert "mutual aid" in joined or "workers" in joined


class TestPluggability:
    """The whole point of moving knobs to a registry: a user-defined
    knob with its own keyword pool participates in theme_studio's
    quality gate without ANY code change to theme_studio."""

    def test_user_added_knob_extends_keyword_pool(self, monkeypatch):
        """Pretend a user added a 'pirates' knob with a few keywords;
        when we dial it up the pool should include them."""
        from org_llm.knobs import KnobDef, BUILTIN_KNOBS
        pirate = KnobDef(
            name="pirates",
            description="Pirate Radio voice — yarr, hearties, treasure",
            default_level=2,
            keywords_by_level={
                1: ["arr", "matey"],
                2: ["pirate", "ship"],
                3: ["yarr", "treasure", "hearties", "doubloons"],
            },
        )
        monkeypatch.setattr(
            "org_llm.knobs.load_knobs",
            lambda session=None: list(BUILTIN_KNOBS) + [pirate],
        )
        # Dial pirates up, others off
        pool = ts._active_keyword_pool({"trek": 0, "commie": 0,
                                          "queer": 0, "pirates": 3})
        assert "doubloons" in pool
        assert "matey" in pool          # level 1 carries up
        # Trek words should NOT be present — only pirates is dialed up
        assert not any("warp" in w.lower() for w in pool), pool

    def test_user_can_override_builtin_via_db(self, monkeypatch):
        """If the user redefines 'trek' in config, their pool wins."""
        from org_llm.knobs import KnobDef, BUILTIN_KNOBS
        # Replace 'trek' entirely with a user-customised version
        custom_trek = KnobDef(
            name="trek",
            description="Custom trek pool — only TNG-era refs",
            default_level=2,
            keywords_by_level={3: ["picard", "earl grey", "tng"]},
        )
        merged = [custom_trek] + [k for k in BUILTIN_KNOBS if k.name != "trek"]
        monkeypatch.setattr(
            "org_llm.knobs.load_knobs",
            lambda session=None: merged,
        )
        pool = ts._active_keyword_pool({"trek": 3, "commie": 0, "queer": 0})
        joined = " ".join(pool).lower()
        assert "picard" in joined
        # Built-in trek words should be absent — they were replaced
        assert "lcars" not in joined


# ── get_themed: cache hit/miss + fallback ─────────────────────────────────────

class TestGetThemed:
    def test_returns_default_when_cache_missing(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        out = ts.get_themed("splash_subtitle", "FALLBACK")
        assert out == "FALLBACK"

    def test_returns_surface_default_when_no_default_passed(self, tmp_path,
                                                               monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        out = ts.get_themed("splash_subtitle")
        assert out == "your second brain, scripted"  # registered default

    def test_returns_default_for_unknown_surface(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        assert ts.get_themed("does-not-exist", "X") == "X"

    def test_returns_cached_variant_when_cache_warm(self, tmp_path,
                                                       monkeypatch):
        cache_file = tmp_path / "cache.json"
        # Match whatever signature _theme_levels() produces — write
        # under EVERY plausible key so the test isn't coupled to the
        # current dial state of the dev machine.
        monkeypatch.setattr(ts, "_CACHE_PATH", cache_file)
        from org_llm.ui import _theme_levels
        sig = ts._levels_signature(_theme_levels())
        cache_file.write_text(json.dumps({
            sig: {
                "splash_subtitle": {
                    "variants": ["TESTED VARIANT A", "TESTED VARIANT B"],
                }
            }
        }))
        out = ts.get_themed("splash_subtitle", "fallback-shouldnt-render")
        assert out in ("TESTED VARIANT A", "TESTED VARIANT B")


# ── regenerate end-to-end with stubbed LLM ────────────────────────────────────

class TestRegenerate:
    def test_writes_accepted_variants_to_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")

        # Pretend trek is fully dialed up so the keyword gate enforces
        # Trek refs, AND have the stubbed LLM return Trek-flavored lines
        # that pass length + keyword gates.
        from org_llm import ui as _ui
        monkeypatch.setattr(_ui, "_theme_levels",
                              lambda: {"trek": 3, "commie": 0, "queer": 0})

        themed_response = (
            "Make it so — your warp-driven second brain.\n"
            "Engage subspace memory. Stardate ready.\n"
            "Tea Earl Grey. Notes hot, fresh, federation-ready.\n"
            "Captain on the bridge of your own knowledge graph.\n"
            "Number One, prepare the holodeck of recall.\n"
            "Set course for stardate clarity, warp 9.\n"
        )
        from org_llm import llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda u, model, base_url, system, timeout=None:
                              themed_response)

        report = ts.regenerate(model="fast", base_url="http://nope",
                                  only=["splash_subtitle"])
        assert "splash_subtitle" in report
        assert report["splash_subtitle"]["accepted"] >= 2

        # Cache file written and contains our themed variants
        data = json.loads((tmp_path / "cache.json").read_text())
        sig = ts._levels_signature({"trek": 3, "commie": 0, "queer": 0})
        variants = data[sig]["splash_subtitle"]["variants"]
        assert len(variants) >= 2
        # Every accepted variant mentions at least one Trek pool word
        joined = " ".join(variants).lower()
        assert any(kw.lower() in joined for kw in
                    ["warp", "stardate", "federation", "make it so",
                     "captain", "subspace", "earl grey", "holodeck",
                     "number one", "engage"])

    def test_rejects_off_brand_at_high_dial(self, tmp_path, monkeypatch):
        """With trek_level=3, plain corporate copy must be rejected by
        the gate — proves the LLM is held to a quality bar, not just
        accepted blindly."""
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        from org_llm import ui as _ui
        monkeypatch.setattr(_ui, "_theme_levels",
                              lambda: {"trek": 3, "commie": 0, "queer": 0})

        plain_response = (
            "your second brain, scripted\n"
            "the productivity tool you needed\n"
            "ai-powered note-taking for everyone\n"
            "smart, simple, on your terms\n"
        )
        from org_llm import llm as _llm
        monkeypatch.setattr(_llm, "chat",
                              lambda u, model, base_url, system, timeout=None:
                              plain_response)

        report = ts.regenerate(model="fast", base_url="http://nope",
                                  only=["splash_subtitle"])
        # NONE of the plain lines mention warp/stardate/etc → all rejected
        assert report["splash_subtitle"]["accepted"] == 0


# ── verify ────────────────────────────────────────────────────────────────────

class TestOpencodeWiring:
    """The opencode workspace prompt + MCP themed output now pull from
    theme_studio. These tests pin that wiring so we don't lose it in a
    refactor — call sites must call get_themed for the registered keys."""

    def test_workspace_prompt_includes_themed_greeting_default(self,
                                                                  tmp_path,
                                                                  monkeypatch):
        """When the cache is cold the workspace prompt should still
        contain the splash_subtitle/opencode_greeting DEFAULTS — not
        nothing, not raw markup."""
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        from org_llm.cli import _opencode_workspace_prompt
        prompt = _opencode_workspace_prompt(
            workspace="all",
            n_files=1, n_nodes=1, n_embedded=1, pct_e=100,
            org_dir="/tmp/org", skill_str="", recent_str="",
            top_tags_str="", model_status="", discover_str="",
            knobs_str="", hardware_str="",
        )
        # Default greeting from the surface registry must appear when
        # cache is empty
        assert "Hailing frequencies" in prompt or \
                "org-llm operator" in prompt.lower() or \
                "operating inside opencode" in prompt
        # And the proactive-doctor themed leading line
        assert "proactive_doctor" in prompt or \
                "drifting" in prompt

    def test_themed_appends_success_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        from org_llm.mcp_server import _themed
        out = _themed("search_notes", "found 3 hits",
                       body="node1\nnode2\nnode3", outcome="ok")
        assert "↳ done." in out                  # default success suffix
        assert "search_notes" in out

    def test_themed_appends_error_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        from org_llm.mcp_server import _themed
        out = _themed("search_notes", "ollama unreachable",
                       outcome="error")
        assert "↳ red alert" in out

    def test_themed_uses_cached_variant_when_warm(self, tmp_path,
                                                     monkeypatch):
        """Once theme_studio's cache has a themed suffix, _themed picks
        it up — proves the wiring goes through get_themed, not
        hardcoded."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(ts, "_CACHE_PATH", cache_file)
        from org_llm.ui import _theme_levels
        sig = ts._levels_signature(_theme_levels())
        cache_file.write_text(json.dumps({
            sig: {
                "mcp_tool_success_suffix": {
                    "variants": ["↳ make it so"],
                }
            }
        }))
        from org_llm.mcp_server import _themed
        out = _themed("x", "y", outcome="ok")
        assert "make it so" in out


class TestCrossReferences:
    """`theme_cross_references_level` controls how aggressively the
    LLM hunts for overlaps between dialed-up knobs (Worf's labor
    solidarity, queer joy in the holodeck, …). Levels 0-3."""

    def test_default_level_is_2_normal(self, monkeypatch):
        """Cold-cache / no env / no DB row → level 2 (normal)."""
        monkeypatch.delenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL",
                            raising=False)
        # Ensure no DB at the override path
        from pathlib import Path
        monkeypatch.setenv("ORG_LLM_DB", str(Path("/tmp/no-such-db.sqlite")))
        assert ts._read_cross_ref_level() == 2

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "0")
        assert ts._read_cross_ref_level() == 0
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "3")
        assert ts._read_cross_ref_level() == 3

    def test_env_clamps_out_of_range(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "99")
        assert ts._read_cross_ref_level() == 3
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "-5")
        assert ts._read_cross_ref_level() == 0

    def test_higher_level_picks_stronger_guidance(self):
        """The system-prompt builder swaps in different cross-ref
        guidance per level — pin the contract so a future refactor
        can't drop the intensity gradient."""
        gen0 = ts._gen_system_for_level(0)
        gen3 = ts._gen_system_for_level(3)
        # Level 0 says "OFF"; level 3 says "MAX" / "every variant"
        assert "OFF" in gen0 and "do not mix" in gen0.lower()
        assert "MAX" in gen3 and "every variant" in gen3.lower()
        # Both share the same base rules
        for shared in ("Honor the active theme dials",
                        "Stay on-task"):
            assert shared in gen0 and shared in gen3

    def test_user_msg_includes_cross_hint_when_2plus_knobs_active(
            self, monkeypatch):
        """When 2+ knobs are active AND cross-ref level >= 2, the
        per-call user message gains a CROSS-REFERENCE hint listing
        the active knobs. Helps small models stay on task."""
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "2")
        s = ts.SURFACES[0]   # use the first registered surface
        user_msg, _sys = ts._gen_prompt(s, {"trek": 2, "commie": 3,
                                                "queer": 0})
        assert "CROSS-REFERENCE" in user_msg
        assert "trek" in user_msg and "commie" in user_msg

    def test_user_msg_skips_cross_hint_at_level_below_2(
            self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_THEME_CROSS_REFERENCES_LEVEL", "1")
        s = ts.SURFACES[0]
        user_msg, _sys = ts._gen_prompt(s, {"trek": 2, "commie": 3})
        assert "CROSS-REFERENCE" not in user_msg


class TestVerify:
    def test_reports_per_variant_pass_fail(self, tmp_path, monkeypatch):
        """First variant has 'engage' (level 2) and 'warp' (level 1) —
        both in the cumulative pool at trek=3, so it passes. Second is
        plain corporate copy with zero theme refs — fails the gate."""
        monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "cache.json")
        sig = "commie=0,queer=0,trek=3"
        (tmp_path / "cache.json").write_text(json.dumps({
            sig: {
                "splash_subtitle": {
                    "variants": [
                        "Engage warp — second brain online",   # passes
                        "your second brain, scripted",         # fails (no kw)
                    ]
                }
            }
        }))
        rep = ts.verify(only=["splash_subtitle"])
        assert sig in rep
        results = rep[sig]["splash_subtitle"]["results"]
        assert len(results) == 2
        assert results[0]["ok"] is True, results[0]
        assert results[1]["ok"] is False
        assert "no theme keyword" in results[1]["reason"]
