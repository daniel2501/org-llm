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
    r"\bin (?:my |the )?(daily files?|dailies|notes|vault|"
    r"roam( notes)?|journal)\b,?\s*",
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


# ── recent_activity_summary runner ───────────────────────────────────────────

def _run_recent_activity_summary(user_text: str) -> RunResult | None:
    """Manager pre-fetches the data the recipe needs (recent nodes
    + recent files), agent narrates. Same Node/File queries the
    inline `list_recent_nodes` / `recent_files` MCP tools run —
    duplicated here so the runner stays free of MCP machinery."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        from .db import Node, File, make_engine
        from sqlalchemy.orm import Session
    except Exception:
        return None

    days_nodes = 14
    days_files = 7
    nodes_out:  list[dict] = []
    files_out:  list[dict] = []
    try:
        engine = make_engine()
        with Session(engine) as s:
            since_n = (_dt.now() - _td(days=days_nodes)).timestamp()
            seen_titles: set[str] = set()
            # Exclude captains-log nodes at the SQL layer — those
            # repeat per session and would crowd out user content.
            # The recent_files bucket below still surfaces a count
            # so the agent knows there was log activity.
            from sqlalchemy import not_, or_  # noqa: PLC0415
            log_filter = or_(
                Node.tags.like("%captains-log%"),
                Node.tags == "captains-log",
            )
            for n in (s.query(Node)
                       .filter(Node.mtime >= since_n)
                       .filter(not_(log_filter))
                       .order_by(Node.mtime.desc())
                       .limit(80).all()):
                title = (n.title or "").strip()
                tags  = (n.tags or "").strip().strip(":")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                nodes_out.append({
                    "title": title,
                    "tags":  tags,
                    "date":  (_dt.fromtimestamp(n.mtime).date().isoformat()
                                if n.mtime else "?"),
                })
                if len(nodes_out) >= 20:
                    break
            since_f = (_dt.now() - _td(days=days_files)).timestamp()
            # opt-2 (2026-05-03): exclude captains-log files at
            # the SQL layer too. The query previously fetched 200
            # rows and discarded most via Python-side startswith;
            # SQL pushdown drops that to ~60 actual user files.
            # A separate COUNT keeps the bucket-summary line
            # reflecting the full count.
            from sqlalchemy import not_ as _sql_not  # noqa: PLC0415
            log_filter_f = File.path.like("%captains-log%")
            log_file_count = (
                s.query(File)
                  .filter(File.mtime >= since_f)
                  .filter(log_filter_f)
                  .count()
            )
            for f in (s.query(File)
                       .filter(File.mtime >= since_f)
                       .filter(_sql_not(log_filter_f))
                       .order_by(File.mtime.desc())
                       .limit(60).all()):
                files_out.append({
                    "path": str(f.path),
                    "date": (_dt.fromtimestamp(f.mtime).date().isoformat()
                                if f.mtime else "?"),
                })
                if len(files_out) >= 25:
                    break
            if log_file_count > 0:
                files_out.insert(0, {
                    "path": (f"({log_file_count} captains-log file(s) "
                              f"— auto-generated log)"),
                    "date": _dt.now().date().isoformat(),
                })
    except Exception:
        return None

    if not nodes_out and not files_out:
        return None

    return RunResult(
        answer="",   # agent narrates from the pre-fetch block
        tool_name="list_recent_activity",
        tool_args={"days_nodes": days_nodes, "days_files": days_files},
        tool_result={
            "recent_nodes": nodes_out,
            "recent_files": files_out,
            "n_nodes":      len(nodes_out),
            "n_files":      len(files_out),
        },
    )


# ── dailies_routine_filter runner ────────────────────────────────────────────

def _run_dailies_routine_filter(user_text: str) -> RunResult | None:
    """Promote the v1 advisory recipe (which asked the LLM to do
    its own filter pass over recent dailies) to manager pre-fetch.

    Phase 21.1's `routine_chores` inferrer already produces the
    REJECT-first chore list deterministically — we just hand it to
    the agent so it can draft the flat list per CAPTURE STYLE and
    handle the confirm-and-save flow. No LLM filter pass needed.

    Falls back to advisory mode (returns None) when the fact is
    empty — e.g. fresh vault with no dailies yet."""
    try:
        from . import vault_facts as _vf
    except Exception:
        return None
    fact = _vf.get_fact("routine_chores") or {}
    chores = fact.get("chores") or []
    if not chores:
        return None
    return RunResult(
        answer="",
        tool_name="routine_chores",
        tool_args={"window_dailies": fact.get("dailies_scanned", 0),
                    "limit": len(chores)},
        tool_result={
            "chores":           chores,
            "samples_total":    fact.get("samples_total", 0),
            "samples_rejected": fact.get("samples_rejected", 0),
            "dailies_scanned":  fact.get("dailies_scanned", 0),
        },
    )


# ── dailies_general runner ───────────────────────────────────────────────────

def _run_dailies_general(user_text: str) -> RunResult | None:
    """Manager pre-fetches the last 5 dailies (path + first heading
    + content excerpt) so the agent answers without an extra MCP
    round-trip.

    Mirrors the inline `list_dailies` MCP tool's body, but capped
    at ~1500 chars per file (vs. 4000) to keep the pre-fetch block
    under ~10KB. Agents that need fuller context can still call
    `list_dailies` themselves."""
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
    except Exception:
        return None
    try:
        with Session(make_engine()) as s:
            org_row   = s.get(Config, "org_dir")
            daily_row = s.get(Config, "daily_dir")
        from pathlib import Path as _Path
        org_dir = _Path((org_row.value if org_row else "~/org")
                          ).expanduser()
        daily_dir = (_Path(daily_row.value).expanduser()
                      if daily_row and daily_row.value
                      else (org_dir / "daily"))
    except Exception:
        return None
    if not daily_dir.exists():
        return None
    try:
        files = sorted(daily_dir.glob("*.org"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True)[:5]
    except Exception:
        return None
    if not files:
        return None
    from datetime import datetime as _dt
    rows: list[dict] = []
    max_chars = 1500
    for f in files:
        try:
            full = f.read_text(errors="replace")
        except Exception:
            continue
        title = ""
        for line in full.splitlines()[:8]:
            s = line.strip()
            if s.lower().startswith("#+title:"):
                title = s.split(":", 1)[1].strip()
                break
            if s.startswith("* "):
                title = s[2:].strip()
                break
        body = full[:max_chars]
        if len(full) > max_chars:
            body += f"\n... [truncated; full file {len(full)} chars]"
        rows.append({
            "date":       _dt.fromtimestamp(f.stat().st_mtime).date().isoformat(),
            "stem":       f.stem,
            "title":      title,
            "content":    body,
            "truncated":  len(full) > max_chars,
            "full_chars": len(full),
        })
    if not rows:
        return None
    return RunResult(
        answer="",
        tool_name="list_dailies",
        tool_args={"limit": 5, "include_content": True},
        tool_result={"dailies": rows, "n_dailies": len(rows)},
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
    elif "recent_nodes" in res or "recent_files" in res:
        # Recent-activity shape: list nodes + files compactly.
        # Each line ~80 chars; 20+15 = ≤3KB pre-fetch block. Worth
        # the bytes — agent gets the data it needs to group + cite.
        lines: list[str] = []
        nodes = res.get("recent_nodes") or []
        if nodes:
            lines.append(f"recent_nodes ({len(nodes)}, last 14d):")
            for n in nodes[:20]:
                tags = (" :" + n.get("tags", "") + ":") if n.get("tags") else ""
                lines.append(f"  {n.get('date', '?')}  "
                              f"{(n.get('title','') or '')[:60]}{tags}")
        files = res.get("recent_files") or []
        if files:
            lines.append(f"recent_files ({len(files)}, last 7d):")
            for f in files[:15]:
                lines.append(f"  {f.get('date', '?')}  "
                              f"{os.path.basename(str(f.get('path','')))}")
        body = "\n  ".join(lines)
    elif "chores" in res:
        # routine_chores shape — flat list, top N kept already.
        chores_list = res.get("chores") or []
        scanned     = res.get("dailies_scanned", 0)
        total_s     = res.get("samples_total", 0)
        rejected    = res.get("samples_rejected", 0)
        lines = [f"routine_chores (scanned {scanned} dailies, "
                  f"{total_s} samples, {rejected} rejected):"]
        for c in chores_list:
            lines.append(f"  {c.get('count', 0):>3}× "
                          f"{c.get('phrase', '')}  "
                          f"(first {c.get('first_seen', '?')} → "
                          f"last {c.get('last_seen', '?')})")
        body = "\n  ".join(lines)
    elif "dailies" in res:
        # dailies_general shape — file-by-file content excerpts.
        dailies_list = res.get("dailies") or []
        lines = [f"recent_dailies ({len(dailies_list)} files):"]
        for d in dailies_list:
            head = (f"  --- {d.get('date','?')}  {d.get('stem','')}.org"
                    + (f"  ({d.get('title','')})" if d.get('title') else "")
                    + (f"  [truncated, {d.get('full_chars',0)} chars]"
                        if d.get("truncated") else "")
                    + " ---")
            lines.append(head)
            lines.append(d.get("content", ""))
        body = "\n".join(lines)
    else:
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
    "count_across_vault":      _run_count_across_vault,
    "recent_activity_summary": _run_recent_activity_summary,
    "dailies_routine_filter":  _run_dailies_routine_filter,
    "dailies_general":         _run_dailies_general,
}
