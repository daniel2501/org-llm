"""Timeout recovery hook — Phase 24.2 — recovery hooks.

When a tool call raises `TimeoutError` (or socket.timeout, which
inherits from OSError but also from TimeoutError on modern
Python), retry once with a 2× extended deadline. If the second
attempt also times out, re-raise so the LLM sees the failure.

Why retry on timeout: most tool timeouts in this codebase are
transient — a slow ollama warm-up, a one-off DNS hiccup, a
contended SQLite write. A single retry with a wider window
recovers those without burning a cloud round-trip; a stuck
backend still surfaces after attempt 2 so the agent can change
strategy. Two attempts is the budget the orchestration top
priority allows: enough to absorb transient flakiness, not
enough to make the user wait visibly twice.
"""
from __future__ import annotations

from . import (
    RecoveryAction,
    RecoveryContext,
    RecoveryHook,
    register,
)


_NAME = "timeout"
_MAX_ATTEMPTS = 2          # original + one retry
_TIMEOUT_MULTIPLIER = 2.0  # double the timeout on retry


def _matches(exc: BaseException, ctx: RecoveryContext) -> bool:
    # `socket.timeout` is `TimeoutError` on Python 3.10+, so this
    # one-line check covers both.
    return isinstance(exc, TimeoutError)


def _recover(exc: BaseException, ctx: RecoveryContext) -> RecoveryAction:
    if ctx.attempts >= _MAX_ATTEMPTS:
        # Used our retry budget — surface to the LLM.
        return RecoveryAction.RAISE
    # Extend the deadline for the next attempt. Hooks may set
    # timeout to 0 / negative; clamp to a small positive floor
    # so the retry is given *some* room.
    base = ctx.timeout_s if ctx.timeout_s and ctx.timeout_s > 0 else 30.0
    ctx.timeout_s = base * _TIMEOUT_MULTIPLIER
    ctx.meta["timeout.extended_to"] = ctx.timeout_s
    return RecoveryAction.RETRY


HOOK = RecoveryHook(name=_NAME, matches=_matches, recover=_recover)
register(HOOK)


__all__ = ["HOOK"]
# end
