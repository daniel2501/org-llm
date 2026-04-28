"""Pluggable theme-knob registry.

Background: org-llm originally hardcoded three knobs — trek, commie, queer —
plus their keyword pools and message bundles, scattered across ui.py
and theme_studio.py. That made every "add a new knob" task a code
change touching 4-5 files and impossible for a user to do without
forking the project.

This module collapses all of that into one place:

  * `KnobDef` — the schema (name, description, keyword pools per
    level, default starting level, optional message bundles).
  * `BUILTIN_KNOBS` — the baked-in defaults (trek/commie/queer) so a
    fresh install behaves exactly like before.
  * `load_knobs(session)` — merges BUILTIN_KNOBS with whatever the
    user has stored in the SQLite `theme_knobs` config row, so users
    can edit, replace, or extend any knob without touching code.
  * `active_keyword_pool(levels)` — computes the cumulative pool
    (levels 1..active are all included; higher levels add MORE
    keywords on top of lower ones, instead of replacing them).

Backward compat:
  - `ui.trek_level()`, `commie_level()`, `queer_level()` still work
    (they read the same SQLite rows + env vars they always did).
  - `user_theme_knobs` (rich theme bundles defined via the existing
    `org-llm knob add --llm` command) keeps working untouched. Knob
    bundles and knob keyword-pools are separate concepts that share
    a name — the BUNDLE drives spinner messages; the POOL drives the
    LLM-theming quality gate.

Any new knob with a `keywords_by_level` field automatically gets
quality-gate participation in `theme_studio` — that's the whole point
of moving to a registry.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class KnobDef:
    """One pluggable theme knob — keyword pools per level + metadata."""
    name: str
    description: str = ""
    default_level: int = 2
    # Map level -> list of keywords. Level keys are strings in JSON for
    # round-trip safety; we coerce on read.
    keywords_by_level: dict[int, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "KnobDef":
        kbl = {}
        for k, v in (d.get("keywords_by_level") or {}).items():
            try:
                kbl[int(k)] = list(v) if isinstance(v, list) else []
            except (TypeError, ValueError):
                continue
        return cls(
            name=str(d.get("name") or "").strip(),
            description=str(d.get("description") or ""),
            default_level=int(d.get("default_level") or 2),
            keywords_by_level=kbl,
        )

    def to_dict(self) -> dict:
        return {
            "name":              self.name,
            "description":       self.description,
            "default_level":     self.default_level,
            "keywords_by_level": {str(k): list(v)
                                  for k, v in self.keywords_by_level.items()},
        }

    def cumulative_keywords(self, level: int) -> list[str]:
        """Keywords for level 1..level, combined. Higher dial = MORE
        flavour available, not different. Empty for level 0 (off)."""
        out: list[str] = []
        for L in sorted(self.keywords_by_level):
            if L <= 0:
                continue
            if L > level:
                break
            out.extend(self.keywords_by_level[L])
        # de-dupe preserving order
        seen = set()
        result = []
        for kw in out:
            kl = kw.lower()
            if kl in seen:
                continue
            seen.add(kl)
            result.append(kw)
        return result


# ── Built-in knob defaults (seeded into DB on first load) ─────────────────────

# These are the historical hardcoded values, now expressed once as
# KnobDefs and never referenced by name elsewhere. Users can override
# any of them by editing the `theme_knobs` config row — `org-llm knob
# edit <name>` will be the friendly path once we wire it up; for now
# direct config edits work via the literate-config tangle.

BUILTIN_KNOBS: list[KnobDef] = [
    KnobDef(
        name="trek",
        description=("Star Trek references — LCARS readouts, warp drives, "
                      "stardates, captains, holodecks. Level 3 = full "
                      "Federation-coded UI."),
        default_level=2,
        keywords_by_level={
            1: ["starship", "warp", "stardate", "captain"],
            2: ["LCARS", "engage", "shuttle", "subspace", "phaser"],
            3: ["Federation", "delta quadrant", "tea Earl Grey",
                "warp 9", "holodeck", "Number One", "make it so",
                "Worf", "Borg", "transporter"],
        },
    ),
    KnobDef(
        name="commie",
        description=("Collectivist / liberatory references — solidarity, "
                      "mutual aid, workers, abolition. Level 3 = unmistakably "
                      "left-coded copy."),
        default_level=2,
        keywords_by_level={
            1: ["solidarity", "comrade"],
            2: ["mutual aid", "workers", "collective", "from each",
                "to each"],
            3: ["seize", "general strike", "commune", "abolish",
                "free", "Kropotkin", "the people", "rank-and-file",
                "wildcat"],
        },
    ),
    KnobDef(
        name="queer",
        description=("Queer / trans / pride references. Level 3 = "
                      "queer joy is unmistakable."),
        default_level=2,
        keywords_by_level={
            1: ["pride", "queer"],
            2: ["queer", "rainbow", "gender", "trans"],
            3: ["queer joy", "trans rights", "non-binary",
                "abolish gender", "lavender", "stonewall"],
        },
    ),
]


_DEFAULTS_BY_NAME = {k.name: k for k in BUILTIN_KNOBS}


# ── Loading: merge built-ins + DB-stored overrides ────────────────────────────

_CONFIG_KEY = "theme_knobs"


def load_knobs(session=None) -> list[KnobDef]:
    """Return the active list of knob definitions.

    Resolution order (last wins):
      1. BUILTIN_KNOBS — defaults shipped with the app
      2. SQLite `theme_knobs` config row — JSON list of KnobDef dicts
         (overrides any built-in by name; new knobs append)

    Resilient to a missing/uninitialised DB — falls back to built-ins.
    """
    # Start with built-in copies so callers can mutate freely.
    by_name: dict[str, KnobDef] = {
        k.name: KnobDef.from_dict(k.to_dict()) for k in BUILTIN_KNOBS
    }
    # Layer DB overrides
    db_rows = _read_db_rows(session)
    for raw in db_rows:
        if not isinstance(raw, dict):
            continue
        knob = KnobDef.from_dict(raw)
        if not knob.name:
            continue
        by_name[knob.name] = knob
    # Stable order: built-ins first (in their declared order), then any
    # user-added knobs (alphabetical for determinism).
    out: list[KnobDef] = []
    for name in [k.name for k in BUILTIN_KNOBS]:
        if name in by_name:
            out.append(by_name.pop(name))
    out.extend(by_name[k] for k in sorted(by_name))
    return out


def _read_db_rows(session) -> list[dict]:
    """Pull the JSON list from the config row, with full resilience.

    Accepts either a live SQLAlchemy session OR None — when None we
    open one ourselves against the canonical DB path."""
    try:
        from .db import Config, DB_PATH, make_engine
        from sqlalchemy.orm import Session as _S
        if session is not None:
            row = session.get(Config, _CONFIG_KEY)
            if row and row.value:
                data = json.loads(row.value)
                return data if isinstance(data, list) else []
            return []
        # Default path
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return []
        engine = make_engine(path)
        with _S(engine) as s:
            row = s.get(Config, _CONFIG_KEY)
            if row and row.value:
                data = json.loads(row.value)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_knobs(knobs: list[KnobDef], session=None) -> None:
    """Write the user's overrides back to the DB. Best-effort."""
    try:
        from .db import Config, DB_PATH, make_engine
        from sqlalchemy.orm import Session as _S
        payload = json.dumps([k.to_dict() for k in knobs])
        if session is not None:
            row = session.get(Config, _CONFIG_KEY)
            if row:
                row.value = payload
            else:
                session.add(Config(key=_CONFIG_KEY, value=payload))
            session.commit()
            return
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return
        engine = make_engine(path)
        with _S(engine) as s:
            row = s.get(Config, _CONFIG_KEY)
            if row:
                row.value = payload
            else:
                s.add(Config(key=_CONFIG_KEY, value=payload))
            s.commit()
    except Exception:
        pass


def active_keyword_pool(levels: dict[str, int],
                          knobs: Optional[list[KnobDef]] = None) -> list[str]:
    """Cumulative keyword pool for the active knob levels.

    `levels` is the mapping name -> level the user has dialed (typically
    from `ui._theme_levels()`). For each knob with a corresponding level >= 1,
    we union its level-1..level keywords. Higher dial = bigger pool,
    not a different pool.

    Returns [] when every active knob is at level 0 (neutral mode).
    """
    if knobs is None:
        knobs = load_knobs()
    pool: list[str] = []
    seen = set()
    for knob in knobs:
        L = int(levels.get(knob.name, 0) or 0)
        if L <= 0:
            continue
        for kw in knob.cumulative_keywords(L):
            if kw.lower() in seen:
                continue
            seen.add(kw.lower())
            pool.append(kw)
    return pool
