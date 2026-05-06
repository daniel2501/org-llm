"""Missing-argument recovery hook — Phase 24.2 — recovery hooks.

When a tool call raises `TypeError` shaped like a missing /
unexpected / wrong-typed argument, ESCALATE: don't retry blindly
(the same args will fail again), but build a structured hint
the framework can append to the tool's error response so the
LLM self-corrects on its next turn.

Why ESCALATE rather than RETRY: a missing-argument failure is
deterministic — retrying with the same args is guaranteed to
fail. The cheap recovery is a structured "expected X, got Y"
hint that lets the LLM pick the right shape on its next call.
Mirrors the resolver pattern: deterministic code prepares the
LLM with the right next move, no observer agent required.

Why match on message shape: Python's `TypeError` covers a wide
surface (bad isinstance args, abstract-method errors). We match
narrowly on the message shapes that mean "your call was
malformed" — `missing N required positional argument`,
`unexpected keyword argument`, `got an unexpected`, etc.
"""
from __future__ import annotations

import re

from . import (
    RecoveryAction,
    RecoveryContext,
    RecoveryHook,
    register,
)


_NAME = "missing_argument"

# Substrings that mean "your call signature is wrong". Lowercase
# match. Kept narrow on purpose — broadening to all TypeError is
# how unrelated bugs get masked as call-shape problems.
_SIGNATURE_MARKERS = (
    "missing 1 required",          # Python's exact phrasing
    "missing 2 required",
    "missing 3 required",
    "missing required argument",
    "unexpected keyword argument",
    "got an unexpected keyword",
    "takes no arguments",
    "takes 0 positional",
    "required positional argument",
    "required keyword-only argument",
)

# Pull the missing arg name out of the message, e.g.
#   "f() missing 1 required positional argument: 'path'"
# → "path"
_MISSING_NAME_RE = re.compile(
    r"required (?:positional |keyword-only )?argument(?:s)?:\s*['\"]?([\w_]+)",
    re.IGNORECASE,
)
# Pull the unexpected keyword, e.g.
#   "f() got an unexpected keyword argument 'paht'"
# → "paht"
_UNEXPECTED_NAME_RE = re.compile(
    r"unexpected keyword argument\s*['\"]?([\w_]+)",
    re.IGNORECASE,
)


def _matches(exc: BaseException, ctx: RecoveryContext) -> bool:
    if not isinstance(exc, TypeError):
        return False
    msg = str(exc).lower()
    return any(m in msg for m in _SIGNATURE_MARKERS)


def _build_hint(exc: BaseException, ctx: RecoveryContext) -> str:
    msg = str(exc)
    parts: list[str] = [
        f"Tool {ctx.tool!r} was called with the wrong shape.",
        f"Underlying error: {msg}",
    ]
    missing = _MISSING_NAME_RE.search(msg)
    if missing:
        parts.append(
            f"Missing argument: {missing.group(1)!r}. "
            f"Add it to your next call."
        )
    unexpected = _UNEXPECTED_NAME_RE.search(msg)
    if unexpected:
        bad = unexpected.group(1)
        parts.append(
            f"Unknown argument: {bad!r}. Check the tool signature; "
            f"this might be a typo of a real parameter."
        )
    if ctx.args:
        # Surface what the caller actually passed so the LLM can
        # diff it against the signature itself.
        keys = ", ".join(sorted(ctx.args.keys())) or "(none)"
        parts.append(f"You called with arguments: {keys}")
    return " ".join(parts)


def _recover(exc: BaseException, ctx: RecoveryContext) -> RecoveryAction:
    ctx.hint = _build_hint(exc, ctx)
    ctx.meta["missing_argument.hint_set"] = True
    return RecoveryAction.ESCALATE


HOOK = RecoveryHook(name=_NAME, matches=_matches, recover=_recover)
register(HOOK)


__all__ = ["HOOK"]
# end
