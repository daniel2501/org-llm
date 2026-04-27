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
    full bodies land in the org log + DB so dbt can analyze drift."""
    from .logbook import track_event
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
        return out
# llm.py:1 ends here
