# [[file:../../../org/20260425230731-org_llm.org::*llm.py][llm.py:1]]
from __future__ import annotations

import ollama


def list_models(base_url: str = "http://localhost:11434") -> list[str]:
    client = ollama.Client(host=base_url)
    return [m.model for m in client.list().models]


def embed(text: str, model: str, base_url: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("embed() called with empty text")
    # Logged via the logbook so dbt + the user can see embedding throughput
    # and failure rates over time. Verbose mode includes the input text;
    # normal stores just the metadata (model, latency, len of input).
    from .logbook import track_event
    with track_event("llm", "embed", model=model,
                       args=f"input_chars={len(text)}") as ev:
        client = ollama.Client(host=base_url)
        resp = client.embed(model=model, input=text)
        if not resp.embeddings:
            ev["outcome"] = "error"
            ev["response"] = "no embeddings returned"
            raise RuntimeError(f"Ollama returned no embeddings for model {model!r}")
        ev["response"] = f"dim={len(resp.embeddings[0])}"
        return resp.embeddings[0]


def chat(prompt: str, model: str, base_url: str, system: str = "",
         timeout: float | None = None) -> str:
    """Chat with Ollama. `timeout` (seconds) bounds the HTTP round-trip;
    when set, a stalled / overloaded Ollama raises rather than hanging
    indefinitely. Caller decides whether to swallow or surface the error.

    Every call is recorded to the logbook (kind=llm). At normal verbosity
    the prompt + response are stored truncated; at verbose verbosity the
    full bodies land in the org log + DB so dbt can analyze drift.

    Inline lag detection: when a call takes more than ~8s AND a faster
    alternative is already on disk, surface a one-line hint pointing
    the user at `models --upgrade`. Cheap (one indexed query against
    the History rows we just wrote). Suppressible via env
    ORG_LLM_LAG_DETECTOR=off."""
    from .logbook import track_event
    import time as _t
    t0 = _t.monotonic()
    with track_event("llm", "chat", model=model,
                       args=f"prompt_chars={len(prompt)}, "
                            f"system_chars={len(system or '')}, "
                            f"timeout={timeout}") as ev:
        client = ollama.Client(host=base_url, timeout=timeout)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        out = client.chat(model=model, messages=messages).message.content
        ev["response"] = out or ""

    # Lag check runs OUTSIDE track_event so its own DB queries don't
    # nest into the open transaction. Fail-silent — perf telemetry must
    # never break a chat call.
    # Suppressed when either ORG_LLM_LAG_DETECTOR=off (granular toggle)
    # or ORG_LLM_PROACTIVE_DOCTOR=off (umbrella suppression for tests +
    # scripted runs that don't want auto-healing side-effects).
    if (out and len(out) >= 10
            and os.environ.get("ORG_LLM_LAG_DETECTOR", "").lower() != "off"
            and os.environ.get("ORG_LLM_PROACTIVE_DOCTOR", "").lower() != "off"):
        try:
            from . import perf as _perf
            warn = _perf.check_lag(model, _t.monotonic() - t0, out)
            if warn is not None:
                _emit_lag_warning(warn, model)
                # Record to the ring buffer so the MCP proactive_doctor
                # can surface this lag event to the LLM next time it's
                # called. opencode/claude don't render stderr, so the
                # inline `_emit_lag_warning` print is invisible to them
                # — the buffer is how they hear about it.
                _perf.record_perf_alert(
                    model           = model,
                    elapsed_s       = warn.elapsed_s,
                    current_tok_s   = warn.current_tok_s,
                    suggested_model = warn.suggested_model,
                    suggested_tok_s = warn.suggested_tok_s,
                )
        except Exception:
            pass
    return out


def _emit_lag_warning(warn, current_model: str) -> None:
    """One-line themed hint shown above the next CLI output. Imports
    from ui at call time to avoid a circular import."""
    try:
        from .ui import on_screen as _on
    except Exception:
        return
    cur = warn.current_tok_s or 0.0
    sug = warn.suggested_tok_s
    speedup = (sug / cur) if cur > 0 else None
    bits = [
        f"[yellow]Lag:[/yellow] {current_model} ran "
        f"{warn.elapsed_s:.0f}s",
    ]
    if cur:
        bits.append(f"({cur:.1f} tok/s)")
    if speedup and speedup >= 1.5:
        bits.append(f"— [bold]{warn.suggested_model}[/bold] "
                    f"runs {speedup:.1f}× faster on this hardware "
                    f"({sug:.1f} tok/s).")
    else:
        bits.append(f"— faster alternatives exist.")
    bits.append("[dim]Tune:[/dim] [bold]org-llm models --upgrade[/bold]")
    _on(" ".join(bits))


import os  # used by lag-detector env override
# llm.py:1 ends here
