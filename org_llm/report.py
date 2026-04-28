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
    """Tag frequency leaderboard.

    Tags are space-separated in the column. We split into a JSON array
    and let SQLite's json_each iterate. Earlier impl stripped EVERY
    colon to handle org-mode literal :tag: syntax — but that mangled
    valid `code:python` / `code:markdown` etc. into `codepython` /
    `codemarkdown`. Now we strip leading/trailing colons only via
    SQL trim(), preserving the colon-as-namespace shape `code:lang`.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT trim(value, ':') AS tag, count(*) AS cnt
        FROM nodes,
             json_each('["' || replace(tags, ' ', '","') || '"]')
        WHERE tags != '' AND trim(value, ':') != ''
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
    # LOG-SCALE bars — when a few system/journal tags dominate by 30-50×
    # (e.g. captains-log, noexport), linear bars push the long tail to
    # zero width and the visualisation degenerates into "the top 3 are
    # tall, everything else is invisible". Log scale preserves the
    # ordering AND keeps every row legible.
    import math
    max_log = math.log1p(rows[0].cnt) or 1
    for r in rows:
        bar_len = int(math.log1p(r.cnt) / max_log * 28)
        bar = f"[lcars3]{'█' * bar_len}[/lcars3][dim]{'░' * (28 - bar_len)}[/dim]"
        table.add_row(r.tag, str(r.cnt), bar)
    console.print(Panel(table,
                        title=f"[lcars1]{NF['trophy']}  Top Tags[/lcars1]  "
                              f"[dim](bar = log scale)[/dim]",
                        border_style="lcars1"))


def report_recent(session, days: int = 14) -> None:
    """Recently modified files (one row per file — sub-headings collapsed).

    Splits notes vs code into TWO panels so a busy code-index session
    can't drown out the user's actual writing. Code edits get a
    condensed (compact) view since most users care about churn-volume
    more than per-file detail; notes get the full title + tags layout.
    """
    from sqlalchemy import text
    from .db import merged_tags_sql
    merged = merged_tags_sql("n")
    base_q = f"""
        WITH file_level AS (
            SELECT MIN(id) AS id, file_id
            FROM nodes
            GROUP BY file_id
        )
        SELECT n.title, {merged} AS tags, f.path,
               datetime(n.mtime,'unixepoch','localtime') AS modified
        FROM nodes n
        JOIN file_level fl ON fl.id = n.id
        JOIN files f ON f.id = n.file_id
        WHERE n.mtime >= strftime('%s','now','-:days days')
          AND ({{tag_clause}})
        ORDER BY n.mtime DESC
        LIMIT :lim
    """.replace(":days", str(days))

    # Notes panel — exclude `code`-tagged nodes
    notes_rows = session.execute(text(
        base_q.format(tag_clause=
            "(' ' || COALESCE(n.tags,'') || ' ') NOT LIKE '% code %'"
        )
    ), {"lim": 15}).fetchall()
    if notes_rows:
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column(f"{NF['recent']}  Modified", style="lcars1",
                        width=19, no_wrap=True)
        tbl.add_column(f"{NF['node']}  Title",  style="lcars2")
        tbl.add_column(f"{NF['tag']}  Tags",   style="dim", width=22)
        tbl.add_column(f"{NF['file']}  File",  style="dim")
        for r in notes_rows:
            tbl.add_row(r.modified, r.title, r.tags or "—",
                         Path(r.path).name)
        console.print(Panel(tbl,
                              title=f"[lcars2]{NF['recent']}  "
                                    f"Recent NOTES (last {days}d)[/lcars2]",
                              border_style="lcars2"))

    # Code panel — only `code`-tagged nodes, condensed (no title col,
    # path-based grouping). Skip entirely when the user has no code
    # corpus indexed.
    code_rows = session.execute(text(
        base_q.format(tag_clause=
            "(' ' || COALESCE(n.tags,'') || ' ') LIKE '% code %'"
        )
    ), {"lim": 12}).fetchall()
    if code_rows:
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column(f"{NF['recent']}  Modified", style="lcars3",
                        width=19, no_wrap=True)
        tbl.add_column("Lang",  style="lcars1", width=10)
        tbl.add_column(f"{NF['file']}  File", style="dim")
        for r in code_rows:
            # Pull the code:<lang> tag if present
            tags = (r.tags or "").split()
            lang = next((t.split(":", 1)[1] for t in tags
                          if t.startswith("code:")), "—")
            tbl.add_row(r.modified, lang, Path(r.path).name)
        console.print(Panel(tbl,
                              title=f"[lcars3]{NF['recent']}  "
                                    f"Recent CODE edits (last {days}d)[/lcars3]",
                              border_style="lcars3"))


def report_orphans(session, limit: int = 20) -> None:
    """Nodes that have an org-roam ID but no backlinks."""
    from sqlalchemy import text
    from .db import merged_tags_sql
    merged = merged_tags_sql("n")
    rows = session.execute(text(f"""
        SELECT n.title, {merged} AS tags, f.path
        FROM nodes n JOIN files f ON f.id = n.file_id
        WHERE n.node_id IS NOT NULL
          AND n.body NOT LIKE '%[[id:%'
        ORDER BY n.mtime DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    # Cap titles + tags + filenames to single-line widths. Without
    # no_wrap=True, Rich wraps long titles across 3-4 visual rows and
    # the orphan list reads as a wall.
    def _cap(s: str, n: int) -> str:
        s = (s or "").replace("\n", " ").strip()
        return (s[:n - 1] + "…") if len(s) > n else s

    table = Table(box=None, pad_edge=False)
    table.add_column(f"{NF['orphan']}  Title", style="lcars2",
                       no_wrap=True, width=42)
    table.add_column(f"{NF['tag']}  Tags",     style="dim",
                       no_wrap=True, width=22)
    table.add_column(f"{NF['file']}  File",    style="dim",
                       no_wrap=True)
    for r in rows:
        table.add_row(_cap(r.title, 42),
                        _cap(r.tags or "—", 22),
                        _cap(Path(r.path).name, 36))
    console.print(Panel(table,
                        title=f"[warn]{NF['orphan']}  Potential Orphans (no outgoing links)[/warn]",
                        border_style="yellow"))


def report_daily(session, limit: int = 14) -> None:
    """Recent daily notes — ONE row per daily file.

    Was: SELECT n.title FROM nodes WHERE path LIKE '%/daily/%' ORDER BY
    n.mtime — which returned every HEADING in every daily file, all
    sharing the file's mtime, so every "Date" column read identically.
    Now: aggregate per-file, prefer the filename's YYYY-MM-DD pattern
    over mtime since that's what the date actually means for a daily,
    and pull a body preview from the first non-empty heading body.
    """
    import re
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT f.path, f.mtime AS fmtime,
               (SELECT n.title FROM nodes n WHERE n.file_id = f.id
                ORDER BY n.id ASC LIMIT 1) AS first_title,
               -- Skip the file-level node (id 0) since its body is
               -- typically just "#+title: …" boilerplate. Pull the
               -- first SUB-HEADING with non-empty body.
               (SELECT n.body  FROM nodes n WHERE n.file_id = f.id
                AND n.body != '' AND n.body NOT LIKE '#+title:%'
                ORDER BY n.id ASC LIMIT 1) AS first_body
        FROM files f
        WHERE f.path LIKE '%/daily/%'
        ORDER BY f.mtime DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    # Daily filenames typically follow YYYY-MM-DD.org; pull the date
    # from the path so the "Date" column reflects the day the note is
    # ABOUT, not just when the file was last edited.
    _date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    from datetime import datetime as _dt
    table = Table(box=None, pad_edge=False)
    table.add_column(f"{NF['daily']}  Date",  style="lcars1", width=12, no_wrap=True)
    table.add_column(f"{NF['node']}  Title", style="lcars2")
    table.add_column("Preview",               style="dim")
    for r in rows:
        m = _date_re.search(r.path or "")
        date_str = m.group(1) if m else \
            _dt.fromtimestamp(float(r.fmtime)).strftime("%Y-%m-%d")
        title = r.first_title or "(untitled)"
        body = (r.first_body or "").replace("\n", " ").strip()
        # Belt-and-braces: even after the SQL skips title-only nodes,
        # some daily files inline boilerplate at the top of the first
        # heading body. Strip a leading "#+title: …" if present.
        if body.lower().startswith("#+title:"):
            body = body.split(None, 1)[-1] if " " in body else ""
        if not body:
            body = "(empty)"
        preview = body[:60] + ("…" if len(body) > 60 else "")
        table.add_row(date_str, title, preview)
    console.print(Panel(table,
                        title=f"[lcars3]{NF['daily']}  Daily Notes[/lcars3]",
                        border_style="lcars3"))
# report.py:1 ends here
