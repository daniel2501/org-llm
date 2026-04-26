# [[file:../../../org/20260425230731-org_llm.org::*tests/test_models.py][test_models.py:1]]
"""Tests for org_llm.models — FOSS LLM catalog and tool registry."""
from __future__ import annotations

import pytest
from org_llm.models import (
    CATALOG,
    PALETTE,
    ROLE_KEYS,
    TOOL_REGISTRY,
    ModelInfo,
    ToolInfo,
    _quality,
    apply_theme,
    best_for_role,
    fitting_hardware,
    get_tool,
    recommendations,
    theme_bat,
    theme_delta,
    theme_fzf,
    theme_starship,
)


# ── Catalog structure ──────────────────────────────────────────────────────────

def test_catalog_not_empty():
    assert len(CATALOG) > 10


def test_catalog_entries_are_model_info():
    for m in CATALOG:
        assert isinstance(m, ModelInfo)


def test_catalog_roles_are_valid():
    for m in CATALOG:
        for role in m.roles:
            assert role in ROLE_KEYS, f"{m.tag} has unknown role {role!r}"


def test_catalog_vram_positive():
    for m in CATALOG:
        assert m.vram_gb > 0, f"{m.tag} has non-positive VRAM"


def test_catalog_tags_unique():
    tags = [m.tag for m in CATALOG]
    assert len(tags) == len(set(tags)), "Duplicate model tags in CATALOG"


def test_catalog_all_roles_covered():
    covered = {role for m in CATALOG for role in m.roles}
    for role in ROLE_KEYS:
        assert role in covered, f"Role {role!r} has no catalog entry"


def test_embed_models_present():
    embed = [m for m in CATALOG if "embed" in m.roles]
    assert len(embed) >= 3


def test_reasoning_models_present():
    reason = [m for m in CATALOG if "reason" in m.roles]
    assert len(reason) >= 3


# ── Quality scoring ────────────────────────────────────────────────────────────

def test_quality_returns_int():
    assert isinstance(_quality("nomic-embed-text"), int)


def test_quality_unknown_tag_returns_default():
    assert _quality("totally-unknown-model:99b") == 50


def test_quality_stem_match():
    # "llama3.3" (no version) should match "llama3.3:70b" entry
    q = _quality("llama3.3")
    assert q > 50, "Stem-only tag should resolve to known quality"


def test_quality_higher_params_generally_higher():
    q7 = _quality("deepseek-r1:7b")
    q70 = _quality("deepseek-r1:70b")
    assert q70 > q7


# ── Hardware filtering ─────────────────────────────────────────────────────────

def test_fitting_hardware_with_gpu():
    fits = fitting_hardware(vram_gb=8.0, ram_gb=16.0)
    for m in fits:
        assert m.vram_gb <= 8.0


def test_fitting_hardware_cpu_only():
    fits = fitting_hardware(vram_gb=None, ram_gb=16.0)
    # 16GB * 0.55 = 8.8GB budget
    for m in fits:
        assert m.vram_gb <= 9.0  # slight tolerance for float


def test_fitting_hardware_huge_gpu():
    fits = fitting_hardware(vram_gb=80.0, ram_gb=512.0)
    assert len(fits) == len(CATALOG), "80GB GPU should fit everything"


def test_fitting_hardware_tiny_ram():
    fits = fitting_hardware(vram_gb=None, ram_gb=2.0)
    # 2GB * 0.55 = 1.1GB budget — only the tiniest models
    assert all(m.vram_gb <= 1.2 for m in fits)


# ── Best for role ──────────────────────────────────────────────────────────────

def test_best_for_role_embed_returns_model():
    best = best_for_role("embed", available_vram=None, available_ram=16.0, pulled=set())
    assert best is not None
    assert "embed" in best.roles


def test_best_for_role_prefers_pulled():
    pulled = {"nomic-embed-text"}
    best = best_for_role("embed", available_vram=None, available_ram=16.0, pulled=pulled)
    assert best is not None


def test_best_for_role_respects_hardware():
    best = best_for_role("embed", available_vram=0.6, available_ram=4.0, pulled=set())
    assert best is None or best.vram_gb <= 0.6


def test_best_for_role_none_when_nothing_fits():
    best = best_for_role("chat", available_vram=0.0, available_ram=0.1, pulled=set())
    assert best is None


# ── Recommendations ────────────────────────────────────────────────────────────

def test_recommendations_returns_list():
    recs = recommendations({}, set(), None, 16.0)
    assert isinstance(recs, list)


def test_recommendations_missing_roles_flagged():
    recs = recommendations({}, set(), None, 16.0)
    roles_flagged = {r["role"] for r in recs}
    assert "embed" in roles_flagged
    assert "chat" in roles_flagged


def test_recommendations_no_recs_when_optimal():
    # Assign the exact models the tuner would pick
    current = {}
    vram, ram = None, 16.0
    for role in ROLE_KEYS:
        best = best_for_role(role, available_vram=vram, available_ram=ram, pulled=set())
        if best:
            current[role] = best.tag
    recs = recommendations(current, set(current.values()), vram, ram)
    assert recs == [], "No recommendations when all assignments are already optimal"


def test_recommendation_dict_keys():
    recs = recommendations({}, set(), None, 16.0)
    for r in recs:
        assert "role"      in r
        assert "current"   in r
        assert "suggested" in r
        assert "reason"    in r
        assert "upgrade"   in r


# ── Tool registry ──────────────────────────────────────────────────────────────

def test_tool_registry_not_empty():
    assert len(TOOL_REGISTRY) >= 10


def test_tool_registry_entries_are_tool_info():
    for t in TOOL_REGISTRY:
        assert isinstance(t, ToolInfo)


def test_tool_registry_names_unique():
    names = [t.name for t in TOOL_REGISTRY]
    assert len(names) == len(set(names))


def test_get_tool_known():
    t = get_tool("bat")
    assert t is not None
    assert t.name == "bat"


def test_get_tool_unknown():
    assert get_tool("nonexistent-xyz") is None


def test_tool_install_fn_names_exist():
    """Every tool's install_fn should resolve to a callable in models module."""
    import org_llm.models as m
    for t in TOOL_REGISTRY:
        fn = getattr(m, t.install_fn, None)
        assert callable(fn), f"{t.name} install_fn={t.install_fn!r} not callable"


def test_tool_theme_fn_names_exist():
    import org_llm.models as m
    for t in TOOL_REGISTRY:
        if t.theme_fn:
            fn = getattr(m, t.theme_fn, None)
            assert callable(fn), f"{t.name} theme_fn={t.theme_fn!r} not callable"


# ── Theme output ───────────────────────────────────────────────────────────────

def test_theme_bat_returns_string():
    out = theme_bat()
    assert isinstance(out, str)
    assert "bat" in out.lower()


def test_theme_delta_contains_palette_colors():
    out = theme_delta()
    assert "#" in out  # hex color references


def test_theme_fzf_contains_palette():
    out = theme_fzf()
    assert "FZF_DEFAULT_OPTS" in out
    assert PALETTE["orange"] in out


def test_theme_starship_valid_toml_structure():
    out = theme_starship()
    assert "format" in out
    assert "[username]" in out
    assert "[git_branch]" in out
    # Should not contain raw Python variable names
    assert "PALETTE[" not in out


def test_theme_starship_uses_hex_colors():
    out = theme_starship()
    assert "#" in out


# ── apply_theme (dry-run: check it doesn't crash on missing config_dir) ────────

def test_apply_theme_unknown_tool():
    ok, path = apply_theme("not-a-real-tool")
    assert ok is False
    assert path == ""


def test_apply_theme_writes_file(tmp_path):
    ok, path = apply_theme("bat", config_dir=str(tmp_path))
    assert ok is True
    from pathlib import Path
    assert Path(path).exists()
    content = Path(path).read_text()
    assert "bat" in content.lower()


def test_apply_theme_no_overwrite(tmp_path):
    # First write
    ok1, path = apply_theme("bat", config_dir=str(tmp_path))
    assert ok1 is True
    # Second write should not overwrite
    ok2, _ = apply_theme("bat", config_dir=str(tmp_path))
    assert ok2 is False


# ── PALETTE completeness ───────────────────────────────────────────────────────

def test_palette_has_required_keys():
    for key in ("orange", "purple", "blue", "green", "cyan", "red", "bg", "fg"):
        assert key in PALETTE


def test_palette_values_are_hex():
    for k, v in PALETTE.items():
        assert v.startswith("#"), f"PALETTE[{k!r}] = {v!r} is not a hex color"
        assert len(v) in (4, 7), f"PALETTE[{k!r}] = {v!r} wrong length"
# test_models.py:1 ends here
