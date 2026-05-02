"""Read Doom Emacs configuration so org-llm can stop asking the user
for paths Emacs already knows.

Two probes, in order:

  1. Live RPC via `emacsclient --eval` against the running Emacs.
     Most accurate (post-`(after! …)`, post-`(setq! …)` doom macros,
     post-customize) but only works when the Emacs server is up.

  2. Static parse of `~/.config/doom/{config,init}.el`. Best-effort
     regex sweep for `(setq …)` / `(setq! …)` forms. Brittle by
     design (skips `let`-bindings, conditionals, package code), but
     a useful fallback when Emacs isn't running.

The public entry point is `gather_doom_config()` which returns a
dict keyed by the canonical org-llm config keys (e.g. `org_dir`,
`daily_dir`) with values pulled from whichever probe succeeded.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


_EMACSCLIENT_TIMEOUT = 2.0


# ── Live probe ────────────────────────────────────────────────────────────────

def _emacsclient_available() -> bool:
    return shutil.which("emacsclient") is not None


def _emacs_eval(form: str, timeout: float = _EMACSCLIENT_TIMEOUT) -> str | None:
    """Run `emacsclient --eval FORM` and return trimmed stdout, or
    None if the server is unreachable / errored. Never raises."""
    if not _emacsclient_available():
        return None
    try:
        out = subprocess.run(
            ["emacsclient", "--eval", form],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    if not text or text.startswith("*ERROR*"):
        return None
    return text


def _emacs_string(symbol: str) -> str | None:
    """Read a string-valued elisp variable. Returns the unquoted
    string, or None if the variable is unbound / empty / not a string."""
    raw = _emacs_eval(f"(if (boundp '{symbol}) {symbol} nil)")
    if raw is None or raw == "nil":
        return None
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        s = raw[1:-1]
        s = s.replace("\\\\", "\\").replace('\\"', '"')
        return s.strip() or None
    return None


def _emacs_string_list(symbol: str) -> list[str] | None:
    """Read a list-of-strings elisp variable. Tolerates `nil`,
    parses simple `("a" "b" ...)` shapes."""
    raw = _emacs_eval(f"(if (boundp '{symbol}) {symbol} nil)")
    if raw is None or raw == "nil":
        return None
    if not (raw.startswith("(") and raw.endswith(")")):
        return None
    inner = raw[1:-1]
    # Pull every "..."-quoted run; ignores nested cons cells, which is
    # fine for our path-list use cases.
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
    return [s.replace('\\"', '"').replace("\\\\", "\\") for s in items] or None


# ── Static fallback ───────────────────────────────────────────────────────────

_SETQ_RE = re.compile(
    r"\((?:setq|setq!|customize-set-variable)\s+([\w@:!*/+?\-]+)"
    r"\s+\"((?:[^\"\\]|\\.)*)\"",
    re.MULTILINE,
)


def _static_parse_dir(doom_dir: Path) -> dict[str, str]:
    """Best-effort scan of `config.el` + `init.el` for top-level
    `(setq <name> "<value>")` forms. Returns name → value (strings
    only; lists are ignored — emacsclient handles those better)."""
    out: dict[str, str] = {}
    for fname in ("config.el", "init.el"):
        f = doom_dir / fname
        if not f.exists():
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for m in _SETQ_RE.finditer(text):
            name, value = m.group(1), m.group(2)
            out.setdefault(name, value.replace('\\"', '"').replace("\\\\", "\\"))
    return out


# ── Canonical mapping ─────────────────────────────────────────────────────────

# Maps elisp variable names → org-llm config keys for paths we want
# to keep in lockstep with Doom. Add entries here, not in callers.
_PATH_VARS = {
    "org-directory":              "org_dir",
    "org-roam-dailies-directory": "daily_dir",
    "org-default-notes-file":     "inbox_path",
}

# Sometimes the user has `org-roam-directory` set distinct from
# `org-directory`. We only sync `org_dir` to `org-directory` since
# org-llm's vault concept matches Emacs's org concept, not roam's.

_LIST_VARS = {
    "org-agenda-files": "agenda_files",
}


def gather_doom_config(doom_dir: Path | None = None) -> dict:
    """Return a dict with two top-level keys:

      `values`: {org-llm-config-key: detected-value}
      `source`: {org-llm-config-key: 'live' | 'static' | 'missing'}

    Doesn't write anything. Caller decides whether to apply, prompt,
    or just diff."""
    doom_dir = doom_dir or Path("~/.config/doom").expanduser()
    values: dict[str, str] = {}
    source: dict[str, str] = {}

    # 1. Live probe wins for everything it can answer
    if _emacsclient_available():
        for evar, ckey in _PATH_VARS.items():
            v = _emacs_string(evar)
            if v:
                # Expand "~" / env vars for path-typed values
                v = os.path.expanduser(os.path.expandvars(v))
                values[ckey] = v
                source[ckey] = "live"
        for evar, ckey in _LIST_VARS.items():
            lst = _emacs_string_list(evar)
            if lst:
                expanded = [os.path.expanduser(os.path.expandvars(p))
                             for p in lst]
                values[ckey] = ",".join(expanded)
                source[ckey] = "live"

    # 2. Static parse fills in anything missed
    static = _static_parse_dir(doom_dir)
    for evar, ckey in _PATH_VARS.items():
        if ckey in values:
            continue
        if evar in static:
            values[ckey] = os.path.expanduser(os.path.expandvars(static[evar]))
            source[ckey] = "static"

    # 3. Mark known-but-missing keys so callers can show them
    for ckey in (*_PATH_VARS.values(), *_LIST_VARS.values()):
        source.setdefault(ckey, "missing")

    return {"values": values, "source": source, "doom_dir": str(doom_dir)}
