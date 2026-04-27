# [[file:../../../org/20260425230731-org_llm.org::*ui.py][ui.py:1]]
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib    import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.text import Text
from rich.theme import Theme


def _detect_nerd_fonts() -> bool:
    """Return True if a Nerd Font appears to be installed and usable."""
    env = os.environ.get("ORG_LLM_NERD_FONTS", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    try:
        out = subprocess.check_output(
            ["fc-list", ":spacing=mono"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="ignore")
        if "Nerd" in out or "NFM" in out or "NF " in out:
            return True
    except Exception:
        pass
    from pathlib import Path
    font_dirs = [
        Path.home() / ".local/share/fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    for d in font_dirs:
        if d.exists() and any(d.rglob("*Nerd*")):
            return True
    return False


NERD_FONTS = _detect_nerd_fonts()

# ── Theme intensity levels (0–3) ─────────────────────────────────────────────
# Resolution order: env var → SQLite config row → default. Each call is live,
# so `org-llm config queer_level 1` takes effect on the next command without
# any re-import or restart.

def _theme_level_from_config(db_key: str) -> int | None:
    """Read a theme-level config row, returning None if missing/invalid/no DB."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        from pathlib import Path as _P
        path = _P(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return None
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, db_key)
            if row and row.value.strip().isdigit():
                return max(0, min(3, int(row.value.strip())))
    except Exception:
        pass
    return None


def _theme_level(env_var: str, default: int = 2, db_key: str | None = None) -> int:
    raw = os.environ.get(env_var, "").strip()
    if raw.isdigit():
        return max(0, min(3, int(raw)))
    if raw.lower() in ("off", "false"):  return 0
    if raw.lower() in ("max", "full"):   return 3
    if db_key:
        cfg_val = _theme_level_from_config(db_key)
        if cfg_val is not None:
            return cfg_val
    return default


def trek_level()   -> int: return _theme_level("ORG_LLM_TREK_LEVEL",   2, "trek_level")
def commie_level() -> int: return _theme_level("ORG_LLM_COMMIE_LEVEL", 2, "commie_level")
def queer_level()  -> int: return _theme_level("ORG_LLM_QUEER_LEVEL",  2, "queer_level")


# Back-compat: callers (and a few tests) reference these as module constants.
# Module-level __getattr__ makes the read live, so config-DB changes apply
# without a restart.
def __getattr__(name: str):
    if name == "TREK_LEVEL":   return trek_level()
    if name == "COMMIE_LEVEL": return commie_level()
    if name == "QUEER_LEVEL":  return queer_level()
    raise AttributeError(name)


# ── Dark/light palette switching ──────────────────────────────────────────────
#
# Dark mode (default) uses LCARS-canonical bright colors that pop on a dark
# terminal background. Light mode darkens every color so it remains legible
# on a white/light terminal background. Both palettes expose the SAME keys —
# every consumer (Rich theme, banners, models.py tool themes, gallery.py)
# reads from PALETTE, so flipping the env var flips the whole UI.

DARK_PALETTE: dict[str, str] = {
    # LCARS (Star Trek)
    "lcars1":       "#FF9900",   # signature LCARS orange
    "lcars2":       "#CC88FF",   # purple
    "lcars3":       "#4488FF",   # blue
    # Pride flag
    "pride.red":    "#E40303",
    "pride.orange": "#FF8C00",
    "pride.yellow": "#FFED00",
    "pride.green":  "#008026",
    "pride.blue":   "#004DFF",
    "pride.violet": "#750787",
    # Trans flag
    "trans.blue":   "#55CDFC",
    "trans.pink":   "#F7A8B8",
    "trans.white":  "#FFFFFF",
    # Bi flag
    "bi.pink":      "#D60270",
    "bi.purple":    "#9B4F96",
    "bi.blue":      "#0038A8",
    # Doom dark-theme accents (used by tool-theme generators)
    "doom.green":   "#98be65",
    "doom.cyan":    "#46d9ff",
    "doom.magenta": "#c678dd",
    "doom.red":     "#ff6c6b",
    "doom.orange":  "#da8548",
    "doom.yellow":  "#ecbe7b",
    # Semantic
    "fg":           "#E0E0E0",
    "bg":           "#1B1D24",
    "dim":          "#888888",
    "info.cyan":    "#46d9ff",
    "warn.yellow":  "#ecbe7b",
}

LIGHT_PALETTE: dict[str, str] = {
    # LCARS — darkened for legibility on white
    "lcars1":       "#B36300",   # dark orange (was #FF9900)
    "lcars2":       "#6633CC",   # dark purple (was #CC88FF)
    "lcars3":       "#1E5BC6",   # dark blue   (was #4488FF)
    # Pride flag — darkened brand colors
    "pride.red":    "#A00000",
    "pride.orange": "#B25C00",
    "pride.yellow": "#996600",   # mustard, since pure yellow is invisible on white
    "pride.green":  "#006020",
    "pride.blue":   "#0033AA",
    "pride.violet": "#5A0560",
    # Trans flag — darkened
    "trans.blue":   "#0099CC",
    "trans.pink":   "#CC4477",
    "trans.white":  "#444444",   # gray instead of white
    # Bi flag
    "bi.pink":      "#A8005C",
    "bi.purple":    "#6E3268",
    "bi.blue":      "#002066",
    # Doom light-theme analogues (Doom one-light palette)
    "doom.green":   "#669933",
    "doom.cyan":    "#1078a8",
    "doom.magenta": "#9c4881",
    "doom.red":     "#cc4444",
    "doom.orange":  "#cf6e2e",
    "doom.yellow":  "#a88820",
    # Semantic
    "fg":           "#1A1A1A",
    "bg":           "#FFFFFF",
    "dim":          "#666666",
    "info.cyan":    "#1078a8",
    "warn.yellow":  "#a88820",
}


def _selected_mode() -> str:
    """Return 'light' or 'dark'. Env var wins; default is dark.

    Note: this is read at module import time. The mode can be changed for a
    single command run with `ORG_LLM_THEME=light org-llm …`. To make it the
    persistent default, run `org-llm config theme light` (which the cloud /
    doctor / etc. commands honor on next invocation).
    """
    raw = (os.environ.get("ORG_LLM_THEME") or "").strip().lower()
    if raw in ("light", "day"):
        return "light"
    if raw in ("dark", "night"):
        return "dark"
    # Fall through to SQLite config — but reading it here would create an
    # import cycle (db imports skills imports nothing yet, but db needs
    # Base from db itself first). So we only honor the env var here.
    # _resolve_mode_with_db() below performs the full lookup for callers
    # who can afford the import.
    return "dark"


THEME_MODE: str = _selected_mode()
PALETTE: dict[str, str] = LIGHT_PALETTE if THEME_MODE == "light" else DARK_PALETTE


def _resolve_mode_with_db() -> str:
    """Re-resolve the theme mode, consulting the SQLite `theme` config key.

    Order: ORG_LLM_THEME env > config table > 'dark'. Used by code that needs
    the persisted mode (e.g. tool theme generators when no env var is set).
    """
    raw = (os.environ.get("ORG_LLM_THEME") or "").strip().lower()
    if raw in ("light", "dark"):
        return raw
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        if DB_PATH.exists():
            engine = make_engine(DB_PATH)
            with Session(engine) as s:
                row = s.get(Config, "theme")
                if row and row.value.strip().lower() in ("light", "dark"):
                    return row.value.strip().lower()
    except Exception:
        pass
    return "dark"


def reload_palette() -> None:
    """Re-read the palette after a config change. Idempotent."""
    global THEME_MODE, PALETTE, THEME, console
    new_mode = _resolve_mode_with_db()
    if new_mode == THEME_MODE:
        return
    THEME_MODE = new_mode
    PALETTE = LIGHT_PALETTE if THEME_MODE == "light" else DARK_PALETTE
    THEME = _build_theme(PALETTE)
    console = Console(theme=THEME)


def _build_theme(p: dict[str, str]) -> Theme:
    return Theme({
        "info":         f"bold {p['info.cyan']}",
        "success":      f"bold {p['pride.green']}",
        "warn":         f"bold {p['warn.yellow']}",
        "error":        f"bold {p['pride.red']}",
        "dim":          f"dim {p['dim']}",
        # LCARS
        "lcars1":       f"bold {p['lcars1']}",
        "lcars2":       f"bold {p['lcars2']}",
        "lcars3":       f"bold {p['lcars3']}",
        # Pride
        "pride.red":    f"bold {p['pride.red']}",
        "pride.orange": f"bold {p['pride.orange']}",
        "pride.yellow": f"bold {p['pride.yellow']}",
        "pride.green":  f"bold {p['pride.green']}",
        "pride.blue":   f"bold {p['pride.blue']}",
        "pride.violet": f"bold {p['pride.violet']}",
        # Trans
        "trans.blue":   f"bold {p['trans.blue']}",
        "trans.pink":   f"bold {p['trans.pink']}",
        "trans.white":  f"bold {p['trans.white']}",
        # Bi
        "bi.pink":      f"bold {p['bi.pink']}",
        "bi.purple":    f"bold {p['bi.purple']}",
        "bi.blue":      f"bold {p['bi.blue']}",
    })


THEME = _build_theme(PALETTE)
console = Console(theme=THEME)


# ── back-compat aliases for external imports (report.py, models.py, gallery) ─
# These mirror the previous module-level constants and now resolve from PALETTE.

def _p(key: str) -> str:
    return PALETTE[key]

# Old-style module attributes (still used by gallery.py and tests):
_LCARS_ORANGE = PALETTE["lcars1"]
_LCARS_PURPLE = PALETTE["lcars2"]
_LCARS_BLUE   = PALETTE["lcars3"]
_PRIDE_RED    = PALETTE["pride.red"]
_PRIDE_ORANGE = PALETTE["pride.orange"]
_PRIDE_YELLOW = PALETTE["pride.yellow"]
_PRIDE_GREEN  = PALETTE["pride.green"]
_PRIDE_BLUE   = PALETTE["pride.blue"]
_PRIDE_VIOLET = PALETTE["pride.violet"]
_TRANS_BLUE   = PALETTE["trans.blue"]
_TRANS_PINK   = PALETTE["trans.pink"]
_TRANS_WHITE  = PALETTE["trans.white"]
_BI_PINK      = PALETTE["bi.pink"]
_BI_PURPLE    = PALETTE["bi.purple"]
_BI_BLUE      = PALETTE["bi.blue"]
_DOOM_GREEN   = PALETTE["doom.green"]
_DOOM_CYAN    = PALETTE["doom.cyan"]
_DOOM_MAGENTA = PALETTE["doom.magenta"]
_DOOM_RED     = PALETTE["doom.red"]
_DOOM_ORANGE  = PALETTE["doom.orange"]
_DOOM_YELLOW  = PALETTE["doom.yellow"]


TREK_MSGS = {
    "embed":   "Initializing deflector array",
    "index":   "Scanning sector",
    "search":  "Accessing memory banks",
    "ask":     "Hailing frequencies open",
    "init":    "Engaging warp drive",
    "models":  "Querying starfleet database",
    "tag":     "Running pattern recognition",
    "capture": "Opening hailing channel",
    "code":    "Computing algorithms",
    "cloud":   "Hailing starfleet relay",
    "assess":  "Analyzing ship resources",
    "launch":  "Initializing holodecks",
    "default": "Processing",
}


# ── rainbow text helper ───────────────────────────────────────────────────────

def _rainbow_seq() -> list[str]:
    return [PALETTE["pride.red"], PALETTE["pride.orange"], PALETTE["pride.yellow"],
            PALETTE["pride.green"], PALETTE["pride.blue"], PALETTE["pride.violet"]]


def rainbow(text: str) -> Text:
    """Return a Rich Text object with each character in a cycling pride colour."""
    out = Text()
    seq = _rainbow_seq()
    for i, ch in enumerate(text):
        out.append(ch, style=f"bold {seq[i % len(seq)]}")
    return out


def trans_stripe(width: int = 48) -> Text:
    """Return a trans-flag coloured stripe."""
    t = Text()
    colours = [PALETTE["trans.blue"], PALETTE["trans.pink"],
               PALETTE["trans.white"], PALETTE["trans.pink"],
               PALETTE["trans.blue"]]
    seg = width // len(colours)
    for c in colours:
        t.append("█" * seg, style=f"bold {c}")
    return t


# ── progress context managers ─────────────────────────────────────────────────

@contextmanager
def warp(msg: str = "Processing", transient: bool = True):
    """Star Trek spinner with LCARS styling."""
    with Progress(
        SpinnerColumn(spinner_name="arc", style="lcars1"),
        TextColumn("[lcars2]{task.description}[/lcars2]"),
        transient=transient,
        console=console,
    ) as progress:
        progress.add_task(msg, total=None)
        yield progress


# ── Themed spinner catalogue ──────────────────────────────────────────────
# Each entry: (rich-spinner-name, style key into PALETTE) keyed by a theme
# substring. The "thinking" context manager picks a spinner based on the
# user's currently-active theme knobs/dials so LLM waits reflect their
# personal vibe instead of a single global default.
#
# Default is tuned to Doom Emacs: purple/violet palette with a smooth dots
# animation, which sits comfortably in any Doom-themed terminal.

SPINNER_CATALOG: dict[str, tuple[str, str]] = {
    # Built-in dials
    "trek":      ("arc",            "lcars1"),
    "commie":    ("dots12",         "pride.red"),
    "queer":     ("aesthetic",      "pride.violet"),

    # Theme-name keywords (substring-matched against active knob names)
    "synth":     ("dots12",         "pride.violet"),
    "wave":      ("bouncingBar",    "lcars3"),
    "neon":      ("aesthetic",      "pride.violet"),
    "homelab":   ("bouncingBar",    "lcars3"),
    "lab":       ("dots3",          "pride.green"),
    "schema":    ("dots4",          "lcars3"),
    "work":      ("growHorizontal", "lcars1"),
    "bench":     ("dots5",          "lcars1"),
    "shop":      ("growHorizontal", "lcars1"),
    "brain":     ("dots11",         "lcars2"),
    "garden":    ("growVertical",   "pride.green"),
    "solar":     ("growVertical",   "pride.yellow"),
    "espresso":  ("dots2",          "lcars1"),
    "coffee":    ("dots",           "lcars1"),
    "tea":       ("dots6",          "pride.green"),
    "punk":      ("aesthetic",      "pride.red"),
    "core":      ("dots12",         "lcars3"),
    "academic":  ("dots5",          "lcars3"),
    "academia":  ("dots5",          "lcars3"),
    "art":       ("aesthetic",      "pride.violet"),
    "music":     ("aesthetic",      "pride.violet"),
    "code":      ("dots3",          "lcars3"),
    "dev":       ("dots4",          "lcars3"),
    "data":      ("dots7",          "lcars3"),
    "ml":        ("dots8",          "pride.violet"),
    "ai":        ("dots11",         "pride.violet"),
    "writing":   ("dots5",          "lcars2"),
    "research":  ("dots6",          "lcars3"),
    "system":    ("dots7",          "lcars1"),
    "ops":       ("bouncingBar",    "lcars3"),
    "off-grid":  ("growVertical",   "pride.green"),
    "cottage":   ("growVertical",   "pride.green"),
    "witch":     ("aesthetic",      "pride.violet"),
    "occult":    ("aesthetic",      "pride.violet"),
    "pirate":    ("dots9",          "pride.red"),
    "radio":     ("bouncingBar",    "lcars3"),
    "moon":      ("moon",           "lcars2"),
    "earth":     ("earth",          "pride.green"),
    "weather":   ("weather",        "lcars3"),
    "clock":     ("clock",          "lcars1"),
    "circuit":   ("dots11",         "pride.green"),
    "federation": ("arc",           "lcars3"),
    "warp":      ("arc",            "lcars1"),
    "hifi":      ("dots12",         "lcars2"),
    "jazz":      ("dots10",         "lcars2"),
    "zine":      ("dots3",          "pride.violet"),
    "guild":     ("dots5",          "lcars1"),
    "rpg":       ("dots4",          "pride.violet"),
}

# Doom Emacs feels at home with purple+blue accents; this is the default.
_SPINNER_DEFAULT: tuple[str, str] = ("dots11", "lcars2")


def _resolve_spinner_for_theme(theme: str) -> tuple[str, str] | None:
    """Find the catalogue entry whose key best matches `theme` (substring)."""
    if not theme:
        return None
    t = theme.lower()
    # Exact match first
    if t in SPINNER_CATALOG:
        return SPINNER_CATALOG[t]
    # Substring match — pick the longest matching key
    best: tuple[str, str] | None = None
    best_len = 0
    for key, val in SPINNER_CATALOG.items():
        if key in t and len(key) > best_len:
            best = val
            best_len = len(key)
    return best


def _pick_thinking_spinner() -> tuple[str, str]:
    """Choose a (spinner_name, style) tuple based on active theme knobs.

    Reads ORG_LLM_<NAME>_LEVEL env vars + config DB rows + user_theme_knobs
    and picks deterministically-but-varied across calls. Falls back to the
    Doom-aligned default when nothing is active.
    """
    import os
    import random
    candidates: list[tuple[str, str]] = []

    # Built-in dials
    for name in ("trek", "commie", "queer"):
        try:
            level = int(os.environ.get(f"ORG_LLM_{name.upper()}_LEVEL", "") or 0)
        except ValueError:
            level = 0
        if level <= 0:
            # Try config DB
            try:
                lvl = _theme_level_from_config(f"{name}_level")
                if lvl and lvl > 0:
                    level = lvl
            except Exception:
                pass
        if level > 0 and name in SPINNER_CATALOG:
            for _ in range(level):
                candidates.append(SPINNER_CATALOG[name])

    # User-defined knobs from config DB
    try:
        from .db import Config, get_session, make_engine, DB_PATH
        import json as _json
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if path.exists():
            engine = make_engine(path)
            with get_session(engine) as s:
                row = s.get(Config, "user_theme_knobs")
                if row and row.value:
                    knobs = _json.loads(row.value)
                    for k in (knobs if isinstance(knobs, list) else []):
                        kname = (k.get("name") or "").lower()
                        if not kname:
                            continue
                        # Active level
                        env_level = os.environ.get(
                            f"ORG_LLM_{kname.upper().replace('-', '_')}_LEVEL", "")
                        try:
                            level = int(env_level) if env_level else \
                                    int(k.get("default_level") or 0)
                        except ValueError:
                            level = 0
                        if level <= 0:
                            continue
                        match = _resolve_spinner_for_theme(kname)
                        if match:
                            for _ in range(level):
                                candidates.append(match)
    except Exception:
        pass

    if not candidates:
        return _SPINNER_DEFAULT
    return random.choice(candidates)


# Late import to break a circular dep (cli.py imports from ui)
def _theme_level_from_config(key: str) -> int | None:
    try:
        from .db import Config, get_session, make_engine, DB_PATH
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return None
        engine = make_engine(path)
        with get_session(engine) as s:
            row = s.get(Config, key)
            if row and row.value:
                try:
                    return int(row.value)
                except ValueError:
                    return None
    except Exception:
        return None
    return None


@contextmanager
def thinking(msg: str = "Thinking", model: str = "",
             transient: bool = True,
             spinner: tuple[str, str] | None = None):
    """Spinner for LLM calls — themed by the user's active knobs/dials.

    Distinct from `warp` (which is for general I/O work) so the user can
    see at a glance whether the wait is the model thinking. The spinner
    animation + colour are picked per-call based on which theme dials
    (trek/commie/queer) and user knobs (synthwave/homelab/laboratory…)
    are currently active. If you have `synthwave` at level 3, expect to
    see neon-violet ditherings; if `homelab` is dominant, a green
    bouncingBar; etc. Default (no knobs) is a Doom-Emacs-aligned smooth
    purple `dots11`.

    Pass an explicit `spinner=(name, style)` to override.

    Example:
        with thinking("Synthesising themes", model="llama3.2:1b"):
            resp = chat(...)
    """
    name, style = spinner or _pick_thinking_spinner()
    label = f"{msg} [{model}]" if model else msg
    with Progress(
        SpinnerColumn(spinner_name=name, style=style),
        TextColumn("[lcars3]{task.description}[/lcars3]"),
        transient=transient,
        console=console,
    ) as progress:
        progress.add_task(label, total=None)
        yield progress


@contextmanager
def impulse(msg: str, total: int, transient: bool = False):
    """Rainbow pride progress bar for countable work."""
    with Progress(
        SpinnerColumn(spinner_name="arc", style="trans.blue"),
        TextColumn("[trans.pink]{task.description}[/trans.pink]"),
        BarColumn(bar_width=32, style=f"{PALETTE['pride.violet']}",
                  complete_style=f"{PALETTE['pride.red']}"),
        TaskProgressColumn(style="lcars2"),
        TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
        transient=transient,
        console=console,
    ) as progress:
        task = progress.add_task(msg, total=total)
        yield progress, task


@contextmanager
def heartbeat(msg: str, *, stall_secs: float = 60.0,
              warn_at: float = 10.0,
              poll_secs: float = 2.0):
    """Live spinner that updates description with elapsed time, and emits
    a one-shot yellow warning if the wrapped block goes silent past
    `stall_secs * 0.5`. The block can call the yielded `tick()` callable
    to reset the elapsed-time counter (e.g. after each unit of work).

    Use for *any* blocking call where the user otherwise sees no UI
    feedback — DB migrations, slow LLM calls, network ops. Cheaper than
    a full Progress bar; gives "is it alive?" feedback for free.

    Example:
        with heartbeat("Embedding nodes…", stall_secs=30.0) as tick:
            for n in nodes:
                embed(n)
                tick()              # mark progress, reset slow-warning
    """
    import threading, time as _t
    last      = [_t.monotonic()]
    stop_evt  = threading.Event()
    warned    = [False]

    with Progress(
        SpinnerColumn(spinner_name="arc", style="trans.blue"),
        TextColumn("[trans.pink]{task.description}[/trans.pink]"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(msg, total=None)

        def _ticker() -> None:
            while not stop_evt.is_set():
                if stop_evt.wait(poll_secs):
                    return
                gap = _t.monotonic() - last[0]
                if gap < warn_at:
                    progress.update(task, description=msg)
                    continue
                if gap > stall_secs * 0.5 and not warned[0]:
                    warned[0] = True
                    # Emit a real warning above the spinner — escapes the
                    # Live region so it stays in the transcript even after
                    # the spinner is torn down.
                    console.print(
                        f"[yellow]heartbeat: '{msg}' silent for "
                        f"{gap:.0f}s (stall budget {stall_secs:.0f}s).[/yellow]"
                    )
                progress.update(task,
                                description=f"{msg}  [dim]({gap:.0f}s)[/dim]")

        t = threading.Thread(target=_ticker, daemon=True)
        t.start()

        def tick() -> None:
            last[0]   = _t.monotonic()
            warned[0] = False

        try:
            yield tick
        finally:
            stop_evt.set()
            t.join(timeout=2.0)


# ── output helpers ────────────────────────────────────────────────────────────

def hail(msg: str) -> None:
    console.print(f"[lcars1]◀[/lcars1] [info]{msg}[/info]")


def on_screen(msg: str) -> None:
    console.print(f"[lcars2]▶[/lcars2] {msg}")


def red_alert(msg: str) -> None:
    console.print(f"[error]◀ RED ALERT:[/error] {msg}")


# ── banners (use named styles so they switch with the theme) ─────────────────

SOLIDARITY_BANNER = (
    "[lcars1]  ╔══════════════════════════════════════════════════╗[/lcars1]\n"
    "[lcars1]  ║[/lcars1] [lcars2]  ✊  WORKERS OF THE FEDERATION, UNITE!  ✊  [/lcars2] [lcars1]║[/lcars1]\n"
    "[lcars1]  ╚══════════════════════════════════════════════════╝[/lcars1]"
)


def _pride_banner() -> str:
    """Build the pride banner from the current palette (switches with theme)."""
    headline_color = PALETTE["fg"] if THEME_MODE == "light" else "white"
    blocks = "  [pride.red]█[/] [pride.orange]█[/] [pride.yellow]█[/] [pride.green]█[/] [pride.blue]█[/] [pride.violet]█[/]"
    rev    = "[pride.violet]█[/] [pride.blue]█[/] [pride.green]█[/] [pride.yellow]█[/] [pride.orange]█[/] [pride.red]█[/]"
    return f"{blocks}  [bold {headline_color}]QUEER & PRESENT[/]  {rev}"


PRIDE_BANNER = _pride_banner()


# ── Themed completion messages ───────────────────────────────────────────────
#
# Each message is explicitly tagged with the themes it draws on. Several
# messages straddle two flags (Trek + commie, Trek + trans, etc.) so the
# tag is a frozenset. A message renders iff EVERY tag in its set has a
# level >= 1. Untagged ("neutral") messages always render.
#
# Built-in themes: trek, commie, queer. Users can register custom themes
# (with their own keywords, messages, and ORG_LLM_<NAME>_LEVEL env var)
# via `org-llm knob add` — see `theme_knobs.py` and the `_load_user_knobs`
# call below.

_DONE_MSGS_TAGGED: list[tuple[str, str, frozenset[str]]] = [
    ("◀ Make it so.",                                                          "success",     frozenset({"trek"})),
    ("◀ Engage.",                                                              "lcars2",      frozenset({"trek"})),
    ("◀ From each according to ability, to each according to need.",           "pride.green", frozenset({"commie"})),
    ("◀ The needs of the many outweigh the needs of the few.",                 "pride.blue",  frozenset({"trek", "commie"})),
    ("◀ Live long and organize.",                                              "trans.blue",  frozenset({"trek", "commie"})),
    ("◀ Solidarity achieved. ✊",                                              "pride.violet",frozenset({"commie"})),
    ("◀ No one left behind — not on this ship.",                              "trans.pink",  frozenset({"trek", "queer"})),
    ("◀ Trans rights are non-negotiable, even in the delta quadrant.",         "trans.blue",  frozenset({"queer", "trek"})),
    ("◀ Queer, collective, free.",                                             "pride.red",   frozenset({"queer", "commie"})),
    ("◀ To boldly go where no comrade has gone before.",                       "lcars1",      frozenset({"trek", "commie"})),
    ("◀ The revolution will be federated.",                                    "pride.green", frozenset({"trek", "commie"})),
    ("◀ All power to the workers of the federation.",                          "lcars2",      frozenset({"trek", "commie"})),
    ("◀ Doom Emacs: the editor of the liberated.",                            "lcars3",      frozenset({"commie"})),
    ("◀ Warp speed toward a classless society.",                               "pride.orange",frozenset({"trek", "commie"})),
    ("◀ We are the Borg — jk, we have unions.",                               "success",     frozenset({"trek", "commie"})),
    ("◀ On the holodeck of history, we are not NPCs.",                         "lcars1",      frozenset({"trek", "commie"})),
    ("◀ Property is theft. Knowledge is free.",                                "pride.violet",frozenset({"commie"})),
    ("◀ Beam me up — there is no intelligent life in capitalism.",             "trans.blue",  frozenset({"trek", "commie"})),
    ("◀ The dialectic is irreversible. So is git push.",                      "lcars2",      frozenset({"commie"})),
    ("◀ Each node a comrade. Each link a bond of solidarity.",                "pride.green", frozenset({"commie"})),
    ("◀ Done.",                                                                "success",     frozenset()),  # neutral
]

# Back-compat: legacy attribute used by test_theme.py and external code.
_DONE_MSGS = [(m, s) for (m, s, _) in _DONE_MSGS_TAGGED]
_msg_idx = 0


def _theme_levels() -> dict[str, int]:
    """Map theme name → level (live; reads env + config DB on every call)."""
    levels = {
        "trek":   trek_level(),
        "commie": commie_level(),
        "queer":  queer_level(),
    }
    for knob in _user_knobs():
        env = f"ORG_LLM_{knob['name'].upper()}_LEVEL"
        levels[knob["name"]] = _theme_level(
            env, int(knob.get("default_level", 2)),
            db_key=f"{knob['name']}_level",
        )
    return levels


def _user_knobs() -> list[dict]:
    """Load user-defined theme knobs from the SQLite config row.

    Stored as JSON under the key `user_theme_knobs`. Each knob has:
        name (str), keywords (list[str]), messages (list[[text, style]]),
        default_level (int).
    Resilient to a missing/uninitialised DB.
    """
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = os.environ.get("ORG_LLM_DB") or str(DB_PATH)
        from pathlib import Path as _P
        if not _P(path).exists():
            return []
        engine = make_engine(_P(path))
        import json as _json
        with Session(engine) as s:
            row = s.get(Config, "user_theme_knobs")
            if not row or not row.value:
                return []
            data = _json.loads(row.value)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


_LEVEL_WEIGHT = {0: 0, 1: 1, 2: 2, 3: 4}


def _enabled_msgs() -> list[tuple[str, str]]:
    """Build the make_it_so message pool, respecting per-theme levels.

    Levels behave as relative weights, not just on/off:
        0 = silent
        1 = sparse  (half as often as 2)
        2 = normal  (default)
        3 = max     (twice as often as 2)

    For multi-tag messages we use the MIN level across tags — a tag that's
    silenced silences the whole message. Untagged ("neutral") entries always
    render at full weight. User knobs contribute on top of the built-in pool.
    """
    levels = _theme_levels()
    keep: list[tuple[str, str]] = []
    for msg, style, tags in _DONE_MSGS_TAGGED:
        if not tags:
            keep.extend([(msg, style)] * _LEVEL_WEIGHT[2])  # neutral, normal
            continue
        min_lvl = min(levels.get(t, 0) for t in tags)
        weight  = _LEVEL_WEIGHT.get(min_lvl, 0)
        keep.extend([(msg, style)] * weight)
    # User knobs add their own messages when their level is on
    for knob in _user_knobs():
        lvl = levels.get(knob["name"], 0)
        weight = _LEVEL_WEIGHT.get(lvl, 0)
        if not weight:
            continue
        for entry in knob.get("messages", []) or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                pair = (str(entry[0]), str(entry[1]))
            elif isinstance(entry, str):
                pair = (entry, "info")
            else:
                continue
            keep.extend([pair] * weight)
    return keep


def make_it_so() -> None:
    global _msg_idx
    pool = _enabled_msgs() or [("◀ Done.", "success")]
    msg, style = pool[_msg_idx % len(pool)]
    _msg_idx += 1
    console.print(f"[{style}]{msg}[/{style}]")


def solidarity() -> None:
    """Render the opening banners. Each section respects its theme dial."""
    if commie_level() >= 1:
        console.print(SOLIDARITY_BANNER)
    if queer_level() >= 1:
        console.print(PRIDE_BANNER)
        console.print(trans_stripe())


def stardate() -> str:
    """Return a Trek-style stardate string."""
    import datetime
    now = datetime.datetime.now()
    day_of_year = now.timetuple().tm_yday
    frac = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
    return f"{now.year}.{day_of_year + frac:.2f}"


def lcars_panel(lines: list[tuple[str, str]], title: str = "") -> "Text":
    """Render a minimal LCARS-style panel as a Rich Text object."""
    from rich.text import Text
    out = Text()
    if title:
        out.append(f"  ┌─ {title} ", style=f"bold {PALETTE['lcars1']}")
        out.append("─" * max(0, 50 - len(title)), style=f"bold {PALETTE['lcars1']}")
        out.append("\n")
    for label, value in lines:
        out.append(f"  │ ", style=f"bold {PALETTE['lcars1']}")
        out.append(f"{label:<18}", style=f"bold {PALETTE['lcars2']}")
        out.append(f"{value}\n", style=f"bold {PALETTE['lcars3']}")
    out.append(f"  └{'─' * 52}\n", style=f"bold {PALETTE['lcars1']}")
    return out


# Communist star for solidarity decoration
COMRADE_STAR = "★"
# ui.py:1 ends here
