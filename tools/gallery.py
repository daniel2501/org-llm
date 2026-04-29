#!/usr/bin/env python3
# [[file:../../../org/20260425230731-org_llm.org::*tools/gallery.py][gallery.py:1]]
"""Generate SVG screenshots of org-llm CLI output for the README.

Each scene renders one or more commands into a Rich console with SVG export
turned on, then writes the result under docs/img/<scene>.svg.

Run from the repo root:
    uv run python tools/gallery.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# So `from org_llm…` works when run as a script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console
from rich.panel  import Panel

import org_llm.ui as ui
import org_llm.report as report_mod
from org_llm.cli import _TUTOR_STEPS
from org_llm.cloud  import PROVIDERS, cost_per_1k_tokens
from org_llm.creds  import status as creds_status
from org_llm.db     import File, Node, init_db, get_session, make_engine
from org_llm.models import CATALOG, fitting_hardware, recommendations, ROLE_KEYS
from org_llm.search import to_blob


IMG_DIR = ROOT / "docs" / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def _new_console(width: int = 100):
    """Fresh recording console with the project theme."""
    return Console(theme=ui.THEME, record=True, force_terminal=True,
                   width=width, color_system="truecolor")


def _save(con: Console, name: str, title: str | None = None):
    out = IMG_DIR / f"{name}.svg"
    svg = con.export_svg(title=title or f"org-llm {name}", clear=True)
    out.write_text(svg)
    print(f"  ✓ {out.relative_to(ROOT)}")


# ── Seed a tmp DB so report scenes have data ──────────────────────────────────

def _populate(engine):
    now = time.time()
    week = now - (5 * 86400)
    old = now - (40 * 86400)
    with get_session(engine) as s:
        f1 = File(path="/vault/notes/socialism.org", indexed_at="now",
                  node_count=2, mtime=now)
        f2 = File(path="/vault/daily/2026-04-26.org", indexed_at="now",
                  node_count=1, mtime=week)
        f3 = File(path="/vault/archive/star-trek.org", indexed_at="now",
                  node_count=1, mtime=old)
        s.add_all([f1, f2, f3]); s.flush()
        for nid, fid, title, body, tags, mt, embed in [
            ("n1", f1.id, "Solidarity ✊",
             "Workers of the world, unite — there is nothing to lose but our chains.",
             "politics work", now, to_blob([1.0, 0.0, 0.0])),
            ("n2", f1.id, "Mutual aid",
             "From each according to ability, to each according to need.",
             "politics community", now, to_blob([0.9, 0.1, 0.0])),
            ("n3", f2.id, "2026-04-26",
             "Started org-llm overhaul; multi-provider cloud + pass creds.",
             "daily", week, to_blob([0.0, 1.0, 0.0])),
            ("n4", f3.id, "TNG: Measure of a Man",
             "An exploration of personhood as a labor question.",
             "trek scifi", old, to_blob([0.0, 0.0, 1.0])),
        ]:
            s.add(Node(file_id=fid, node_id=nid, title=title, body=body,
                       tags=tags, mtime=mt, embedding=embed))
        s.commit()


# ── Scenes ────────────────────────────────────────────────────────────────────

def scene_pride_banner():
    con = _new_console(width=80)
    con.print(ui.PRIDE_BANNER)
    con.print(ui.trans_stripe(60))
    con.print(ui.SOLIDARITY_BANNER)
    _save(con, "01-banner", "org-llm — queer, collective, free")


def scene_doctor_table():
    """Mock a doctor-shaped output (without running the real doctor — needs Ollama live)."""
    from rich.table import Table
    con = _new_console(width=88)

    rows = [
        ("",       "[lcars1]System[/lcars1]", ""),
        ("[bold green]✓[/]", "Python", "3.11.14"),
        ("[bold green]✓[/]", "uv", "/home/daniel/.local/bin/uv"),
        ("[bold green]✓[/]", "ollama binary", "/home/daniel/.local/bin/ollama"),
        ("[bold green]✓[/]", "opencode", "~/.opencode/bin/opencode"),
        ("[bold green]✓[/]", "Disk space", "47 GB free; DB is 4 MB"),
        ("",       "[lcars1]Database[/lcars1]", ""),
        ("[bold green]✓[/]", "DB reachable", "~/.local/share/org-llm/org-llm.db"),
        ("[bold green]✓[/]", "sqlite-vec extension", ""),
        ("[bold green]✓[/]", "DB integrity", "PRAGMA integrity_check = ok"),
        ("[bold green]✓[/]", "Index populated", "187 files / 2,143 nodes"),
        ("[bold green]✓[/]", "Embeddings", "2,143/2,143 (100%)"),
        ("",       "[lcars1]Ollama[/lcars1]", ""),
        ("[bold green]✓[/]", "Ollama API", "http://localhost:11434"),
        ("[bold green]✓[/]", "  embed_model", "nomic-embed-text"),
        ("[bold green]✓[/]", "  chat_model",  "llama3.3"),
        ("[bold green]✓[/]", "  code_model",  "qwen2.5-coder"),
        ("",       "[lcars1]Cloud GPU[/lcars1]", ""),
        ("[bold yellow]⚠[/]", "Cloud GPU",  "not configured — org-llm cloud --providers"),
        ("",       "[lcars1]Credentials (pass)[/lcars1]", ""),
        ("[bold green]✓[/]", "pass installed",         "/usr/bin/pass"),
        ("[bold green]✓[/]", "pass store initialized", "~/.password-store"),
        ("[dim]·[/]",        "stored secrets",          "1 (org-llm/*)"),
    ]
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("St", width=3)
    table.add_column("Check", style="lcars2")
    table.add_column("Detail", style="dim")
    for r in rows:
        table.add_row(*r)

    con.print()
    con.print(ui.trans_stripe(78))
    con.print(ui.PRIDE_BANNER)
    con.print(Panel(table, title="[lcars1]org-llm doctor[/lcars1]", border_style="lcars1"))
    con.print(ui.trans_stripe(78))
    _save(con, "02-doctor", "org-llm doctor — health check")


def scene_cloud_providers():
    from rich.table import Table
    con = _new_console(width=110)
    con.rule("[lcars1]Supported Cloud GPU Providers[/lcars1]")
    tbl = Table(box=None, pad_edge=False)
    tbl.add_column("Slug",       style="lcars1",  no_wrap=True, width=14)
    tbl.add_column("Name",       style="lcars2",  no_wrap=True)
    tbl.add_column("API",        style="dim",     width=8)
    tbl.add_column("Cheapest",   width=22)
    tbl.add_column("Description", style="dim")
    for p in PROVIDERS:
        cheapest_gpu = min(p.gpu_costs, key=p.gpu_costs.get)
        cheapest_cost = p.gpu_costs[cheapest_gpu]
        label = f"FREE — {cheapest_gpu}" if cheapest_cost == 0 else f"${cheapest_cost:.2f}/hr ({cheapest_gpu})"
        tbl.add_row(p.slug, p.name, p.api_compat, label, p.description)
    con.print(tbl)
    con.print()
    con.print("▶ Sign up: [bold]org-llm cloud --signup <slug>[/bold]   "
              "│ Configure: [bold]org-llm cloud --configure[/bold]", style="lcars2")
    _save(con, "03-cloud-providers", "org-llm cloud --providers")


def scene_cloud_cost():
    from rich.table import Table
    con = _new_console(width=88)
    con.rule("[lcars1]Cloud GPU Cost Comparison[/lcars1]")
    con.print()
    for p in PROVIDERS[:5]:
        tbl = Table(title=f"[lcars2]{p.name}[/lcars2]  ({p.api_compat})",
                    box=None, pad_edge=False)
        tbl.add_column("GPU",       style="lcars1")
        tbl.add_column("$/hr",      style="lcars3", justify="right")
        tbl.add_column("¢/1k tok",  style="lcars2", justify="right")
        for gpu, hourly in p.gpu_costs.items():
            cpp = cost_per_1k_tokens(gpu, tokens_per_sec=30.0, provider_slug=p.slug)
            tbl.add_row(gpu, f"${hourly:.2f}", f"{cpp*100:.3f}¢")
        con.print(tbl); con.print()
    _save(con, "04-cloud-cost", "org-llm cloud --cost")


def scene_models_discover():
    from rich.table import Table
    con = _new_console(width=110)
    con.rule("[lcars1]FOSS LLM Catalog[/lcars1]")
    con.print("◀ Hardware: 8GB RAM (CPU)  |  showing models that fit + all others", style="info")
    con.print()
    fits = {m.tag for m in fitting_hardware(None, 8.0)}
    tbl = Table(box=None, pad_edge=False, show_header=True)
    tbl.add_column("Fits", width=4)
    tbl.add_column("Model",   style="lcars2", no_wrap=True)
    tbl.add_column("Params",  width=6, style="dim")
    tbl.add_column("VRAM",    width=6, style="lcars3")
    tbl.add_column("License", style="dim", no_wrap=True)
    tbl.add_column("Roles",   style="lcars1", no_wrap=True)
    tbl.add_column("Description")
    # Show first 14 entries to keep SVG height reasonable
    for m in CATALOG[:14]:
        fit_sym = "[bold green]✓[/]" if m.tag in fits else "[dim]→cloud[/]"
        tbl.add_row(fit_sym, m.tag, m.params, f"{m.vram_gb:.1f}G",
                    m.license, " ".join(m.roles), m.description)
    con.print(tbl)
    _save(con, "05-models-discover", "org-llm models --discover")


def scene_report_overview():
    """Render report overview against a temp seeded DB."""
    tmpdb = Path(tempfile.mkdtemp()) / "g.db"
    engine = make_engine(tmpdb)
    init_db(engine)
    _populate(engine)

    con = _new_console(width=84)
    saved_ui = ui.console
    saved_rep = report_mod.console
    ui.console = con
    report_mod.console = con
    try:
        with get_session(engine) as s:
            con.rule(report_mod.REPORT_HEADER)
            report_mod.report_overview(s)
            report_mod.report_top_tags(s)
    finally:
        ui.console = saved_ui
        report_mod.console = saved_rep
    _save(con, "06-report-all", "org-llm report all")


def scene_tutor_welcome():
    con = _new_console(width=80)
    body = next(b for n, b in _TUTOR_STEPS if n == "welcome")
    con.print(Panel(body, title="[lcars1]1/24  welcome[/lcars1]",
                    border_style="lcars2", padding=(1, 2)))
    _save(con, "07-tutor-welcome", "org-llm tutor welcome")


def scene_creds_status():
    from rich.table import Table
    con = _new_console(width=80)
    con.rule("[lcars1]Cloud Credentials  ·  pass[/lcars1]")
    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key", style="lcars1", width=20)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("pass installed",    "[green]yes[/green]")
    tbl.add_row("store initialized", "[green]yes[/green]")
    tbl.add_row("store path",        "/home/daniel/.password-store")
    tbl.add_row("gpg key id",        "9F4BC0AAE32B98A2")
    con.print(Panel(tbl, title="[lcars1]pass[/lcars1]", border_style="lcars2"))
    con.print()

    tbl2 = Table(title="Stored secrets (org-llm/*)", box=None, pad_edge=False)
    tbl2.add_column("Slug", style="lcars3")
    for s in ["org-llm/anthropic/api-key",
              "org-llm/cloud/openrouter/api-key",
              "org-llm/cloud/runpod/api-key"]:
        tbl2.add_row(s)
    con.print(tbl2)
    _save(con, "08-creds", "org-llm cloud --creds")


def scene_review_emacs_panel():
    con = _new_console(width=92)
    sample = """## Summary
Doom Emacs config focused on org-roam, vterm, and a custom org-llm integration.
Compact (~6KB config.el) with one custom module under lisp/.

## Strengths
- `config.el:18` — clean `setq doom-font` block, no font-spec eval at load time.
- `packages.el:24` — pinned packages declared explicitly, no surprises on sync.
- `lisp/org-llm.el` — keybindings live behind `(after! …)` so Doom load order is respected.

## Issues
- `config.el:42` — `(global-set-key (kbd "C-c o") …)` shadows Doom's leader binding.
  Migrate to `map!` for consistency.
- `init.el:88` — `:term vterm` enabled but `vterm-toggle` not pinned in packages.el.

## Improvements (ordered by impact)
1. Replace `(global-set-key …)` with Doom's `map!` macro across config.el (~5 sites).
2. Add `(setq gc-cons-threshold (* 64 1024 1024))` in early-init.el — startup -180ms.
3. Pin `vterm-toggle` in packages.el; current build pulls latest from MELPA.

## Optional polish
- Move `lisp/org-llm.el` into `~/.config/doom/modules/private/org-llm/` to follow
  Doom module conventions; lets you `(:private org-llm)` in init.el."""
    con.print()
    con.rule("[lcars1]Emacs config review  ·  deepseek-r1  ·  focus: all[/lcars1]")
    con.print(Panel(sample, border_style="lcars2", padding=(1, 2)))
    con.rule()
    _save(con, "09-review-emacs", "org-llm review-emacs")


def _splash_console_with_colorway(colorway: dict[str, str], width: int = 100):
    """Build a fresh recording Console where the lcars1/2/3 styles are
    overridden to a custom palette — used by the LCARS color-variant
    scenes (red, green, gold, etc.) so we don't have to fork the
    splash render code."""
    from rich.console import Console
    from rich.theme   import Theme
    base = dict(ui.PALETTE)
    base.update(colorway)
    # Mirror ui._build_theme so every lcars-flavored style keeps its
    # bold weight when we override the underlying color.
    theme = Theme({
        "info":         f"bold {base['info.cyan']}",
        "success":      f"bold {base['pride.green']}",
        "warn":         f"bold {base['warn.yellow']}",
        "error":        f"bold {base['pride.red']}",
        "dim":          f"dim {base['dim']}",
        "lcars1":       f"bold {base['lcars1']}",
        "lcars2":       f"bold {base['lcars2']}",
        "lcars3":       f"bold {base['lcars3']}",
        "pride.red":    f"bold {base['pride.red']}",
        "pride.orange": f"bold {base['pride.orange']}",
        "pride.yellow": f"bold {base['pride.yellow']}",
        "pride.green":  f"bold {base['pride.green']}",
        "pride.blue":   f"bold {base['pride.blue']}",
        "pride.violet": f"bold {base['pride.violet']}",
        "doom.cyan":    f"bold {base.get('doom.cyan', '#46d9ff')}",
        "doom.magenta": f"bold {base.get('doom.magenta', '#c678dd')}",
        "doom.green":   f"bold {base.get('doom.green',   '#98be65')}",
        "doom.red":     f"bold {base.get('doom.red',     '#ff6c6b')}",
        "doom.orange":  f"bold {base.get('doom.orange',  '#da8548')}",
        "doom.yellow":  f"bold {base.get('doom.yellow',  '#ecbe7b')}",
    })
    return Console(theme=theme, record=True, force_terminal=True,
                    width=width, color_system="truecolor")


def _scene_splash_with_colorway(slug: str, title: str, colorway: dict[str, str]):
    """Render the splash with a custom LCARS palette and save."""
    from org_llm.cli import _render_splash_logo
    con = _splash_console_with_colorway(colorway, width=100)
    con.print(_render_splash_logo())
    _save(con, slug, title)


def scene_splash_lcars_red():
    """LCARS red colorway — red alert / battle stations vibe."""
    _scene_splash_with_colorway(
        "20-splash-lcars-red",
        "org-llm splash — LCARS red colorway",
        {
            "lcars1":       "#FF3B30",   # red alert
            "lcars2":       "#FF8C7A",   # salmon
            "lcars3":       "#FFD60A",   # warning amber
            "pride.yellow": "#FFD60A",
            "pride.orange": "#FF9500",
        },
    )


def scene_splash_lcars_green():
    """LCARS green colorway — Voyager-era astrometrics vibe."""
    _scene_splash_with_colorway(
        "21-splash-lcars-green",
        "org-llm splash — LCARS green colorway",
        {
            "lcars1":       "#34C759",   # green
            "lcars2":       "#5AC8FA",   # sky blue
            "lcars3":       "#FFD60A",   # gold
            "pride.yellow": "#FFD60A",
            "pride.orange": "#A2C56A",
        },
    )


def scene_splash_lcars_gold():
    """LCARS gold/amber colorway — Operations / engineering panels."""
    _scene_splash_with_colorway(
        "22-splash-lcars-gold",
        "org-llm splash — LCARS gold colorway",
        {
            "lcars1":       "#FFD60A",   # gold
            "lcars2":       "#FF9500",   # warm amber
            "lcars3":       "#FF3B30",   # red accent
            "pride.yellow": "#FFE066",
            "pride.orange": "#FF9500",
        },
    )


def scene_splash_lcars_violet():
    """LCARS violet/magenta colorway — Sciences / medbay panels."""
    _scene_splash_with_colorway(
        "23-splash-lcars-violet",
        "org-llm splash — LCARS violet colorway",
        {
            "lcars1":       "#BF5AF2",   # violet
            "lcars2":       "#FF6B9D",   # magenta
            "lcars3":       "#5AC8FA",   # sky blue
            "pride.yellow": "#FFD60A",
            "pride.orange": "#D982E0",
        },
    )


def scene_splash_lcars_classic():
    """LCARS classic colorway — the default orange/purple/blue."""
    _scene_splash_with_colorway(
        "24-splash-lcars-classic",
        "org-llm splash — LCARS classic (default) colorway",
        {
            "lcars1":       "#FF9900",
            "lcars2":       "#CC88FF",
            "lcars3":       "#4488FF",
        },
    )


def scene_palette_picker():
    """`org-llm palette` — text-based picker showing all 5 LCARS palettes."""
    from rich.table import Table
    con = _new_console(width=100)
    tbl = Table(box=None, pad_edge=False, show_header=True,
                  header_style="lcars1")
    tbl.add_column("",            width=2)
    tbl.add_column("Name",        style="lcars2", no_wrap=True, width=10)
    tbl.add_column("Swatch",      no_wrap=True, width=24)
    tbl.add_column("Description", style="dim")
    bundles = [
        ("classic", "#FF9900", "#CC88FF", "#4488FF",
            "Canonical TNG — orange · purple · blue", True),
        ("red",     "#FF3B30", "#FF8C7A", "#FFD60A",
            "Red alert — red · salmon · amber", False),
        ("green",   "#34C759", "#5AC8FA", "#FFD60A",
            "Voyager astrometrics — green · sky · gold", False),
        ("gold",    "#FFD60A", "#FF9500", "#FF3B30",
            "Operations / engineering — gold · amber · red", False),
        ("violet",  "#BF5AF2", "#FF6B9D", "#5AC8FA",
            "Sciences / medbay — violet · magenta · sky", False),
    ]
    for name, c1, c2, c3, desc, is_active in bundles:
        marker = "◉" if is_active else "◯"
        swatch = (f"[{c1}]████████[/{c1}]"
                   f"[{c2}]████████[/{c2}]"
                   f"[{c3}]████████[/{c3}]")
        tbl.add_row(marker, name, swatch, desc)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]LCARS palettes[/lcars1]  "
                            "[dim]active: classic[/dim]",
                      border_style="lcars2", padding=(1, 1)))
    con.print()
    con.print("▶ [dim]Pick:[/dim]   [bold]org-llm palette <name>[/bold]")
    con.print("▶ [dim]Tweak:[/dim]  [bold]org-llm palette <name> "
              "--primary '#RRGGBB'[/bold]")
    con.print("▶ [dim]Reset:[/dim]  [bold]org-llm palette reset[/bold]")
    _save(con, "25-palette-picker", "org-llm palette — picker")


def scene_knob_list():
    """`org-llm knob list` — pluggable theme-knob registry view."""
    from rich.table import Table
    con = _new_console(width=100)
    tbl = Table(box=None, pad_edge=False, show_header=True,
                  header_style="lcars1")
    tbl.add_column("Knob",        style="lcars1", no_wrap=True, width=14)
    tbl.add_column("Source",      style="dim", width=9)
    tbl.add_column("Active level", style="lcars2", width=12)
    tbl.add_column("Pool size",   style="lcars3", justify="right", width=9)
    tbl.add_column("Description", style="dim")
    rows = [
        ("commie",     "built-in",  "[lcars1]3 (default)[/lcars1]", "16",
         "Solidarity / mutual aid / collective / abolition"),
        ("trek",       "built-in",  "[lcars1]2 (default)[/lcars1]", "19",
         "Star Trek references — LCARS readouts, warp, stardates"),
        ("queer",      "built-in",  "[lcars1]2 (default)[/lcars1]", "12",
         "Queer / trans / pride references"),
        ("synthwave",  "user",      "[lcars2]3 (env)[/lcars2]",    "11",
         "Neon · VHS · 1980s · dusk light"),
        ("cottagecore","user",      "[dim]2 (default)[/dim]",      " 7",
         "Sourdough · linen · garden · slow living"),
        ("dinosaur",   "user",      "[dim]0 (silent)[/dim]",       " 4",
         "Roar · prehistoric · jurassic"),
    ]
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]Theme knobs[/lcars1]  "
                            "[dim](built-in + user-defined; pool feeds the "
                            "LLM-driven theme gate)[/dim]",
                      border_style="lcars2", padding=(1, 1)))
    con.print()
    con.print("▶ [bold]org-llm knob edit synthwave[/bold]   "
              "[dim]edit pool in $EDITOR[/dim]")
    con.print("▶ [bold]org-llm knob add NAME --llm --vibe '...'[/bold]   "
              "[dim]LLM-generate a new knob bundle[/dim]")
    _save(con, "26-knob-list", "org-llm knob list")


def scene_doctor_power_boost():
    """`org-llm doctor --power-boost` — RAM-fit suggestion + cloud route."""
    from rich.table import Table
    con = _new_console(width=100)
    body = (
        "[lcars1]Hardware probe[/lcars1]\n"
        "  Total RAM:     16.0 GB\n"
        "  Free RAM:       3.2 GB    [yellow]△ tight[/yellow]\n"
        "  GPU VRAM:       (none — CPU inference)\n"
        "  Active model:  [lcars2]gemma3:12b[/lcars2]  (8.1 GB)\n\n"
        "[lcars1]Verdict[/lcars1]\n"
        "  [yellow]downsize[/yellow]   active model exceeds free RAM by 4.9 GB; "
        "swap is likely\n"
        "             paging — explains the 18s response times you've\n"
        "             been seeing.\n\n"
        "[lcars1]Recommendation[/lcars1]\n"
        "  Local swap →  [bold lcars2]llama3.2:3b[/bold lcars2] "
        "[dim](2.0 GB, fits with 1.2 GB headroom)[/dim]\n"
        "  Cloud route → [bold lcars2]anthropic/claude-haiku-4-5[/bold lcars2] "
        "[dim](~$0.0008/req)[/dim]\n\n"
        "[lcars3]Apply now?[/lcars3]\n"
        "  [bold]org-llm doctor --power-boost --apply[/bold]    "
        "[dim](writes chat_model)[/dim]\n"
        "  [bold]org-llm cloud --route anthropic[/bold]        "
        "[dim](one-shot)[/dim]"
    )
    con.print()
    con.print(Panel(body,
                      title="[lcars1]doctor --power-boost[/lcars1]  "
                            "[dim]proactive RAM-fit probe[/dim]",
                      border_style="lcars2", padding=(1, 2)))
    _save(con, "27-doctor-power-boost", "org-llm doctor --power-boost")


def scene_self_snapshot():
    """`org-llm self snapshot` — bundle source + DB + rollback.sh."""
    from rich.table import Table
    con = _new_console(width=100)
    body = (
        "[lcars3]✓[/lcars3] Snapshot bundled to "
        "[bold]~/.local/share/org-llm/snapshots/2026-04-27T22-31-04Z/[/bold]\n\n"
        "[lcars1]Bundle contents[/lcars1]\n"
        "  src.tar.zst         13.4 KB   org_llm/ at HEAD c90b6e2\n"
        "  org-llm.db.zst      4.7 MB    full DB (config + history + "
        "embeddings)\n"
        "  rollback.sh         2.1 KB    standalone restore (no Python "
        "dep — bash + tar)\n"
        "  manifest.json       0.9 KB    sha256 + counts + git rev\n\n"
        "[lcars1]Tagged[/lcars1]   pre-rescue-test\n"
        "[lcars1]Triggered by[/lcars1]   self llm-revise — about to patch "
        "[bold]search.py[/bold]\n\n"
        "[lcars3]Roll back[/lcars3]\n"
        "  [bold]org-llm self rollback pre-rescue-test[/bold]   "
        "[dim](this snapshot)[/dim]\n"
        "  [bold]org-llm self rollback[/bold]                   "
        "[dim](most recent)[/dim]\n"
        "  [bold]bash ~/.local/share/org-llm/snapshots/.../rollback.sh[/bold]\n"
        "                                              "
        "[dim](no org-llm needed)[/dim]\n\n"
        "[dim]Snapshot list:[/dim] [bold]org-llm self snapshots[/bold]"
    )
    con.print()
    con.print(Panel(body,
                      title="[lcars1]self snapshot[/lcars1]  "
                            "[dim]source + DB + standalone rollback bundle[/dim]",
                      border_style="lcars2", padding=(1, 2)))
    _save(con, "28-self-snapshot", "org-llm self snapshot")


def scene_pi_status():
    """`org-llm pi --status` — Pi bridge registration + tool count."""
    from rich.table import Table
    con = _new_console(width=100)
    rows = [
        ("Pi binary",        "✓",
         "/home/u/.pi/bin/pi  v0.18.4"),
        ("Bridge installed", "✓",
         "~/.pi/extensions/pi-org-llm/  (40+ org_llm_* tools)"),
        ("Auto-load",        "✓",
         "~/.pi/config.json registers org-llm at boot"),
        ("MCP subprocess",   "✓",
         "spawned on session start, terminated cleanly on exit"),
        ("Tool registration","✓",
         "40 MCP tools → 40 Pi tools (org_llm_search_notes, …)"),
        ("Persona injection","✓",
         "before_agent_start hook adds search-first system prompt"),
        ("Theme parity",     "✓",
         "uses opencode_persona_intro / mcp_*_suffix from theme_studio"),
    ]
    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Check",  style="lcars2", width=20, no_wrap=True)
    tbl.add_column("Status", style="lcars1", width=4)
    tbl.add_column("Detail", style="dim")
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]pi --status[/lcars1]  "
                            "[dim]bridge to pi.dev — third conversational "
                            "interface[/dim]",
                      border_style="lcars2", padding=(1, 2)))
    con.print()
    con.print("▶ [bold]org-llm pi[/bold]              "
              "[dim]start a Pi session with org-llm tools loaded[/dim]")
    con.print("▶ [bold]org-llm pi --reinstall[/bold]  "
              "[dim]rebuild the bridge from the bundled TypeScript[/dim]")
    _save(con, "29-pi-status", "org-llm pi --status")


def scene_splash():
    """LCARS splash menu — the default no-args view (Doom-Emacs-style)."""
    from rich.columns import Columns
    con = _new_console(width=110)
    from org_llm.cli import _render_splash_logo, _SPLASH_MENU
    con.print(_render_splash_logo())
    groups: dict[str, list] = {}
    for key, verb, label, group in _SPLASH_MENU:
        groups.setdefault(group, []).append((key, verb, label))
    panels = []
    for group_name in ("Query", "Write", "Workspaces", "Insight",
                          "Maintenance", "Config", "Help"):
        items = groups.get(group_name) or []
        if not items: continue
        body = "\n".join(
            f"  [lcars1]{key:>2}[/lcars1]  [lcars2]{verb:<22}[/lcars2] "
            f"[dim]{label}[/dim]"
            for key, verb, label in items)
        panels.append(Panel(body,
                              title=f"[lcars1]{group_name}[/lcars1]",
                              border_style="lcars2", padding=(0, 1)))
    con.print(Columns(panels, equal=False, expand=False))
    con.print()
    con.print("▶ [dim]Pick a verb above —[/dim] [bold]org-llm <verb>[/bold]  "
              "or [bold]org-llm --help[/bold] for the full list.")
    _save(con, "10-splash", "org-llm — default splash menu")


def scene_askbook():
    """Multi-model askbook — same question to chat / reason / cloud."""
    from rich.table import Table
    con = _new_console(width=110)
    tbl = Table(box=None, pad_edge=False)
    tbl.add_column("When",    style="lcars1", no_wrap=True, width=19)
    tbl.add_column("Backend", style="lcars3", width=8)
    tbl.add_column("Model",   style="dim",   width=18)
    tbl.add_column("Status",  width=8)
    tbl.add_column("Title",   style="lcars2")
    rows = [
        ("2026-04-27T09:12:03", "chat",   "gemma3",       "[green]done[/green]",
         "How does dbt fit org-llm?"),
        ("2026-04-27T09:12:08", "reason", "deepseek-r1",  "[green]done[/green]",
         "Plan the next refactor of mcp_server.py"),
        ("2026-04-27T09:13:21", "cloud",  "gpt-oss-20b",  "[green]done[/green]",
         "Second opinion on the dbt model design"),
        ("2026-04-27T09:14:02", "fast",   "phi3.5",       "[green]done[/green]",
         "Tag suggestions for synthwave note"),
        ("2026-04-27T09:15:44", "claude", "(claude code)","[yellow]pending[/yellow]",
         "Review the Pi extension for bugs"),
        ("2026-04-27T09:16:00", "pi",     "(pi default)", "[yellow]pending[/yellow]",
         "What would a 16th-extension look like?"),
    ]
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]askbook[/lcars1]  "
                            "[dim](6 entries — chat, reason, fast, code, "
                            "text, cloud, claude, pi)[/dim]",
                      border_style="lcars2", padding=(1, 1)))
    con.print()
    con.print("▶ Add: [bold]org-llm askbook add 'your question' "
              "--backend reason[/bold]")
    con.print("▶ Run pending: [bold]org-llm askbook run[/bold]  "
              "│  Open: [bold]~/org/llm-askbook.org[/bold]")
    _save(con, "11-askbook", "org-llm askbook — multi-model Q/A")


def scene_models_dashboard():
    """The default `org-llm models` view: assignments + auto-suggestions."""
    from rich.table import Table
    con = _new_console(width=110)
    con.rule("[lcars1]Model Assignments[/lcars1]  "
              "[dim](hardware: 15 GB RAM (CPU))[/dim]")
    tbl = Table(box=None, pad_edge=False)
    tbl.add_column("Role",    style="lcars1",  no_wrap=True)
    tbl.add_column("Model",   style="lcars2",  no_wrap=True)
    tbl.add_column("Purpose", style="dim")
    tbl.add_column("VRAM",    style="lcars3", width=6, no_wrap=True)
    tbl.add_column("Pulled",  width=8, no_wrap=True)
    tbl.add_column("Fits",    width=6, no_wrap=True)
    rows = [
        ("embed",    "nomic-embed-text", "Semantic search embeddings",
         "0.3G", "[green]✓[/green]", "[green]✓[/green]"),
        ("chat",     "gemma3",            "ask / general Q&A",
         "3.3G", "[green]✓[/green]", "[green]✓[/green]"),
        ("code",     "qwen2.5-coder",     "Code generation",
         "4.7G", "[green]✓[/green]", "[green]✓[/green]"),
        ("reason",   "deepseek-r1",       "Planning & complex reasoning",
         "5.2G", "[green]✓[/green]", "[green]✓[/green]"),
        ("fast",     "phi3.5",            "Tagging & classification",
         "2.2G", "[green]✓[/green]", "[green]✓[/green]"),
        ("instruct", "mistral-nemo",      "Capture & instruction following",
         "7.1G", "[green]✓[/green]", "[red]✗[/red]"),
        ("text",     "gemma3",            "Summarization & text analysis",
         "3.3G", "[green]✓[/green]", "[green]✓[/green]"),
    ]
    for r in rows: tbl.add_row(*r)
    con.print(tbl)
    con.print()
    sug = Table(box=None, pad_edge=False)
    sug.add_column("Role",      style="lcars1")
    sug.add_column("Current",   style="dim")
    sug.add_column("→",         width=2)
    sug.add_column("Suggested", style="lcars2")
    sug.add_column("VRAM",      style="lcars3", width=6)
    sug.add_column("Why",       style="dim")
    sug.add_row("instruct",
                "mistral-nemo",
                "[bold yellow]↓[/bold yellow]",
                "llama3.2:3b",
                "2.0G",
                "current too big for 4 GB free RAM")
    con.print(Panel(sug, title="[lcars1]Suggestions[/lcars1]",
                      border_style="lcars2", padding=(1, 1)))
    con.print()
    con.print("▶ Apply one:   [bold]org-llm models --set instruct=llama3.2:3b[/bold]")
    con.print("▶ Apply all:   [bold]org-llm models --tune --apply[/bold]")
    _save(con, "12-models-dashboard", "org-llm models — dashboard")


def scene_captains_log():
    """Captain's Log — recent events table + reflect digest."""
    from rich.table import Table
    con = _new_console(width=110)
    tbl = Table(box=None, pad_edge=False)
    tbl.add_column("When",     style="lcars1", no_wrap=True, width=19)
    tbl.add_column("Kind",     style="lcars3", width=10)
    tbl.add_column("Command",  style="lcars2", no_wrap=True, width=22)
    tbl.add_column("Model",    style="dim", width=14)
    tbl.add_column("ms",       style="lcars3", width=6, justify="right")
    tbl.add_column("Outcome",  width=8)
    rows = [
        ("2026-04-27T09:12:03", "cli",   "ask",            "gemma3",        "1842", "[green]ok[/green]"),
        ("2026-04-27T09:12:21", "llm",   "chat",           "gemma3",        "1521", "[green]ok[/green]"),
        ("2026-04-27T09:13:01", "mcp",   "search_notes",   "(no llm)",      "  47", "[green]ok[/green]"),
        ("2026-04-27T09:13:22", "cli",   "models --tune",  "(no llm)",      "  18", "[green]ok[/green]"),
        ("2026-04-27T09:14:11", "cloud", "cloud_chat",     "gpt-oss-20b",   "2104", "[green]ok[/green]"),
        ("2026-04-27T09:14:30", "mcp",   "proactive_doctor","(no llm)",     " 102", "[green]ok[/green]"),
        ("2026-04-27T09:15:12", "cli",   "tag",            "phi3.5",        " 833", "[green]ok[/green]"),
        ("2026-04-27T09:16:55", "cli",   "embed",          "nomic-embed",   "  91", "[red]err[/red]"),
    ]
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]Captain's Log[/lcars1]  "
                            "[dim](recent — DB ↔ ~/org/captains-log.org)[/dim]",
                      border_style="lcars2", padding=(1, 1)))
    digest = (
        "[lcars1]Patterns[/lcars1]\n"
        "  • [lcars2]ask[/lcars2] is the dominant verb (38% of last 50 events) — "
        "consider warming the chat model on launch.\n"
        "  • [lcars2]embed[/lcars2] failed once after a long idle gap — "
        "Ollama process likely OOM-killed; suggest [bold]watch[/bold].\n"
        "  • [lcars2]cloud_chat[/lcars2] used 4× this hour — "
        "switch to local [bold]reason[/bold] role for cost savings?\n\n"
        "[lcars1]Suggestions[/lcars1]\n"
        "  ▶ [bold]org-llm watch[/bold] — start the background auto-embedder\n"
        "  ▶ [bold]org-llm models --set chat=gemma3:12b[/bold] — bigger fits"
    )
    con.print()
    con.print(Panel(digest,
                      title="[lcars1]log --reflect[/lcars1]  "
                            "[dim](LLM analysis of recent events)[/dim]",
                      border_style="lcars3", padding=(1, 2)))
    _save(con, "13-captains-log", "org-llm log — Captain's Log + reflect")


def scene_literate_config():
    """Literate config tangle — round-trippable org file preview."""
    con = _new_console(width=110)
    body = (
        "[dim]# ~/org/org-llm-config.org  (generated by `org-llm config --tangle`)[/dim]\n"
        "[lcars1]#+TITLE: org-llm — literate config[/lcars1]\n"
        "[lcars1]#+OPTIONS: toc:nil[/lcars1]\n"
        "\n"
        "[lcars2]* chat_model[/lcars2]\n"
        "  :PROPERTIES:\n"
        "  :ENV:        ORG_LLM_CHAT_MODEL\n"
        "  :SOURCE:     env\n"
        "  :END:\n"
        "  Model used for [lcars3]ask[/lcars3], capture, and free-form chat.\n"
        "  [dim]#+begin_src text :tangle ~/.local/share/org-llm/cfg/chat_model[/dim]\n"
        "  qwen2.5:14b\n"
        "  [dim]#+end_src[/dim]\n"
        "\n"
        "[lcars2]* doctor_proactive_mode[/lcars2]\n"
        "  :PROPERTIES:\n"
        "  :ENV:        ORG_LLM_DOCTOR_PROACTIVE_MODE\n"
        "  :SOURCE:     config\n"
        "  :ALLOWED:    off | passive | active | aggressive\n"
        "  :END:\n"
        "  How aggressively the doctor self-invokes from inside opencode.\n"
        "  [dim]#+begin_src text :tangle ~/.local/share/org-llm/cfg/doctor_proactive_mode[/dim]\n"
        "  active\n"
        "  [dim]#+end_src[/dim]\n"
        "\n"
        "[lcars2]** Knob: trek_level[/lcars2]\n"
        "  :PROPERTIES:\n"
        "  :ENV:        ORG_LLM_TREK_LEVEL\n"
        "  :SOURCE:     env\n"
        "  :END:\n"
        "  *** msg 0 [neutral]   :: \"Engaging.\"\n"
        "  *** msg 1 [warm]      :: \"Engaging warp drive.\"\n"
        "  *** msg 2 [festive]   :: \"Engage. Make it so. Tea, Earl Grey, hot.\"\n"
        "  *** msg 3 [maximal]   :: \"To boldly go where no comrade has gone before.\"\n"
    )
    con.print()
    con.print(Panel(body,
                      title="[lcars1]config --tangle  →  ~/org/org-llm-config.org[/lcars1]",
                      border_style="lcars2", padding=(1, 2)))
    con.print()
    con.print("▶ Round-trip: [bold]org-llm config --apply-from-org[/bold]  "
              "│  Diff: [bold]config --diff-org[/bold]")
    _save(con, "14-literate-config", "org-llm config --tangle")


def scene_dbt_status():
    """`org-llm dbt status` — model freshness + lineage health."""
    from rich.table import Table
    con = _new_console(width=110)
    tbl = Table(box=None, pad_edge=False)
    tbl.add_column("Model",      style="lcars2", no_wrap=True, width=22)
    tbl.add_column("Layer",      style="lcars1", width=10)
    tbl.add_column("Rows",       style="lcars3", justify="right", width=8)
    tbl.add_column("Last build", style="dim", no_wrap=True, width=19)
    tbl.add_column("Tests",      width=10)
    tbl.add_column("Status",     width=10)
    rows = [
        ("stg_files",         "staging", "1842",  "2026-04-27T08:00:00",
         "[green]4/4[/green]",   "[green]fresh[/green]"),
        ("stg_nodes",         "staging", "23104", "2026-04-27T08:00:01",
         "[green]6/6[/green]",   "[green]fresh[/green]"),
        ("stg_history",       "staging", "987",   "2026-04-27T08:00:02",
         "[green]3/3[/green]",   "[green]fresh[/green]"),
        ("nodes_by_tag",      "marts",   "412",   "2026-04-27T08:00:03",
         "[green]2/2[/green]",   "[green]fresh[/green]"),
        ("recent_nodes",      "marts",   "318",   "2026-04-27T08:00:04",
         "[green]1/1[/green]",   "[green]fresh[/green]"),
        ("cli_invocations",   "marts",   "742",   "2026-04-27T08:00:05",
         "[green]2/2[/green]",   "[green]fresh[/green]"),
        ("llm_calls",         "marts",   "245",   "2026-04-27T08:00:06",
         "[green]2/2[/green]",   "[green]fresh[/green]"),
        ("recent_activity",   "marts",   " 60",   "2026-04-26T22:14:00",
         "[yellow]0/0[/yellow]", "[yellow]stale[/yellow]"),
    ]
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]dbt status[/lcars1]  "
                            "[dim](~/.local/share/org-llm/dbt/  ·  dbt-sqlite)[/dim]",
                      border_style="lcars2", padding=(1, 1)))
    con.print()
    con.print("▶ Build all:    [bold]org-llm dbt build[/bold]")
    con.print("▶ Lessons:      [bold]org-llm dbt lessons --level intro[/bold]")
    con.print("▶ LLM design:   [bold]org-llm dbt design 'sessions per day from history'[/bold]")
    _save(con, "15-dbt-status", "org-llm dbt status")


def scene_llm_rescue():
    """LLM rescue — uncaught exception → diagnosis → optional self-rewrite."""
    con = _new_console(width=110)
    err = (
        "[red]Traceback (most recent call last):[/red]\n"
        "  File \"org_llm/cli.py\", line 4821, in ask\n"
        "    rows = search.find(query, top_k=top_k)\n"
        "  File \"org_llm/search.py\", line 113, in find\n"
        "    vec = embed_one(query, model=embed_model, base_url=url)\n"
        "  File \"org_llm/llm.py\", line 87, in embed_one\n"
        "    raise ConnectionError(f\"ollama unreachable at {base_url}\")\n"
        "[red]ConnectionError: ollama unreachable at http://127.0.0.1:11434[/red]"
    )
    diag = (
        "[lcars1]What happened[/lcars1]\n"
        "  Embed call failed because Ollama isn't running. Every search /\n"
        "  ask path needs a live embed model — they all fan out from here.\n\n"
        "[lcars1]Likely cause[/lcars1]\n"
        "  Ollama daemon is stopped or the URL in config is wrong.\n\n"
        "[lcars1]Fix (try in order)[/lcars1]\n"
        "  1. [bold]systemctl --user start ollama[/bold]   — start the daemon\n"
        "  2. [bold]org-llm doctor --fix[/bold]             — auto-detect + start\n"
        "  3. [bold]org-llm config get ollama_url[/bold]    — verify URL\n\n"
        "[lcars1]Self-rewrite available[/lcars1]\n"
        "  This error path could fall back to keyword search when embed is\n"
        "  unreachable. Apply? [bold][y]es[/bold] / [bold][n]o[/bold] / [bold][d]iff[/bold]\n"
        "  [dim](snapshot taken — auto-rollback on test failure)[/dim]"
    )
    con.print()
    con.print(Panel(err,
                      title="[red]Uncaught exception[/red]",
                      border_style="red", padding=(1, 2)))
    con.print()
    con.print(Panel(diag,
                      title="[lcars1]LLM rescue  ·  gemma3[/lcars1]  "
                            "[dim](1.4 s)[/dim]",
                      border_style="lcars2", padding=(1, 2)))
    _save(con, "16-llm-rescue", "org-llm LLM rescue + self-rewrite")


def scene_theme_studio_show():
    """`theme-studio show` — registry of themed surfaces with active values."""
    from rich.table import Table
    con = _new_console(width=110)
    tbl = Table(box=None, pad_edge=False, show_header=True)
    tbl.add_column("Surface",  style="lcars2", no_wrap=True, width=30)
    tbl.add_column("Default",  style="dim",    width=34)
    tbl.add_column("Active (cache)",   style="lcars1")
    rows = [
        ("splash_subtitle",
         "your second brain, scripted",
         "Engage warp 9 — second brain online, captain."),
        ("splash_slogan",
         "solidarity · collective · local · free",
         "from each · to each · stardate ahead · free"),
        ("setup_panel_title",
         "🚀  Setup needed",
         "🛰  Subspace handshake required"),
        ("doctor_all_green",
         "All systems nominal.",
         "All decks green, captain. The bridge is calm."),
        ("captains_log_panel_title",
         "Captain's Log",
         "Captain's Log — Stardate 79213.4"),
        ("models_panel_title",
         "Model Assignments",
         "LCARS Crew Manifest — Models in Service"),
        ("ask_retrieving",
         "Retrieving notes…",
         "Subspace pull from your archive…"),
        ("opencode_greeting",
         "Hailing frequencies open. Org-llm at your service.",
         "Hailing frequencies open. Org-llm reporting from "
         "the bridge of your second brain — make it so."),
        ("mcp_tool_success_suffix",
         "↳ done.",
         "↳ make it so"),
        ("mcp_tool_error_suffix",
         "↳ red alert",
         "↳ shields buckling — red alert"),
    ]
    for r in rows:
        tbl.add_row(*r)
    con.print()
    con.print(Panel(tbl,
                      title="[lcars1]theme-studio show[/lcars1]  "
                            "[dim]Active dials: commie=3 trek=2[/dim]",
                      border_style="lcars2", padding=(0, 1)))
    con.print()
    con.print("▶ [bold]org-llm theme-studio regenerate[/bold]  "
              "[dim]repopulate the cache[/dim]")
    con.print("▶ [bold]org-llm theme-studio verify[/bold]      "
              "[dim]re-gate every cached variant[/dim]")
    _save(con, "18-theme-studio-show", "org-llm theme-studio show")


def scene_theme_studio_verify():
    """`theme-studio verify` — quality gate pass/fail per variant."""
    from rich.table import Table
    con = _new_console(width=110)
    con.rule("[lcars1]Levels: commie=3, trek=2[/lcars1]")

    def _row(passed, variant, reason=""):
        return ("[green]✓[/green]" if passed else "[red]✗[/red]",
                variant, reason)

    panels = []
    for skey, results, n_pass, n_total in [
        ("splash_subtitle", [
            _row(True,  "Engage warp 9 — second brain online, captain."),
            _row(True,  "Make it so: your subspace knowledge graph awaits."),
            _row(True,  "Federation-grade memory, comradely warp 9 ops."),
            _row(False, "your second brain, scripted",
                  "no theme keyword from pool (24 options)"),
            _row(False, "AI productivity, simplified.",
                  "forbidden phrase: 'ai'"),
            _row(True,  "Captain on bridge — solidarity through the deltas."),
        ], 4, 6),
        ("opencode_greeting", [
            _row(True,  "Hailing frequencies open. Org-llm reporting "
                          "from the bridge of your second brain — make it so."),
            _row(True,  "Captain on deck. LCARS coupled. Mutual aid in the "
                          "comms array. What can I dig out for you?"),
            _row(True,  "Worf would be proud — your archive is fortified. "
                          "Solidarity. Standing by."),
            _row(False, "Hello! How can I assist you today?",
                  "no theme keyword from pool (24 options)"),
        ], 3, 4),
    ]:
        tbl = Table(box=None, pad_edge=False, show_header=True)
        tbl.add_column("✓",       width=2)
        tbl.add_column("Variant", style="lcars2")
        tbl.add_column("Reason",  style="dim")
        for r in results:
            tbl.add_row(*r)
        panels.append(Panel(tbl,
                              title=f"[lcars1]{skey}[/lcars1]  "
                                    f"[dim]{n_pass}/{n_total} pass[/dim]",
                              border_style="lcars2", padding=(0, 1)))

    for p in panels:
        con.print(p)
    con.print()
    con.print("[lcars3]Pass rate:[/lcars3] 7/10 (70%)  "
              "[dim]· retry failures with a stronger model:[/dim] "
              "[bold]theme-studio regenerate --upgrade[/bold]")
    _save(con, "19-theme-studio-verify", "org-llm theme-studio verify")


def scene_doctor_walkthrough():
    """`org-llm doctor --walkthrough` — narrated step-by-step."""
    con = _new_console(width=110)
    body = (
        "[lcars1]Step 1/6  ·  Ollama daemon[/lcars1]\n"
        "  $ pgrep -x ollama\n"
        "  [green]✓ running (pid 28471)[/green]\n\n"
        "[lcars1]Step 2/6  ·  Required local models[/lcars1]\n"
        "  $ ollama list | grep -E 'nomic-embed|gemma3|phi3.5'\n"
        "  [green]✓ nomic-embed-text  768 dim   present[/green]\n"
        "  [green]✓ gemma3            3.3 GB   present[/green]\n"
        "  [yellow]△ phi3.5            2.2 GB   not pulled — fast role unavailable[/yellow]\n\n"
        "[lcars1]Step 3/6  ·  RAM headroom vs chat model[/lcars1]\n"
        "  free RAM: 4.1 GB    chat_model: gemma3 (3.3 GB)\n"
        "  [yellow]△ tight — under 1 GB headroom; risk of OOM under context[/yellow]\n\n"
        "[lcars1]Step 4/6  ·  DB integrity[/lcars1]\n"
        "  $ sqlite3 org-llm.db 'PRAGMA integrity_check;'\n"
        "  [green]✓ ok  ·  1842 files  ·  23104 nodes  ·  98% embedded[/green]\n\n"
        "[lcars1]Step 5/6  ·  Cloud reachability (configured)[/lcars1]\n"
        "  openrouter   [green]✓ 200 OK (87 ms)[/green]\n"
        "  anthropic    [green]✓ 200 OK (134 ms)[/green]\n\n"
        "[lcars1]Step 6/6  ·  Verdict[/lcars1]\n"
        "  [green]✓ healthy with 1 warning[/green]\n"
        "  [yellow]→ pull phi3.5 to enable the fast role[/yellow]\n"
        "  [yellow]→ consider downsizing chat_model or adding swap[/yellow]\n\n"
        "[dim]Apply ALL fixes:[/dim] [bold]org-llm doctor --fix[/bold]"
    )
    con.print()
    con.print(Panel(body,
                      title="[lcars1]doctor --walkthrough[/lcars1]  "
                            "[dim](narrated end-to-end check)[/dim]",
                      border_style="lcars2", padding=(1, 2)))
    _save(con, "17-doctor-walkthrough", "org-llm doctor --walkthrough")


# ── Phase 10: life-support / sensors / EMH / Dr. Crusher with vitals ─────────

def _mock_readings():
    """Static Reading list for reproducible screenshots — no live probes,
    so the gallery stays deterministic and doesn't depend on the host's
    current battery / thermal / CPU state.
    """
    from org_llm.life_support import Reading
    return [
        Reading("battery",       99,    0.99, "99% ⚡",
                "nominal", "External power source engaged. Reserves recharging."),
        Reading("cpu",           0.46,  0.94, "load 0.46 / 8c",
                "nominal", "Sublight engines idle — capacity to spare."),
        Reading("memory",        12.5,  0.81, "12.5 / 15.5 GB free",
                "nominal", "Holodecks online. Plenty of working memory."),
        Reading("disk",          268.4, 0.60, "268.4 / 449.5 GB",
                "nominal", "Cargo bays well-stocked."),
        Reading("thermal",       66.0,  0.71, "66°C / crit 100°C",
                "nominal", "Coolant flow nominal — plenty of headroom."),
        Reading("network",       True,  1.0,  "online → 1.1.1.1",
                "nominal", "Subspace link nominal."),
        Reading("ollama",        12,    1.0,  "online · 12 models",
                "nominal", "Local LLM bays online."),
        Reading("auto_embedder", False, 1.0,  "not running",
                "nominal", "Auto-embedder dormant (foreground only)."),
    ]


def scene_life_support():
    """Life-support: 8-probe vital systems panel."""
    con = _new_console(width=110)
    from org_llm.cli import _vitals_panel
    con.print()
    con.print(_vitals_panel(_mock_readings(), "nominal"))
    _save(con, "30-life-support", "org-llm life-support")


def scene_sensors_drill():
    """Sensors --drill cpu: deep-dive view with sparkline + window stats."""
    from rich.table import Table as _T
    con = _new_console(width=110)
    head = _T.grid(padding=(0, 2))
    head.add_column(); head.add_column()
    head.add_row("[lcars1]Now[/lcars1]",
                  "load 0.46 / 8c    Sublight engines idle — capacity to spare.")
    head.add_row("[lcars1]Window[/lcars1]",
                  "min 0.92  mean 0.96  max 0.99  · 82 sample(s) over last 30min")
    head.add_row("[lcars1]Status[/lcars1]",
                  "[green]nominal[/green]=82")
    con.print()
    con.print(Panel(head,
                      title="[lcars1]sensors  · drill: cpu[/lcars1]",
                      border_style="lcars1", padding=(1, 2)))
    spark = "▆▆▆▇▇▇▇▇▇▇▇▆▆▇▇▇▇▆▆▇▇▇▇▇▆▆▇▇▆▆▇▇▇▇▆▆▇▆▇▇▇▇▆▇▇▆▇▇▆▇▆▆▇▆▆▇"
    from rich.text import Text as _Text
    con.print(Panel(_Text(spark, style="lcars3"),
                       title="[lcars1]trend (oldest left → newest right)[/lcars1]",
                       border_style="lcars2", padding=(0, 2)))
    con.print(Panel(_Text("No alert/critical readings in this window — "
                              "everything's been fine.", style="lcars3"),
                       title="[lcars1]activity correlations  · what the user "
                                "was doing during alerts[/lcars1]",
                       border_style="lcars2", padding=(1, 2)))
    _save(con, "31-sensors-drill", "org-llm sensors --drill cpu")


def scene_emh_diagnosis():
    """Emergency Medical Hologram — activation + diagnosis panels."""
    con = _new_console(width=110)
    con.print()
    con.print(Panel(
        "[lcars3]Please state the nature of the medical emergency.[/lcars3]\n"
        "[dim]EMH activated — reviewing 679 reading(s) across 8 probe(s) "
        "(24h window).[/dim]",
        title="[lcars1]Emergency Medical Hologram[/lcars1]",
        border_style="lcars2", padding=(1, 2),
    ))
    con.print(Panel(
        "Examination complete. All 8 vital systems nominal across 679 "
        "reading(s) in the last 24h window.\n\n"
        "No remediation required. The patient is — for once — in good "
        "health.",
        title="[lcars1]EMH · diagnosis[/lcars1]",
        border_style="lcars2", padding=(1, 2),
    ))
    _save(con, "32-emh-diagnosis", "org-llm ask --emh --diagnose")


def scene_doctor_with_vitals():
    """Dr. Crusher: classic doctor warnings + live vitals + LLM diagnosis."""
    from rich.table import Table
    from org_llm.cli import _vitals_panel
    con = _new_console(width=110)
    rows = [
        ("",                "[lcars1]Database[/lcars1]", ""),
        ("[bold green]✓[/]", "DB integrity",     "PRAGMA integrity_check = ok"),
        ("[bold green]✓[/]", "Index populated",  "643 files / 11340 nodes"),
        ("[bold yellow]⚠[/]", "Embeddings partial", "11332/11340 (99%) — "
                                                       "run: org-llm embed"),
        ("",                "[lcars1]Org Files[/lcars1]", ""),
        ("[bold green]✓[/]", "org files found",  "547 .org files"),
        ("[bold yellow]⚠[/]", "Unindexed files", "22 .org file(s) not yet in DB"),
        ("",                "[lcars1]Ollama[/lcars1]", ""),
        ("[bold green]✓[/]", "Ollama API",       "http://localhost:11434"),
        ("[bold green]✓[/]", "  chat_model",     "gemma3:latest"),
    ]
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("St", width=3)
    table.add_column("Check", style="lcars2")
    table.add_column("Detail", style="dim")
    for r in rows:
        table.add_row(*r)
    con.print()
    con.print(Panel(table, title="[lcars1]org-llm doctor[/lcars1]",
                      border_style="lcars1"))
    con.print(_vitals_panel(_mock_readings(), "nominal"))
    diagnosis = (
        "Vital systems overall status: NOMINAL\n\n"
        "Warnings explained:\n"
        "  * Embeddings partial — 8 nodes lack vectors; run [bold]org-llm embed[/bold]\n"
        "  * Unindexed files — 22 .org files new since last index\n\n"
        "Fix steps:\n"
        "  1. org-llm index\n"
        "  2. org-llm embed\n\n"
        "Follow-up checks:\n"
        "  * Re-run org-llm doctor to confirm 100% embedded"
    )
    con.print(Panel(diagnosis,
                      title="[lcars1]LLM Diagnosis (gemma3) · "
                              "Dr. Crusher[/lcars1]",
                      border_style="lcars2", padding=(1, 2)))
    _save(con, "33-doctor-crusher", "org-llm doctor — Dr. Crusher with vitals")


SCENES = [
    scene_pride_banner,
    scene_doctor_table,
    scene_cloud_providers,
    scene_cloud_cost,
    scene_models_discover,
    scene_report_overview,
    scene_tutor_welcome,
    scene_creds_status,
    scene_review_emacs_panel,
    scene_splash,
    scene_askbook,
    scene_models_dashboard,
    scene_captains_log,
    scene_literate_config,
    scene_dbt_status,
    scene_llm_rescue,
    scene_theme_studio_show,
    scene_theme_studio_verify,
    scene_doctor_walkthrough,
    # LCARS color-variant splashes (the new TNG-maximalist panel
    # rendered through five distinct LCARS palettes for the README
    # gallery).
    scene_splash_lcars_classic,
    scene_splash_lcars_red,
    scene_splash_lcars_green,
    scene_splash_lcars_gold,
    scene_splash_lcars_violet,
    # Newly-added feature scenes (palette CLI, knob registry,
    # power-boost doctor, self snapshot, pi bridge status).
    scene_palette_picker,
    scene_knob_list,
    scene_doctor_power_boost,
    scene_self_snapshot,
    scene_pi_status,
    # Phase 10: life-support / sensors / EMH / Dr. Crusher with vitals.
    scene_life_support,
    scene_sensors_drill,
    scene_emh_diagnosis,
    scene_doctor_with_vitals,
]


def main():
    """Generate every scene in dark + light modes.

    The current process inherits the chosen theme via the ORG_LLM_THEME env
    var. To get both modes we shell out to ourselves twice via a marker.
    """
    mode = os.environ.get("ORG_LLM_THEME", "dark").lower()
    suffix = "" if mode == "dark" else f"-{mode}"
    out_dir = IMG_DIR
    print(f"Generating {len(SCENES)} {mode} SVGs → {out_dir.relative_to(ROOT)}/  (suffix: {suffix or '(none)'})")
    # Patch _save to add the suffix
    global _save
    orig_save = _save

    def save_with_suffix(con, name, title=None):
        return orig_save(con, name + suffix, title)

    _save = save_with_suffix
    for scene in SCENES:
        scene()
    print("Done.")

    # If invoked plainly, also kick off the light pass
    if mode == "dark" and not os.environ.get("ORG_LLM_GALLERY_RECURSE"):
        env = {**os.environ, "ORG_LLM_THEME": "light", "ORG_LLM_GALLERY_RECURSE": "1"}
        import subprocess
        print()
        subprocess.run([sys.executable, str(Path(__file__).resolve())], env=env, check=False)


if __name__ == "__main__":
    main()
# gallery.py:1 ends here
