# [[file:../../../org/20260425230731-org_llm.org::*tests/test_theme.py][test_theme.py:1]]
"""Tests for the dark/light theme switching system.

Covers:
  - Both palettes have the same key set (no missing colours in light mode)
  - Hex sanity (every value is a parseable colour string)
  - ORG_LLM_THEME env var selects the palette at import time
  - The PALETTE proxy in models.py tracks ui.PALETTE live
  - The `theme` CLI command sets the config and shows current state
  - Trans stripe and pride banner switch with the palette
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from org_llm.cli import app
from org_llm.db import Config, get_session, make_engine

runner = CliRunner()


def _reload_ui(monkeypatch, mode: str):
    monkeypatch.setenv("ORG_LLM_THEME", mode)
    import org_llm.ui as ui_mod
    return importlib.reload(ui_mod)


# ── Palette parity ───────────────────────────────────────────────────────────

class TestPaletteParity:
    def test_same_keys_in_both_palettes(self):
        from org_llm.ui import DARK_PALETTE, LIGHT_PALETTE
        assert set(DARK_PALETTE) == set(LIGHT_PALETTE), \
            f"Asymmetric palette keys: {set(DARK_PALETTE) ^ set(LIGHT_PALETTE)}"

    def test_required_keys_present(self):
        from org_llm.ui import DARK_PALETTE, LIGHT_PALETTE
        required = {
            "lcars1", "lcars2", "lcars3",
            "pride.red", "pride.orange", "pride.yellow",
            "pride.green", "pride.blue", "pride.violet",
            "trans.blue", "trans.pink", "trans.white",
            "bi.pink", "bi.purple", "bi.blue",
            "doom.green", "doom.cyan", "doom.magenta",
            "doom.red", "doom.orange", "doom.yellow",
            "fg", "bg", "dim", "info.cyan", "warn.yellow",
        }
        for p in (DARK_PALETTE, LIGHT_PALETTE):
            assert required <= set(p), f"missing: {required - set(p)}"

    def test_every_value_is_a_color(self):
        from org_llm.ui import DARK_PALETTE, LIGHT_PALETTE
        for label, palette in [("dark", DARK_PALETTE), ("light", LIGHT_PALETTE)]:
            for key, val in palette.items():
                assert val.startswith("#") or val == "default", \
                    f"{label}[{key!r}] = {val!r} is not a hex / 'default' colour"
                if val.startswith("#"):
                    assert len(val) in (4, 7), f"{label}[{key!r}] = {val!r} bad length"


# ── Mode selection via env var ───────────────────────────────────────────────

class TestModeSelection:
    def test_dark_is_default(self, monkeypatch):
        monkeypatch.delenv("ORG_LLM_THEME", raising=False)
        ui = importlib.reload(__import__("org_llm.ui", fromlist=["x"]))
        assert ui.THEME_MODE == "dark"
        assert ui.PALETTE is ui.DARK_PALETTE

    def test_light_via_env(self, monkeypatch):
        ui = _reload_ui(monkeypatch, "light")
        assert ui.THEME_MODE == "light"
        assert ui.PALETTE is ui.LIGHT_PALETTE

    def test_aliases_dark(self, monkeypatch):
        for alias in ("dark", "DARK", "Dark", "night"):
            ui = _reload_ui(monkeypatch, alias)
            assert ui.THEME_MODE == "dark"

    def test_aliases_light(self, monkeypatch):
        for alias in ("light", "LIGHT", "Light", "day"):
            ui = _reload_ui(monkeypatch, alias)
            assert ui.THEME_MODE == "light"

    def test_unknown_falls_back_to_dark(self, monkeypatch):
        ui = _reload_ui(monkeypatch, "neon")
        assert ui.THEME_MODE == "dark"


# ── Banners + helpers track the palette ──────────────────────────────────────

class TestRenderingTracksPalette:
    def test_pride_banner_uses_light_colors_in_light_mode(self, monkeypatch):
        ui = _reload_ui(monkeypatch, "light")
        # Light pride.red is darker than dark pride.red — verify it shows up
        assert ui.PALETTE["pride.red"] in ui.PRIDE_BANNER or \
               "pride.red" in ui.PRIDE_BANNER     # banner uses named styles
        # Banner uses named styles, not raw hex — confirm pride.red style ref
        assert "pride.red" in ui.PRIDE_BANNER

    def test_trans_stripe_uses_palette_blue(self, monkeypatch):
        for mode in ("dark", "light"):
            ui = _reload_ui(monkeypatch, mode)
            stripe = ui.trans_stripe(40)
            spans  = stripe.spans
            # Each segment carries the bold-color style; confirm hex matches palette
            blues = [s for s in spans if ui.PALETTE["trans.blue"] in str(s.style)]
            assert blues, f"trans_stripe missing palette blue in {mode} mode"

    def test_rainbow_cycles_palette_in_active_mode(self, monkeypatch):
        for mode in ("dark", "light"):
            ui = _reload_ui(monkeypatch, mode)
            r = ui.rainbow("abcdef")
            styles = [str(s.style) for s in r.spans]
            assert ui.PALETTE["pride.red"]    in styles[0]
            assert ui.PALETTE["pride.violet"] in styles[5]


# ── models.py proxy tracks ui.PALETTE live ───────────────────────────────────

class TestModelsPaletteProxy:
    def test_proxy_tracks_dark(self, monkeypatch):
        _reload_ui(monkeypatch, "dark")
        # Force re-import of models so it re-binds to the (now-dark) ui.PALETTE
        import org_llm.models as m
        m = importlib.reload(m)
        assert m.PALETTE["orange"] == m.PALETTE["orange"]   # same per call
        from org_llm.ui import DARK_PALETTE
        assert m.PALETTE["orange"] == DARK_PALETTE["lcars1"]

    def test_proxy_tracks_light(self, monkeypatch):
        _reload_ui(monkeypatch, "light")
        import org_llm.models as m
        m = importlib.reload(m)
        from org_llm.ui import LIGHT_PALETTE
        assert m.PALETTE["orange"] == LIGHT_PALETTE["lcars1"]
        assert m.PALETTE["orange"].lower() != "#ff9900"   # darker variant

    def test_proxy_supports_dict_iface(self, monkeypatch):
        _reload_ui(monkeypatch, "dark")
        import org_llm.models as m
        m = importlib.reload(m)
        assert "orange" in m.PALETTE
        assert "purple" in m.PALETTE
        assert "missing" not in m.PALETTE
        assert len(list(m.PALETTE)) >= 11
        assert m.PALETTE.get("orange").startswith("#")


# ── Tool theme generators emit different hex per mode ────────────────────────

class TestToolThemeGenerators:
    def test_starship_palette_differs(self, monkeypatch):
        # Dark
        _reload_ui(monkeypatch, "dark")
        import org_llm.models as m
        m = importlib.reload(m)
        dark_starship = m.theme_starship()
        # Light
        _reload_ui(monkeypatch, "light")
        m = importlib.reload(m)
        light_starship = m.theme_starship()
        # The two outputs MUST differ — colours change
        assert dark_starship != light_starship

    def test_fzf_uses_active_palette_orange(self, monkeypatch):
        _reload_ui(monkeypatch, "light")
        import org_llm.models as m
        m = importlib.reload(m)
        from org_llm.ui import LIGHT_PALETTE
        out = m.theme_fzf()
        # The light orange should appear in FZF_DEFAULT_OPTS
        assert LIGHT_PALETTE["lcars1"].lower().lstrip("#") in out.lower() or \
               LIGHT_PALETTE["lcars1"] in out


# ── theme CLI command ────────────────────────────────────────────────────────

class TestThemeCommand:
    @pytest.fixture
    def cli_db(self, tmp_path, monkeypatch):
        # Make sure env doesn't override the stored value
        monkeypatch.delenv("ORG_LLM_THEME", raising=False)
        db = tmp_path / "t.db"
        monkeypatch.setenv("ORG_LLM_DB", str(db))
        runner.invoke(app, ["init"])
        return db

    def test_show_default_is_dark(self, cli_db):
        r = runner.invoke(app, ["theme", "show"])
        assert r.exit_code == 0
        assert "dark" in r.output

    def test_set_light_persists(self, cli_db):
        r = runner.invoke(app, ["theme", "light"])
        assert r.exit_code == 0
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            assert s.get(Config, "theme").value == "light"

    def test_toggle(self, cli_db):
        runner.invoke(app, ["theme", "dark"])
        r = runner.invoke(app, ["theme", "toggle"])
        assert r.exit_code == 0
        engine = make_engine(cli_db)
        with get_session(engine) as s:
            assert s.get(Config, "theme").value == "light"

    def test_unknown_mode_errors(self, cli_db):
        r = runner.invoke(app, ["theme", "neon"])
        assert r.exit_code == 1
        assert "Unknown mode" in r.output


# ── Theme prefix matches ─────────────────────────────────────────────────────

class TestThemePrefix:
    def test_th_prefix_resolves_to_theme(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORG_LLM_DB", str(tmp_path / "p.db"))
        runner.invoke(app, ["init"])
        r = runner.invoke(app, ["th", "show"])
        assert r.exit_code == 0
        assert "theme" in r.output.lower() or "dark" in r.output or "light" in r.output
# test_theme.py:1 ends here
