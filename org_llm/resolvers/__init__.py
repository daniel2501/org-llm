"""Pre-flight resolver registry — Phase 24.1.

Pure functions that try to resolve ambiguity in a user's prompt
*before* `delegate()` calls the cloud. Each resolver inspects
the prompt+context and returns a list of `ResolvedFact`s; the
registry runs them in parallel with a per-resolver time budget
and concatenates their output into a `RESOLVED CONTEXT` block
that prepends the sub-LLM's user message.

Implements the architecture rule canonized in
`docs/wiki/architecture.org` § /Design rule — supervision is
deterministic by default/. The rule, sharply: if a confusion
can be detected by code, it must be. Resolvers are the
pre-flight layer of the trinity (recovery hooks + confusion
detector are 24.2 + 24.3).

Adding a new resolver: write a function `(prompt, context) ->
list[ResolvedFact]` in a new module under this package and
register it in `_DEFAULT_RESOLVERS` below. No core changes.
"""
from __future__ import annotations

import concurrent.futures as _fut
from dataclasses import dataclass
from typing      import Callable, Iterable


@dataclass(frozen=True)
class ResolvedFact:
    """One disambiguation result.

    `label`    — what was being resolved (e.g. "path 'wiki/superset.org'")
    `value`    — the resolved value (e.g. an absolute path)
    `evidence` — short tag for HOW it was resolved (e.g. "basename match")
    `source`   — name of the resolver that produced this fact (auto-set)
    """
    label:    str
    value:    str
    evidence: str = ""
    source:   str = ""


ResolverFn = Callable[[str, str], list[ResolvedFact]]


def resolve_all(
    prompt:           str,
    context:          str = "",
    *,
    time_budget_ms:   int = 80,
    resolvers:        Iterable[tuple[str, ResolverFn]] | None = None,
) -> list[ResolvedFact]:
    """Run every registered resolver in parallel; return their
    combined facts.

    `time_budget_ms` is the *wall-clock* cap for the whole call.
    Resolvers that haven't finished by then are dropped; their
    threads keep running in the background but their results
    are discarded. Raised exceptions are dropped silently. The
    supervision rule says supervision can NEVER tax the user's
    turn — we'd rather lose an enrichment than block.
    """
    rs = list(resolvers if resolvers is not None else _DEFAULT_RESOLVERS)
    if not rs:
        return []
    results: list[ResolvedFact] = []
    budget_s = time_budget_ms / 1000.0
    # ThreadPoolExecutor is right for I/O-bound resolvers (file
    # globs, DB reads). Sequential calls would defeat the budget.
    # `daemon` threads via thread_name_prefix so a slow resolver
    # doesn't block process exit.
    pool = _fut.ThreadPoolExecutor(
        max_workers=len(rs),
        thread_name_prefix="resolver",
    )
    try:
        future_to_name = {
            pool.submit(_safe_call, fn, prompt, context): name
            for name, fn in rs
        }
        # `wait` returns once budget elapses OR all complete.
        done, not_done = _fut.wait(future_to_name, timeout=budget_s)
        for fut in done:
            name = future_to_name[fut]
            try:
                facts = fut.result(timeout=0)
            except Exception:
                continue
            for f in facts:
                # Stamp the source if the resolver didn't.
                results.append(
                    f if f.source else ResolvedFact(
                        label=f.label, value=f.value,
                        evidence=f.evidence, source=name)
                )
        for fut in not_done:
            fut.cancel()  # no-op if already running, but cheap
    finally:
        # Don't block on slow threads — they finish in the
        # background and their results get GC'd.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _safe_call(fn: ResolverFn, prompt: str, context: str) -> list[ResolvedFact]:
    """Wrap a resolver so a raise doesn't kill the thread budget."""
    try:
        out = fn(prompt, context)
        return list(out) if out else []
    except Exception:
        return []


def format_resolved_context(facts: Iterable[ResolvedFact]) -> str:
    """Render facts as a `RESOLVED CONTEXT` block for the agent
    prompt. Empty input → empty string (caller skips the block)."""
    facts = list(facts)
    if not facts:
        return ""
    lines = ["RESOLVED CONTEXT (manager pre-flight; trust these"
             " over your own guesses):"]
    for f in facts:
        line = f"- {f.label} → {f.value}"
        if f.evidence:
            line += f"  ({f.evidence})"
        lines.append(line)
    return "\n".join(lines)


# ── registry ─────────────────────────────────────────────────────

# Imported lazily inside this list to avoid circulars and to keep
# `from .resolvers import resolve_all` cheap for callers that
# only need the framework (e.g. tests that pass their own list).
def _builtin_resolvers() -> list[tuple[str, ResolverFn]]:
    from .agent import resolve_agents
    from .path  import resolve_paths
    from .repo  import resolve_repos
    return [
        ("repo",  resolve_repos),
        ("path",  resolve_paths),
        ("agent", resolve_agents),
    ]


_DEFAULT_RESOLVERS: list[tuple[str, ResolverFn]] = _builtin_resolvers()


def _prewarm_caches() -> None:
    """Warm the slow caches (DB-backed allowlist read, agent
    builtin index, repo scandir) in a background daemon thread
    at module import.

    Without this, the first delegate after MCP server boot pays
    a ~600ms penalty on `access.allowlist()` (engine creation +
    config-row read) and the resolver result is dropped because
    it overruns the 80ms wall-clock budget. Pre-warming here
    means by the time the first user prompt arrives (typically
    seconds after import), `_search_roots`, `_known_repos`, and
    `_builtin_index` already have their caches populated.

    Daemon thread so it never blocks process exit. Silent on any
    failure — the rule says supervision must never block, and
    that includes its own warmup.
    """
    import threading

    def _warm() -> None:
        try:
            from .agent import _builtin_index
            from .path  import _search_roots
            from .repo  import _known_repos
            _search_roots()
            _known_repos()
            _builtin_index()
        except Exception:
            pass

    t = threading.Thread(target=_warm, name="resolver-prewarm",
                         daemon=True)
    t.start()


_prewarm_caches()


__all__ = [
    "ResolvedFact",
    "ResolverFn",
    "resolve_all",
    "format_resolved_context",
]
# end[[file:../../../org/20260425230731-org_llm.org::*resolvers/__init__.py][resolvers/__init__.py:1]]
