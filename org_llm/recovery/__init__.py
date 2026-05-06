"""Tool-failure recovery hook registry — Phase 24.2 — recovery hooks.

The middle layer of the supervision trinity (DEC-006 — deterministic
supervision). Pre-flight resolvers (Phase 24.1 — pre-flight resolvers)
fire *before* a tool call; the confusion detector (Phase 24.3 —
confusion detector) watches *across* turns; recovery hooks fire
*at the moment a tool call raises* and decide what the framework
should do next — retry, skip, escalate to the LLM with a structured
hint, or re-raise.

The orchestration top priority — *smoother + faster + higher-quality
than solo* — fails the moment a transient timeout takes down a turn
or an obvious typo costs the user a round-trip. Recovery hooks
turn those into deterministic local recoveries, no extra cloud
call required.

This module ships the registry + decorator + lookup function only.
The integration wiring (`@recover` at MCP tool-call sites in
`mcp_server.py`) is the v0.1 follow-up sprint, deferred to dodge
conflicts with the in-flight rename sweep editing `mcp_server.py`.

Adding a new hook: write a `RecoveryHook` in a new module under
this package and register it via `@register` (or append to
`_DEFAULT_HOOKS` below). Order matters — the first hook whose
`matches(exc, ctx)` returns True wins. No core changes.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing      import Any, Callable, Iterable


# ── public types ────────────────────────────────────────────────


class RecoveryAction(enum.Enum):
    """What the framework should do after a recovery hook fires.

    `RETRY`    — call the tool again (the hook may have mutated
                 `ctx` to extend a timeout, sleep for a backoff,
                 etc.). The framework is responsible for honouring
                 a per-hook attempt cap; hooks themselves track
                 attempts via `ctx.attempts`.
    `SKIP`     — swallow the exception and continue with a
                 framework-supplied empty / default result. Used
                 for non-load-bearing telemetry calls.
    `ESCALATE` — surface a structured hint string (stored on
                 `ctx.hint`) to the LLM so it can self-correct on
                 its next turn. The framework formats this into
                 the tool's error response. Mirrors the resolver
                 pattern: deterministic code prepares the LLM
                 with the right next move.
    `RAISE`    — re-raise the original exception unchanged. The
                 default when no hook matches.
    """
    RETRY    = "retry"
    SKIP     = "skip"
    ESCALATE = "escalate"
    RAISE    = "raise"


@dataclass
class RecoveryContext:
    """Mutable state passed to every hook for one tool-call site.

    Hooks read + write this. The framework owns its lifecycle —
    one `RecoveryContext` per call site, reused across retries
    so attempt counts and accumulated metadata persist.

    `tool`      — name of the tool being recovered (e.g. "delegate")
    `args`      — the arguments that were passed; hooks may inspect
                  but should not mutate (mutation is what `hint`
                  + ESCALATE is for)
    `attempts`  — number of attempts the framework has made for
                  this call site, including the current failed one.
                  First failure → attempts == 1.
    `timeout_s` — current timeout in seconds; hooks may extend it
                  before returning RETRY
    `hint`      — structured suggestion for the LLM, set by
                  ESCALATE-returning hooks
    `meta`      — free-form scratch (e.g. backoff sleep target)
    """
    tool:      str
    args:      dict[str, Any]              = field(default_factory=dict)
    attempts:  int                          = 1
    timeout_s: float                        = 0.0
    hint:      str                          = ""
    meta:      dict[str, Any]              = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryHook:
    """One recovery rule.

    `name`     — short tag for logs + dispatch ("timeout",
                 "connection_error", "missing_argument")
    `matches`  — `(exc, ctx) -> bool`. Cheap, deterministic,
                 must not raise. The first matcher to return
                 True wins. Hooks should match narrowly:
                 exception class + (optional) message shape.
    `recover`  — `(exc, ctx) -> RecoveryAction`. May mutate ctx
                 (extend timeout, set hint, increment meta).
                 Must be deterministic given (exc, ctx) — same
                 input → same action — so the registry's
                 `recover_from()` lookup is reproducible.
    """
    name:    str
    matches: Callable[[BaseException, RecoveryContext], bool]
    recover: Callable[[BaseException, RecoveryContext], RecoveryAction]


# ── registry ────────────────────────────────────────────────────


_REGISTERED: list[RecoveryHook] = []


def register(hook: RecoveryHook) -> RecoveryHook:
    """Register a hook. Idempotent on (name): a re-registration
    replaces the prior entry in place so its position in the
    matcher chain is preserved (load-order is the chain order).

    Usable as a decorator:

        @register
        def _h() -> RecoveryHook:  # not the typical shape
            ...

    But the typical shape is plain function call after building
    the hook value (see hook modules in this package).
    """
    for i, existing in enumerate(_REGISTERED):
        if existing.name == hook.name:
            _REGISTERED[i] = hook
            return hook
    _REGISTERED.append(hook)
    return hook


def unregister(name: str) -> bool:
    """Remove a hook by name. Returns True if removed. Tests
    use this between fixtures to keep the registry clean."""
    for i, h in enumerate(_REGISTERED):
        if h.name == name:
            del _REGISTERED[i]
            return True
    return False


def registered_hooks() -> list[RecoveryHook]:
    """Snapshot of the current registry (in match-order). Mainly
    for tests + introspection."""
    return list(_REGISTERED)


def recover_from(
    exc:    BaseException,
    ctx:    RecoveryContext,
    *,
    hooks:  Iterable[RecoveryHook] | None = None,
) -> RecoveryAction:
    """Find the first registered hook that matches `(exc, ctx)`
    and call its `recover()`. If no hook matches, return `RAISE`
    so the framework re-raises unchanged.

    Sequential (not parallel) — order is meaningful, and matchers
    must be cheap by contract. A hook that raises during `matches`
    or `recover` is treated as non-matching; the search continues.
    The supervision rule says supervision can never tax the
    user's turn — that includes its own failure modes.
    """
    chain = list(hooks) if hooks is not None else _REGISTERED
    for hook in chain:
        try:
            if not hook.matches(exc, ctx):
                continue
        except Exception:
            continue
        try:
            return hook.recover(exc, ctx)
        except Exception:
            # A misbehaving hook must not eat the original
            # exception — fall through and re-raise.
            return RecoveryAction.RAISE
    return RecoveryAction.RAISE


# ── eager-import builtin hook modules ───────────────────────────

# Each module registers its hook(s) at import-time via `register()`.
# We import them here so that `from org_llm.recovery import
# recover_from` is enough to get the default chain. Order matters:
# the chain is matched first-to-last, so put narrow / specific
# matchers ahead of broad ones.
def _load_builtins() -> None:
    from . import timeout          as _timeout            # noqa: F401
    from . import connection_error as _connection_error   # noqa: F401
    from . import missing_argument as _missing_argument   # noqa: F401


_load_builtins()


__all__ = [
    "RecoveryAction",
    "RecoveryContext",
    "RecoveryHook",
    "register",
    "unregister",
    "registered_hooks",
    "recover_from",
]
# end
