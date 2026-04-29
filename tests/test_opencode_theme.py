"""Schema-validate the opencode theme JSON we generate.

Catches the class of bug we hit live in Phase 9 of the test session
where opencode crashed in `setBackgroundColor` because our theme file
was missing required fields (text / textMuted / background) AND had
extra keys (`colors:` block at root) that the schema rejected.

The schema is cached at tests/data/opencode_theme_schema.json so the
test runs offline. Refresh manually with:

    curl -sS https://opencode.ai/theme.json \
      > tests/data/opencode_theme_schema.json
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_llm.cli import _opencode_lcars_theme


SCHEMA_PATH = (Path(__file__).parent / "data"
                / "opencode_theme_schema.json")


def _load_schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


def test_theme_has_required_root_keys():
    """Schema requires `theme`. Spot-check that key exists."""
    theme = _opencode_lcars_theme()
    assert "theme" in theme, "theme dict must have a 'theme' key"
    assert isinstance(theme["theme"], dict)


def test_theme_has_required_subkeys():
    """Schema requires primary, secondary, accent, text, textMuted,
    background under the `theme` block. Earlier we shipped without
    text / textMuted / background and opencode crashed in
    setBackgroundColor."""
    theme = _opencode_lcars_theme()
    required = {"primary", "secondary", "accent",
                "text", "textMuted", "background"}
    missing = required - set(theme["theme"].keys())
    assert not missing, f"theme missing required keys: {missing}"


def test_theme_no_unknown_root_keys():
    """Schema forbids additionalProperties at root. Catches a future
    regression where someone adds a flat `colors:` block (we shipped
    one for half the session) or some other field opencode rejects."""
    theme = _opencode_lcars_theme()
    schema = _load_schema()
    allowed = set(schema["properties"].keys())
    unknown = set(theme.keys()) - allowed
    assert not unknown, (
        f"theme has root keys not in opencode schema: {unknown}. "
        f"Schema allows: {sorted(allowed)}"
    )


def test_theme_no_unknown_subkeys():
    """Same for keys under `theme:` — opencode validates strictly."""
    theme = _opencode_lcars_theme()
    schema = _load_schema()
    allowed = set(schema["properties"]["theme"]["properties"].keys())
    unknown = set(theme["theme"].keys()) - allowed
    assert not unknown, (
        f"theme has subkeys not in opencode schema: {unknown}"
    )


def test_color_values_are_well_shaped():
    """Each value is either a hex string, ANSI int, 'none', a named
    reference, OR a {dark, light} object. Spot-check the {dark, light}
    branch since that's what we use exclusively."""
    theme = _opencode_lcars_theme()
    for key, val in theme["theme"].items():
        assert isinstance(val, dict), (
            f"theme.{key} must be a dict for {{dark, light}} mode swap; "
            f"got {type(val).__name__}: {val!r}"
        )
        assert set(val.keys()) == {"dark", "light"}, (
            f"theme.{key} must have exactly dark + light keys; "
            f"got {sorted(val.keys())}"
        )
        for mode in ("dark", "light"):
            v = val[mode]
            assert isinstance(v, str) and v.startswith("#") and len(v) == 7, (
                f"theme.{key}.{mode} must be a #RRGGBB hex string; "
                f"got {v!r}"
            )


def test_theme_validates_against_schema():
    """Full jsonschema validation. The test is skipped when the
    `jsonschema` package isn't available (it's not a hard runtime dep),
    so run it manually:

        uv pip install jsonschema && pytest tests/test_opencode_theme.py
    """
    pytest.importorskip("jsonschema")
    import jsonschema
    theme  = _opencode_lcars_theme()
    schema = _load_schema()
    jsonschema.validate(instance=theme, schema=schema)


def test_palette_overrides_propagate_to_theme():
    """Switching `lcars_palette` (e.g. red / green / gold / violet)
    or setting per-channel hex overrides MUST change the generated
    theme primary/secondary/accent. Earlier the theme generator
    bypassed the palette layer and stayed classic-orange even after
    `org-llm palette green`."""
    import os
    # Override via env so we don't need a DB. palettes module reads
    # ORG_LLM_LCARS_PALETTE first.
    prev = os.environ.get("ORG_LLM_LCARS_PALETTE")
    try:
        os.environ["ORG_LLM_LCARS_PALETTE"] = "green"
        themed_green = _opencode_lcars_theme()
        os.environ["ORG_LLM_LCARS_PALETTE"] = "classic"
        themed_classic = _opencode_lcars_theme()
    finally:
        if prev is None:
            os.environ.pop("ORG_LLM_LCARS_PALETTE", None)
        else:
            os.environ["ORG_LLM_LCARS_PALETTE"] = prev

    g_primary = themed_green["theme"]["primary"]["dark"]
    c_primary = themed_classic["theme"]["primary"]["dark"]
    assert g_primary != c_primary, (
        "Palette switch (classic → green) didn't change theme.primary. "
        f"Both runs returned {g_primary!r}. The opencode theme generator "
        "is bypassing palette_overrides()."
    )
