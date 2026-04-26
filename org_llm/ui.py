# [[file:../../../org/20260425230731-org_llm.org::*ui.py][ui.py:1]]
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.text import Text
from rich.theme import Theme


def _detect_nerd_fonts() -> bool:
    """Return True if a Nerd Font appears to be installed and usable."""
    # Explicit override via env or config file
    env = os.environ.get("ORG_LLM_NERD_FONTS", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    # Check fc-list for any Nerd Font family
    try:
        out = subprocess.check_output(
            ["fc-list", ":spacing=mono"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="ignore")
        if "Nerd" in out or "NFM" in out or "NF " in out:
            return True
    except Exception:
        pass
    # Fallback: check common font directories
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

# ── Trek / communist intensity levels (0–3, env-overridable) ─────────────────
# 0 = minimal, 1 = normal, 2 = extra, 3 = maximum solidarity
def _theme_level(env_var: str, default: int = 2) -> int:
    raw = os.environ.get(env_var, "").strip()
    if raw.isdigit():
        return max(0, min(3, int(raw)))
    if raw.lower() in ("off", "0", "false"): return 0
    if raw.lower() in ("max", "3", "full"):  return 3
    return default

TREK_LEVEL      = _theme_level("ORG_LLM_TREK_LEVEL",      2)
COMMIE_LEVEL    = _theme_level("ORG_LLM_COMMIE_LEVEL",     2)

# ── colour palette ────────────────────────────────────────────────────────────
# LCARS (Star Trek)
_LCARS_ORANGE = "#FF9900"
_LCARS_PURPLE = "#CC88FF"
_LCARS_BLUE   = "#4488FF"

# Rainbow pride flag
_PRIDE_RED    = "#E40303"
_PRIDE_ORANGE = "#FF8C00"
_PRIDE_YELLOW = "#FFED00"
_PRIDE_GREEN  = "#008026"
_PRIDE_BLUE   = "#004DFF"
_PRIDE_VIOLET = "#750787"

# Trans pride flag
_TRANS_BLUE  = "#55CDFC"
_TRANS_PINK  = "#F7A8B8"
_TRANS_WHITE = "#FFFFFF"

# Bi pride flag
_BI_PINK   = "#D60270"
_BI_PURPLE = "#9B4F96"
_BI_BLUE   = "#0038A8"

THEME = Theme({
    "info":         "bold cyan",
    "success":      f"bold {_PRIDE_GREEN}",
    "warn":         "bold yellow",
    "error":        f"bold {_PRIDE_RED}",
    "dim":          "dim white",
    # LCARS
    "lcars1":       f"bold {_LCARS_ORANGE}",
    "lcars2":       f"bold {_LCARS_PURPLE}",
    "lcars3":       f"bold {_LCARS_BLUE}",
    # Pride
    "pride.red":    f"bold {_PRIDE_RED}",
    "pride.orange": f"bold {_PRIDE_ORANGE}",
    "pride.yellow": f"bold {_PRIDE_YELLOW}",
    "pride.green":  f"bold {_PRIDE_GREEN}",
    "pride.blue":   f"bold {_PRIDE_BLUE}",
    "pride.violet": f"bold {_PRIDE_VIOLET}",
    # Trans
    "trans.blue":   f"bold {_TRANS_BLUE}",
    "trans.pink":   f"bold {_TRANS_PINK}",
})

console = Console(theme=THEME)

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
_RAINBOW = [_PRIDE_RED, _PRIDE_ORANGE, _PRIDE_YELLOW,
            _PRIDE_GREEN, _PRIDE_BLUE, _PRIDE_VIOLET]


def rainbow(text: str) -> Text:
    """Return a Rich Text object with each character in a cycling pride colour."""
    out = Text()
    for i, ch in enumerate(text):
        out.append(ch, style=f"bold {_RAINBOW[i % len(_RAINBOW)]}")
    return out


def trans_stripe(width: int = 48) -> Text:
    """Return a trans-flag coloured stripe."""
    t = Text()
    colours = [_TRANS_BLUE, _TRANS_PINK, _TRANS_WHITE, _TRANS_PINK, _TRANS_BLUE]
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


@contextmanager
def impulse(msg: str, total: int, transient: bool = False):
    """Rainbow pride progress bar for countable work."""
    with Progress(
        SpinnerColumn(spinner_name="arc", style="trans.blue"),
        TextColumn("[trans.pink]{task.description}[/trans.pink]"),
        BarColumn(bar_width=32, style=f"{_PRIDE_VIOLET}",
                  complete_style=f"{_PRIDE_RED}"),
        TaskProgressColumn(style="lcars2"),
        TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
        transient=transient,
        console=console,
    ) as progress:
        task = progress.add_task(msg, total=total)
        yield progress, task


# ── output helpers ────────────────────────────────────────────────────────────

def hail(msg: str) -> None:
    console.print(f"[lcars1]◀[/lcars1] [info]{msg}[/info]")


def on_screen(msg: str) -> None:
    console.print(f"[lcars2]▶[/lcars2] {msg}")


def red_alert(msg: str) -> None:
    console.print(f"[error]◀ RED ALERT:[/error] {msg}")


# ── banners ───────────────────────────────────────────────────────────────────

SOLIDARITY_BANNER = """\
[lcars1]  ╔══════════════════════════════════════════════════╗[/lcars1]
[lcars1]  ║[/lcars1] [lcars2]  ✊  WORKERS OF THE FEDERATION, UNITE!  ✊  [/lcars2] [lcars1]║[/lcars1]
[lcars1]  ╚══════════════════════════════════════════════════╝[/lcars1]"""

PRIDE_BANNER = (
    f"  [bold {_PRIDE_RED}]█[/] "
    f"[bold {_PRIDE_ORANGE}]█[/] "
    f"[bold {_PRIDE_YELLOW}]█[/] "
    f"[bold {_PRIDE_GREEN}]█[/] "
    f"[bold {_PRIDE_BLUE}]█[/] "
    f"[bold {_PRIDE_VIOLET}]█[/]"
    "  [bold white]QUEER & PRESENT[/]  "
    f"[bold {_PRIDE_VIOLET}]█[/] "
    f"[bold {_PRIDE_BLUE}]█[/] "
    f"[bold {_PRIDE_GREEN}]█[/] "
    f"[bold {_PRIDE_YELLOW}]█[/] "
    f"[bold {_PRIDE_ORANGE}]█[/] "
    f"[bold {_PRIDE_RED}]█[/]"
)

_DONE_MSGS = [
    ("◀ Make it so.",                                                         "success"),
    ("◀ Engage.",                                                             "lcars2"),
    ("◀ From each according to ability, to each according to need.",          "pride.green"),
    ("◀ The needs of the many outweigh the needs of the few.",                "pride.blue"),
    ("◀ Live long and organize.",                                             "trans.blue"),
    ("◀ Solidarity achieved. ✊",                                             "pride.violet"),
    ("◀ No one left behind — not on this ship.",                             "trans.pink"),
    ("◀ Trans rights are non-negotiable, even in the delta quadrant.",        "trans.blue"),
    ("◀ Queer, collective, free.",                                            "pride.red"),
    ("◀ To boldly go where no comrade has gone before.",                      "lcars1"),
    ("◀ The revolution will be federated.",                                   "pride.green"),
    ("◀ All power to the workers of the federation.",                         "lcars2"),
    ("◀ Doom Emacs: the editor of the liberated.",                           "lcars3"),
    ("◀ Warp speed toward a classless society.",                              "pride.orange"),
    ("◀ We are the Borg — jk, we have unions.",                              "success"),
    ("◀ On the holodeck of history, we are not NPCs.",                        "lcars1"),
    ("◀ Property is theft. Knowledge is free.",                               "pride.violet"),
    ("◀ Beam me up — there is no intelligent life in capitalism.",            "trans.blue"),
    ("◀ The dialectic is irreversible. So is git push.",                     "lcars2"),
    ("◀ Each node a comrade. Each link a bond of solidarity.",               "pride.green"),
]

_msg_idx = 0

# Filtered message lists by level
_TREK_ONLY_MSGS = [m for m in _DONE_MSGS if any(
    kw in m[0] for kw in ("Make it so", "Engage", "Borg", "holodeck",
                           "Beam", "Warp", "warp", "federation", "delta quadrant",
                           "starfleet", "ship")
)]
_COMMIE_ONLY_MSGS = [m for m in _DONE_MSGS if any(
    kw in m[0] for kw in ("comrade", "solidarity", "workers", "property",
                           "dialectic", "revolution", "communism", "class",
                           "from each", "collective")
)]


def make_it_so() -> None:
    global _msg_idx
    # Select pool based on level settings
    if TREK_LEVEL >= 2 and COMMIE_LEVEL >= 2:
        pool = _DONE_MSGS
    elif TREK_LEVEL >= 1 and COMMIE_LEVEL == 0:
        pool = _TREK_ONLY_MSGS or _DONE_MSGS[:2]
    elif COMMIE_LEVEL >= 1 and TREK_LEVEL == 0:
        pool = _COMMIE_ONLY_MSGS or _DONE_MSGS[2:5]
    elif TREK_LEVEL == 0 and COMMIE_LEVEL == 0:
        pool = [("◀ Done.", "success")]
    else:
        pool = _DONE_MSGS
    msg, style = pool[_msg_idx % len(pool)]
    _msg_idx += 1
    console.print(f"[{style}]{msg}[/{style}]")


def solidarity() -> None:
    console.print(SOLIDARITY_BANNER)
    console.print(PRIDE_BANNER)
    console.print(trans_stripe())


def stardate() -> str:
    """Return a Trek-style stardate string."""
    import datetime
    now = datetime.datetime.now()
    # Stardate: YYYY.DDD (year + fractional day of year)
    day_of_year = now.timetuple().tm_yday
    frac = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
    return f"{now.year}.{day_of_year + frac:.2f}"


def lcars_panel(lines: list[tuple[str, str]], title: str = "") -> "Text":
    """Render a minimal LCARS-style panel as a Rich Text object."""
    from rich.text import Text
    out = Text()
    if title:
        out.append(f"  ┌─ {title} ", style=f"bold {_LCARS_ORANGE}")
        out.append("─" * max(0, 50 - len(title)), style=f"bold {_LCARS_ORANGE}")
        out.append("\n")
    for label, value in lines:
        out.append(f"  │ ", style=f"bold {_LCARS_ORANGE}")
        out.append(f"{label:<18}", style=f"bold {_LCARS_PURPLE}")
        out.append(f"{value}\n", style=f"bold {_LCARS_BLUE}")
    out.append(f"  └{'─' * 52}\n", style=f"bold {_LCARS_ORANGE}")
    return out


# Doom Emacs dark-theme palette hints (for panels/banners referencing Doom)
_DOOM_GREEN  = "#98be65"
_DOOM_CYAN   = "#46d9ff"
_DOOM_MAGENTA = "#c678dd"
_DOOM_RED    = "#ff6c6b"
_DOOM_ORANGE = "#da8548"
_DOOM_YELLOW = "#ecbe7b"

# Communist star for solidarity decoration
COMRADE_STAR = "★"
# ui.py:1 ends here
