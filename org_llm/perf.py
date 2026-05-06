# [[file:../../../org/20260425230731-org_llm.org::*perf.py][perf.py:1]]
"""Performance tracking + tuning feedback.

Derives tok/s for every model the user has actually run by mining the
existing `History` table (kind=llm rows have model + duration_ms +
response). No new schema — everything we need is already logged.

Three jobs:

1. ``recent_tok_s(model)`` — rolling-average tok/s for one model based
   on the last N completed chat calls. Used by the lag detector to
   know what "normal" looks like for this model.

2. ``fastest_known(role, current=...)`` — across all models we have
   history for, pick the fastest one that supports ``role``. Used by
   ``models --upgrade`` and the lag-detector hint to point users at a
   known-faster alternative without re-running the benchmark.

3. ``check_lag(model, elapsed_s, response_chars)`` — called inline
   from ``llm.chat()``. Returns a one-line warning string if the call
   was meaningfully slower than this model's recent baseline AND a
   faster alternative is on disk; returns None otherwise. Cheap.

Why History instead of a dedicated perf table: every chat call already
writes one row to History via ``logbook.track_event``. Adding a parallel
table would double the writes and require a migration. The sample size
is also large (every chat invocation), so noisy single-shot variance
gets averaged out.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


# ── tok/s estimation ──────────────────────────────────────────────────────────

# Rough chars-per-token for English. Same approximation the benchmark
# verb uses; lets us derive tok/s from response length without asking
# Ollama for stats (which the chat API doesn't return).
_CHARS_PER_TOKEN = 4


def _engine():
    """Lazy engine — defer import so this module can be loaded without
    triggering the SQLAlchemy/db pipeline if a caller only needs the
    pure-function helpers."""
    from .db import DB_PATH, make_engine
    db = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not db.exists():
        return None
    return make_engine(db)


def _normalize_model(name: str) -> str:
    """Match the same normalization as cli._normalize_tag — strip
    ':latest' so 'phi3.5' and 'phi3.5:latest' compare equal."""
    n = (name or "").strip().lower()
    if n.endswith(":latest"):
        n = n[: -len(":latest")]
    return n


def _row_tok_s(response: str | None, duration_ms: int | None,
                 outcome: str | None = None) -> float | None:
    """tok/s for a single History row, or None if it can't be measured.

    Filters out junk samples that would otherwise inflate baselines:

      • Error rows. The History.response column stores the exception
        message text on outcome=error (e.g. "ResponseError: model
        requires more system memory (6.9 GiB)…"). Treating those as
        real generation made fastest_known() report mistral-nemo at
        ~28 tok/s when it actually OOMs and runs ~1 tok/s — every
        "fast" mistral row was a fast-failing 500 with the error
        message as the response.

      • Sub-second durations. A real 10+ chars completion at 28+
        tok/s is conceivable on a fast cloud endpoint but on local
        CPU is almost always a cache hit / refusal / immediate
        error, not actual generation work.

      • Tiny replies. Single-word answers / refusals give noisy
        per-call tok/s estimates.
    """
    if outcome and outcome.lower() == "error":
        return None
    if not response or not duration_ms or duration_ms <= 0:
        return None
    if duration_ms < 1000:
        # Anything that "completed" in under a second on local
        # hardware is almost certainly a cache hit or error response.
        return None
    chars = len(response)
    if chars < 10:
        return None
    # Defensive: error messages routinely start with "ResponseError:"
    # or "HTTP" / "401" / "404" patterns. Drop any response that
    # looks like a stringified exception. This catches rows where
    # outcome was incorrectly recorded as "ok" but the body is an
    # error trail.
    head = response.lstrip()[:60].lower()
    if (head.startswith(("responseerror", "http", "error:",
                            "traceback", "exception"))
            or "status code: 5" in head or "status code: 4" in head):
        return None
    tok_est = chars / _CHARS_PER_TOKEN
    return tok_est / (duration_ms / 1000.0)


# ── public: rolling baseline ──────────────────────────────────────────────────

def recent_tok_s(model: str, *, limit: int = 20) -> float | None:
    """Average tok/s for the last `limit` completed chat calls to `model`.

    Returns None when we don't have enough samples yet.
    """
    eng = _engine()
    if eng is None:
        return None
    from .db import History, get_session
    target = _normalize_model(model)
    samples: list[float] = []
    with get_session(eng) as s:
        rows = (s.query(History)
                  .filter(History.kind == "llm",
                          History.duration_ms.isnot(None),
                          History.response.isnot(None))
                  .order_by(History.id.desc())
                  .limit(limit * 4).all())  # over-fetch; filter below
    for r in rows:
        if _normalize_model(r.model or "") != target:
            continue
        v = _row_tok_s(r.response, r.duration_ms, getattr(r, "outcome", None))
        if v is None:
            continue
        samples.append(v)
        if len(samples) >= limit:
            break
    if len(samples) < 3:
        return None
    return sum(samples) / len(samples)


# ── public: fastest known alternative for a role ──────────────────────────────

class _Alt(NamedTuple):
    model:    str
    tok_s:    float
    n:        int   # samples
    quality:  int


def fastest_known(role: str, *, current_model: str = "",
                    limit_per_model: int = 20) -> _Alt | None:
    """Across every model with chat history, find the fastest one that
    supports `role` per CATALOG (or has no catalog entry — then we
    assume "chat", since plain chat is the default role tag).

    Returns None when no alternative exists or no model has data.
    """
    eng = _engine()
    if eng is None:
        return None
    from .db import History, get_session
    from .models import CATALOG, _quality
    role_models: dict[str, list[float]] = {}
    with get_session(eng) as s:
        rows = (s.query(History)
                  .filter(History.kind == "llm",
                          History.duration_ms.isnot(None),
                          History.response.isnot(None))
                  .order_by(History.id.desc())
                  .limit(limit_per_model * 50).all())
    for r in rows:
        m = _normalize_model(r.model or "")
        if not m:
            continue
        v = _row_tok_s(r.response, r.duration_ms, getattr(r, "outcome", None))
        if v is None:
            continue
        role_models.setdefault(m, []).append(v)
        if len(role_models[m]) > limit_per_model * 2:
            role_models[m] = role_models[m][:limit_per_model]

    cur = _normalize_model(current_model)
    best: _Alt | None = None
    for m, samples in role_models.items():
        if m == cur:
            continue
        if len(samples) < 3:
            continue
        # Match catalog entry by stem to confirm role support
        meta = next((c for c in CATALOG
                      if c.tag.split(":")[0] == m.split(":")[0]),
                     None)
        roles = meta.roles if meta else ("chat",)
        if role and role not in roles:
            continue
        tok_s = sum(samples) / len(samples)
        qual = _quality(m) or 50
        cand = _Alt(model=m, tok_s=tok_s, n=len(samples), quality=qual)
        if best is None or cand.tok_s > best.tok_s:
            best = cand
    return best


# ── public: lag-detector for inline use in llm.chat() ─────────────────────────

class LagWarning(NamedTuple):
    elapsed_s:        float
    baseline_tok_s:   float | None
    current_tok_s:    float | None
    suggested_model:  str
    suggested_tok_s:  float


def check_lag(model: str, elapsed_s: float, response: str,
               *, slow_factor: float = 3.0,
               min_elapsed: float = 8.0) -> LagWarning | None:
    """Inline lag check — call from inside the chat path right after
    a request returns. Returns a `LagWarning` if (a) the call took
    more than `min_elapsed` seconds AND (b) we have a faster
    alternative already on disk.

    Cheap: one indexed History query (last ~20 rows for this model)
    plus one query for fastest-known. Skip when DB is missing or
    samples are insufficient.

    `slow_factor` tunes when "current call slower than baseline" is
    noisy enough to mention. 3× is conservative — first-token latency
    dominates short calls so we don't want to alarm on noise.
    """
    if elapsed_s < min_elapsed:
        return None
    cur_tok = _row_tok_s(response, int(elapsed_s * 1000))
    baseline = recent_tok_s(model)
    # Only escalate if we KNOW it's slow. Without a baseline (cold
    # start), still warn when a faster pulled alternative exists —
    # the user benefits even on the first call.
    if baseline and cur_tok and cur_tok * slow_factor > baseline:
        return None  # within normal noise
    alt = fastest_known("chat", current_model=model)
    if alt is None:
        return None
    if cur_tok and alt.tok_s < cur_tok * 1.3:
        # Alternative isn't meaningfully faster; not worth nagging.
        return None
    return LagWarning(
        elapsed_s        = elapsed_s,
        baseline_tok_s   = baseline,
        current_tok_s    = cur_tok,
        suggested_model  = alt.model,
        suggested_tok_s  = alt.tok_s,
    )


# ── public: regression warner for batch operations ──────────────────────────

# ── public: perf-alert ring buffer (visible to MCP / proactive_doctor) ──────

_ALERT_BUFFER_PATH_ENV = "ORG_LLM_PERF_ALERTS_PATH"
_ALERT_BUFFER_DEFAULT  = "~/.local/share/org-llm/perf_alerts.json"
_ALERT_BUFFER_LIMIT    = 32


def _alert_buffer_path() -> Path:
    raw = os.environ.get(_ALERT_BUFFER_PATH_ENV) or _ALERT_BUFFER_DEFAULT
    return Path(raw).expanduser()


def record_perf_alert(*, model: str, elapsed_s: float,
                         current_tok_s: float | None,
                         suggested_model: str,
                         suggested_tok_s: float,
                         source: str = "lag_detector") -> None:
    """Append a perf-alert event to the on-disk ring buffer.

    The lag detector calls this whenever it would have printed a
    yellow warning. Storing each event lets the MCP `proactive_doctor`
    tool surface them later — MCP clients don't render stderr,
    so an inline-only warning was invisible.

    Best-effort: if the file is unwritable or malformed we skip
    silently. Telemetry must NEVER block a chat call.
    """
    import json, time
    try:
        p = _alert_buffer_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(p.read_text()) if p.exists() else []
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
        existing.append({
            "ts":              time.time(),
            "source":          source,
            "model":           model,
            "elapsed_s":       round(elapsed_s, 2),
            "current_tok_s":   (round(current_tok_s, 2)
                                  if current_tok_s is not None else None),
            "suggested_model": suggested_model,
            "suggested_tok_s": round(suggested_tok_s, 2),
        })
        # Cap the buffer so it doesn't grow unbounded.
        existing = existing[-_ALERT_BUFFER_LIMIT:]
        p.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


def recent_perf_alerts(limit: int = 10) -> list[dict]:
    """Read the most recent N alerts (newest last). Returns [] when
    the file is missing / malformed / unreadable."""
    import json
    p = _alert_buffer_path()
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text())
        if not isinstance(rows, list):
            return []
        return rows[-max(1, limit):]
    except Exception:
        return []


def clear_perf_alerts() -> int:
    """Drop the buffer. Returns how many entries were cleared."""
    import json
    p = _alert_buffer_path()
    if not p.exists():
        return 0
    try:
        rows = json.loads(p.read_text())
        n = len(rows) if isinstance(rows, list) else 0
        p.write_text("[]")
        return n
    except Exception:
        return 0


def regression_warning(model: str, *, recent_window: int = 5,
                         baseline_window: int = 50,
                         slow_factor: float = 1.5) -> str | None:
    """Compare recent runs for `model` against an older baseline.
    Returns a one-line warning string when the model has gotten
    materially slower vs its history (e.g. ollama upgrade, RAM
    pressure from a new long-running process).
    """
    eng = _engine()
    if eng is None:
        return None
    from .db import History, get_session
    target = _normalize_model(model)
    recent: list[float] = []
    older:  list[float] = []
    with get_session(eng) as s:
        rows = (s.query(History)
                  .filter(History.kind == "llm",
                          History.duration_ms.isnot(None),
                          History.response.isnot(None))
                  .order_by(History.id.desc())
                  .limit(baseline_window * 6).all())
    for r in rows:
        if _normalize_model(r.model or "") != target:
            continue
        v = _row_tok_s(r.response, r.duration_ms, getattr(r, "outcome", None))
        if v is None:
            continue
        if len(recent) < recent_window:
            recent.append(v)
        elif len(older) < baseline_window:
            older.append(v)
        else:
            break
    if len(recent) < 3 or len(older) < 5:
        return None
    r_avg = sum(recent) / len(recent)
    o_avg = sum(older)  / len(older)
    if r_avg <= 0 or o_avg <= 0:
        return None
    if o_avg > r_avg * slow_factor:
        slowdown = o_avg / r_avg
        return (f"{model}: recent runs are {slowdown:.1f}× slower "
                f"than baseline ({r_avg:.1f} vs {o_avg:.1f} tok/s). "
                f"Run [bold]org-llm doctor --power-boost[/bold] or "
                f"[bold]org-llm models --upgrade[/bold].")
    return None
# perf.py:1 ends here
