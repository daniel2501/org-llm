"""Integration tests for the @recover_on_failure decorator —
Phase 24.2 — recovery hooks.

Companion to `tests/test_recovery.py` (which exercises the
registry + builtin hooks in isolation). Here we test the wiring
in `org_llm.mcp_server`: the decorator itself, applied to a
fake tool function, with the recovery registry stubbed via the
`hooks=` override on `recover_from`.

Coverage matrix:

  - RETRY:    decorator re-invokes the wrapped function once on
              the first exception, returns the second-call value.
  - SKIP:     decorator returns a structured 'skipped' string the
              LLM can read, swallowing the exception.
  - ESCALATE: decorator returns a structured hint payload built
              from `ctx.hint`, swallowing the exception.
  - RAISE:    decorator re-raises the original exception unchanged
              so the existing `_wrap_tool_decorator` rescue path
              still runs (backward compatibility).
  - NO HOOK MATCH: same as RAISE — default for unrecognised
              exceptions; existing behaviour preserved.
  - SUCCESS PATH: when the wrapped function doesn't raise, the
              decorator is a transparent passthrough.
  - ASYNC PATH: the decorator handles coroutine functions
              identically.
  - REGISTRY DISPATCH: an end-to-end test using the real
              registered hooks (timeout → RETRY) to prove the
              decorator's `recover_from` call wires through to
              the live chain.

The decorator code lives in mcp_server.py at module scope, so
we can exercise it directly without spinning up a FastMCP
server.
"""
from __future__ import annotations

import asyncio
import pytest

from org_llm.mcp_server import recover_on_failure
from org_llm.recovery import (
    RecoveryAction,
    RecoveryHook,
    register,
    registered_hooks,
    unregister,
)


# ── shared fixture: clean registry per test ─────────────────────


@pytest.fixture
def clean_registry():
    """Snapshot the registry, clear it, run the test, restore.

    Same pattern as test_recovery.py — lets each test register
    exactly the hooks it wants without leaking state across
    siblings or across files (test_recovery.py runs in the same
    process).
    """
    saved = registered_hooks()
    for h in list(saved):
        unregister(h.name)
    yield
    for h in list(registered_hooks()):
        unregister(h.name)
    for h in saved:
        register(h)


# ── helpers ─────────────────────────────────────────────────────


def _run(coro):
    """Run an awaitable from a sync test.

    Builds a fresh event loop each call. `asyncio.get_event_loop()`
    is unreliable across pytest sessions on Python 3.11+ (it raises
    when no loop is current and another test in the run has
    already disposed of one), so we own the loop's lifetime here.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── decorator behaviour ─────────────────────────────────────────


class TestRecoverOnFailureSync:
    """Sync-tool decorator behaviour."""

    def test_success_path_is_passthrough(self, clean_registry):
        # No exception → decorator returns the function's result
        # unchanged. Even with no hooks registered, the success
        # path must not be perturbed.
        @recover_on_failure
        def tool(x: int) -> str:
            return f"got {x}"

        assert tool(x=42) == "got 42"

    def test_retry_action_reinvokes_once(self, clean_registry):
        # Hook returns RETRY → decorator calls the function a
        # second time. We use a closure counter to flip the
        # function from "raise" to "succeed" between attempts.
        register(RecoveryHook(
            name="retry_test",
            matches=lambda e, c: True,
            recover=lambda e, c: RecoveryAction.RETRY,
        ))
        calls = {"n": 0}

        @recover_on_failure
        def tool() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first call always fails")
            return "second call ok"

        assert tool() == "second call ok"
        assert calls["n"] == 2  # exactly one retry

    def test_skip_action_returns_structured_message(self, clean_registry):
        # Hook returns SKIP → decorator returns a structured
        # 'skipped' string. The LLM should be able to read
        # this and proceed; we don't pin exact wording but
        # the function name and exception type must be in there.
        register(RecoveryHook(
            name="skip_test",
            matches=lambda e, c: True,
            recover=lambda e, c: RecoveryAction.SKIP,
        ))

        @recover_on_failure
        def telemetry_tool() -> str:
            raise ValueError("transient telemetry blip")

        result = telemetry_tool()
        assert isinstance(result, str)
        assert "SKIPPED" in result
        assert "telemetry_tool" in result
        assert "ValueError" in result

    def test_escalate_action_surfaces_hint(self, clean_registry):
        # Hook returns ESCALATE and sets ctx.hint → decorator
        # surfaces the hint to the LLM in the response string so
        # the LLM can self-correct on its next turn.
        def _set_hint(exc, ctx):
            ctx.hint = "Try again with path='/tmp/foo'."
            return RecoveryAction.ESCALATE

        register(RecoveryHook(
            name="escalate_test",
            matches=lambda e, c: True,
            recover=_set_hint,
        ))

        @recover_on_failure
        def file_tool() -> str:
            raise TypeError("missing required argument")

        result = file_tool()
        assert isinstance(result, str)
        assert "RECOVERY HINT" in result
        assert "Try again with path='/tmp/foo'." in result
        assert "file_tool" in result

    def test_raise_action_reraises_original(self, clean_registry):
        # Hook returns RAISE → decorator must re-raise the
        # ORIGINAL exception unchanged. Backward-compat contract:
        # tools without recovery hooks behave exactly as before,
        # because RAISE is the default for unknown exceptions.
        register(RecoveryHook(
            name="raise_test",
            matches=lambda e, c: True,
            recover=lambda e, c: RecoveryAction.RAISE,
        ))

        @recover_on_failure
        def tool() -> str:
            raise RuntimeError("original error message")

        with pytest.raises(RuntimeError, match="original error message"):
            tool()

    def test_no_hook_match_falls_through_to_raise(self, clean_registry):
        # Empty registry / no matcher → recover_from returns RAISE
        # by default. The decorator MUST re-raise — anything else
        # would silently swallow exceptions for undecorated tools'
        # equivalents and break the rescue chain in mcp_server.
        @recover_on_failure
        def tool() -> str:
            raise RuntimeError("nobody catches me")

        with pytest.raises(RuntimeError, match="nobody catches me"):
            tool()


class TestRecoverOnFailureAsync:
    """Async-tool decorator behaviour. The decorator must detect
    coroutine functions and use the async branch."""

    def test_async_success_path(self, clean_registry):
        @recover_on_failure
        async def tool(x: int) -> str:
            return f"async got {x}"

        assert _run(tool(x=7)) == "async got 7"

    def test_async_retry_reinvokes_once(self, clean_registry):
        register(RecoveryHook(
            name="async_retry",
            matches=lambda e, c: True,
            recover=lambda e, c: RecoveryAction.RETRY,
        ))
        calls = {"n": 0}

        @recover_on_failure
        async def tool() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("flake")
            return "async ok"

        assert _run(tool()) == "async ok"
        assert calls["n"] == 2

    def test_async_escalate_returns_hint_string(self, clean_registry):
        # Cover the async ESCALATE branch end-to-end with the real
        # missing_argument-shaped TypeError to prove a wrapper
        # can carry deterministic hints out of an async tool.
        def _set_hint(exc, ctx):
            ctx.hint = "async hint payload"
            return RecoveryAction.ESCALATE

        register(RecoveryHook(
            name="async_esc",
            matches=lambda e, c: True,
            recover=_set_hint,
        ))

        @recover_on_failure
        async def tool() -> str:
            raise TypeError("oops")

        result = _run(tool())
        assert "RECOVERY HINT" in result
        assert "async hint payload" in result


# ── end-to-end with the real registered chain ───────────────────


class TestEndToEndWithBuiltinChain:
    """Don't `clean_registry` here — we want the real builtin
    hooks (timeout, connection_error, missing_argument) wired in
    so this test proves the full integration path."""

    def test_timeout_routes_through_to_retry(self):
        # First attempt raises TimeoutError. The timeout builtin
        # hook says RETRY (with extended deadline). Second
        # attempt succeeds. End-to-end the decorator should
        # absorb the timeout and return the success value.
        calls = {"n": 0}

        @recover_on_failure
        def tool() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("slow on first call")
            return "second-attempt ok"

        assert tool() == "second-attempt ok"
        assert calls["n"] == 2

    def test_missing_argument_routes_to_escalate_with_hint(self):
        # The missing_argument builtin matches the message shape
        # and sets ctx.hint. The decorator surfaces that hint in
        # the response — this is the LLM-self-correction loop.
        @recover_on_failure
        def read_thing() -> str:
            # The error message must match the hook's regex.
            raise TypeError(
                "read_thing() missing 1 required positional argument: 'path'"
            )

        result = read_thing()
        assert "RECOVERY HINT" in result
        # The builtin hint mentions the missing arg name 'path'.
        assert "path" in result

    def test_unrecognised_exception_re_raises(self):
        # A bare RuntimeError matches none of the builtin hooks,
        # so the registry returns RAISE by default. The decorator
        # MUST re-raise unchanged so existing rescue logic is
        # unaffected — this is the backward-compat contract.
        @recover_on_failure
        def tool() -> str:
            raise RuntimeError("nothing matches this")

        with pytest.raises(RuntimeError, match="nothing matches this"):
            tool()
# end
