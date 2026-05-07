"""Render a PNG avatar to colored ASCII for terminal display.

The vterm display path for Phase 31 avatars. Uses the same half-block
trick as `avatars.py` (one terminal cell holds two pixels via the upper-
half-block character ▀ — top pixel is foreground, bottom is background)
plus 24-bit truecolor escape sequences. Result renders inline in any
terminal that supports truecolor + Unicode block characters: vterm,
kitty, alacritty, wezterm, foot, iTerm2, modern xterm.

Usage:

    python -m org_llm.avatar_render <png-path> [--cells N]

`--cells N` sets the output width in terminal cells (default 32). The
height auto-scales to preserve aspect ratio. At 32 cells wide × 64
pixels tall (after half-block doubling), a 512×512 source produces a
recognizable thumbnail that fits beside text.

The renderer is dependency-light: only Pillow (~3MB, BSD-3) for image
loading. No chafa / jp2a / external CLIs. Per FOSS-Emacs-first: works
everywhere Python + Pillow run.
"""
from __future__ import annotations

import sys
from pathlib import Path


TOP_BLOCK = "▀"
RESET = "\033[0m"


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


def render(png_path: Path, *, cells: int = 32) -> str:
    """Return colored half-block ASCII for `png_path`. Output is
    `cells` wide; height auto-scales to preserve the source aspect.
    Each terminal cell is 2 source pixels stacked vertically."""
    try:
        from PIL import Image
    except ImportError:
        return ("Pillow not installed. Install with `uv add pillow` or "
                "`pip install pillow`. Required for PNG → terminal render.\n")
    img = Image.open(png_path).convert("RGB")
    src_w, src_h = img.size
    # Each terminal cell carries 2 pixels stacked vertically. Aspect-
    # correct: terminal cells are roughly twice as tall as wide, so a
    # 1:1 source image gets cells × (cells × 2) source pixels and
    # renders in cells × cells terminal cells.
    target_w = cells
    target_h = max(2, round(cells * 2 * (src_h / src_w)))
    if target_h % 2 == 1:
        target_h += 1   # need even rows for half-block pairing
    img = img.resize((target_w, target_h), Image.LANCZOS)
    px = img.load()
    out: list[str] = []
    for y in range(0, target_h, 2):
        line = []
        for x in range(target_w):
            tr, tg, tb = px[x, y]
            br, bg, bb = px[x, y + 1]
            line.append(f"{_fg(tr, tg, tb)}{_bg(br, bg, bb)}{TOP_BLOCK}")
        out.append("".join(line) + RESET)
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cells = 32
    if "--cells" in argv:
        i = argv.index("--cells")
        cells = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    path = Path(argv[1])
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    print(render(path, cells=cells))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
