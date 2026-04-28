# [[file:../../../org/20260425230731-org_llm.org::*rescue.py][rescue.py:1]]
"""Structured error-rescue registry + LLM-rescue sanitizer.

Two layers of defence between a raw Python exception and the user:

1. ``match_structured(exc)`` consults a curated registry of known
   error patterns and returns a precise hint whenever one fires. Cheap,
   deterministic, no LLM required. This is the primary path: ~95% of
   exceptions a user actually hits in this CLI fit a known pattern.

2. ``sanitize_llm_advice(text)`` is the safety net for the rare
   exceptions the registry doesn't know about. The LLM rescue path
   stays available, but its emitted FIX lines are filtered through
   a vetted command whitelist before they reach the user. A model
   that hallucinates ``pip install shutil`` (real example from this
   session) gets its bogus command stripped so the user doesn't run
   it by reflex.

Why a registry instead of always-LLM:

The LLM rescue is right when the user hits something genuinely
unfamiliar — say a sqlite migration edge case, a corrupted vault.
But for the routine stuff (Ollama not running, model not pulled,
empty input, missing dependency), the LLM is slow, expensive, and
sometimes wrong. The registry catches the routine, the LLM handles
the rare.
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple


# ── pattern registry ──────────────────────────────────────────────────────────

class StructuredHint(NamedTuple):
    """A pre-formatted recovery hint for a known error class.

    ``why`` describes the root cause in one sentence; ``fix`` is a
    concrete shell command (or ``manual:`` prefix); ``confidence`` is
    "high" when we KNOW this matches, "medium" when it might.
    """
    why:        str
    fix:        str
    confidence: str  # "high" | "medium"


# Each registry entry is (predicate, builder). The predicate gets the
# exception object + its formatted traceback tail; if it returns True
# the builder is called to produce the StructuredHint (lazy so the hint
# can quote details from the exception text).

_Pred    = Callable[[BaseException, str], bool]
_Builder = Callable[[BaseException, str], StructuredHint]


def _exc_msg(exc: BaseException) -> str:
    return str(exc) or ""


def _exc_type(exc: BaseException) -> str:
    return type(exc).__name__


# Helpers for common predicate shapes

def _msg_re(pattern: str, *, flags: int = re.IGNORECASE) -> _Pred:
    rx = re.compile(pattern, flags)
    return lambda e, _tb: bool(rx.search(_exc_msg(e)))


def _type_eq(name: str) -> _Pred:
    return lambda e, _tb: _exc_type(e) == name


def _and(*preds: _Pred) -> _Pred:
    return lambda e, tb: all(p(e, tb) for p in preds)


# The actual registry. Order matters — first match wins, so put the
# most-specific predicates first.

_REGISTRY: list[tuple[_Pred, _Builder]] = [
    # ── Ollama: model not pulled (404 from chat call) ─────────────────────────
    (
        _msg_re(r"model ['\"](.+?)['\"] not found"),
        lambda e, _: StructuredHint(
            why="The model isn't pulled into your local Ollama yet.",
            fix=(
                "org-llm models --pull "
                + (re.search(r"model ['\"]([^'\"]+)['\"] not found",
                              _exc_msg(e)).group(1)
                    if re.search(r"model ['\"]([^'\"]+)['\"] not found",
                                  _exc_msg(e))
                    else "<tag>")
            ),
            confidence="high",
        ),
    ),
    # ── Ollama: model too big for RAM (OOM at load time) ──────────────────────
    (
        _msg_re(r"requires more system memory.*GiB.*available"),
        lambda e, _: StructuredHint(
            why="Ollama refused to load the model because it would not fit in available RAM.",
            fix="org-llm doctor --power-boost --apply",
            confidence="high",
        ),
    ),
    # ── Ollama: connection refused / unreachable ──────────────────────────────
    (
        _and(
            lambda e, _: ("connection" in _exc_msg(e).lower()
                          or "refused" in _exc_msg(e).lower()
                          or "11434" in _exc_msg(e)),
            lambda e, _: any(s in _exc_type(e).lower()
                              for s in ("connect", "url", "request", "http",
                                        "ssl", "timeout")),
        ),
        lambda e, _: StructuredHint(
            why="The Ollama daemon isn't reachable on its configured port.",
            fix="ollama serve",
            confidence="high",
        ),
    ),
    # ── Ollama: embed-only model called as chat ───────────────────────────────
    (
        _msg_re(r"does not support chat"),
        lambda e, _: StructuredHint(
            why="An embedding model was called as a chat model.",
            fix="org-llm config chat_model llama3.2",
            confidence="high",
        ),
    ),
    # ── SQLite: DB locked ─────────────────────────────────────────────────────
    (
        _and(_type_eq("OperationalError"),
             _msg_re(r"database is locked")),
        lambda e, _: StructuredHint(
            why="Another org-llm process holds the SQLite write lock right now.",
            fix=("ps aux | grep org-llm | grep -v grep   "
                  "# find the holder, then wait or kill it"),
            confidence="high",
        ),
    ),
    # ── SQLite: missing column / migration drift ──────────────────────────────
    (
        _and(_type_eq("OperationalError"),
             _msg_re(r"no such column|no such table")),
        lambda e, _: StructuredHint(
            why="The DB schema is out of sync with this build of org-llm.",
            fix="org-llm doctor --fix",
            confidence="high",
        ),
    ),
    # ── NameError on a stdlib module → import bug in our code ─────────────────
    # (this is the one that produced the hallucinated "pip install shutil"
    # in this very session — exactly what we want to catch deterministically)
    (
        _and(_type_eq("NameError"),
             _msg_re(r"name '([a-z_]+)' is not defined")),
        lambda e, _: StructuredHint(
            why=("An org-llm module forgot to import a name. This is a "
                 "bug in org-llm itself, NOT a missing system package."),
            fix=("manual: report at "
                 "https://github.com/danielbenedict/org-llm/issues — include "
                 "the traceback. Do NOT run `pip install <name>`; the name "
                 "is almost certainly a stdlib module."),
            confidence="high",
        ),
    ),
    # ── FileNotFoundError on the configured org_dir ───────────────────────────
    (
        _and(_type_eq("FileNotFoundError"),
             lambda e, _: ".org" in _exc_msg(e) or "/org" in _exc_msg(e)),
        lambda e, _: StructuredHint(
            why="A configured directory or file does not exist on disk.",
            fix=("org-llm config org_dir ~/org   "
                  "# or wherever your vault lives"),
            confidence="medium",
        ),
    ),
    # ── ImportError: a real third-party dep is missing (uv install drift) ─────
    (
        _and(_type_eq("ModuleNotFoundError"),
             _msg_re(r"No module named '([a-z0-9_]+)'")),
        lambda e, _: StructuredHint(
            why=("A Python package org-llm depends on isn't installed. "
                 "Most likely a stale uv install."),
            fix="uv tool install --reinstall org-llm",
            confidence="high",
        ),
    ),
    # ── PermissionError on the DB or org_dir ──────────────────────────────────
    (
        _type_eq("PermissionError"),
        lambda e, _: StructuredHint(
            why="The OS denied read or write access to a file org-llm needed.",
            fix=("manual: check ownership of "
                  "~/.local/share/org-llm/ and ~/org/."),
            confidence="medium",
        ),
    ),
    # ── KeyError on a Config row → migration / typo ───────────────────────────
    (
        _and(_type_eq("KeyError"),
             _msg_re(r"_model|cloud_|theme_|fast_|chat_")),
        lambda e, _: StructuredHint(
            why="A config key org-llm expected isn't present.",
            fix="org-llm doctor --fix",
            confidence="medium",
        ),
    ),
    # ── empty-input crashes (defensive — we now catch most upfront) ───────────
    (
        _msg_re(r"empty text|empty.*query|empty.*prompt"),
        lambda e, _: StructuredHint(
            why="The command needs a non-empty input.",
            fix="manual: re-run the command with actual text in quotes.",
            confidence="high",
        ),
    ),
]


def match_structured(exc: BaseException, traceback_tail: str = ""
                       ) -> StructuredHint | None:
    """First-match-wins lookup against the registry. Returns ``None``
    when no pattern matches (caller should fall back to the LLM rescue
    path)."""
    for pred, build in _REGISTRY:
        try:
            if pred(exc, traceback_tail):
                return build(exc, traceback_tail)
        except Exception:
            # A predicate crash should NEVER abort the rescue path —
            # the user is already in a bad state. Skip and continue.
            continue
    return None


# ── LLM-rescue advice sanitizer ──────────────────────────────────────────────
# When the registry has no match and we fall back to the LLM, this
# layer redacts dangerous suggestions before showing them to the user.

# Commands the LLM is allowed to suggest. A FIX line that doesn't lead
# with one of these (after stripping `manual:`) is flagged as suspect
# and rendered in [yellow] with a "verify before running" note.

_SAFE_LEAD_TOKENS: set[str] = {
    "org-llm", "ollama", "uv", "pass", "gpg",
    "sudo",     # the user might need it for system installs; we still
                # keep the verify warning loud
    "ls", "cat", "ps", "grep", "find",   # diagnostic, read-only
    "mkdir", "rmdir",                    # narrow filesystem scope
    "python3", "python",                 # acceptable for one-off probes
    "manual:",
}

# Hard-banned prefixes — even if the LLM frames them as a "fix", they're
# almost always wrong (the NameError → "pip install shutil" example).
_BANNED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bpip install\b",                   re.IGNORECASE),
    re.compile(r"\bcurl\s+.*\|\s*(sh|bash)\b",       re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b",                      re.IGNORECASE),
    re.compile(r"\bchmod\s+-?[0-7]{3,4}\s+/\b",      re.IGNORECASE),
    re.compile(r"\bdd\s+if=",                        re.IGNORECASE),
    re.compile(r"\bmkfs",                            re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force\b",          re.IGNORECASE),
]


class SanitizedAdvice(NamedTuple):
    text:    str          # original advice (markup preserved)
    safe:    bool         # True when nothing was redacted
    flagged: list[str]    # human-readable reasons it's suspect


def sanitize_llm_advice(advice: str) -> SanitizedAdvice:
    """Inspect an LLM-emitted FIX block. Returns the original text plus
    a flag list for any suspect content. The caller decides how to
    render — typically: clean → black panel; suspect → yellow panel
    with the flag list above the advice.

    We do NOT rewrite the text; the user can always read what the LLM
    said. We just refuse to present it as a confident recommendation
    when it contains something dangerous.
    """
    if not advice:
        return SanitizedAdvice(text=advice, safe=True, flagged=[])
    flagged: list[str] = []
    # Banned patterns first — these always taint the advice.
    for rx in _BANNED_PATTERNS:
        if rx.search(advice):
            flagged.append(f"contains a banned shell pattern "
                            f"(matched /{rx.pattern}/i)")
    # Lead-token heuristic — pull each FIX-line and check.
    fix_lines = [ln for ln in advice.splitlines()
                 if ln.strip().lower().startswith("fix:")]
    for line in fix_lines:
        body = line.split(":", 1)[1].strip()
        first = body.split(maxsplit=1)[0] if body else ""
        # Strip any leading shell adornments (backticks, $, etc.)
        first = first.strip("`$()[]{}\"'")
        if first and first.lower() not in _SAFE_LEAD_TOKENS:
            flagged.append(
                f"FIX line starts with an unrecognised command: "
                f"`{first}`"
            )
    return SanitizedAdvice(
        text=advice,
        safe=(len(flagged) == 0),
        flagged=flagged,
    )
# rescue.py:1 ends here
