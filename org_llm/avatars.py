"""Trek-character avatars for org-llm agents — terminal art in the
90s Sierra / Star Trek 25th Anniversary aesthetic.

Each avatar is a 16-row × 16-column pixel grid. Letters in the grid
map to ANSI 256-color values via PALETTE (with a 16-color fallback
mind for terminals that don't support 256). Two pixel rows per
terminal line via the upper-half-block character ▀ (top pixel = FG,
bottom pixel = BG), so a 16×16 avatar renders in 8 terminal lines.

The aesthetic is intentional: blocky pixels, hard edges, no
anti-aliasing, EGA/VGA-era color palette — burgundy command red, gold
ops, dark teal sciences, silver VISOR, Klingon ridge highlights.

To add an avatar:
  1. Compose a 16-line × 16-char grid string below in AVATARS.
  2. Use palette letters from PALETTE (or extend the palette).
  3. Add an entry in MAPPING that ties it to an org-llm agent.

To render: ``python -m org_llm.avatars [name]`` prints all avatars
or just the named one. Pipes cleanly into a vterm or any
xterm-256color terminal.
"""
from __future__ import annotations

import sys
from typing import Optional


# ── palette ─────────────────────────────────────────────────────────
# 256-color indices. Stay close to the EGA/VGA 16-color set for the
# uniform colors so the look reads as "1990s PC game" not "modern
# pixel art."

PALETTE: dict[str, Optional[int]] = {
    '.': None,    # transparent — terminal default
    'K': 16,      # near-black (outline, hair)
    'H': 235,     # very dark gray (deep shadow / Klingon ridges)
    'h': 238,     # slightly-lighter dark gray (Cardassian neck ridge)
    'E': 8,       # dim gray (eye sockets, nose shadow)
    'W': 252,     # warm white (highlights, mouth)
    'V': 250,     # silver (VISOR band, Borg implant)
    'I': 254,     # bright silver (Borg implant highlight)
    'S': 223,     # light skin (Picard, Crusher)
    's': 215,     # mid-skin shadow
    'F': 173,     # darker skin (Geordi, Sisko, Worf)
    'f': 137,     # darker-skin shadow
    'D': 230,     # very-pale skin (Data, android sheen)
    'd': 187,     # pale-skin shadow (Data face)
    'T': 180,     # Cardassian / pale-tan skin (Garak, Odo)
    't': 144,     # Cardassian shadow
    'A': 94,      # auburn/dark-brown hair (Riker, Troi)
    'a': 130,     # mid-brown hair (Worf forelock, Janeway)
    'g': 246,     # gray hair (Sarek, Pulaski)
    'G': 240,     # darker gray hair
    'N': 220,     # blonde hair (Seven of Nine)
    'O': 88,      # rust (Klingon brow ridge)
    'R': 52,      # burgundy red — TNG/VOY command (Picard, Riker)
    'r': 124,     # brighter red highlight on uniform
    'Y': 100,     # dark gold — TNG/VOY ops (Worf, Geordi, Data)
    'y': 178,     # gold highlight + rank pip
    'B': 24,      # teal — TNG sciences/medical
    'b': 31,      # teal highlight
    'X': 18,      # TOS sciences blue (Spock, McCoy, Bashir)
    'x': 25,      # TOS blue highlight
    'P': 220,     # pip yellow (rank insignia)
    'p': 226,     # bright pip
    'L': 240,     # collar undershirt charcoal
    'M': 96,      # purple/plum (Q's robes, Sarek's robe)
    'm': 132,     # plum highlight
    'Q': 22,      # dark green (ENT jumpsuit, civilian)
    'q': 28,      # green highlight
    'J': 95,      # tan/civilian (Garak, Quark)
    'j': 137,     # tan shadow
    'C': 234,     # very dark (DS9 security uniform; Odo)
    'c': 238,     # dark gray
    'Z': 33,      # blue (ENT uniform, Hoshi)
}

RESET = "\033[0m"
TOP_BLOCK = "▀"  # ▀ — upper half block


def _bg(col: Optional[int]) -> str:
    return "\033[49m" if col is None else f"\033[48;5;{col}m"


def _fg(col: Optional[int]) -> str:
    return "\033[39m" if col is None else f"\033[38;5;{col}m"


def render(grid: str) -> str:
    """Render a 16×N pixel-grid string into half-block terminal art.

    Each row in `grid` is one pixel row (16 chars wide, typically).
    Adjacent pixel rows are paired into one terminal line, with the
    upper pixel as the foreground of ▀ and the lower as the background.
    Odd-numbered final pixel rows pair with a transparent row.
    """
    rows = [r for r in grid.strip("\n").splitlines() if r is not None]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r.ljust(width, '.') for r in rows]
    out: list[str] = []
    for i in range(0, len(rows), 2):
        top = rows[i]
        bot = rows[i + 1] if i + 1 < len(rows) else '.' * width
        line_chars: list[str] = []
        for col in range(width):
            tc = PALETTE.get(top[col])
            bc = PALETTE.get(bot[col])
            line_chars.append(f"{_fg(tc)}{_bg(bc)}{TOP_BLOCK}")
        out.append("".join(line_chars) + RESET)
    return "\n".join(out)


# ── avatars ─────────────────────────────────────────────────────────
# 16 rows × 16 cols each. A few notes on the convention:
#   - K outlines + L-charcoal undershirt define the silhouette
#   - S/s/F/f/D for skin tones; A/a for hair
#   - R/r = command red, Y/y = ops gold, B/b = sciences teal
#   - One "highlight" pixel (W or H) per face for that 286-era pop
#   - Rank pips are P (yellow). Black collar is L.

AVATARS: dict[str, str] = {

    # Captain Jean-Luc Picard — bald, narrow face, command red
    'picard': """
....KKKKKKKKKK..
...KKSSSSSSSSKK.
..KSSSSSSSSSSSSK
..KSSSSSSSSSSSSK
..KSEESSSSSSEESK
..KSSSSSSSSSSSSK
..KSSSSEEESSSSK.
..KSSSSSEESSSSK.
..KSSSSSSSSSSSK.
..KSSSWWWWWSSSK.
..KKKsssssssKK..
..LLLLLLLLLLLL..
.RRRRRRRRRRRRRR.
RRRRrrrRRRrrrRRR
RRRRRRPPRPPRRRRR
RRRRRRRRRRRRRRRR
""",

    # Lt. Cmdr. Data — pale android sheen, jet hair, ops gold
    'data': """
....KKKKKKKKKK..
...KKKKKKKKKKKK.
..KKDDDDDDDDDKK.
..KDDDDDDDDDDDDK
..KDDPPDDDDPPDDK
..KDDDDDDDDDDDDK
..KDDDDEDDDDDDK.
..KDDDDDDEDDDDK.
..KDDDWWWDDDDDK.
..KDDDDDDDDDDDK.
..KKKKKKKKKKKK..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPYPPYYYYY
YYYYYYYYYYYYYYYY
""",

    # Lt. Cmdr. Worf — Klingon ridges, dark skin, ops gold
    'worf': """
....aaaaaaaaaa..
...aaKKKKKKKKaa.
..aKOOOOOOOOOKa.
..KOFFFFFFFFFOOK
..KFFEEFFFFEEFFK
..KFFFOOFFFOOFFK
..KFFFFFFFFFFFFK
..KFFFEEFFEEFFK.
..KFFFFFFFFFFK..
..KFFFWWWWWFFK..
..KKfffffffffK..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPYPPYYYYY
YYYYYYYYYYYYYYYY
""",

    # Lt. Cmdr. La Forge — VISOR over eyes, darker skin, ops gold
    'laforge': """
....KKKKKKKKKK..
...KKKKKKKKKKKK.
..KKFFFFFFFFFKK.
..KFFFFFFFFFFFFK
..KVVVVVVVVVVVVK
..KFFFFFFFFFFFFK
..KFFFFEEFFFFFK.
..KFFFFFFFFFFFK.
..KFFFFWWWFFFFK.
..KFFFFFFFFFFFK.
..KKKfffffffKK..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPYPPYYYYY
YYYYYYYYYYYYYYYY
""",

    # Counselor Deanna Troi — dark wavy hair, teal sciences
    'troi': """
...AAAAAAAAAAAA.
..AAAAAAAAAAAAAA
..AAASSSSSSSSAAA
..AASSSSSSSSSSAA
..ASEESSSSEESSSA
..ASSSSSSSSSSSSA
..ASSSEESSSSSSA.
..ASSSSSSSSSSSA.
..ASSSWWWWSSSAA.
..AASSSSSSSSAAA.
...AAAAAAAAAA...
..LLLLLLLLLLLL..
.BBBBBBBBBBBBBB.
BBBBbbbBBBbbbBBB
BBBBBBPPBPPBBBBB
BBBBBBBBBBBBBBBB
""",

    # Cmdr. William Riker — beard, hairline, command red
    'riker': """
...AAAAAAAAAAAA.
..AAAAAAAAAAAAAA
..AASSSSSSSSSSAA
..ASSSSSSSSSSSSA
..ASEESSSSSSEESA
..ASSSSSSSSSSSSA
..ASSSSEEESSSSSA
..ASSAAAAAAASSSA
..ASSAAAAAAAASSA
..ASSAAWWWAAASSA
..AAAAAAAAAAAAA.
..LLLLLLLLLLLL..
.RRRRRRRRRRRRRR.
RRRRrrrRRRrrrRRR
RRRRRRPPRPPRRRRR
RRRRRRRRRRRRRRRR
""",

    # Spock — pointy ears, bowl cut, TOS sciences blue
    'spock': """
...KKKKKKKKKKKK.
..KKKKKKKKKKKKKK
..KSSSSSSSSSSSSK
.KSSSSSSSSSSSSSK
KSSEESSSSSSEESSK
.KSSSSSSSSSSSSSK
.KSSSSEEESSSSSK.
.KSSSSSSSSSSSSK.
.KSSSWWWWWSSSSK.
.KSSSSSSSSSSSSK.
..KKKsssssssKK..
..LLLLLLLLLLLL..
.XXXXXXXXXXXXXX.
XXXXxxxXXXxxxXXX
XXXXXXPPXPPXXXXX
XXXXXXXXXXXXXXXX
""",

    # Sarek — Vulcan elder, gray hair, formal robes (plum)
    'sarek': """
...gggggggggggg.
..ggggggggggggGG
..gSSSSSSSSSSSGG
.gSSSSSSSSSSSSSG
gSSEESSSSSSEESSG
.gSSSSSSSSSSSSSG
.gSSSSEEESSSSSG.
.gSSSSSSSSSSSSG.
.gSSSWWWWWSSSSG.
.gSSSSSSSSSSSSG.
..ggGsssssssGGG.
..GGGGGGGGGGGGG.
.MMMMMMMMMMMMMM.
MMMMmmmMMMmmmMMM
MMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMM
""",

    # Tuvok — Vulcan, dark complexion, VOY ops gold
    'tuvok': """
...KKKKKKKKKKKK.
..KKKKKKKKKKKKKK
..KFFFFFFFFFFFFK
.KFFFFFFFFFFFFFK
KFFEEFFFFFFEEFFK
.KFFFFFFFFFFFFFK
.KFFFFEEEFFFFFK.
.KFFFFFFFFFFFFK.
.KFFFWWWWWFFFFK.
.KFFFFFFFFFFFFK.
..KKKfffffffKK..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPPPPYYYYY
YYYYYYYYYYYYYYYY
""",

    # Janeway (early VOY, hair-up) — auburn bun, command red, captain (4 pips)
    'janeway': """
....aaaaaaaaaa..
...aaaaaaaaaaaa.
..aaaSSSSSSSSaaa
..aaSSSSSSSSSSAA
..aSEESSSSSSEESA
..ASSSSSSSSSSSSA
..ASSSSEESSSSSSA
..ASSSSSSSSSSSSA
..ASSSWWWWWSSSSA
..ASSSSSSSSSSSSA
..AAAsssssssAA..
..LLLLLLLLLLLL..
.RRRRRRRRRRRRRR.
RRRRrrrRRRrrrRRR
RRRRPPPPPPPPPRRR
RRRRRRRRRRRRRRRR
""",

    # Capt. Sisko — bald, dark complexion, DS9 command red (4 pips)
    'sisko': """
....KKKKKKKKKK..
...KKFFFFFFFFKK.
..KFFFFFFFFFFFFK
..KFFFFFFFFFFFFK
..KFEEFFFFFFEEFK
..KFFFFFFFFFFFFK
..KFFFFEEEFFFFK.
..KFFFFFFFFFFFK.
..KFFFWWWWWFFFK.
..KFFFFFFFFFFFK.
..KKKfffffffKK..
..LLLLLLLLLLLL..
.RRRRRRRRRRRRRR.
RRRRrrrRRRrrrRRR
RRRRPPPPPPPPPRRR
RRRRRRRRRRRRRRRR
""",

    # Seven of Nine — blonde, ocular implant, gray catsuit
    'seven': """
....NNNNNNNNNN..
...NNNNNNNNNNNN.
..NNDDDDDDDDDNNN
..NDDDDDDDDDDDNN
..NDDEIIIIIIDDDN
..NDDDDDDDDDDDNN
..NDDDDEEDDDDDN.
..NDDDDDDDDDDDN.
..NDDDDWWWDDDDN.
..NDDDDDDDDDDDN.
..NNNdddddddNN..
..LLLLLLLLLLLL..
.cccccccccccccc.
cccccccccccccccc
cccccVVVVVVccccc
cccccccccccccccc
""",

    # Constable Odo — featureless face, brown DS9 security uniform
    'odo': """
....KKKKKKKKKK..
...KKKKKKKKKKKK.
..KKTTTTTTTTTKK.
..KTTTTTTTTTTTK.
..KTTEETTTTEETTK
..KTTTTTTTTTTTTK
..KTTTTTtttTTTK.
..KTTTTTTTTTTTK.
..KTTTTtttTTTTK.
..KTTTTTTTTTTTK.
..KKKtttttttKK..
..LLLLLLLLLLLL..
.JJJJJJJJJJJJJJ.
JJJJjjjJJJjjjJJJ
JJJJVVVVVVVJJJJJ
JJJJJJJJJJJJJJJJ
""",

    # Dr. Pulaski — graying short hair, TNG s2 sciences teal
    'pulaski': """
...gggggggggggg.
..gggSSSSSSSSggg
..gSSSSSSSSSSSgg
..gSSSSSSSSSSSSg
..gSEESSSSSSEESg
..gSSSSSSSSSSSSg
..gSSSSEESSSSSSg
..gSSSSSSSSSSSg.
..gSSSWWWWWSSSg.
..gSSSSSSSSSSSg.
..ggsssssssssgg.
..LLLLLLLLLLLL..
.BBBBBBBBBBBBBB.
BBBBbbbBBBbbbBBB
BBBBBBPPPPPBBBBB
BBBBBBBBBBBBBBBB
""",

    # Garak — Cardassian: spoon-shaped forehead indent, neck ridges
    'garak': """
....AAAAAAAAA...
...KAAAAAAAAAK..
..KKAAAAAAAAAKK.
..KAAAAAAAAAAAK.
..KTTtHHHHtTTTK.
..KTTTtHHtTTTTK.
..KTEETTTTEETTK.
..KTTTTTtTTTTTK.
..KTTTTtttTTTTK.
..KTTTWWWWWTTTK.
..KKtttttttttKK.
...KhTTTTTThK...
...KhttttttthK..
.JJJJJJJJJJJJJJ.
JJJJjjjJJJjjjJJJ
JJJJJJJJJJJJJJJJ
""",

    # Dr. Leonard McCoy — TOS sciences blue, brown hair
    'mccoy': """
...AAAAAAAAAAAA.
..AAAAAAAAAAAAAA
..AASSSSSSSSSSAA
..ASSSSSSSSSSSSA
..ASEESSSSSSEESA
..ASSSSSSSSSSSSA
..ASSSSEEESSSSSA
..ASSSSSSSSSSSSA
..ASSSWWWWWSSSSA
..ASSSSSSSSSSSSA
..AAAsssssssAA..
..LLLLLLLLLLLL..
.XXXXXXXXXXXXXX.
XXXXxxxXXXxxxXXX
XXXXXPPPPPPPXXXX
XXXXXXXXXXXXXXXX
""",

    # Scotty (Montgomery Scott) — TOS engineering red, sandy hair
    'scotty': """
...aaaaaaaaaaaa.
..aaaaaaaaaaaaaa
..aaSSSSSSSSSSaa
..aSSSSSSSSSSSSa
..aSEESSSSSSEESa
..aSSSSSSSSSSSSa
..aSSSSEEESSSSSa
..aSSSSSSSSSSSSa
..aSSSWWWWWSSSSa
..aSSSSSSSSSSSSa
..aaasssssssaa..
..LLLLLLLLLLLL..
.RRRRRRRRRRRRRR.
RRRRrrrRRRrrrRRR
RRRRRPPPPPPPRRRR
RRRRRRRRRRRRRRRR
""",

    # Hoshi Sato — ENT, black bobbed hair, blue jumpsuit
    'hoshi': """
...KKKKKKKKKKKK.
..KKKKKKKKKKKKKK
..KKSSSSSSSSSSKK
..KSSSSSSSSSSSSK
..KSEESSSSSSEESK
..KSSSSSSSSSSSSK
..KSSSSEEESSSSK.
..KSSSSSSSSSSSK.
..KSSSWWWWWSSSK.
..KSSSSSSSSSSSK.
..KKKsssssssKK..
..LLLLLLLLLLLL..
.ZZZZZZZZZZZZZZ.
ZZZZyyyZZZyyyZZZ
ZZZZZZPPZPPZZZZZ
ZZZZZZZZZZZZZZZZ
""",

    # Jake Sisko — short hair, civilian (DS9 cadet/casual)
    'jake': """
....KKKKKKKKKK..
...KKKKKKKKKKKK.
..KKFFFFFFFFFKK.
..KFFFFFFFFFFFFK
..KFEEFFFFFFEEFK
..KFFFFFFFFFFFFK
..KFFFFEEFFFFFK.
..KFFFFFFFFFFFK.
..KFFFFWWWFFFFK.
..KFFFFFFFFFFFK.
..KKKfffffffKK..
..LLLLLLLLLLLL..
.QQQQQQQQQQQQQQ.
QQQQQQQQQQQQQQQQ
QQQQQQQQQQQQQQQQ
QQQQQQQQQQQQQQQQ
""",

    # Q — smug expression, plum/purple robes, ageless human face
    'q': """
....AAAAAAAAAA..
...AAAAAAAAAAAA.
..AASSSSSSSSSAAA
..ASSSSSSSSSSSAA
..ASEESSSSSSEESA
..ASSSSSSSSSSSSA
..ASSSSEEESSSSSA
..ASSSSSSSSSSSSA
..ASSWWWWWWWSSSA
..ASSSSSSSSSSSSA
..AAAsssssssAA..
..LLLLLLLLLLLL..
.MMMMMMMMMMMMMM.
MMMMmmmMMMmmmMMM
MMMMMMpppppMMMMM
MMMMMMMMMMMMMMMM
""",

    # Reginald Barclay — receding hairline, nervous, ops gold
    'barclay': """
.....AAAAAAAA...
...AAASSSSSSSSAA
..AASSSSSSSSSSAA
..ASSSSSSSSSSSSA
..ASEESSSSSSEESA
..ASSSSSSSSSSSSA
..ASSSSEEESSSSSA
..ASSSSSSSSSSSSA
..ASSSWWWWWSSSSA
..ASSSSSSSSSSSSA
..AAAsssssssAA..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPYYYYYYYY
YYYYYYYYYYYYYYYY
""",

    # Keiko O'Brien — long black hair, sciences teal (botanist)
    'keiko': """
..KKKKKKKKKKKKKK
.KKKKKKKKKKKKKKK
.KKSSSSSSSSSSSKK
.KSSSSSSSSSSSSSK
.KSEESSSSSSEESSK
.KSSSSSSSSSSSSSK
.KSSSSEESSSSSSSK
.KSSSSSSSSSSSSSK
.KSSSSWWWWWSSSSK
.KSSSSSSSSSSSSSK
.KKKsssssssKKKK.
..LLLLLLLLLLLL..
.BBBBBBBBBBBBBB.
BBBBbbbBBBbbbBBB
BBBBBBPPBPPBBBBB
BBBBBBBBBBBBBBBB
""",

    # B'Elanna Torres — half-Klingon ridges (subtle), dark hair, ops gold
    'btorres': """
...AAAAAAAAAAAA.
..AAAAAAAAAAAAAA
..AAASSSSSSSSSAA
..AASSSSSSSSSSAA
..AASOOOOOOOOSSA
..ASSEESSSSEESSA
..ASSSSSSSSSSSSA
..ASSSSEEESSSSSA
..ASSSWWWWWSSSSA
..ASSSSSSSSSSSSA
..AAAsssssssAA..
..LLLLLLLLLLLL..
.YYYYYYYYYYYYYY.
YYYYRRRYYYRRRYYY
YYYYYYPPPPPYYYYY
YYYYYYYYYYYYYYYY
""",
}


# Agent → avatar mapping. Each pairing has a one-line affinity note.
# User can override per-agent in literate config later.
MAPPING: dict[str, str] = {
    # ── core six (TNG bridge crew) ───────────────────────────────
    'crew':           'picard',   # commander / front door
    'gardener':       'data',     # methodical hygiene + classification
    'ops':            'worf',     # discipline + structure + diagnostics
    'researcher':     'laforge',  # VISOR reads invisible signals
    'journalist':     'troi',     # empathic, sentiment, mood
    'planner':        'riker',    # first officer plans + delegates

    # ── specialists (TOS / TNG / DS9 / VOY / ENT) ─────────────────
    'classifier':     'tuvok',    # Vulcan precision; methodical sort
    'scribe':         'jake',     # Jake Sisko — actual writer character
    'engineer':       'scotty',   # iconic engineer
    'triager':        'mccoy',    # gruff first-pass medic
    'summarizer':     'spock',    # logical condensations
    'librarian':      'sarek',    # scholar / statesman / knowledge keeper
    'vision-analyst': 'seven',    # Borg implant; analytical pattern reader
    'extractor':      'odo',      # shape-shifts, gets at hidden truths
    'reviewer':       'pulaski',  # sharp no-nonsense second opinion
    'writer':         'garak',    # DS9 writer-of-stories (the tailor)
    'translator':     'hoshi',    # ENT translator
    'analyst':        'sisko',    # captain / builder / analytical
    'agenda':         'janeway',  # commands the schedule
    'doom':           'btorres',  # B'Elanna — deep expert, sharp opinions
    'agentsmith':     'q',        # meta-being who creates new realities
    'bookworm':       'barclay',  # niche enthusiast
    'curator':        'keiko',    # cultivates the (concept) garden
}


def render_named(name: str) -> str:
    """Return rendered terminal art for the avatar named `name`, or an error string."""
    grid = AVATARS.get(name)
    if grid is None:
        return f"(no avatar named {name!r}; have: {sorted(AVATARS)})"
    return render(grid)


def main(argv: list[str]) -> int:
    """Print all avatars or a single named avatar to stdout; return exit code."""
    if len(argv) > 1:
        print(render_named(argv[1]))
        return 0
    # Print all avatars, two-up where terminal width allows.
    for name in AVATARS:
        print(f"\n  ── {name} ──")
        print(render(AVATARS[name]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
