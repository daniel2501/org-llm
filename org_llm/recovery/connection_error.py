"""Connection-error recovery hook — Phase 24.2 — recovery hooks.

When a tool call raises `ConnectionError` (or one of its
subclasses: `ConnectionRefusedError`, `ConnectionResetError`,
`ConnectionAbortedError`), retry up to 3 times with exponential
backoff (0.25s → 0.5s → 1.0s). If the third attempt also fails,
re-raise so the LLM sees the failure and can change strategy
(e.g. fall back from cloud to local ollama).

Why exponential backoff: connection failures are usually one of
two patterns — a transient network blip (DNS, MTU, brief NAT
reset) that clears in <1s, or a backend that's down (ollama not
running, cloud key revoked) and will stay down. Three attempts
with widening sleeps separate those cases without making the
user wait long when it's truly down. The wall-clock cost of an
exhausted budget is ~1.75s — under the user's "did this work?"
attention threshold.
"""
from __future__ import annotations

import time

from . import (
    RecoveryAction,
    RecoveryContext,
    RecoveryHook,
    register,
)


_NAME = "connection_error"
_MAX_ATTEMPTS = 3              # original + two retries
_BACKOFF_BASE_S = 0.25         # 0.25s, 0.5s, 1.0s
_BACKOFF_FACTOR = 2.0


def _matches(exc: BaseException, ctx: RecoveryContext) -> bool:
    # Catches ConnectionError + all three subclasses
    # (Refused/Reset/Aborted) via inheritance.
    return isinstance(exc, ConnectionError)


def _recover(exc: BaseException, ctx: RecoveryContext) -> RecoveryAction:
    if ctx.attempts >= _MAX_ATTEMPTS:
        return RecoveryAction.RAISE
    # ctx.attempts is 1 on the first failure, so the first sleep
    # is _BACKOFF_BASE_S * factor^0 = 0.25s.
    sleep_s = _BACKOFF_BASE_S * (_BACKOFF_FACTOR ** (ctx.attempts - 1))
    ctx.meta["connection.backoff_s"] = sleep_s
    # The hook does the sleep itself — keeps the framework's
    # retry loop trivially simple. Sleeps are short by design
    # (≤1s on the third attempt) so this can't tax a turn.
    try:
        time.sleep(sleep_s)
    except Exception:
        # Don't let a clock weirdness kill recovery.
        pass
    return RecoveryAction.RETRY


HOOK = RecoveryHook(name=_NAME, matches=_matches, recover=_recover)
register(HOOK)


__all__ = ["HOOK"]
# end
