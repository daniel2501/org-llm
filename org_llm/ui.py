# [[file:../../../org/20260425230731-org_llm.org::*ui.py][ui.py:1]]
from __future__ import annotations

from contextlib import contextmanager

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.text import Text
from rich.theme import Theme

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
    ("◀ Make it so.",                                          "success"),
    ("◀ Engage.",                                              "lcars2"),
    ("◀ From each according to ability, to each according to need.", "pride.green"),
    ("◀ The needs of the many outweigh the needs of the few.", "pride.blue"),
    ("◀ Live long and organize.",                              "trans.blue"),
    ("◀ Solidarity achieved. ✊🏳️‍🌈",                          "pride.violet"),
    ("◀ No one left behind — not on this ship.",               "trans.pink"),
    ("◀ Trans rights are non-negotiable, even in the delta quadrant.", "trans.blue"),
    ("◀ Queer, collective, free.",                             "pride.red"),
    ("◀ To boldly go where no comrade has gone before.",       "lcars1"),
]

_msg_idx = 0


def make_it_so() -> None:
    global _msg_idx
    msg, style = _DONE_MSGS[_msg_idx % len(_DONE_MSGS)]
    _msg_idx += 1
    console.print(f"[{style}]{msg}[/{style}]")


def solidarity() -> None:
    console.print(SOLIDARITY_BANNER)
    console.print(PRIDE_BANNER)
    console.print(trans_stripe())
# ui.py:1 ends here
