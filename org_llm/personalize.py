"""Auto-personalization: derive theme knobs from real content via the LLM.

The throughline of org-llm is that the *app* should know what the user has,
not force the user to describe themselves. `personalize` reads the vault
(top tags, recent titles, body excerpts), the filesystem (project READMEs
via discover), and the corpus (preferred language) — then asks the LLM
to synthesize **evocative theme names** from that content, not literal
identifier strings.

A theme is a single short word or hyphenated phrase capturing an
*aesthetic*, *interest*, or *vibe*. Good: synthwave, homelab, solarpunk,
espresso, federation, dark-academia, cottage-witch, brutalism. Bad:
project-name-1, exam-cert-code, todo, agenda, python.

Each theme becomes a `knob` — a named bundle of `make_it_so` completion
messages controlled by ORG_LLM_<NAME>_LEVEL. The messages reference the
theme's actual *imagery* (neon, cassettes, garden tools, patrons of art,
…), not just the theme name as a noun.

The whole flow defaults to LLM-driven; --no-llm falls back to a strict
deterministic filter that produces FEWER themes rather than dumb ones.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib  import Path


# Tag-shape patterns we explicitly skip. These rarely produce good themes
# and when they do, the LLM picks them up via the title/body channels.
_BORING_TAGS = {
    "todo", "done", "draft", "wip", "test", "tmp", "fixme", "note",
    "personal", "work", "misc", "general", "archive",
    "project", "code",
    # built-ins
    "trek", "commie", "queer",
    # very-generic words that appear across many notes
    "audit", "report", "walkthrough", "doctor", "setup", "tour",
    "org-llm", "bench", "fixer-bench", "testing",
}


def _looks_like_identifier(s: str) -> bool:
    """Reject tokens that read as identifiers/codes rather than concepts.

    Tableau-associate-architect-partner-exam, beanhub-sync-to-github,
    bh-gh-spcs — those are project/exam/internal codes, not themes.
    """
    if not s:
        return True
    if "_" in s:
        return True               # snake_case → likely identifier
    if s.count("-") >= 2:
        return True               # multi-segment hyphenated → likely project name
    if len(s) > 20:
        return True               # long single tokens → identifier-ish
    if any(ch.isdigit() for ch in s):
        return True               # contains digits → version / code
    if len(s.split("-")) > 2:
        return True
    return False


@dataclass
class ThemeProposal:
    """One auto-detected theme that could become a knob."""
    name:          str           # canonical knob name (lowercase, [a-z0-9_-]+)
    score:         int           # bigger = more central to the user
    sample_titles: list[str]     # short examples / reasoning that anchored this theme
    source:        str           # "llm-synthesized" | "fallback-tag"
    default_level: int = 2       # 0..3; 2 maps to 1× pool weight
    reason:        str = ""      # LLM's one-line explanation, when available


def _normalize(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"[^a-z0-9_\- ]+", "", n)
    n = re.sub(r"\s+", "-", n).strip("-_")
    return n


# ── Evidence gathering ────────────────────────────────────────────────────

def _gather_content_evidence(session) -> dict:
    """Pull the kind of evidence a *human* would skim to figure out what
    someone is interested in: titles, body excerpts, top tags, project
    READMEs. The LLM then synthesizes themes from this material."""
    from .db import Node

    # Top-N non-boring, non-identifier tags from the merged file+auto-tag
    # set — both buckets contribute equally to "what is this user about?".
    tag_counts: Counter = Counter()
    for tags, auto in session.query(Node.tags, Node.auto_tags).all():
        for raw in ((tags or "") + " " + (auto or "")).split():
            t = raw.strip().lower()
            if not t or t in _BORING_TAGS:
                continue
            if t == "code" or t.startswith("code:"):
                continue
            if _looks_like_identifier(t):
                continue
            tag_counts[t] += 1
    top_tags = [(t, c) for t, c in tag_counts.most_common(20) if c >= 2]

    # Recent note titles + first 200 chars of body. Skip code-tagged nodes.
    recent_titles: list[str] = []
    body_excerpts: list[str] = []
    rows = (
        session.query(Node.title, Node.body, Node.tags, Node.auto_tags)
        .order_by(Node.mtime.desc())
        .limit(60).all()
    )
    for title, body, tags, auto_tags in rows:
        ttags = (tags or "") + " " + (auto_tags or "")
        if "code" in ttags.split():
            continue
        if title:
            recent_titles.append(title.strip()[:80])
        if body and len(recent_titles) <= 18:
            ex = body.strip()[:240].replace("\n", " ")
            if ex:
                body_excerpts.append(f"({title or '?'}) {ex}")
        if len(recent_titles) >= 25:
            break

    # Project README first paragraph (one per project)
    project_summaries: list[str] = []
    try:
        from .discover import discover, detect_preferred_language
        for f in discover():
            if f.kind != "repos-root":
                continue
            try:
                children = sorted(p for p in f.path.iterdir()
                                   if p.is_dir() and not p.name.startswith("."))
            except OSError:
                continue
            for child in children[:8]:
                readme = next((child / n for n in
                                ("README.md", "README.org", "Readme.md", "README")
                                if (child / n).exists()), None)
                if not readme:
                    continue
                try:
                    text = readme.read_text(errors="replace")
                except Exception:
                    continue
                # First non-trivial paragraph (skip headings / badges)
                para = ""
                for line in text.splitlines():
                    s = line.strip()
                    if not s:
                        if para:
                            break
                        continue
                    if s.startswith(("#", "*", "=", "[![", "<!", "---")):
                        continue
                    para += (" " if para else "") + s
                    if len(para) >= 240:
                        break
                if para:
                    project_summaries.append(f"{child.name}: {para[:240]}")
            if len(project_summaries) >= 8:
                break
    except Exception:
        pass

    try:
        from .discover import detect_preferred_language
        lang = detect_preferred_language()
    except Exception:
        lang = ""

    return {
        "top_tags":          top_tags,
        "recent_titles":     recent_titles,
        "body_excerpts":     body_excerpts,
        "project_summaries": project_summaries,
        "preferred_lang":    lang,
        "has_signal":        bool(recent_titles or top_tags or project_summaries),
    }


# ── LLM-driven theme synthesis ────────────────────────────────────────────

_THEME_SYNTHESIS_SYSTEM = """\
You analyse a person's notes and code projects to propose EVOCATIVE
THEME NAMES for a CLI completion-message theming system.

A theme is a single short word or hyphenated phrase capturing an
AESTHETIC, INTEREST, or VIBE — not a literal noun describing what
they DO. Aim for theme names you'd see on a moodboard.

GOOD theme names (evocative, aesthetic, vibe-y):
  synthwave  homelab  solarpunk  espresso  federation
  cottage-witch  dark-academia  brutalism  cyberpunk
  mountaineering  hifi  zine-culture  pirate-radio
  off-grid  jazz-cafe  warpcore  guild-hall

BAD theme names (literal, identifier-shaped, project-y):
  project-bh-gh-spcs  beanhub-sync-to-github
  tableau-associate-exam  python  agenda  todo  work
  user  meeting  documentation

Rules:
  - Each theme name: lowercase, ≤ 16 chars, ≤ 2 words (hyphenated).
  - NEVER repeat or paraphrase a literal project name from the input.
  - NEVER use the user's preferred programming language as a theme
    unless their notes show CULTURE around it (lisp.scheme, rustlang
    discourse), not just usage.
  - Themes should *imply* something specific — neon synths for
    "synthwave", server racks for "homelab", patrons of art for
    "renaissance".
  - Prefer fewer, sharper themes over many shallow ones. If the
    evidence is weak, propose only 2-3.

Output STRICT JSON, no prose, no markdown:

  {"themes": [
    {"name": "synthwave",
     "reason": "frequent music notes about retro electronic / 80s soundtracks",
     "imagery": "neon arcades, cassette decks, gradient sunsets"},
    {"name": "homelab",
     "reason": "Proxmox, k8s, self-host notes recur",
     "imagery": "server racks, ethernet, soft 2 a.m. fan hum"}
  ]}

If the input genuinely doesn't suggest a coherent vibe, return
{"themes": []} rather than reaching for forced suggestions.
"""


def _llm_synthesize_themes(evidence: dict, *, model: str, base_url: str,
                             max_themes: int = 5) -> list[ThemeProposal]:
    """Ask the local LLM to extract evocative themes from evidence."""
    if not evidence.get("has_signal"):
        return []
    try:
        from .llm import chat
    except Exception:
        return []

    titles_str = "\n".join(f"  - {t}" for t in evidence["recent_titles"][:25])
    tags_str   = "\n".join(f"  - {t} (×{c})" for t, c in evidence["top_tags"][:15])
    bodies_str = "\n\n".join(f"  {b}" for b in evidence["body_excerpts"][:12])
    proj_str   = "\n".join(f"  - {p}" for p in evidence["project_summaries"][:8])
    lang       = evidence.get("preferred_lang") or "(unknown)"

    # Trim aggressively — small models are slow on long prompts. Cap each
    # bucket; the LLM doesn't need a saga to extract themes.
    user_prompt = f"""Note titles ({len(evidence['recent_titles'])}):
{titles_str[:1200] or "  (none)"}

Top tags:
{tags_str[:600] or "  (none)"}

Body excerpts:
{bodies_str[:1500] or "  (none)"}

Project READMEs:
{proj_str[:800] or "  (none)"}

Lang: {lang}

Propose up to {max_themes} evocative themes. JSON only."""

    def _strip_fences(s: str) -> str:
        s = (s or "").strip()
        if not s.startswith("```"):
            return s
        lines = s.splitlines()[1:]
        if lines and lines[-1].rstrip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    # Run with a hard timeout so a slow / hung model can't block the user.
    # Wrap in the themed `thinking` spinner so the wait isn't silent.
    import threading
    try:
        from .ui import thinking
    except Exception:
        thinking = None
    result: dict = {"resp": "", "error": ""}
    def _run():
        try:
            result["resp"] = chat(user_prompt, model=model, base_url=base_url,
                                   system=_THEME_SYNTHESIS_SYSTEM) or ""
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
    if thinking is not None:
        with thinking("Synthesising themes", model=model):
            t = threading.Thread(target=_run, daemon=True)
            t.start(); t.join(timeout=90.0)
    else:
        t = threading.Thread(target=_run, daemon=True)
        t.start(); t.join(timeout=90.0)
    # Stash the failure mode on the function itself so the caller can
    # surface it to the user. Same pattern as messages_from_vibe.
    if t.is_alive():
        _llm_synthesize_themes.last_error = "timed out after 90s"
        return []
    if result["error"]:
        _llm_synthesize_themes.last_error = result["error"]
        return []
    if not result["resp"]:
        _llm_synthesize_themes.last_error = "model returned empty response"
        return []
    cleaned = _strip_fences(result["resp"])
    try:
        plan = json.loads(cleaned)
    except Exception as e:
        _llm_synthesize_themes.last_error = (
            f"JSON parse failed: {type(e).__name__}: {e}. "
            f"Raw response (first 200 chars): {cleaned[:200]!r}"
        )
        return []

    raw_themes = (plan.get("themes") or [])
    rejected: list[str] = []
    proposals: list[ThemeProposal] = []
    for raw in raw_themes[:max_themes]:
        name_orig = str(raw.get("name", ""))
        name = _normalize(name_orig)
        if not name or len(name) > 24:
            rejected.append(f"{name_orig!r} (length)")
            continue
        if name in _BORING_TAGS:
            rejected.append(f"{name!r} (boring)")
            continue
        if _looks_like_identifier(name):
            rejected.append(f"{name!r} (identifier-shaped)")
            continue
        reason  = (raw.get("reason")  or "")[:200]
        imagery = (raw.get("imagery") or "")[:200]
        # Pack imagery into sample_titles so message generation can
        # reference it without re-prompting.
        anchors = [imagery] if imagery else []
        proposals.append(ThemeProposal(
            name=name, score=10,
            sample_titles=anchors,
            source="llm-synthesized",
            default_level=2,
            reason=reason,
        ))
    # If the LLM produced themes but ALL got filtered, surface that —
    # the user otherwise sees fallback-tag with no clue why.
    if raw_themes and not proposals:
        _llm_synthesize_themes.last_error = (
            f"LLM produced {len(raw_themes)} themes but all rejected: "
            + "; ".join(rejected[:5])
        )
    elif proposals:
        _llm_synthesize_themes.last_error = ""
    return proposals


# ── Deterministic fallback (--no-llm) ─────────────────────────────────────

def _deterministic_fallback(evidence: dict,
                              max_themes: int = 5) -> list[ThemeProposal]:
    """When the LLM is unavailable, pick a SMALL number of credible themes
    from the filtered tag list. Strict: better to propose 0-2 themes
    than to surface project names or exam codes.
    """
    proposals: list[ThemeProposal] = []
    seen: set[str] = set()
    for tag, count in evidence["top_tags"][:max_themes * 2]:
        norm = _normalize(tag)
        if not norm or norm in seen:
            continue
        if _looks_like_identifier(norm) or norm in _BORING_TAGS:
            continue
        # Single-word, ≥ 5 occurrences — only the strongest signals.
        if "-" in norm or count < 5:
            continue
        seen.add(norm)
        proposals.append(ThemeProposal(
            name=norm, score=count,
            sample_titles=[],
            source="fallback-tag",
            default_level=1,
            reason=f"appears {count}x as a single-word tag",
        ))
        if len(proposals) >= max_themes:
            break
    return proposals


def detect_themes(session, max_themes: int = 5, *,
                   model: str = "", base_url: str = "",
                   use_llm: bool = True) -> list[ThemeProposal]:
    """Public entry point. LLM-driven by default; deterministic fallback
    when use_llm=False or LLM call fails."""
    evidence = _gather_content_evidence(session)
    if use_llm and model and base_url:
        themes = _llm_synthesize_themes(evidence, model=model,
                                          base_url=base_url,
                                          max_themes=max_themes)
        if themes:
            return themes
    return _deterministic_fallback(evidence, max_themes=max_themes)


# ── Message generation ────────────────────────────────────────────────────

# Style cycle: spread messages across LCARS palette + pride colours so a
# single knob feels visually varied in the make_it_so pool.
_STYLE_CYCLE = ["lcars1", "lcars2", "lcars3", "info", "pride.green",
                "pride.violet", "pride.blue"]


def _template_messages(theme: str, samples: list[str]) -> list[list[str]]:
    """Deterministic fallback messages — boring on purpose so the user
    notices and configures cloud for richer copy."""
    label = theme.replace("-", " ").replace("_", " ").title()
    msgs = [
        [f"◀ {label}: ready.",         "lcars1"],
        [f"▶ Done. ({label})",         "lcars2"],
        [f"◀ {label} flow complete.",  "lcars3"],
        [f"▶ Holding the {label} line.", "info"],
    ]
    return msgs


_MESSAGE_SYSTEM = """\
Write punchy CLI completion messages flavoured with a given theme.

Each message celebrates a successful action through the lens of the
theme's imagery — references CONCRETE objects/sensations from that
world, not the theme name as a generic noun.

Length: 6-15 words per message. Style: terse, evocative, ≤1 metaphor
per line, no exclamation points. No emojis. No markdown. Lowercase.

Format: prefix odd-indexed messages with "◀ " and even-indexed with
"▶ " (LCARS arrows). One message per line, no numbering, no quotes.

Examples for theme "synthwave":
◀ Neon corridors aligned. Operation green.
▶ Saved. The synth swells into the chorus.
◀ Arpeggios wrapped, gradient locked, ready.
▶ Done — like an '85 dashboard catching the sunset.

Examples for theme "homelab":
◀ Rack rebooted, lights green, fans humming.
▶ Yaml committed. The cluster hums approval.
◀ Backup snapshot through the sled like clockwork.
▶ Provisioned. The lab is patient and self-hosted.

Examples for theme "espresso":
◀ Pulled clean, crema golden, ready for service.
▶ Saved. Tamped, dosed, dialled.
◀ Yield locked at 2:1. Perfect.
"""


def _llm_messages(proposal: ThemeProposal, *, model: str,
                   base_url: str, n: int = 6) -> list[list[str]] | None:
    """Generate themed messages with concrete imagery. Falls back to None
    so the caller can use templates instead."""
    try:
        from .llm import chat
    except Exception:
        return None
    imagery = proposal.sample_titles[0] if proposal.sample_titles else ""
    reason  = proposal.reason or ""
    prompt = (
        f"Theme: {proposal.name}\n"
        + (f"Imagery the theme suggests: {imagery}\n" if imagery else "")
        + (f"Why it fits the user: {reason}\n" if reason else "")
        + f"\nGenerate exactly {n} messages."
    )
    try:
        from .ui import thinking
        with thinking(f"Writing {proposal.name}", model=model):
            resp = chat(prompt, model=model, base_url=base_url,
                         system=_MESSAGE_SYSTEM)
    except Exception:
        try:
            resp = chat(prompt, model=model, base_url=base_url,
                         system=_MESSAGE_SYSTEM)
        except Exception:
            return None
    if not resp:
        return None
    lines = []
    for raw in resp.splitlines():
        l = raw.strip(" -–—•\"'").strip()
        # Keep messages already-prefixed with ◀ or ▶, otherwise add one.
        if not l:
            continue
        if not (l.startswith("◀") or l.startswith("▶")):
            l = ("◀ " if len(lines) % 2 == 0 else "▶ ") + l
        if 8 <= len(l) <= 110:
            lines.append(l)
        if len(lines) >= n:
            break
    if len(lines) < 3:
        return None
    msgs: list[list[str]] = []
    for i, l in enumerate(lines[:n]):
        msgs.append([l, _STYLE_CYCLE[i % len(_STYLE_CYCLE)]])
    return msgs


def generate_messages(proposal: ThemeProposal, model: str = "",
                       base_url: str = "", use_llm: bool = True) -> list[list[str]]:
    """LLM messages with imagery; templates as fallback."""
    if use_llm and model and base_url:
        msgs = _llm_messages(proposal, model=model, base_url=base_url)
        if msgs:
            return msgs
    return _template_messages(proposal.name, proposal.sample_titles)


def messages_from_vibe(
    name: str,
    *,
    vibe: str = "",
    specifics: dict[str, str] | None = None,
    model: str,
    base_url: str,
    n: int = 8,
    seed_messages: list[list[str]] | None = None,
) -> list[list[str]] | None:
    """LLM-generate themed messages for a user-specified knob.

    Free-form `vibe` describes the world (e.g. "1980s neon, late-night
    coding"). `specifics` is a small dict of detail directives the user
    wants reflected in EVERY message — font name, icon, wording quirk,
    color, sound, image. Optional `seed_messages` give the LLM concrete
    style anchors before it writes more.

    Returns the full message list (seed + new) on success, None on
    failure so the caller can fall back to manual flags or templates.
    """
    try:
        from .llm import chat
    except Exception:
        return None
    spec_lines = []
    for k, v in (specifics or {}).items():
        spec_lines.append(f"  - {k}: {v}")
    spec_block = ("\n".join(spec_lines) or "  (none)")
    seed_block = ""
    if seed_messages:
        seed_block = "User-provided seed messages (preserve style):\n"
        for m in seed_messages:
            text = m[0] if isinstance(m, list) and m else str(m)
            seed_block += f"  - {text}\n"
    user_msg = (
        f"Theme name: {name}\n"
        f"Vibe: {vibe or '(unstated — infer from the name)'}\n\n"
        f"Specifics the user wants reflected:\n{spec_block}\n\n"
        f"{seed_block}"
        f"Generate exactly {n} new messages in the style above."
    )
    # Same SYSTEM prompt as _llm_messages but tweaked to honour the
    # specifics dict — each line should reference at least one of the
    # specifics when sensible, without sounding forced.
    sys_msg = (
        _MESSAGE_SYSTEM
        + "\n\nWHEN THE USER SUPPLIES SPECIFICS:\n"
          "- Honor every concrete detail the user named (a font name, "
          "a particular icon, a phrase, a color, a sound). Each detail "
          "should appear in AT LEAST ONE message; spread them across "
          "the bundle.\n"
          "- If the user named seed messages above, match their voice "
          "and don't duplicate them.\n"
    )
    last_error: str = ""
    resp = ""
    try:
        from .ui import thinking
        with thinking(f"Writing {name}", model=model):
            resp = chat(user_msg, model=model, base_url=base_url,
                         system=sys_msg, timeout=120.0)
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        try:
            resp = chat(user_msg, model=model, base_url=base_url,
                         system=sys_msg, timeout=120.0)
        except Exception as e2:
            last_error = f"{type(e2).__name__}: {e2}"
            messages_from_vibe.last_error = last_error  # surface to caller
            return None
    if not resp:
        messages_from_vibe.last_error = (
            last_error or "model returned empty response (likely OOM/timeout)"
        )
        return None
    lines: list[str] = []
    for raw in resp.splitlines():
        l = raw.strip(" -–—•\"'").strip()
        if not l:
            continue
        if not (l.startswith("◀") or l.startswith("▶")):
            l = ("◀ " if len(lines) % 2 == 0 else "▶ ") + l
        if 8 <= len(l) <= 140:
            lines.append(l)
        if len(lines) >= n:
            break
    if len(lines) < 3:
        messages_from_vibe.last_error = (
            f"only {len(lines)} valid lines in {len(resp)}-char response "
            "(model may have refused / hallucinated commentary)"
        )
        return None
    # Successful — clear any prior error sentinel so callers can rely on it
    messages_from_vibe.last_error = ""
    msgs: list[list[str]] = []
    if seed_messages:
        msgs.extend([list(m) for m in seed_messages
                       if isinstance(m, list) and len(m) >= 1])
    for i, l in enumerate(lines[:n]):
        msgs.append([l, _STYLE_CYCLE[(len(msgs) + i) % len(_STYLE_CYCLE)]])
    return msgs


# ── Apply: write to the user_theme_knobs config row ───────────────────────

def proposals_to_knobs(proposals: list[ThemeProposal], model: str = "",
                        base_url: str = "", use_llm: bool = True) -> list[dict]:
    """Convert ThemeProposals into the dict shape `_read_user_knobs`
    expects: {name, default_level, messages: [[text, style], ...]}."""
    out: list[dict] = []
    for p in proposals:
        msgs = generate_messages(p, model=model, base_url=base_url,
                                  use_llm=use_llm)
        out.append({
            "name":          p.name,
            "default_level": p.default_level,
            "messages":      msgs,
            "_source":       p.source,
            "_reason":       p.reason,
        })
    return out
