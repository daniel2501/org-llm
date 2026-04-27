# [[file:../../../org/20260425230731-org_llm.org::*llm.py][llm.py:1]]
from __future__ import annotations

import ollama


def list_models(base_url: str = "http://localhost:11434") -> list[str]:
    client = ollama.Client(host=base_url)
    return [m.model for m in client.list().models]


def embed(text: str, model: str, base_url: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("embed() called with empty text")
    client = ollama.Client(host=base_url)
    resp = client.embed(model=model, input=text)
    if not resp.embeddings:
        raise RuntimeError(f"Ollama returned no embeddings for model {model!r}")
    return resp.embeddings[0]


def chat(prompt: str, model: str, base_url: str, system: str = "",
         timeout: float | None = None) -> str:
    """Chat with Ollama. `timeout` (seconds) bounds the HTTP round-trip;
    when set, a stalled / overloaded Ollama raises rather than hanging
    indefinitely. Caller decides whether to swallow or surface the error."""
    client = ollama.Client(host=base_url, timeout=timeout)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return client.chat(model=model, messages=messages).message.content
# llm.py:1 ends here
