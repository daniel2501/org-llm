"""Auto-personalization: derive theme knobs from the user's actual content.

The throughline of org-llm is that the *app* should know what the user has,
not force the user to describe themselves. `personalize` reads the vault
(top tags, project names, language preference) and the filesystem (via
discover) to propose theme knobs that match the user's real interests.

Each knob is a named bundle of `make_it_so` completion messages plus a
default level (1–3). Once registered in the `user_theme_knobs` config
row, the knob's messages roll into the make_it_so pool whenever its
level is ≥ 1, just like the built-in dials (trek/commie/queer).

Detection is deterministic and free. Message generation can optionally
go through a local LLM for richer copy; otherwise a per-theme template
pool is used.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib  import Path


# Tags / project-name patterns we explicitly do NOT theme around: too
# generic, or already covered by built-in dials.
_BORING_TAGS = {
    "todo", "done", "draft", "wip", "test", "tmp", "fixme", "note",
    "personal", "work", "misc", "general", "archive",
    "project", "code",
    # built-ins
    "trek", "commie", "queer",
    # very-generic single words that appear across many notes
    "audit", "report", "walkthrough", "doctor", "setup", "tour",
    "org-llm", "bench", "fixer-bench", "testing",
}


@dataclass
class ThemeProposal:
    """One auto-detected theme that could become a knob."""
    name:          str           # canonical knob name (lowercase, [a-z0-9_-]+)
    score:         int           # bigger = more central to the user
    sample_titles: list[str]     # short examples that anchored this theme
    source:        str           # "vault-tag" | "vault-title" | "fs-project" | "fs-lang"
    default_level: int = 2       # 0..3; 2 maps to 1× pool weight


def _normalize(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"[^a-z0-9_\- ]+", "", n)
    n = re.sub(r"\s+", "-", n).strip("-_")
    return n


def detect_themes(session, max_themes: int = 6) -> list[ThemeProposal]:
    """Probe the vault + filesystem and propose themes.

    Returns proposals sorted by score (descending). Capped at `max_themes`.
    """
    from .db import Node

    proposals: list[ThemeProposal] = []
    seen: set[str] = set()

    # ── Top non-boring tags (vault) ────────────────────────────────────────
    tag_counts: Counter = Counter()
    sample_titles_by_tag: dict[str, list[str]] = {}
    for title, tags in session.query(Node.title, Node.tags).filter(
            Node.tags.isnot(None)).all():
        # org-roam stores tags space-separated in the index. A single token
        # may itself look like `code:python` (from code-index); skip those.
        for raw in (tags or "").split():
            t = raw.strip().lower()
            if not t or t.startswith("code:") or t == "code":
                continue
            if t in _BORING_TAGS:
                continue
            tag_counts[t] += 1
            sample_titles_by_tag.setdefault(t, []).append(title or "")

    for tag, score in tag_counts.most_common(20):
        norm = _normalize(tag)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        samples = [s for s in sample_titles_by_tag.get(tag, [])[:3] if s]
        # Score floor: tag must appear at least 3 times to be theme-worthy.
        if score < 3:
            continue
        proposals.append(ThemeProposal(
            name=norm, score=score * 3,
            sample_titles=samples,
            source="vault-tag",
            default_level=1 if score < 8 else 2,
        ))

    # ── Top project names from ~/repos and similar (filesystem) ────────────
    try:
        from .discover import discover, suggest_code_dirs, detect_preferred_language
        found = discover()
        code_roots = suggest_code_dirs(found)
        for root in code_roots[:3]:
            try:
                children = sorted(
                    [p.name for p in root.iterdir() if p.is_dir()
                     and not p.name.startswith(".")]
                )[:8]
            except OSError:
                continue
            for name in children:
                norm = _normalize(name)
                if not norm or norm in seen or norm in _BORING_TAGS:
                    continue
                seen.add(norm)
                proposals.append(ThemeProposal(
                    name=norm, score=4,
                    sample_titles=[],
                    source="fs-project",
                    default_level=1,
                ))

        lang = detect_preferred_language()
        if lang and _normalize(lang) not in seen:
            seen.add(_normalize(lang))
            proposals.append(ThemeProposal(
                name=_normalize(lang), score=6,
                sample_titles=[],
                source="fs-lang",
                default_level=1,
            ))
    except Exception:
        pass

    proposals.sort(key=lambda p: -p.score)
    return proposals[:max_themes]


# ── Message generation ────────────────────────────────────────────────────

# Style cycle: spread messages across LCARS palette + pride colours so a
# single knob feels visually varied in the make_it_so pool.
_STYLE_CYCLE = ["lcars1", "lcars2", "lcars3", "info", "pride.green",
                "pride.violet", "pride.blue"]


def _template_messages(theme: str, samples: list[str]) -> list[list[str]]:
    """Deterministic fallback messages for a theme — no LLM needed.

    Returns list of [text, style] pairs in the format the knob system
    already expects (see cli._read_user_knobs).
    """
    # A small grab-bag of phrasings; reuse across themes by interpolation.
    phrasings = [
        "◀ Engaging {theme} systems.",
        "◀ {theme} vector locked.",
        "▶ {theme} subroutine complete.",
        "◀ Resolved via the {theme} pathway.",
        "▶ Honouring the {theme} of it all.",
        "◀ {theme}-flavoured success.",
    ]
    msgs: list[list[str]] = []
    label = theme.replace("-", " ").replace("_", " ")
    for i, p in enumerate(phrasings):
        msgs.append([p.format(theme=label.title()), _STYLE_CYCLE[i % len(_STYLE_CYCLE)]])
    if samples:
        for i, s in enumerate(samples[:2]):
            short = s if len(s) <= 40 else s[:37] + "…"
            msgs.append([f"◀ Honouring \"{short}\".", _STYLE_CYCLE[(i + 2) % len(_STYLE_CYCLE)]])
    return msgs


def _llm_messages(theme: str, samples: list[str], model: str,
                   base_url: str, n: int = 6) -> list[list[str]] | None:
    """Ask the configured chat model for theme-flavoured make_it_so lines.

    Returns list of [text, style] pairs, or None on any error so the caller
    can fall back to templates.
    """
    try:
        from .llm import chat
    except Exception:
        return None
    sample_str = ("\nSample note titles tagged with this theme:\n  - "
                  + "\n  - ".join(samples[:3])) if samples else ""
    sys = (
        "You write short, punchy completion messages for a CLI in the "
        "spirit of LCARS Star Trek consoles. Each message is one line, "
        "8-14 words, lowercase 'casual sci-fi'. No emojis. No markdown."
    )
    prompt = (
        f"Theme: {theme}{sample_str}\n\n"
        f"Generate exactly {n} completion messages celebrating a successful "
        f"action through the lens of this theme. One per line, no numbering, "
        f"no quotes, no preamble. Vary the tone."
    )
    try:
        resp = chat(prompt, model=model, base_url=base_url, system=sys)
    except Exception:
        return None
    if not resp:
        return None
    lines = [l.strip(" -–—•").strip() for l in resp.splitlines()]
    lines = [l for l in lines if l and 4 <= len(l) <= 100]
    if len(lines) < 3:
        return None
    msgs: list[list[str]] = []
    for i, line in enumerate(lines[:n]):
        # Add the LCARS arrow in front so they read like other make_it_so
        # entries; pick a style from the rotation.
        if not line.startswith(("◀", "▶")):
            line = ("◀ " if i % 2 == 0 else "▶ ") + line
        msgs.append([line, _STYLE_CYCLE[i % len(_STYLE_CYCLE)]])
    return msgs


def generate_messages(proposal: ThemeProposal, model: str = "",
                       base_url: str = "", use_llm: bool = True) -> list[list[str]]:
    """Pick LLM or template messages for a theme. Always returns at least 4."""
    if use_llm and model and base_url:
        msgs = _llm_messages(proposal.name, proposal.sample_titles,
                              model=model, base_url=base_url)
        if msgs:
            return msgs
    return _template_messages(proposal.name, proposal.sample_titles)


# ── Apply: write to the user_theme_knobs config row ───────────────────────

def proposals_to_knobs(proposals: list[ThemeProposal], model: str = "",
                        base_url: str = "", use_llm: bool = True) -> list[dict]:
    """Convert detected ThemeProposals into the dict shape `_read_user_knobs`
    expects: {name, default_level, messages: [[text, style], ...]}."""
    out: list[dict] = []
    for p in proposals:
        msgs = generate_messages(p, model=model, base_url=base_url,
                                  use_llm=use_llm)
        out.append({
            "name":          p.name,
            "default_level": p.default_level,
            "messages":      msgs,
            "_source":       p.source,   # provenance, not consumed by ui.py
        })
    return out
