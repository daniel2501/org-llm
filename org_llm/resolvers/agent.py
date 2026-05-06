"""Agent resolver — turns `@<handle>` mentions into birth-name
+ one-line capability summary.

Helps the sub-LLM disambiguate between "the user @-tagged
@atoz" (specific persona, specific tool roster) versus
loose handle-shaped tokens that mean nothing.

Sources from `org_llm.agents.get_builtins()` only — fast
in-process, no DB read. Aliases route to their canonical
birth_name. Phase 25's inversion will let this hit the DB
for user-supplied agents (Phase 23.5).
"""
from __future__ import annotations

import re

from . import ResolvedFact


# Match `@<handle>` where handle is identifier-shaped. We avoid
# matching email-like @ usage by requiring the prefix be
# whitespace, line-start, or punctuation other than `\w`.
_HANDLE_RE = re.compile(r"(?<![\w@])@([a-zA-Z][\w-]{1,32})\b")


def resolve_agents(prompt: str, context: str = "") -> list[ResolvedFact]:
    text = f"{prompt}\n{context}".strip()
    if not text:
        return []
    handles = _extract_handles(text)
    if not handles:
        return []
    by_handle = _builtin_index()
    if not by_handle:
        return []
    out: list[ResolvedFact] = []
    seen: set[str] = set()
    for h in handles:
        agent = by_handle.get(h.lower())
        if agent is None or agent.birth_name in seen:
            continue
        seen.add(agent.birth_name)
        summary = (agent.description or "").strip().split("\n", 1)[0][:200]
        # Note routing distinction (alias → canonical) so the
        # sub-LLM doesn't get confused by a label mismatch in
        # downstream tool calls (which use birth_name).
        if h.lower() != agent.birth_name.lower():
            label = f'@{h} (alias → @{agent.birth_name})'
        else:
            label = f'@{agent.birth_name}'
        out.append(ResolvedFact(
            label=label,
            value=summary or "(no description)",
            evidence="builtin agent registry",
            source="agent",
        ))
    return out


def _extract_handles(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _HANDLE_RE.finditer(text):
        h = m.group(1)
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append(h)
    return out


def _builtin_index() -> dict[str, object]:
    """Build a {handle.lower(): Agent} map from builtins, with
    every alias routing to its canonical agent."""
    try:
        from ..agents import get_builtins
    except Exception:
        return {}
    try:
        builtins = list(get_builtins())
    except Exception:
        return {}
    out: dict[str, object] = {}
    for a in builtins:
        out[a.birth_name.lower()] = a
        for alias in (a.aliases or ()):
            out[alias.lower()] = a
    return out
# end
