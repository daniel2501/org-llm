"""Tests for org_llm.knobs — pluggable theme-knob registry.

The whole point of this module is that adding a new dial is a
**config change** — never a code change. These tests pin that
contract so future refactors don't accidentally re-couple theme
names to call sites."""
from __future__ import annotations

import json

from org_llm import knobs as K


# ── KnobDef shape + serialisation ─────────────────────────────────────────────

class TestKnobDef:
    def test_round_trip_dict(self):
        original = K.KnobDef(
            name="dinosaur",
            description="Dinosaur references",
            default_level=2,
            keywords_by_level={1: ["raptor"], 2: ["t-rex"], 3: ["jurassic"]},
        )
        roundtrip = K.KnobDef.from_dict(original.to_dict())
        assert roundtrip.name == "dinosaur"
        assert roundtrip.description == "Dinosaur references"
        assert roundtrip.default_level == 2
        # int keys preserved across JSON's str-key boundary
        assert roundtrip.keywords_by_level == {1: ["raptor"],
                                                  2: ["t-rex"],
                                                  3: ["jurassic"]}

    def test_from_dict_skips_invalid_levels(self):
        raw = {
            "name": "x", "description": "",
            "keywords_by_level": {"1": ["ok"], "garbage": ["nope"]},
        }
        knob = K.KnobDef.from_dict(raw)
        assert 1 in knob.keywords_by_level
        assert "garbage" not in knob.keywords_by_level

    def test_cumulative_keywords_includes_all_lower_levels(self):
        knob = K.KnobDef(name="t",
                          keywords_by_level={1: ["a"], 2: ["b"], 3: ["c"]})
        assert knob.cumulative_keywords(0) == []
        assert knob.cumulative_keywords(1) == ["a"]
        assert knob.cumulative_keywords(2) == ["a", "b"]
        assert knob.cumulative_keywords(3) == ["a", "b", "c"]

    def test_cumulative_keywords_dedupes(self):
        knob = K.KnobDef(name="t",
                          keywords_by_level={1: ["a"], 2: ["a", "b"]})
        assert knob.cumulative_keywords(2) == ["a", "b"]

    def test_cumulative_keywords_caps_at_max_defined(self):
        knob = K.KnobDef(name="t",
                          keywords_by_level={1: ["a"], 2: ["b"]})
        # Asking for level 5 just gets you all of 1..2
        assert knob.cumulative_keywords(5) == ["a", "b"]


# ── Built-in seed: the historical knobs are still there ──────────────────────

class TestBuiltinSeed:
    """The built-in defaults preserve the historical user-facing
    behaviour — fresh installs still have trek/commie/queer at level 2."""

    def test_trek_in_builtins(self):
        names = [k.name for k in K.BUILTIN_KNOBS]
        assert "trek" in names
        assert "commie" in names
        assert "queer" in names

    def test_each_builtin_has_three_levels(self):
        for knob in K.BUILTIN_KNOBS:
            assert set(knob.keywords_by_level.keys()) >= {1, 2, 3}, knob.name

    def test_builtin_default_levels(self):
        """Commie is the most-baseline voice of the app — defaults to
        level 3 so the collective feel shows through any rendering.
        Trek + queer hover at level 2 (visible but not maxed)."""
        defaults = {k.name: k.default_level for k in K.BUILTIN_KNOBS}
        assert defaults.get("commie") == 3, defaults
        assert defaults.get("trek")   == 2, defaults
        assert defaults.get("queer")  == 2, defaults


# ── load_knobs: merge built-ins + DB overrides ────────────────────────────────

class TestLoadKnobs:
    def test_returns_builtins_when_db_empty(self, monkeypatch):
        monkeypatch.setattr(K, "_read_db_rows", lambda session=None: [])
        loaded = K.load_knobs()
        names = [k.name for k in loaded]
        assert names == ["trek", "commie", "queer"]  # declared order

    def test_db_override_replaces_builtin_by_name(self, monkeypatch):
        custom = {
            "name": "trek",
            "description": "user-customised trek",
            "default_level": 1,
            "keywords_by_level": {"3": ["picard-only"]},
        }
        monkeypatch.setattr(K, "_read_db_rows", lambda session=None: [custom])
        loaded = K.load_knobs()
        trek = next(k for k in loaded if k.name == "trek")
        assert trek.description == "user-customised trek"
        assert trek.default_level == 1
        assert 3 in trek.keywords_by_level
        assert trek.keywords_by_level[3] == ["picard-only"]
        # Other built-ins still present
        assert {k.name for k in loaded} >= {"trek", "commie", "queer"}

    def test_db_can_add_brand_new_knob(self, monkeypatch):
        new_knob = {
            "name": "synthwave",
            "description": "neon, retrofuturism",
            "default_level": 2,
            "keywords_by_level": {"2": ["neon", "vhs"]},
        }
        monkeypatch.setattr(K, "_read_db_rows", lambda session=None: [new_knob])
        loaded = K.load_knobs()
        names = [k.name for k in loaded]
        assert "synthwave" in names
        sw = next(k for k in loaded if k.name == "synthwave")
        assert sw.cumulative_keywords(2) == ["neon", "vhs"]


# ── active_keyword_pool: the function theme_studio actually uses ──────────────

class TestActiveKeywordPool:
    def test_neutral_yields_empty(self):
        assert K.active_keyword_pool({"trek": 0, "commie": 0, "queer": 0}) == []

    def test_trek_3_includes_lower_levels_cumulative(self):
        pool = K.active_keyword_pool({"trek": 3})
        joined = " ".join(pool).lower()
        # level 1 carry-up
        assert "warp" in joined
        # level 3 specific
        assert "federation" in joined or "make it so" in joined

    def test_combined_dials_combine(self):
        pool = K.active_keyword_pool({"trek": 1, "commie": 1, "queer": 0})
        joined = " ".join(pool).lower()
        assert "warp" in joined         # trek level 1
        assert "solidarity" in joined   # commie level 1
        assert "queer" not in joined    # queer is off

    def test_dedupes_across_knobs(self):
        """If two knobs both list 'free', it should appear once."""
        from org_llm.knobs import KnobDef
        a = KnobDef(name="a", keywords_by_level={1: ["free", "x"]})
        b = KnobDef(name="b", keywords_by_level={1: ["free", "y"]})
        pool = K.active_keyword_pool({"a": 1, "b": 1}, knobs=[a, b])
        assert pool.count("free") == 1
        assert "x" in pool and "y" in pool


# ── round-trip via load_knobs / save_knobs API surface ───────────────────────

class TestKnobEditCommand:
    """`org-llm knob edit NAME` opens a YAML editor, validates on save,
    persists as a user override. We don't actually spawn $EDITOR — we
    pre-write the YAML the editor "would have produced" and assert the
    knob got the override."""

    def test_show_renders_yaml_for_a_builtin(self, monkeypatch, cli_db):
        """`--show` prints the YAML without spawning an editor — used
        when the user just wants to see the structure."""
        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        r = runner.invoke(app, ["knob", "edit", "trek", "--show"])
        assert r.exit_code == 0, r.output
        assert "name: trek" in r.output
        assert "keywords_by_level:" in r.output
        # Built-in level 1 keyword should appear
        assert "warp" in r.output or "starship" in r.output

    def test_unknown_name_errors_clearly(self, cli_db):
        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        r = runner.invoke(app, ["knob", "edit", "no-such-knob"])
        assert r.exit_code == 1
        assert "no knob" in r.output.lower() or "not" in r.output.lower()

    def test_revert_clears_user_override(self, monkeypatch, cli_db):
        """After saving a custom version of trek, `--revert` should
        drop it back to the built-in."""
        # Plant a custom trek override
        custom = K.KnobDef(
            name="trek",
            description="custom",
            default_level=2,
            keywords_by_level={3: ["customword"]},
        )
        K.save_knobs(list(K.BUILTIN_KNOBS) + [custom])
        # Sanity: the custom keyword is now in the active pool
        pool = K.active_keyword_pool({"trek": 3})
        assert "customword" in pool

        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        # Stub out the regen spawn so the test stays offline + fast
        from org_llm import cli as _cli
        monkeypatch.setattr(_cli, "_spawn_theme_regen_in_background",
                              lambda key, value: None)
        r = runner.invoke(app, ["knob", "edit", "trek", "--revert"])
        assert r.exit_code == 0, r.output

        # After revert, the built-in pool wins again
        pool = K.active_keyword_pool({"trek": 3})
        assert "customword" not in pool

    def test_edit_validates_locked_name_field(self, monkeypatch, cli_db,
                                                 tmp_path):
        """If the user changes 'name:' in the YAML, the save should
        abort cleanly without writing anything — built-in identity is
        the boundary."""
        # Simulate the editor by replacing subprocess.call with a writer
        import subprocess
        def fake_call(argv):
            path = argv[-1]
            with open(path, "w") as f:
                f.write("name: not-trek\nkeywords_by_level:\n  3:\n    - x\n")
            return 0
        monkeypatch.setattr(subprocess, "call", fake_call)
        # Force the editor probe to find SOMETHING so we get to subprocess.call
        monkeypatch.setenv("EDITOR", "stub")

        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        r = runner.invoke(app, ["knob", "edit", "trek"])
        assert r.exit_code == 1
        assert "name" in r.output.lower() and "lock" in r.output.lower()


class TestAutoRegenTrigger:
    """Setting a *_level config or rewriting theme_knobs should kick off
    a background theme-cache regen. We assert the spawn-helper gets
    called rather than running a real LLM."""

    def test_setting_trek_level_spawns_regen(self, monkeypatch, cli_db):
        from org_llm import cli as _cli
        called: list[tuple[str, str]] = []
        monkeypatch.setattr(_cli, "_spawn_theme_regen_in_background",
                              lambda key, value: called.append((key, value)))
        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        r = runner.invoke(app, ["config", "trek_level", "3"])
        assert r.exit_code == 0, r.output
        assert ("trek_level", "3") in called

    def test_setting_unrelated_key_does_not_spawn_regen(self, monkeypatch,
                                                          cli_db):
        from org_llm import cli as _cli
        called: list[tuple[str, str]] = []
        monkeypatch.setattr(_cli, "_spawn_theme_regen_in_background",
                              lambda key, value: called.append((key, value)))
        from typer.testing import CliRunner
        from org_llm.cli import app
        runner = CliRunner()
        # Pick a real config key the validator accepts
        r = runner.invoke(app, ["config", "log_level", "minimal"])
        assert r.exit_code == 0, r.output
        assert called == []


class TestSaveRoundTrip:
    def test_save_then_load_persists_user_overrides(self, monkeypatch,
                                                       tmp_path):
        # Build an isolated DB with the schema our code expects
        from org_llm.db import init_db, make_engine
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("ORG_LLM_DB", str(db_path))
        init_db(make_engine(db_path))

        custom = K.KnobDef(
            name="cottagecore",
            description="grandma's kitchen, sourdough, fields",
            default_level=2,
            keywords_by_level={1: ["sourdough"], 2: ["mushroom", "linen"]},
        )
        # Persist alongside the built-ins
        K.save_knobs(list(K.BUILTIN_KNOBS) + [custom])

        # Fresh load should bring it back
        loaded = K.load_knobs()
        names = [k.name for k in loaded]
        assert "cottagecore" in names
        cc = next(k for k in loaded if k.name == "cottagecore")
        assert cc.cumulative_keywords(2) == ["sourdough", "mushroom", "linen"]
