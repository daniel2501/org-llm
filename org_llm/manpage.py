"""Generate a man page for org-llm in parity with the rest of the docs.

Walks the registered Typer command tree, pulls each command's docstring
+ option list, and renders a roff(7) man page suitable for `man 1
org-llm`. Stays in sync automatically — every new CLI verb appears in
the man page on the next regeneration.

Public API:
  - render_manpage()      → roff source as string
  - install_manpage(dir?) → write to MANPATH-discoverable location

Why this lives in the package (not a build-time artifact):
  - Always reflects the *installed* version's command set, not whatever
    was committed when someone last ran a build.
  - Lets `org-llm man` regenerate on demand.
  - Removes "is the man page stale" as a class of doc bug — it's
    derived from the same docstrings already shown by `--help`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date
from pathlib import Path


_MAN_VERSION = "1.0"
_MAN_DATE    = date.today().strftime("%Y-%m-%d")


def _esc(text: str) -> str:
    """Escape backslashes + leading dots for roff. Strip Rich markup
    so the man page reads cleanly without [bold] / [lcars2] tokens."""
    if not text:
        return ""
    import re
    text = re.sub(r"\[/?[^\]]+\]", "", text)        # strip Rich tags
    text = text.replace("\\", "\\\\")               # literal backslash
    out_lines = []
    for line in text.splitlines():
        if line.startswith("."):
            line = "\\&" + line                      # escape leading dot
        out_lines.append(line)
    return "\n".join(out_lines)


def _command_docs() -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """Walk the Typer app and yield (verb, summary, options) for each
    registered command. Returns options as (name, type, help)."""
    import typer
    from .cli import app
    out: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    click_root = typer.main.get_command(app)
    for verb in sorted(click_root.commands.keys()):
        cmd = click_root.commands[verb]
        summary = (cmd.help or "").split("\n\n")[0].strip()
        opts: list[tuple[str, str, str]] = []
        for p in (cmd.params or []):
            if not getattr(p, "name", None):
                continue
            # Skip Click's auto-generated --help
            if p.name == "help":
                continue
            names = [o for o in (p.opts or []) + (p.secondary_opts or [])]
            name = ", ".join(names) if names else p.name
            try:
                ptype = p.type.name
            except Exception:
                ptype = "string"
            opts.append((name, ptype, getattr(p, "help", "") or ""))
        out.append((verb, summary, opts))
    return out


def render_manpage() -> str:
    """Build the full roff source. Sections: NAME, SYNOPSIS, DESCRIPTION,
    COMMANDS (one block per verb), CONFIGURATION, FILES, ENVIRONMENT,
    SEE ALSO. Each command lists its options with -h / --help-style
    descriptions pulled directly from the Typer decorators."""
    cmds = _command_docs()
    lines: list[str] = []
    lines.append(rf'.TH ORG-LLM 1 "{_MAN_DATE}" "{_MAN_VERSION}" "User Commands"')
    lines.append(".SH NAME")
    lines.append("org-llm \\- LLM-powered CLI for org-roam knowledge bases")
    lines.append(".SH SYNOPSIS")
    lines.append(".B org-llm")
    lines.append(r"[\fIGLOBAL-OPTIONS\fR] \fICOMMAND\fR [\fICOMMAND-OPTIONS\fR] [\fIARGS\fR]")
    lines.append(".SH DESCRIPTION")
    lines.append(_esc(
        "org-llm indexes an org-roam vault into SQLite + sqlite-vec, then\n"
        "exposes that index through ~30+ commands and an MCP server so\n"
        "opencode (and any other MCP client, e.g. Claude Code) can\n"
        "read/write the vault as a second brain. Local-first (Ollama) by\n"
        "default; cloud routing (OpenRouter\n"
        "et al.) is opt-in. Captain's Log records every CLI invocation,\n"
        "LLM call, and config change to BOTH the SQLite history table AND\n"
        "~/org/captains-log.org for vault-level analytics via dbt."))
    lines.append("")
    lines.append(".SH ONBOARDING")
    lines.append(_esc(
        "First-time users should run:\n"
        "  org-llm setup            — interactive walkthrough (15 steps)\n"
        "Setup is resume-aware: an interrupted run picks up where it left\n"
        "off on the next invocation. Pass --restart to discard saved state."))
    lines.append("")
    lines.append(".SH COMMANDS")
    for verb, summary, opts in cmds:
        lines.append(rf".SS {_esc(verb)}")
        if summary:
            lines.append(_esc(summary))
        if opts:
            lines.append(".RS 4")
            for name, ptype, help_text in opts:
                lines.append(rf".TP")
                lines.append(rf"\fB{_esc(name)}\fR")
                if help_text:
                    lines.append(_esc(help_text))
            lines.append(".RE")
        lines.append("")
    lines.append(".SH CONFIGURATION")
    lines.append(_esc(
        "Config lives in the SQLite config table. View / set with:\n"
        "  org-llm config              show all keys\n"
        "  org-llm config <key>        get one\n"
        "  org-llm config <key> <val>  set one\n"
        "  org-llm config --search PAT fuzzy-search keys + descriptions\n"
        "  org-llm config --tangle     write a literate ~/org/org-llm-config.org\n"
        "                              mirror you can edit in place\n"
        "  org-llm config --apply-from-org  push edits back to the DB"))
    lines.append("")
    lines.append(".SH FILES")
    lines.append(".TP")
    lines.append(r"\fI~/.local/share/org-llm/org-llm.db\fR")
    lines.append(_esc("Primary SQLite database (override with $ORG_LLM_DB)."))
    lines.append(".TP")
    lines.append(r"\fI~/org/captains-log.org\fR")
    lines.append(_esc("Captain's Log — every event mirrored from the history table."))
    lines.append(".TP")
    lines.append(r"\fI~/org/org-llm-config.org\fR")
    lines.append(_esc("Literate config (after `org-llm config --tangle`)."))
    lines.append(".TP")
    lines.append(r"\fI~/org/org-llm-context.org\fR, \fI~/org/llm-history.org\fR")
    lines.append(_esc("LLM context + history narratives (org-babel-tangled)."))
    lines.append(".TP")
    lines.append(r"\fI~/org/org-llm-self-mod.org\fR")
    lines.append(_esc("Self-modification log: every snapshot/rollback/llm-revise."))
    lines.append(".TP")
    lines.append(r"\fI~/.local/share/org-llm/dbt/\fR")
    lines.append(_esc("User-editable dbt project (after `org-llm dbt init`)."))
    lines.append(".TP")
    lines.append(r"\fI~/.local/share/org-llm/snapshots/\fR")
    lines.append(_esc("Self-mod snapshots with standalone bash rollback scripts."))
    lines.append("")
    lines.append(".SH ENVIRONMENT")
    lines.append(".TP")
    lines.append(r"\fBORG_LLM_DB\fR")
    lines.append(_esc("Path to SQLite database (default ~/.local/share/org-llm/org-llm.db)."))
    lines.append(".TP")
    lines.append(r"\fBORG_LLM_ORG_DIR\fR")
    lines.append(_esc("Override org_dir (else read from config table)."))
    lines.append(".TP")
    lines.append(r"\fBORG_LLM_THEME\fR")
    lines.append(_esc("dark or light. Overrides config:theme for this invocation."))
    lines.append(".TP")
    lines.append(r"\fBORG_LLM_TREK_LEVEL\fR, \fBORG_LLM_COMMIE_LEVEL\fR, \fBORG_LLM_QUEER_LEVEL\fR")
    lines.append(_esc("0..3 — voice intensity for built-in dials (also: config <name>_level)."))
    lines.append(".TP")
    lines.append(r"\fBORG_LLM_DBT_DIR\fR")
    lines.append(_esc("Override the dbt project directory."))
    lines.append("")
    lines.append(".SH SEE ALSO")
    lines.append(_esc(
        "org-llm tutor welcome   — interactive walkthrough\n"
        "org-llm log             — Captain's Log\n"
        "org-llm doctor          — health check + LLM diagnosis\n"
        "https://github.com/daniel2501/org-llm  — source + issue tracker"))
    lines.append("")
    lines.append(".SH AUTHORS")
    lines.append(_esc("daniel2501 + contributors"))
    return "\n".join(lines) + "\n"


# ── Install / discover ─────────────────────────────────────────────────────────

def _man_install_dir() -> Path:
    """Where to write the man page so `man org-llm` finds it.

    Priority:
      1. $XDG_DATA_HOME/man/man1/      (XDG-sanctioned user man dir)
      2. ~/.local/share/man/man1/      (the de-facto default for #1)
    Both are picked up by default `manpath` on most systems.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.local/share").expanduser()
    return base / "man" / "man1"


def install_manpage(dir_override: Path | None = None) -> tuple[Path, bool]:
    """Write the man page to the user's local man dir. Returns
    (path, manpath_already_includes_it). Caller decides how to surface
    the manpath status — typically a hint to add MANPATH if the
    discovery probe fails."""
    target_dir = dir_override or _man_install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "org-llm.1"
    target.write_text(render_manpage())
    return (target, _is_in_manpath(target_dir.parent.parent))


def _is_in_manpath(man_root: Path) -> bool:
    """Probe `manpath` (the GNU/POSIX tool) to see if our man root is
    on the search path. Best-effort — if `manpath` isn't installed we
    say "unknown" by returning True so we don't nag the user."""
    if not shutil.which("manpath"):
        return True
    try:
        out = subprocess.run(["manpath"], capture_output=True,
                              text=True, timeout=5).stdout or ""
    except Exception:
        return True
    paths = out.strip().split(":")
    return any(str(man_root) == p or str(man_root) in p for p in paths)


def manpath_setup_hint(man_root: Path) -> str:
    """Lines to print when our man dir isn't on MANPATH. Detects the
    user's shell and gives the right rc-file recipe."""
    shell = os.environ.get("SHELL", "")
    line = f'export MANPATH="{man_root}:$MANPATH"'
    rc = "~/.bashrc"
    if "zsh" in shell:    rc = "~/.zshrc"
    elif "fish" in shell: rc = "~/.config/fish/config.fish"
    if "fish" in shell:
        line = f'set -gx MANPATH "{man_root}" $MANPATH'
    return (
        f"Add this to {rc} so `man org-llm` works in new shells:\n"
        f"  {line}\n"
        f"Or run once now in this shell to test: {line}"
    )
