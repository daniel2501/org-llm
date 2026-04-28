"""LCARS color palettes — named bundles + per-channel config overrides.

By default org-llm renders LCARS chrome in the canonical TNG palette
(orange + purple + blue). Some users want red-alert mode, some want
the Voyager astrometrics green, some want sciences violet. This
module exposes those as a discrete catalogue + a CLI surface so
choosing one is a config change, not a fork.

Resolution order at theme-build time (last wins):
  1. The DARK_PALETTE / LIGHT_PALETTE base in ui.py
  2. The named bundle keyed by config `lcars_palette` (default 'classic')
  3. Per-channel overrides: config `lcars_color_primary`,
     `lcars_color_secondary`, `lcars_color_tertiary` — accept any
     hex string `#RRGGBB`; empty / unset = no override

Anything that reads PALETTE in ui.py (the Rich theme, banners,
gallery, models tool themes) automatically inherits the chosen palette
the next time the theme is rebuilt — `ui.reload_palette()` triggers
that explicitly after a config change.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# ── Named bundles ────────────────────────────────────────────────────────────

PALETTES: dict[str, dict[str, str]] = {
    "classic": {
        "lcars1":       "#FF9900",
        "lcars2":       "#CC88FF",
        "lcars3":       "#4488FF",
        "description":  "Canonical TNG — orange · purple · blue",
    },
    "red": {
        "lcars1":       "#FF3B30",
        "lcars2":       "#FF8C7A",
        "lcars3":       "#FFD60A",
        "description":  "Red alert — red · salmon · amber",
    },
    "green": {
        "lcars1":       "#34C759",
        "lcars2":       "#5AC8FA",
        "lcars3":       "#FFD60A",
        "description":  "Voyager astrometrics — green · sky · gold",
    },
    "gold": {
        "lcars1":       "#FFD60A",
        "lcars2":       "#FF9500",
        "lcars3":       "#FF3B30",
        "description":  "Operations / engineering — gold · amber · red",
    },
    "violet": {
        "lcars1":       "#BF5AF2",
        "lcars2":       "#FF6B9D",
        "lcars3":       "#5AC8FA",
        "description":  "Sciences / medbay — violet · magenta · sky",
    },
}

DEFAULT_PALETTE = "classic"


_HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def _is_hex_color(s: str) -> bool:
    return bool(s and _HEX_RE.match(s.strip()))


def _normalise_hex(s: str) -> str:
    s = s.strip()
    return s if s.startswith("#") else f"#{s}"


# ── Resolve overrides from the SQLite config row ─────────────────────────────

def _read_db_value(key: str) -> Optional[str]:
    """Best-effort read of a single config row. Returns None on any
    failure (no DB yet, missing row, schema drift). Theme code MUST
    NOT crash when called before init_db."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return None
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, key)
            return row.value if row and row.value else None
    except Exception:
        return None


def active_palette_name() -> str:
    """Resolve the active palette name from env + config. Order:
    ORG_LLM_LCARS_PALETTE env > config `lcars_palette` > 'classic'."""
    env = (os.environ.get("ORG_LLM_LCARS_PALETTE") or "").strip().lower()
    if env in PALETTES:
        return env
    db = (_read_db_value("lcars_palette") or "").strip().lower()
    if db in PALETTES:
        return db
    return DEFAULT_PALETTE


def palette_overrides() -> dict[str, str]:
    """Return the lcars1/2/3 overrides to apply on top of the base
    DARK/LIGHT_PALETTE.

    Layered:
      1. Named bundle from active_palette_name()
      2. Per-channel config keys lcars_color_primary/secondary/tertiary
         (each accepts any hex string; invalid/empty = ignored)
    """
    out: dict[str, str] = {}
    bundle = PALETTES.get(active_palette_name(), {})
    for k in ("lcars1", "lcars2", "lcars3"):
        if k in bundle:
            out[k] = bundle[k]
    # Per-channel overrides
    for cfg_key, palette_key in (
        ("lcars_color_primary",    "lcars1"),
        ("lcars_color_secondary",  "lcars2"),
        ("lcars_color_tertiary",   "lcars3"),
    ):
        env_val = os.environ.get(f"ORG_LLM_{cfg_key.upper()}")
        db_val  = _read_db_value(cfg_key)
        chosen  = env_val or db_val
        if chosen and _is_hex_color(chosen):
            out[palette_key] = _normalise_hex(chosen)
    return out


# ── Public API for the CLI ───────────────────────────────────────────────────

def list_palettes() -> list[tuple[str, dict[str, str]]]:
    """Stable-ordered list of (name, bundle) for the CLI to render."""
    return [(name, PALETTES[name]) for name in
            ("classic", "red", "green", "gold", "violet")]
