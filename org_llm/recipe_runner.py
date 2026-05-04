"""Manager-side deterministic recipe execution.

Phase 22 v2: when a recipe matches a user message, the manager
EXECUTES the underlying tool itself (no LLM round-trip) and
synthesizes the assistant response. This enforces tool use without
trusting the LLM to emit a real tool call — and it does so at zero
cloud cost.

Falls back to advisory mode (recipe body injected into agent prompt)
when the runner can't fully resolve the request — e.g. the phrase
doesn't yield a usable regex.

Wired in `llm_proxy.py` at the recipe injection point. Each runner
returns a `RunResult` (synthesized answer + tool trace for the
manager-log) or None to signal "I can't deterministically resolve
this, use advisory mode instead".
"""

from __future__ import annotations

import functools
import os
import re as _re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .orchestration import RecipeMatch


@dataclass
class RunResult:
    answer:       str                 # human-readable assistant content
    tool_name:    str                 # which tool actually ran (for log)
    tool_args:    dict[str, Any]      # what we passed it
    tool_result:  dict[str, Any] = field(default_factory=dict)  # truncated trace
    duration_ms:  int = 0


# ── public entry point ────────────────────────────────────────────────────────

def execute_recipe(recipe: RecipeMatch, user_text: str) -> RunResult | None:
    """Try to deterministically execute `recipe` for the given user
    text. Returns a RunResult on success; None means "fall back to
    advisory mode (inject recipe body into agent prompt)"."""
    runner = _RUNNERS.get(recipe.name)
    if runner is None:
        return None
    try:
        t0 = time.time()
        result = runner(user_text)
        if result is None:
            return None
        if not result.duration_ms:
            result.duration_ms = int((time.time() - t0) * 1000)
        return result
    except Exception:
        return None


# ── count_across_vault runner ─────────────────────────────────────────────────

def _run_count_across_vault(user_text: str) -> RunResult | None:
    """Translate the user's count question into a regex + path_glob,
    call `org_count_matches`, format an answer."""
    if not user_text:
        return None
    phrase, path_glob = _strip_count_question(user_text)
    if not phrase:
        return None
    pattern = _phrase_to_regex(phrase)
    if not pattern:
        return None

    from . import org_tools as _ot
    r = _ot.org_count_matches(pattern, path_glob=path_glob)
    if r.get("error"):
        return None

    total = int(r.get("total_hits", 0))
    files = int(r.get("files_with_hits", 0))
    top   = list(r.get("top_files") or [])[:3]

    if total == 0:
        # Retry once with a broader regex (drop the [X]/DONE anchor)
        # so we don't false-zero on un-checkboxed mentions.
        broad = _phrase_to_regex(phrase, anchor_done=False)
        if broad and broad != pattern:
            r2 = _ot.org_count_matches(broad, path_glob=path_glob)
            t2 = int(r2.get("total_hits", 0))
            if t2 > 0:
                pattern = broad
                total   = t2
                files   = int(r2.get("files_with_hits", 0))
                top     = list(r2.get("top_files") or [])[:3]

    answer = _format_count_answer(phrase, path_glob, pattern,
                                    total, files, top)
    return RunResult(
        answer=answer,
        tool_name="org_count_matches",
        tool_args={"pattern": pattern, "path_glob": path_glob},
        tool_result={
            "total_hits":      total,
            "files_with_hits": files,
            "top_files":       [list(t) for t in top],
        },
    )


_SCOPE_RE = _re.compile(
    # Two prepositions in practice: "in my dailies" and (finding-6,
    # 2026-05-03) "across the vault". Without `across` here, the
    # tokeniser folded `across the vault` into the count regex and
    # the runner returned 0 hits on prompts the user clearly tracks
    # (e.g. A2 "how often have I exercised across the vault?" lost
    # 5/5 trials in the recipe A/B run).
    r"\b(?:in|across) (?:my |the )?(daily files?|dailies|notes|vault|"
    r"roam( notes)?|journal|all .*notes?)\b,?\s*",
    _re.IGNORECASE,
)
_DAILY_HINT_RE = _re.compile(
    r"\b(daily|dailies|journal|day-?notes?)\b", _re.IGNORECASE
)
# finding-5 (2026-05-03) — strip relative time-modifier phrases
# BEFORE tokenising, so they don't end up as content tokens. The
# user's question is "exercise this year", not "exercise this year
# year"; tokenising "year" as content used to produce nonsense
# regexes like `(exercis|...).{0,12}\byear\b` → 0 hits.
#
# Single broad regex that absorbs the optional preposition
# ("in"/"for"/"over") + optional article ("the") + the time
# qualifier ("this|last|past|next") + optional count ("30") +
# the unit ("month"/"week"/"day"/...). Applied first so leftover
# fragments don't leak into tokenisation.
_TIME_MODIFIER_RES = [
    _re.compile(
        r"\b(?:in|for|over|during)?\s*(?:the\s+)?"
        r"(?:this|last|next|past)\s+"
        r"(?:\d+\s+)?(?:year|month|week|day|quarter)s?\b",
        _re.IGNORECASE,
    ),
    _re.compile(
        r"\b(?:in|for|over|during)\s+(?:the\s+)?"
        r"(?:\d+\s+)(?:year|month|week|day|quarter)s?\b",
        _re.IGNORECASE,
    ),
    _re.compile(
        r"\b(?:today|yesterday|tomorrow|recently|lately|so far)\b",
        _re.IGNORECASE,
    ),
    _re.compile(r"\bin\s+\d{4}\b", _re.IGNORECASE),
    _re.compile(
        r"\bin\s+(?:january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\b",
        _re.IGNORECASE,
    ),
]

_COUNT_PREFIX_RES = [
    _re.compile(r"^how many times (?:have i |did i |do i |i )?",
                 _re.IGNORECASE),
    _re.compile(r"^how often (?:have i |do i |did i |i )?",
                 _re.IGNORECASE),
    _re.compile(r"^count (?:of |the )?(?:times )?(?:i )?",
                 _re.IGNORECASE),
    _re.compile(r"^tally (?:of |the )?(?:times )?(?:i )?",
                 _re.IGNORECASE),
    _re.compile(r"^total (?:of |the )?(?:times )?(?:i )?",
                 _re.IGNORECASE),
    _re.compile(r"^frequency (?:of |the )?(?:i )?",
                 _re.IGNORECASE),
]


def _strip_count_question(text: str) -> tuple[str, str]:
    """Return (content_phrase, path_glob).

    Strips scope markers ("in dailies"), question prefixes
    ("how many times have I"), and trailing punctuation.
    Defaults path_glob to `**/*.org`; switches to `daily/**/*.org`
    when the user mentions dailies/journal."""
    t = (text or "").strip().rstrip("?!. ")
    daily = bool(_DAILY_HINT_RE.search(t))
    # 0. relative time-modifier phrases (finding-5) — strip
    # before scope/prefix/tokenisation so they don't become
    # content tokens.
    for pat in _TIME_MODIFIER_RES:
        t = pat.sub(" ", t)
    # 1. scope marker (in dailies / in my notes …)
    t = _SCOPE_RE.sub(" ", t).strip(" ,").strip()
    # 2. count question prefix
    for pat in _COUNT_PREFIX_RES:
        new = pat.sub("", t, count=1)
        if new != t:
            t = new.strip()
            break
    # 3. residual leading words that sometimes survive
    t = _re.sub(r"^(?:that |where )", "", t, flags=_re.IGNORECASE).strip()
    path_glob = "daily/**/*.org" if daily else "**/*.org"
    return t, path_glob


_STOPWORDS = {
    "i", "my", "the", "a", "an", "of", "in", "on", "to", "for",
    "and", "or", "with", "at", "by", "have", "has", "had",
    "did", "do", "does", "been", "be", "is", "was", "were",
    "this", "that", "these", "those", "it", "its",
}

# Common irregulars in dailies / chore questions. Keeps regex tight.
_IRREGULAR_VERBS: dict[str, list[str]] = {
    "made":  ["made", "make", "making"],
    "make":  ["made", "make", "making"],
    "makes": ["made", "make", "making", "makes"],
    "ate":   ["ate", "eat", "eating", "eaten"],
    "eat":   ["ate", "eat", "eating", "eaten"],
    "went":  ["went", "go", "going", "gone", "goes"],
    "go":    ["went", "go", "going", "gone", "goes"],
    "ran":   ["ran", "run", "running", "runs"],
    "run":   ["ran", "run", "running", "runs"],
    "drove": ["drove", "drive", "driving", "drives"],
    "drive": ["drove", "drive", "driving", "drives"],
    "wrote": ["wrote", "write", "writing", "written", "writes"],
    "write": ["wrote", "write", "writing", "written", "writes"],
    "took":  ["took", "take", "taking", "taken", "takes"],
    "take":  ["took", "take", "taking", "taken", "takes"],
    "saw":   ["saw", "see", "seeing", "seen", "sees"],
    "see":   ["saw", "see", "seeing", "seen", "sees"],
    "did":   ["did", "do", "doing", "done", "does"],
    "do":    ["did", "do", "doing", "done", "does"],
    "had":   ["had", "have", "having", "has"],
    "have":  ["had", "have", "having", "has"],
    "got":   ["got", "get", "getting", "gotten", "gets"],
    "get":   ["got", "get", "getting", "gotten", "gets"],
}


def _verb_stems(word: str) -> list[str]:
    """Return likely tense variants of `word` for permissive matching.

    Tries to recover the lemma first, then dispatches through the
    irregular table when the lemma is irregular (so 'running' →
    'ran/run/running/...', not just regular -ed/-ing decoration)."""
    w = word.lower()
    if w in _IRREGULAR_VERBS:
        return _IRREGULAR_VERBS[w]

    # Derive lemma candidates; if any hits the irregular table, use it.
    lemma_candidates: list[str] = []
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        lemma_candidates.append(stem)
        if len(stem) >= 3 and stem[-1] == stem[-2]:
            lemma_candidates.append(stem[:-1])     # 'runn' → 'run'
        lemma_candidates.append(stem + "e")        # 'mak' → 'make'
    elif w.endswith("ied") and len(w) > 4:
        lemma_candidates.append(w[:-3] + "y")      # 'tried' → 'try'
    elif w.endswith("ed") and len(w) > 3:
        stem = w[:-2]
        lemma_candidates.append(stem)
        lemma_candidates.append(stem + "e")        # 'exercis' → 'exercise'
        if len(stem) >= 3 and stem[-1] == stem[-2]:
            lemma_candidates.append(stem[:-1])
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        lemma_candidates.append(w[:-1])
    for c in lemma_candidates:
        if c in _IRREGULAR_VERBS:
            return _IRREGULAR_VERBS[c]

    # Regular: build forward variants from the lemma we can guess.
    lemma = lemma_candidates[0] if lemma_candidates else w
    variants = {w, lemma}
    if lemma.endswith("e"):
        variants.update({lemma + "d", lemma[:-1] + "ing", lemma + "s"})
    elif lemma.endswith("y") and len(lemma) > 2 and lemma[-2] not in "aeiou":
        variants.update({lemma[:-1] + "ied", lemma[:-1] + "ies",
                          lemma + "ing"})
    else:
        variants.update({lemma + "s", lemma + "ed", lemma + "ing"})
    return sorted(variants)[:6]


@functools.lru_cache(maxsize=64)
def _phrase_to_regex(phrase: str, *, anchor_done: bool = True) -> str:
    """Build a permissive regex from a content phrase.

    'made my bed' → r'(?i)(\\[X\\]|\\bDONE\\b).*?\\b(made|make|making)\\b.{0,12}\\bbed\\b'
    'exercised'   → r'(?i)(\\[X\\]|\\bDONE\\b).*?\\b(exercis\\w*)\\b'

    With `anchor_done=False` the [X]/DONE prefix is dropped — used
    as a fallback when the strict version yields zero hits."""
    if not phrase:
        return ""
    tokens = _re.findall(r"[a-z][a-z0-9_-]*", phrase.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]
    if not tokens:
        return ""
    head_stems = _verb_stems(tokens[0])
    head_pat = ("(?:" + "|".join(_re.escape(s) for s in head_stems) + ")"
                 if len(head_stems) > 1 else _re.escape(head_stems[0]))
    rest_pats = [r"\b" + _re.escape(t) + r"\b" for t in tokens[1:]]
    if rest_pats:
        body = head_pat + "(?:.{0,12}" + ".{0,12}".join(rest_pats) + ")"
    else:
        body = head_pat
    body = r"\b" + body
    if anchor_done:
        return rf"(?i)(\[X\]|\bDONE\b).*?{body}"
    return rf"(?i){body}"


def _format_count_answer(phrase: str, path_glob: str, pattern: str,
                          total: int, files: int,
                          top_files: list) -> str:
    """Render the human answer the assistant turn returns."""
    scope = "dailies" if path_glob.startswith("daily/") else "the vault"
    if total == 0:
        return (f"**0 hits** for '{phrase}' in {scope}.\n\n"
                f"Searched with regex `{pattern}` — broaden the phrase "
                f"if you expected matches.")
    head = (f"**{total}** hit(s) across **{files}** file(s) "
             f"for '{phrase}' in {scope}.")
    lines = [head]
    if top_files:
        lines.append("")
        lines.append("Top files:")
        for entry in top_files:
            try:
                p, n = entry[0], entry[1]
            except (TypeError, IndexError, KeyError):
                continue
            lines.append(f"  {n:>4}  {os.path.basename(str(p))}")
    lines.append("")
    lines.append(f"_(regex `{pattern}` · scope `{path_glob}` · "
                  f"deterministic, no cloud round-trip)_")
    return "\n".join(lines)


# ── weather_aware_agenda runner ──────────────────────────────────────────────

def _run_weather_aware_agenda(user_text: str) -> RunResult | None:
    """Manager pre-fetches agenda + (when configured) forecast +
    outdoor-flagged items so the agent narrates one cloud call.

    HARDENED 2026-05-03 (post-A/B): the previous version returned
    None when weather wasn't configured, which fell through to
    advisory mode and triggered the A7 hallucination — the agent
    saw a recipe body claiming "manager pre-fetched data" with
    nothing under it and invented forecasts. The new shape:

      - weather configured + outdoor items present → full bundle
      - weather configured + no outdoor items      → forecast +
                                                       agenda; agent
                                                       says "nothing
                                                       weather-sensitive
                                                       this week"
      - weather NOT configured                     → agenda alone +
                                                       explicit
                                                       `weather: unavailable`
                                                       marker

    The runner ALWAYS returns a RunResult (no Nones except on a
    true exception). format_prefetch_block renders honestly; the
    recipe body tells the agent never to invent forecasts when
    they're missing.
    """
    weather_status = "ok"
    weather_error  = ""
    bundle: dict = {}
    try:
        from . import weather as _w
        bundle = _w.weather_for_agenda(days=7)
        if bundle.get("error"):
            weather_status = "unavailable"
            weather_error  = bundle["error"]
    except Exception as e:
        weather_status = "unavailable"
        weather_error  = f"{type(e).__name__}: {e}"

    # Always fetch agenda separately so we have something even when
    # weather fails. The bundle's agenda is only populated on the
    # weather-OK path; mirror it locally on the failure path.
    if weather_status == "ok":
        agenda_today    = (bundle.get("agenda") or {}).get("today")    or []
        agenda_upcoming = (bundle.get("agenda") or {}).get("upcoming") or []
        agenda_overdue  = (bundle.get("agenda") or {}).get("overdue")  or []
    else:
        try:
            from . import org_tools as _ot
            agenda = _ot.org_agenda(window_days=7)
            agenda_today    = agenda.get("today") or []
            agenda_upcoming = agenda.get("upcoming") or []
            agenda_overdue  = agenda.get("overdue") or []
        except Exception:
            agenda_today = agenda_upcoming = agenda_overdue = []

    return RunResult(
        answer="",
        tool_name="weather_for_agenda",
        tool_args={"days": 7},
        tool_result={
            "weather_status":  weather_status,
            "weather_error":   weather_error,
            "summary":         bundle.get("summary", "") if weather_status == "ok" else "",
            "outdoor_items":   bundle.get("outdoor_items") or [] if weather_status == "ok" else [],
            "daily_forecast":  ((bundle.get("forecast") or {}).get("daily") or []
                                  if weather_status == "ok" else []),
            "agenda_today":    agenda_today,
            "agenda_upcoming": agenda_upcoming,
            "agenda_overdue":  agenda_overdue,
            "stale":           ((bundle.get("forecast") or {}).get("stale", False)
                                  if weather_status == "ok" else False),
        },
    )


# ── pre-fetch block (manager → agent hand-off) ───────────────────────────────

def _user_context_addendum(recipe_name: str, run: RunResult,
                             request_text: str) -> str:
    """Phase 21 hook — inject relevant DB-backed vault facts into
    the pre-fetch block so the agent has accurate user context
    inline rather than guessing.

    Currently fires for `count_across_vault`: when the count
    target overlaps with a known routine chore (from the
    `routine_chores` fact), the agent learns this is part of the
    user's routine and can shape its narration accordingly
    ("you typically track this as a daily chore — current count
    in the requested window: N").

    Conservative on cost: only reads the cached fact (no
    recompute trigger). Returns "" if nothing relevant."""
    if recipe_name != "count_across_vault":
        return ""
    try:
        from . import vault_facts as _vf
    except Exception:
        return ""
    chores = (_vf.get_fact("routine_chores") or {}).get("chores") or []
    if not chores:
        return ""
    # Token-level overlap rather than literal substring: the user's
    # phrasing ("made my bed") rarely matches the chore stem
    # ("make bed") character-for-character. A 2-token shared head
    # is a strong-enough signal that this is a known chore.
    req_tokens = set(_re.findall(r"[a-z]{3,}", request_text.lower()))
    if not req_tokens:
        return ""
    for c in chores:
        cp = (c.get("phrase") or "").lower()
        if not cp:
            continue
        chore_tokens = set(_re.findall(r"[a-z]{3,}", cp))
        if not chore_tokens:
            continue
        overlap = chore_tokens & req_tokens
        if len(overlap) >= max(1, len(chore_tokens) // 2):
            return ("user_context: this matches a known recurring "
                    f"chore ('{cp}', logged {c['count']}× since "
                    f"{c.get('first_seen','?')}, most recently "
                    f"{c.get('last_seen','?')}).")
    return ""


def format_prefetch_block(recipe_name: str, run: RunResult,
                            request_text: str) -> str:
    """Compact 'manager already ran the tool for you' block to
    inject into the agent's system prompt. Lean by design — top 3
    files at most, no multi-screen JSON dumps. The agent's job is
    to narrate, not to re-do work the manager already did."""
    import json as _json
    args_repr = _json.dumps(run.tool_args, separators=(",", ":"))
    if len(args_repr) > 200:
        args_repr = args_repr[:200] + "..."
    res = run.tool_result or {}
    if "total_hits" in res:
        lines = [f"total_hits={res.get('total_hits')}, "
                  f"files_with_hits={res.get('files_with_hits')}"]
        for entry in (res.get("top_files") or [])[:3]:
            try:
                p, n = entry[0], entry[1]
                lines.append(f"  {n}× {os.path.basename(str(p))}")
            except (TypeError, IndexError, KeyError):
                continue
        body = "\n  ".join(lines)
    elif "weather_status" in res:
        # weather_aware_agenda shape — always renders; weather pieces
        # are gated by `weather_status` so the agent never sees a
        # bare body without data to ground it.
        lines: list[str] = []
        ws = res.get("weather_status", "unavailable")
        if ws == "ok":
            if res.get("summary"):
                lines.append(f"summary: {res['summary']}")
            if res.get("stale"):
                lines.append("[stale forecast — fetch failed; serving cache]")
            df = res.get("daily_forecast") or []
            if df:
                lines.append("forecast:")
                for d in df[:7]:
                    wmax = d.get("wind_max")
                    wind_str = (f"  wind {wmax:.0f}km/h"
                                  if isinstance(wmax, (int, float))
                                  else "")
                    lines.append(
                        f"  {d.get('date')}  {d.get('short','?'):<14s}  "
                        f"{d.get('t_min','?')}-{d.get('t_max','?')}°C  "
                        f"precip {d.get('precip_prob',0)}%{wind_str}"
                    )
            outdoor = res.get("outdoor_items") or []
            if outdoor:
                lines.append(f"outdoor_items_with_concerns ({len(outdoor)}):")
                for it in outdoor[:10]:
                    concerns = ", ".join(it.get("concerns") or []) or "fine"
                    lines.append(
                        f"  {it.get('date')}  [{it.get('state','?')}] "
                        f"{(it.get('item','') or '')[:50]}  → {concerns}"
                    )
                    for alt in (it.get("suggestions") or [])[:2]:
                        delta = alt.get("delta_days", 0)
                        delta_str = f"+{delta}d" if delta > 0 else f"{delta}d"
                        lines.append(
                            f"      ↳ try {alt.get('date')} ({delta_str})"
                            f": {alt.get('label')}, "
                            f"{alt.get('t_min','?')}-{alt.get('t_max','?')}°C, "
                            f"precip {alt.get('precip_prob',0)}%"
                        )
            else:
                lines.append("outdoor_items: none flagged this window.")
        else:
            err = res.get("weather_error", "not configured")
            lines.append(f"weather: unavailable ({err})")
        # Agenda lines render in BOTH branches — that's the always-
        # present floor.
        today    = res.get("agenda_today") or []
        upcoming = res.get("agenda_upcoming") or []
        overdue  = res.get("agenda_overdue") or []
        lines.append(
            f"agenda_counts: today={len(today)}, "
            f"upcoming={len(upcoming)}, overdue={len(overdue)}"
        )
        # Render each non-empty bucket with file:line so the agent
        # can cite specific tasks (matches org_agenda MCP tool's
        # format at mcp_server.py:3621). Without this, on-arm sees
        # only counts and falls back to vague paraphrase while
        # off-arm cites real files — measurable grounding gap on
        # weather_aware_agenda's A7 prompt (overdue=22, today=0).
        for label, items, top_n in (
            ("agenda_today",    today,    8),
            ("agenda_overdue",  overdue,  5),
            ("agenda_upcoming", upcoming, 5),
        ):
            if not items:
                continue
            lines.append(f"{label} ({len(items)}):")
            for it in items[:top_n]:
                date = it.get("scheduled") or it.get("deadline") or "—"
                text = (it.get("text", "") or "")[:60]
                lines.append(
                    f"  [{it.get('state','?')}] {date}  {text}  "
                    f"({it.get('file','?')}:{it.get('line','?')})"
                )
        body = "\n  ".join(lines)
    else:
        # No-shape fallback — only fires if a future recipe lands
        # without a custom branch above. Keeps the call safe; the
        # agent narrates over a generic JSON dump.
        try:
            body = _json.dumps(res, default=str,
                                separators=(",", ":"))[:400]
        except Exception:
            body = str(res)[:400]
    addendum = _user_context_addendum(recipe_name, run, request_text)
    addendum_line = f"  {addendum}\n" if addendum else ""
    return (
        f"MANAGER PRE-FETCH (recipe={recipe_name}):\n"
        f"  tool: {run.tool_name}({args_repr})\n"
        f"  result:\n  {body}\n"
        f"{addendum_line}"
        f"USE THIS DATA. Answer in 1-2 sentences in your normal "
        f"voice. Cite specific files when natural. Do NOT re-invoke "
        f"this tool — the data above is authoritative."
    )


# ── runner registry ──────────────────────────────────────────────────────────

_RUNNERS: dict[str, Callable[[str], RunResult | None]] = {
    "count_across_vault":   _run_count_across_vault,
    "weather_aware_agenda": _run_weather_aware_agenda,
}
