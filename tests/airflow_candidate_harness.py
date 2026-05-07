"""In-process simulators for the four Airflow primitives we lean on.

Per =docs/wiki/agent-time-awareness.org= § Headless validation
harness: each of the eleven candidate use cases exercises some
subset of {DAG, Sensor, Retry, Backfill} so we can read off
empirically which candidates actually need Airflow-shape work
versus what =systemd= timers / cron / NATS could carry.

The simulators are deliberately tiny — they're not a competitor
to Airflow, just enough to demonstrate each candidate's
*shape*.  Headless: no I/O, no sleeps, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ── Verdict registry ─────────────────────────────────────────────────────────


@dataclass
class Verdict:
    """One row per candidate — used by the report doc."""
    n: int
    name: str
    primitives: list[str] = field(default_factory=list)
    sample_output: Any = None
    notes: str = ""


VERDICTS: list[Verdict] = []


def record(v: Verdict) -> Verdict:
    VERDICTS.append(v)
    return v


# ── DAG: topological executor ────────────────────────────────────────────────


class DAG:
    """Map of named nodes with upstream-deps; ``run`` executes in topo order."""

    def __init__(self, name: str):
        self.name = name
        self._nodes: dict[str, tuple[Callable[[dict[str, Any]], Any], list[str]]] = {}

    def add(self, name: str, fn: Callable[[dict[str, Any]], Any], deps: list[str] | tuple[str, ...] = ()) -> "DAG":
        self._nodes[name] = (fn, list(deps))
        return self

    def run(self) -> dict[str, Any]:
        order: list[str] = []
        seen: set[str] = set()

        def visit(n: str) -> None:
            if n in seen:
                return
            for d in self._nodes[n][1]:
                visit(d)
            seen.add(n)
            order.append(n)

        for n in self._nodes:
            visit(n)
        results: dict[str, Any] = {}
        for n in order:
            fn, _ = self._nodes[n]
            results[n] = fn(results)
        return results


# ── Sensor: poll-with-timeout ────────────────────────────────────────────────


class Sensor:
    """Polls a predicate up to ``max_polls``; returns True if it ever fires."""

    def __init__(self, predicate: Callable[[], bool], max_polls: int = 5):
        self.predicate = predicate
        self.max_polls = max_polls
        self.polls = 0

    def wait(self) -> bool:
        for _ in range(self.max_polls):
            self.polls += 1
            if self.predicate():
                return True
        return False


# ── Retry: bounded attempts ──────────────────────────────────────────────────


def retry(fn: Callable[[int], Any], *, attempts: int = 3, on: tuple[type[BaseException], ...] = (Exception,)) -> Any:
    """Call ``fn(i)`` up to ``attempts`` times; raise last on exhaustion."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn(i)
        except on as e:
            last = e
    assert last is not None
    raise last


# ── Backfill: replay over partitions ─────────────────────────────────────────


class Backfill:
    """Apply ``fn`` to each partition key; collect results."""

    def __init__(self, fn: Callable[[Any], Any]):
        self.fn = fn

    def replay(self, partitions: list[Any]) -> dict[Any, Any]:
        return {p: self.fn(p) for p in partitions}
