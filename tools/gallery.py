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
]


def main():
    print(f"Generating {len(SCENES)} SVG screenshots → {IMG_DIR.relative_to(ROOT)}/")
    for scene in SCENES:
        scene()
    print("Done.")


if __name__ == "__main__":
    main()
# gallery.py:1 ends here
