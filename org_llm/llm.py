# [[file:../../../org/20260425230731-org_llm.org::*llm.py][llm.py:1]]
from __future__ import annotations

import ollama


def list_models(base_url: str = "http://localhost:11434") -> list[str]:
    client = ollama.Client(host=base_url)
    return [m.model for m in client.list().models]


def embed(text: str, model: str, base_url: str) -> list[float]:
    client = ollama.Client(host=base_url)
    return client.embed(model=model, input=text).embeddings[0]


def chat(prompt: str, model: str, base_url: str, system: str = "") -> str:
    client = ollama.Client(host=base_url)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return client.chat(model=model, messages=messages).message.content
# llm.py:1 ends here
