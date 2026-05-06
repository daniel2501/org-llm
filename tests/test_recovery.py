"""Tests for org_llm.recovery — Phase 24.2 — recovery hooks.

Coverage:
- registry: register / replace-by-name / unregister / lookup
- recover_from: no-match → RAISE; first-match-wins; misbehaving
  matcher / recover are skipped silently
- timeout hook: matches TimeoutError; first failure → RETRY with
  extended timeout; second failure → RAISE
- connection_error hook: matches ConnectionError + subclasses;
  exponential backoff; exhausts to RAISE
- missing_argument hook: matches TypeError with signature-shape
  message; sets hint; ESCALATE
- determinism: same (exc, ctx) → same action across calls
"""
from __future__ import annotations

import pytest

from org_llm.recovery import (
    RecoveryAction,
    RecoveryContext,
    RecoveryHook,
    recover_from,
    register,
    registered_hooks,
    unregister,
)
from org_llm.recovery.connection_error import HOOK as CONN_HOOK
from org_llm.recovery.missing_argument import HOOK as MISSING_HOOK
from org_llm.recovery.timeout         import HOOK as TIMEOUT_HOOK


# ── shared fixture: clean registry per test ─────────────────────


@pytest.fixture
def clean_registry():
    """Snapshot the registry, clear it, run the test, restore.

    Lets each test register its own hooks without contaminating
    siblings. We don't import the builtin hook modules here on
    purpose — tests that want them call register() explicitly.
    """
    saved = registered_hooks()
    # Clear in place so any module-level reference to the
    # registry list (none today, but defensive) stays valid.
    for h in list(saved):
        unregister(h.name)
    yield
    for h in list(registered_hooks()):
        unregister(h.name)
    for h in saved:
        register(h)


def _ctx(tool: str = "read_file", **kw) -> RecoveryContext:
    return RecoveryContext(tool=tool, **kw)


# ── registry mechanics ──────────────────────────────────────────


class TestRegistry:
    def test_register_appends(self, clean_registry):
        h = RecoveryHook(name="x",
                          matches=lambda e, c: True,
                          recover=lambda e, c: RecoveryAction.SKIP)
        register(h)
        names = [r.name for r in registered_hooks()]
        assert "x" in names

    def test_register_replaces_in_place(self, clean_registry):
        h1 = RecoveryHook(name="dup",
                           matches=lambda e, c: False,
                           recover=lambda e, c: RecoveryAction.SKIP)
        h2 = RecoveryHook(name="dup",
                           matches=lambda e, c: True,
                           recover=lambda e, c: RecoveryAction.RETRY)
        # Buffer hook before + after to prove `dup` keeps its slot.
        before = RecoveryHook(name="before",
                               matches=lambda e, c: False,
                               recover=lambda e, c: RecoveryAction.SKIP)
        after = RecoveryHook(name="after",
                              matches=lambda e, c: False,
                              recover=lambda e, c: RecoveryAction.SKIP)
        register(before)
        register(h1)
        register(after)
        register(h2)
        names = [r.name for r in registered_hooks()]
        assert names == ["before", "dup", "after"]
        # And the action is the new one.
        assert recover_from(RuntimeError("x"), _ctx()) == RecoveryAction.RETRY

    def test_unregister_returns_true_when_removed(self, clean_registry):
        register(RecoveryHook(name="t",
                               matches=lambda e, c: True,
                               recover=lambda e, c: RecoveryAction.SKIP))
        assert unregister("t") is True
        assert unregister("t") is False

    def test_no_match_returns_raise(self, clean_registry):
        register(RecoveryHook(name="never",
                               matches=lambda e, c: False,
                               recover=lambda e, c: RecoveryAction.SKIP))
        assert recover_from(RuntimeError("x"), _ctx()) == RecoveryAction.RAISE

    def test_first_match_wins(self, clean_registry):
        register(RecoveryHook(name="a",
                               matches=lambda e, c: True,
                               recover=lambda e, c: RecoveryAction.RETRY))
        register(RecoveryHook(name="b",
                               matches=lambda e, c: True,
                               recover=lambda e, c: RecoveryAction.SKIP))
        assert recover_from(RuntimeError("x"), _ctx()) == RecoveryAction.RETRY

    def test_matcher_exception_is_skipped(self, clean_registry):
        def boom(e, c):
            raise ValueError("matcher boom")
        register(RecoveryHook(name="boom", matches=boom,
                               recover=lambda e, c: RecoveryAction.SKIP))
        register(RecoveryHook(name="good",
                               matches=lambda e, c: True,
                               recover=lambda e, c: RecoveryAction.RETRY))
        # Boom matcher skipped silently; good matcher takes the call.
        assert recover_from(RuntimeError("x"), _ctx()) == RecoveryAction.RETRY

    def test_recover_exception_falls_through_to_raise(self, clean_registry):
        def boom(e, c):
            raise ValueError("recover boom")
        register(RecoveryHook(name="bad",
                               matches=lambda e, c: True,
                               recover=boom))
        # The recover-side raise must NOT propagate — supervision
        # must never tax the user's turn — but it also must not
        # accidentally swallow the original exception. Behaviour:
        # framework gets RAISE so the original re-raises.
        assert recover_from(RuntimeError("x"), _ctx()) == RecoveryAction.RAISE

    def test_explicit_hooks_arg_overrides_registry(self, clean_registry):
        register(RecoveryHook(name="reg",
                               matches=lambda e, c: True,
                               recover=lambda e, c: RecoveryAction.RETRY))
        custom = [RecoveryHook(name="custom",
                                matches=lambda e, c: True,
                                recover=lambda e, c: RecoveryAction.SKIP)]
        assert (recover_from(RuntimeError("x"), _ctx(), hooks=custom)
                 == RecoveryAction.SKIP)


# ── timeout hook ────────────────────────────────────────────────


class TestTimeoutHook:
    def test_matches_timeout_error(self):
        ctx = _ctx(timeout_s=10.0)
        assert TIMEOUT_HOOK.matches(TimeoutError("slow"), ctx) is True

    def test_does_not_match_other(self):
        ctx = _ctx(timeout_s=10.0)
        assert TIMEOUT_HOOK.matches(ValueError("nope"), ctx) is False
        assert TIMEOUT_HOOK.matches(ConnectionError("nope"), ctx) is False

    def test_first_failure_retries_with_extended_timeout(self):
        ctx = _ctx(timeout_s=10.0, attempts=1)
        action = TIMEOUT_HOOK.recover(TimeoutError("slow"), ctx)
        assert action == RecoveryAction.RETRY
        assert ctx.timeout_s == 20.0
        assert ctx.meta["timeout.extended_to"] == 20.0

    def test_second_failure_raises(self):
        ctx = _ctx(timeout_s=20.0, attempts=2)
        action = TIMEOUT_HOOK.recover(TimeoutError("still slow"), ctx)
        assert action == RecoveryAction.RAISE

    def test_zero_timeout_clamped_to_floor(self):
        ctx = _ctx(timeout_s=0.0, attempts=1)
        TIMEOUT_HOOK.recover(TimeoutError("slow"), ctx)
        # 30s floor × 2 multiplier
        assert ctx.timeout_s == 60.0


# ── connection_error hook ───────────────────────────────────────


class TestConnectionErrorHook:
    def test_matches_connection_error(self):
        assert CONN_HOOK.matches(ConnectionError("nope"), _ctx()) is True

    def test_matches_connection_error_subclasses(self):
        assert CONN_HOOK.matches(ConnectionRefusedError(), _ctx()) is True
        assert CONN_HOOK.matches(ConnectionResetError(), _ctx())   is True
        assert CONN_HOOK.matches(ConnectionAbortedError(), _ctx()) is True

    def test_does_not_match_timeout(self):
        assert CONN_HOOK.matches(TimeoutError("slow"), _ctx()) is False

    def test_first_failure_retries_with_short_backoff(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(
            "org_llm.recovery.connection_error.time.sleep",
            lambda s: sleeps.append(s),
        )
        ctx = _ctx(attempts=1)
        action = CONN_HOOK.recover(ConnectionError("nope"), ctx)
        assert action == RecoveryAction.RETRY
        assert sleeps == [0.25]
        assert ctx.meta["connection.backoff_s"] == 0.25

    def test_second_failure_doubles_backoff(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(
            "org_llm.recovery.connection_error.time.sleep",
            lambda s: sleeps.append(s),
        )
        ctx = _ctx(attempts=2)
        CONN_HOOK.recover(ConnectionError("nope"), ctx)
        assert sleeps == [0.5]

    def test_third_failure_raises(self, monkeypatch):
        called: list[float] = []
        monkeypatch.setattr(
            "org_llm.recovery.connection_error.time.sleep",
            lambda s: called.append(s),
        )
        ctx = _ctx(attempts=3)
        action = CONN_HOOK.recover(ConnectionError("nope"), ctx)
        assert action == RecoveryAction.RAISE
        assert called == []  # no sleep after we've decided to raise


# ── missing_argument hook ───────────────────────────────────────


class TestMissingArgumentHook:
    def test_matches_missing_required_positional(self):
        exc = TypeError("f() missing 1 required positional argument: 'path'")
        assert MISSING_HOOK.matches(exc, _ctx()) is True

    def test_matches_unexpected_keyword(self):
        exc = TypeError("f() got an unexpected keyword argument 'paht'")
        assert MISSING_HOOK.matches(exc, _ctx()) is True

    def test_matches_keyword_only(self):
        exc = TypeError(
            "f() missing 1 required keyword-only argument: 'mode'"
        )
        assert MISSING_HOOK.matches(exc, _ctx()) is True

    def test_does_not_match_random_typeerror(self):
        # Plain TypeError that isn't about call shape — must not
        # match, otherwise we'd mask unrelated bugs as call-shape
        # problems.
        exc = TypeError("unsupported operand type(s) for +: 'int' and 'str'")
        assert MISSING_HOOK.matches(exc, _ctx()) is False

    def test_does_not_match_other_exception_types(self):
        assert MISSING_HOOK.matches(ValueError("missing required argument: 'x'"),
                                       _ctx()) is False

    def test_recover_returns_escalate_with_hint(self):
        exc = TypeError("read_file() missing 1 required positional argument: 'path'")
        ctx = _ctx(tool="read_file", args={"agent": "atoz"})
        action = MISSING_HOOK.recover(exc, ctx)
        assert action == RecoveryAction.ESCALATE
        assert ctx.hint
        assert "read_file" in ctx.hint
        assert "path" in ctx.hint
        assert "agent" in ctx.hint  # surfaced what was passed

    def test_hint_calls_out_unexpected_keyword(self):
        exc = TypeError("f() got an unexpected keyword argument 'paht'")
        ctx = _ctx(tool="read_file", args={"paht": "/tmp/x"})
        MISSING_HOOK.recover(exc, ctx)
        assert "paht" in ctx.hint
        assert "typo" in ctx.hint.lower() or "unknown" in ctx.hint.lower()


# ── determinism ─────────────────────────────────────────────────


class TestDeterminism:
    """Same input → same action. The supervision rule says
    deterministic by default; tests pin that contract."""

    def test_timeout_hook_deterministic(self):
        # Independent ctx objects with identical state must
        # produce identical actions + same post-state.
        ctx_a = _ctx(timeout_s=10.0, attempts=1)
        ctx_b = _ctx(timeout_s=10.0, attempts=1)
        a = TIMEOUT_HOOK.recover(TimeoutError("x"), ctx_a)
        b = TIMEOUT_HOOK.recover(TimeoutError("x"), ctx_b)
        assert a == b
        assert ctx_a.timeout_s == ctx_b.timeout_s

    def test_missing_argument_hook_deterministic(self):
        exc = TypeError("f() missing 1 required positional argument: 'path'")
        ctx_a = _ctx(tool="read_file")
        ctx_b = _ctx(tool="read_file")
        a = MISSING_HOOK.recover(exc, ctx_a)
        b = MISSING_HOOK.recover(exc, ctx_b)
        assert a == b
        assert ctx_a.hint == ctx_b.hint

    def test_recover_from_deterministic(self, clean_registry):
        register(RecoveryHook(name="d",
                               matches=lambda e, c: isinstance(e, TimeoutError),
                               recover=lambda e, c: RecoveryAction.SKIP))
        first  = recover_from(TimeoutError("x"), _ctx())
        second = recover_from(TimeoutError("x"), _ctx())
        third  = recover_from(TimeoutError("x"), _ctx())
        assert first == second == third == RecoveryAction.SKIP


# ── builtin chain wiring ────────────────────────────────────────


class TestBuiltinChain:
    """Sanity-check that importing the package registers all
    three example hooks in order. This is the contract the v0.1
    integration sprint will rely on."""

    def test_all_three_hooks_registered(self):
        names = [h.name for h in registered_hooks()]
        assert "timeout"          in names
        assert "connection_error" in names
        assert "missing_argument" in names

    def test_timeout_dispatches_to_retry(self):
        # End-to-end: registry-level recover_from picks the
        # timeout hook and returns RETRY on first failure.
        ctx = _ctx(timeout_s=5.0, attempts=1)
        assert recover_from(TimeoutError("slow"), ctx) == RecoveryAction.RETRY

    def test_connection_dispatches_to_retry(self, monkeypatch):
        monkeypatch.setattr(
            "org_llm.recovery.connection_error.time.sleep",
            lambda s: None,
        )
        ctx = _ctx(attempts=1)
        assert (recover_from(ConnectionError("nope"), ctx)
                 == RecoveryAction.RETRY)

    def test_missing_argument_dispatches_to_escalate(self):
        exc = TypeError("read_file() missing 1 required positional argument: 'path'")
        ctx = _ctx(tool="read_file")
        assert recover_from(exc, ctx) == RecoveryAction.ESCALATE
        assert "path" in ctx.hint

    def test_unrecognised_exception_raises(self):
        # A bare RuntimeError matches none of the builtin hooks.
        assert (recover_from(RuntimeError("weird"), _ctx())
                 == RecoveryAction.RAISE)
# end
