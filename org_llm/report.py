# [[file:../../../org/20260425230731-org_llm.org::*report.py][report.py:1]]
from __future__ import annotations

from pathlib import Path

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .ui import console, NERD_FONTS

# Nerd Font glyphs with ASCII fallbacks
_NF_ICONS = {
    "org":        "󱓧",
    "node":       "󰆮",
    "tag":        "󰓹",
    "file":       "󰈔",
    "daily":      "󰃭",
    "orphan":     "󰚌",
    "recent":     "󱑂",
    "embed":      "󱙉",
    "search":     "󰍉",
    "star":       "󰓎",
    "warp":       "󱓞",
    "brain":      "󰋮",
    "code":       "󰒋",
    "link":       "󰌹",
    "trophy":     "󰓖",
    "alert":      "󱗓",
    "check":      "󰄬",
    "dot":        "●",
    "fist":       "✊",
    "hammer":     "🔨",
    "sickle":     "☭",
    "star_red":   "★",
    "people":     "󰀫",
    "solidarity": "󰤙",
    "pride":      "🏳️‍🌈",
    "trans":      "🏳️‍⚧️",
    "heart":      "󰣐",
    "nonbinary":  "⚧",
}
_ASCII_ICONS = {
    "org":        "[O]",
    "node":       "[*]",
    "tag":        "[#]",
    "file":       "[F]",
    "daily":      "[D]",
    "orphan":     "[?]",
    "recent":     "[T]",
    "embed":      "[~]",
    "search":     "[/]",
    "star":       "[S]",
    "warp":       "[>]",
    "brain":      "[B]",
    "code":       "[C]",
    "link":       "[L]",
    "trophy":     "[1]",
    "alert":      "[!]",
    "check":      "[v]",
    "dot":        "·",
    "fist":       "(*)",
    "hammer":     "[H]",
    "sickle":     "[s]",
    "star_red":   "*",
    "people":     "[G]",
    "solidarity": "[U]",
    "pride":      "[P]",
    "trans":      "[T]",
    "heart":      "<3",
    "nonbinary":  "[N]",
}

NF = _NF_ICONS if NERD_FONTS else _ASCII_ICONS

REPORT_HEADER = (
    "[pride.red]█[/pride.red][pride.orange]█[/pride.orange]"
    "[pride.yellow]█[/pride.yellow][pride.green]█[/pride.green]"
    "[pride.blue]█[/pride.blue][pride.violet]█[/pride.violet]"
    "  [lcars2 bold]org-llm knowledge base[/lcars2 bold]  "
    "[pride.violet]█[/pride.violet][pride.blue]█[/pride.blue]"
    "[pride.green]█[/pride.green][pride.yellow]█[/pride.yellow]"
    "[pride.orange]█[/pride.orange][pride.red]█[/pride.red]"
    "   [dim]✊ queer, collective, free ✊[/dim]"
)


def _stat_panel(label: str, value: str, icon: str, style: str = "lcars1") -> Panel:
    content = Text(justify="center")
    content.append(f"{icon}\n", style="bold white")
    content.append(f"{value}\n", style=f"bold {style}")
    content.append(label, style="dim")
    return Panel(content, border_style=style, expand=False, width=18)


def report_overview(session) -> None:
    """High-level stats: files, nodes, embeddings, tags."""
    from sqlalchemy import text
    from .ui import trans_stripe, PRIDE_BANNER
    row = session.execute(text("""
        SELECT
            (SELECT count(*) FROM files)                             AS files,
            (SELECT count(*) FROM nodes)                             AS nodes,
            (SELECT count(*) FROM nodes WHERE embedding IS NOT NULL) AS embedded,
            (SELECT count(DISTINCT tags) FROM nodes WHERE tags != '') AS tag_sets
    """)).fetchone()

    panels = [
        _stat_panel("Files indexed",  str(row.files),    NF["file"],   "lcars1"),
        _stat_panel("Nodes",          str(row.nodes),    NF["node"],   "lcars2"),
        _stat_panel("Embedded",       str(row.embedded), NF["embed"],  "trans.blue"),
        _stat_panel("Tag sets",       str(row.tag_sets), NF["tag"],    "pride.violet"),
    ]
    console.print()
    console.print(trans_stripe(52))
    console.print(PRIDE_BANNER)
    console.print(Columns(panels, equal=True, expand=False))
    console.print(trans_stripe(52))
    console.print()


def report_top_tags(session, limit: int = 20) -> None:
    """Tag frequency leaderboard."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT trim(value) AS tag, count(*) AS cnt
        FROM nodes,
             json_each('["' || replace(replace(tags,' ','","'),':','') || '"]')
        WHERE tags != ''
        GROUP BY tag
        ORDER BY cnt DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    table = Table(box=None, pad_edge=False, show_header=True)
    table.add_column(f"{NF['tag']}  Tag",   style="lcars2")
    table.add_column(f"{NF['node']}  Nodes", style="lcars1", justify="right")
    table.add_column("",  width=30)          # bar
    if not rows:
        console.print("[warn]No tags found.[/warn]")
        return
    max_cnt = rows[0].cnt
    for r in rows:
        bar_len = int(r.cnt / max_cnt * 28)
        bar = f"[lcars3]{'█' * bar_len}[/lcars3][dim]{'░' * (28 - bar_len)}[/dim]"
        table.add_row(r.tag, str(r.cnt), bar)
    console.print(Panel(table, title=f"[lcars1]{NF['trophy']}  Top Tags[/lcars1]",
                        border_style="lcars1"))


def report_recent(session, days: int = 14) -> None:
    """Recently modified nodes."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT n.title, n.tags, f.path,
               datetime(n.mtime,'unixepoch','localtime') AS modified
        FROM nodes n JOIN files f ON f.id = n.file_id
        WHERE n.mtime >= strftime('%s','now','-:days days')
        ORDER BY n.mtime DESC
        LIMIT 20
    """.replace(":days", str(days)))).fetchall()

    table = Table(box=None, pad_edge=False)
    table.add_column(f"{NF['recent']}  Modified", style="lcars1", width=19, no_wrap=True)
    table.add_column(f"{NF['node']}  Title",  style="lcars2")
    table.add_column(f"{NF['tag']}  Tags",   style="dim", width=22)
    table.add_column(f"{NF['file']}  File",  style="dim")
    for r in rows:
        table.add_row(r.modified, r.title, r.tags or "—", Path(r.path).name)
    console.print(Panel(table,
                        title=f"[lcars2]{NF['recent']}  Modified in last {days}d[/lcars2]",
                        border_style="lcars2"))


def report_orphans(session, limit: int = 20) -> None:
    """Nodes that have an org-roam ID but no backlinks."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT n.title, n.tags, f.path
        FROM nodes n JOIN files f ON f.id = n.file_id
        WHERE n.node_id IS NOT NULL
          AND n.body NOT LIKE '%[[id:%'
        ORDER BY n.mtime DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    table = Table(box=None, pad_edge=False)
    table.add_column(f"{NF['orphan']}  Title", style="lcars2")
    table.add_column(f"{NF['tag']}  Tags",     style="dim")
    table.add_column(f"{NF['file']}  File",    style="dim")
    for r in rows:
        table.add_row(r.title, r.tags or "—", Path(r.path).name)
    console.print(Panel(table,
                        title=f"[warn]{NF['orphan']}  Potential Orphans (no outgoing links)[/warn]",
                        border_style="yellow"))


def report_daily(session, limit: int = 14) -> None:
    """Recent daily notes."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT n.title, n.body, f.path,
               datetime(n.mtime,'unixepoch','localtime') AS modified
        FROM nodes n JOIN files f ON f.id = n.file_id
        WHERE f.path LIKE '%/daily/%'
        ORDER BY n.mtime DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    table = Table(box=None, pad_edge=False)
    table.add_column(f"{NF['daily']}  Date",  style="lcars1", width=19, no_wrap=True)
    table.add_column(f"{NF['node']}  Title", style="lcars2")
    table.add_column("Preview",               style="dim")
    for r in rows:
        preview = r.body[:60].replace("\n", " ") + ("…" if len(r.body) > 60 else "")
        table.add_row(r.modified, r.title, preview)
    console.print(Panel(table,
                        title=f"[lcars3]{NF['daily']}  Daily Notes[/lcars3]",
                        border_style="lcars3"))
# report.py:1 ends here
