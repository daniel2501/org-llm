"""Persistent LLM context — facts that override stale info in the vault.

The vault is a record of what was true *when written*. People change jobs,
move cities, finish projects. Without overrides, the LLM keeps citing
stale facts because they're statistically dominant in the corpus.

This module gives the user a special context file (`~/org/llm-context.org`
by default) where they can record current truth. The org file is human-
authored and tangle-friendly: `#+begin_src ... :tangle PATH ... #+end_src`
blocks export to plain-text targets that get prepended to every system
prompt under a "USER CONTEXT" header. So the LLM always has the latest
facts before it decides what to cite.

Three flows:

  1. Direct: `org-llm context add "I work at Idexx now (was Unum)"`
  2. LLM-parsed: `org-llm context from-prompt "no longer at Unum, now Idexx"`
     — fast_model rewrites freeform input as a structured fact entry.
  3. Stale-tagging: when a fact contradicts existing notes, the LLM
     surfaces matching nodes and proposes tagging them `:stale:` or
     `:re:<topic>:` so retrieval can de-emphasise them.

The context file is itself an org note in the user's vault, so the
existing index/embed/search machinery picks it up too — but the
TANGLED output is what gets read into prompts (cheap, no embed needed).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime    import datetime
from pathlib     import Path


def context_org_path() -> Path:
    """Where the user-authored context file lives."""
    p = os.environ.get("ORG_LLM_CONTEXT_FILE")
    if p:
        return Path(p).expanduser()
    org_dir = os.environ.get("ORG_LLM_ORG_DIR") or "~/org"
    return Path(org_dir).expanduser() / "llm-context.org"


def context_tangle_path() -> Path:
    """Default tangle target for blocks without explicit `:tangle <path>`."""
    p = os.environ.get("ORG_LLM_CONTEXT_TANGLE")
    if p:
        return Path(p).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "org-llm" / "llm-context.txt"


def history_org_path() -> Path:
    """Where the LLM-generated history narrative file lives."""
    p = os.environ.get("ORG_LLM_HISTORY_FILE")
    if p:
        return Path(p).expanduser()
    org_dir = os.environ.get("ORG_LLM_ORG_DIR") or "~/org"
    return Path(org_dir).expanduser() / "llm-history.org"


def history_tangle_path() -> Path:
    p = os.environ.get("ORG_LLM_HISTORY_TANGLE")
    if p:
        return Path(p).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "org-llm" / "llm-history.txt"


CONTEXT_HEADER = "USER CONTEXT (current truth — overrides stale info in notes)"
HISTORY_HEADER = "HISTORICAL CONTEXT (narrative summary of past notes — provides background)"


# ── Initial template ──────────────────────────────────────────────────────

def _initial_template() -> str:
    """Build the template with the current tangle path resolved at call time.

    Using a function keeps the path resolution lazy so test harnesses can
    monkeypatch ORG_LLM_CONTEXT_TANGLE before any context file is created.
    """
    tangle_target = context_tangle_path()
    return f"""\
#+title: org-llm context
#+filetags: :llm-context:

* About this file
:PROPERTIES:
:LLM_CONTEXT: meta
:END:

This file records *current truth* that may contradict older notes in the
vault. It's tangled to a plain-text file that org-llm prepends to every
system prompt under a "USER CONTEXT" header — so the LLM has the latest
facts before deciding what to cite.

Add facts here directly, or use:
  - =org-llm context add "fact"=
  - =org-llm context from-prompt "natural language statement"=
  - In opencode: ask the model — it can call =add_context()= itself.

After editing, run =org-llm context tangle= (or just any =org-llm ask=
— context is re-tangled automatically when this file's mtime changes).

* Active context
:PROPERTIES:
:LLM_CONTEXT: facts
:END:

#+name: active-facts
#+begin_src text :tangle {tangle_target}
(no facts yet — add some with `org-llm context add` or directly above)
#+end_src

* Disambiguations
:PROPERTIES:
:LLM_CONTEXT: disambiguations
:END:

When older notes use a name/word ambiguously, clarify here. Example:

#+begin_quote
- "the team" before 2026 = my team at <previous-employer>
- "the team" after 2026 = my team at <current-employer>
#+end_quote

* Context history (auto-appended)
:PROPERTIES:
:LLM_CONTEXT: history
:END:

When facts are added or updated, an entry lands here so you can audit
what's been recorded.
"""


# Backwards-compat alias used in tests; do NOT capture at module import,
# evaluation must be lazy.
INITIAL_TEMPLATE = property(lambda _: _initial_template())  # type: ignore


# ── Tangle parser (no Emacs required) ─────────────────────────────────────

@dataclass
class TangleBlock:
    target: Path
    body:   str
    name:   str = ""


def _parse_tangle_blocks(org_text: str,
                          default_target: Path) -> list[TangleBlock]:
    """Extract `#+begin_src ... :tangle <path> ... #+end_src` blocks.

    Returns each as a TangleBlock(target, body). Blocks without an
    explicit :tangle path use `default_target`. Blocks with `:tangle no`
    are skipped.
    """
    blocks: list[TangleBlock] = []
    in_block = False
    target: Path | None = None
    name = ""
    body_lines: list[str] = []
    pending_name = ""
    for raw in org_text.splitlines():
        s = raw.rstrip()
        if not in_block and s.startswith("#+name:"):
            pending_name = s.split(":", 1)[1].strip()
            continue
        if not in_block and s.lower().startswith("#+begin_src"):
            m = re.search(r":tangle\s+(\S+)", s)
            if m:
                tgt = m.group(1)
                if tgt.lower() == "no":
                    target = None
                else:
                    target = Path(tgt).expanduser()
            else:
                target = default_target
            in_block  = True
            name      = pending_name
            pending_name = ""
            body_lines = []
            continue
        if in_block and s.lower().startswith("#+end_src"):
            if target is not None:
                blocks.append(TangleBlock(target=target,
                                            body="\n".join(body_lines),
                                            name=name))
            in_block = False
            target = None
            name = ""
            body_lines = []
            continue
        if in_block:
            body_lines.append(raw)
    return blocks


def tangle(org_path: Path | None = None,
           default_target: Path | None = None) -> dict[Path, str]:
    """Tangle the context org file. Returns {target: combined-body}.

    Writes each target file. Also writes a default fallback target
    containing the raw human-readable contents of any heading tagged
    `:LLM_CONTEXT:` if the file has no tangle blocks.
    """
    org_path = org_path or context_org_path()
    default_target = default_target or context_tangle_path()
    if not org_path.exists():
        return {}
    text = org_path.read_text(errors="replace")

    blocks = _parse_tangle_blocks(text, default_target)
    out: dict[Path, list[str]] = {}
    for b in blocks:
        out.setdefault(b.target, []).append(b.body.strip())

    # Fallback: when the file has zero tangle blocks (or only :tangle no),
    # extract human-readable :LLM_CONTEXT: sections so the LLM still gets
    # SOMETHING. Section is "lines under a heading whose properties drawer
    # has :LLM_CONTEXT: <category>".
    if not out:
        sections: dict[str, list[str]] = {}
        cur_cat: str | None = None
        cur_lines: list[str] = []
        in_drawer = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("*") and not stripped.startswith("**"):
                # Top-level heading — flush previous section
                if cur_cat:
                    sections.setdefault(cur_cat, []).extend(cur_lines)
                cur_cat = None
                cur_lines = []
                in_drawer = False
            if ":PROPERTIES:" in stripped:
                in_drawer = True
                continue
            if ":END:" in stripped and in_drawer:
                in_drawer = False
                continue
            if in_drawer:
                m = re.match(r":LLM_CONTEXT:\s*(\S+)", stripped, re.I)
                if m:
                    cur_cat = m.group(1).lower()
                continue
            if cur_cat and stripped and not stripped.startswith("#+"):
                cur_lines.append(line)
        if cur_cat:
            sections.setdefault(cur_cat, []).extend(cur_lines)
        if sections:
            joined = []
            for cat, lines in sections.items():
                if cat == "meta" or cat == "history":
                    continue
                txt = "\n".join(lines).strip()
                if txt:
                    joined.append(f"# {cat}\n{txt}")
            if joined:
                out[default_target] = joined

    written: dict[Path, str] = {}
    for target, bodies in out.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n\n".join(b for b in bodies if b)
        target.write_text(body)
        written[target] = body
    return written


# ── Read tangled context for system-prompt injection ─────────────────────

def read_context_for_prompt(max_chars: int = 4000) -> str:
    """Return the tangled context text, formatted for prompt prepending.

    Empty string when there's no context yet — caller can use boolean
    truthiness to decide whether to inject the section header.
    """
    target = context_tangle_path()
    org    = context_org_path()
    # Auto-tangle if the org file is newer than the tangle output.
    try:
        if org.exists():
            need = (not target.exists()) or (
                target.stat().st_mtime < org.stat().st_mtime)
            if need:
                tangle(org, target)
    except Exception:
        pass
    if not target.exists():
        return ""
    try:
        body = target.read_text(errors="replace").strip()
    except Exception:
        return ""
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…(truncated)"
    return body


def render_context_block() -> str:
    """Render the prompt-injection block. Empty string when no context."""
    body = read_context_for_prompt()
    if not body:
        return ""
    return f"\n\n{CONTEXT_HEADER}\n{body}\n"


# ── Mutations: add / update / record history ──────────────────────────────

def ensure_context_file_exists() -> Path:
    """Create the context org file from the template if missing."""
    p = context_org_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_initial_template())
    return p


def add_fact(fact: str, source: str = "cli") -> Path:
    """Append a fact to the active-facts tangle block. Creates file if needed.

    Also appends a one-line history entry under the history heading.
    Returns the context file path.
    """
    p = ensure_context_file_exists()
    text = p.read_text(errors="replace")

    fact_line = f"- {fact.strip()}"
    today = datetime.now().date().isoformat()
    history_line = f"- [{today}] {fact.strip()}  ({source})"

    # Insert into active-facts block. Look for "(no facts yet"; if present,
    # replace it. Otherwise append before the closing #+end_src.
    block_re = re.compile(
        r"(#\+name:\s*active-facts\s*\n#\+begin_src[^\n]*\n)([\s\S]*?)(\n#\+end_src)",
        re.M)
    m = block_re.search(text)
    if m:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        if "no facts yet" in body:
            new_body = fact_line
        else:
            new_body = body.rstrip("\n") + "\n" + fact_line
        text = text[:m.start()] + head + new_body + tail + text[m.end():]
    else:
        # No active-facts block found — append a new one near end-of-file.
        addition = (
            "\n\n* Active context\n"
            ":PROPERTIES:\n:LLM_CONTEXT: facts\n:END:\n\n"
            f"#+name: active-facts\n#+begin_src text :tangle {context_tangle_path()}\n"
            f"{fact_line}\n#+end_src\n"
        )
        text += addition

    # History append
    hist_re = re.compile(
        r"(\* Context history[^\n]*\n(?::PROPERTIES:[^\n]*\n(?:[^\n]*\n)*?:END:\s*\n)?)",
        re.M)
    hm = hist_re.search(text)
    if hm:
        insert_at = hm.end()
        text = text[:insert_at] + "\n" + history_line + text[insert_at:]
    else:
        text += f"\n\n* Context history\n:PROPERTIES:\n:LLM_CONTEXT: history\n:END:\n\n{history_line}\n"

    p.write_text(text)
    # Re-tangle so the change is immediately visible to the LLM.
    try:
        tangle(p, context_tangle_path())
    except Exception:
        pass
    return p


# ── LLM-driven helpers ────────────────────────────────────────────────────

_PARSE_SYSTEM = """\
You convert a freeform user statement about themselves or their world
into a SHORT, CRISP fact for org-llm's context file. Output strict JSON:

  {"fact": "Works at Idexx as of 2026-04 (formerly Unum, 2018-2026).",
   "supersedes_keywords": ["Unum"],
   "topic": "employment"}

Rules:
  - "fact" — single sentence, ≤ 28 words, present tense, with dates if
    the user gave any. Include the prior fact for traceability where
    useful ("formerly X").
  - "supersedes_keywords" — words/phrases that older notes might use
    which this fact REPLACES. The vault search will look for nodes
    containing any of these to propose stale-tagging. Be conservative:
    company names, place names, project codenames yes; generic words
    like "team" or "work" no.
  - "topic" — one short word/phrase ('employment', 'address',
    'project-status', 'health', 'relationship'). Used for the tag
    when stale-marking related notes.
  - If the input doesn't contain a verifiable fact (it's a question,
    an opinion, a vague mood), return {"fact": null, "reason": "..."}.
"""


def parse_user_request(prompt: str, *, model: str, base_url: str) -> dict | None:
    """LLM-parse a freeform statement into {fact, supersedes_keywords, topic}.

    Returns None on any failure so the caller can prompt the user to
    rephrase or use direct `add` instead.
    """
    if not prompt or not model:
        return None
    try:
        from .llm import chat
    except Exception:
        return None
    import json as _json, threading
    result: dict = {"resp": ""}
    def _run():
        try:
            result["resp"] = chat(prompt, model=model, base_url=base_url,
                                   system=_PARSE_SYSTEM) or ""
        except Exception:
            pass
    try:
        from .ui import thinking
        with thinking("Parsing context", model=model):
            t = threading.Thread(target=_run, daemon=True)
            t.start(); t.join(timeout=30.0)
    except Exception:
        t = threading.Thread(target=_run, daemon=True)
        t.start(); t.join(timeout=30.0)
    if t.is_alive():
        return None
    raw = (result["resp"] or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        plan = _json.loads(raw.strip())
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    if not plan.get("fact"):
        return None
    return plan


# ── Stale-detection ───────────────────────────────────────────────────────

def find_stale_candidates(session, keywords: list[str],
                            limit: int = 30) -> list:
    """Return Nodes whose title or body contains any of `keywords`.

    Result is a list of (node, matched_keyword) tuples ordered by
    mtime DESC. Skips nodes already tagged `stale`.
    """
    from .db import Node
    if not keywords:
        return []
    results: list = []
    seen: set[str] = set()
    for kw in keywords:
        kw_l = kw.lower()
        if not kw_l or len(kw_l) < 3:
            continue
        rows = (
            session.query(Node)
            .filter(
                (Node.title.ilike(f"%{kw}%")) | (Node.body.ilike(f"%{kw}%"))
            )
            .order_by(Node.mtime.desc())
            .limit(limit * 2)
            .all()
        )
        for n in rows:
            if not n.node_id or n.node_id in seen:
                continue
            tags = (n.tags or "").lower().split()
            if "stale" in tags:
                continue
            seen.add(n.node_id)
            results.append((n, kw))
            if len(results) >= limit:
                return results
    return results


_STALE_SWEEP_SYSTEM = """\
You judge whether org-roam notes are likely STALE given the user's
current context.

For each note, decide:
  - "stale" — the note's content directly contradicts current context
    (e.g. mentions "I'm at Unum" while context says "now at Idexx")
  - "drift" — the note still might be true but its framing assumes
    older facts; worth flagging soft (e.g. "the team" referring to
    a now-former team)
  - "fresh" — no contradiction visible

Output STRICT JSON, no prose:

  {"verdicts": [
    {"node_id": "id-foo", "verdict": "stale",
     "reason": "claims working at Unum; context says Idexx since 2026-04",
     "tag":    "stale"},
    {"node_id": "id-bar", "verdict": "drift",
     "reason": "references 'the Unum team' as ongoing colleagues",
     "tag":    "re:employment"},
    {"node_id": "id-baz", "verdict": "fresh"}
  ]}

Rules:
  - Use the EXACT node_id from the input.
  - Only emit "stale" when there's a clear factual contradiction.
  - "tag" must be one of: stale, drift, re:<topic-snake-case>.
  - Skip emitting fresh entries; you may return {"verdicts": []}.
  - When in doubt, prefer "drift" over "stale" — the user reviews
    these before they're applied.
"""


def llm_stale_sweep(session, *, model: str, base_url: str,
                     limit: int = 20,
                     since_days: int = 0) -> list[dict]:
    """LLM-reasoned staleness pass.

    Samples up to `limit` non-code, non-already-stale nodes (preferring
    older ones — they're likeliest to be stale), feeds them with the
    current context file to fast_model, asks for verdicts. Returns the
    parsed list of {node_id, verdict, reason, tag} dicts (or [] on any
    failure).

    `since_days=0` (default) considers all nodes; set >0 to restrict to
    nodes older than N days. Stale code-tagged nodes are always skipped.
    """
    from .db import Node
    context_text = read_context_for_prompt(max_chars=2000)
    if not context_text:
        return []

    q = (session.query(Node)
         .filter(~Node.tags.like("%stale%"),
                  ~Node.tags.like("%code%")))
    if since_days > 0:
        cutoff = time.time() - since_days * 86400
        q = q.filter(Node.mtime < cutoff)
    rows = q.order_by(Node.mtime.asc()).limit(limit).all()
    if not rows:
        return []

    samples: list[dict] = []
    for n in rows:
        body = (n.body or "").strip().replace("\n", " ")
        if len(body) > 320:
            body = body[:320] + "…"
        samples.append({
            "node_id": n.node_id or f"#{n.id}",
            "title":   (n.title or "")[:80],
            "tags":    n.tags or "",
            "excerpt": body,
        })

    import json as _json, threading
    user_prompt = (
        f"USER'S CURRENT CONTEXT:\n{context_text}\n\n"
        f"NOTES TO JUDGE ({len(samples)}):\n" +
        "\n".join(
            f"  - id={s['node_id']}\n"
            f"    title: {s['title']}\n"
            f"    tags: {s['tags']}\n"
            f"    excerpt: {s['excerpt']}\n"
            for s in samples
        ) +
        "\nReturn JSON verdicts."
    )

    try:
        from .llm import chat
        from .ui  import thinking
    except Exception:
        return []
    result: dict = {"resp": ""}
    def _run():
        try:
            result["resp"] = chat(user_prompt, model=model, base_url=base_url,
                                   system=_STALE_SWEEP_SYSTEM) or ""
        except Exception:
            pass
    try:
        with thinking("Stale sweep", model=model):
            t = threading.Thread(target=_run, daemon=True)
            t.start(); t.join(timeout=120.0)
    except Exception:
        t = threading.Thread(target=_run, daemon=True)
        t.start(); t.join(timeout=120.0)
    if t.is_alive():
        return []

    raw = (result["resp"] or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        plan = _json.loads(raw.strip())
    except Exception:
        return []
    verdicts = plan.get("verdicts") if isinstance(plan, dict) else None
    if not isinstance(verdicts, list):
        return []
    out: list[dict] = []
    valid_ids = {n.node_id for n in rows}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        nid = v.get("node_id")
        if nid not in valid_ids:
            continue
        verdict = (v.get("verdict") or "").lower()
        if verdict not in ("stale", "drift"):
            continue
        tag = (v.get("tag") or verdict).lower()
        if not re.fullmatch(r"(stale|drift|re:[a-z0-9_-]+)", tag):
            tag = verdict
        out.append({
            "node_id": nid,
            "verdict": verdict,
            "reason":  (v.get("reason") or "")[:240],
            "tag":     tag,
        })
    return out


def count_unreviewed_stale_candidates(session) -> int:
    """Quick check: how many active context keywords would tag-match?

    Used by doctor / startup nudges to alert the user when a recent
    context add hasn't been swept yet. Returns 0 when context is empty.
    """
    text = read_context_for_prompt(max_chars=2000)
    if not text:
        return 0
    # Heuristic: pull capitalised tokens (likely proper nouns) from the
    # context text — those are most useful for substring matches.
    cands = set(re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text))
    if not cands:
        return 0
    candidates = find_stale_candidates(session, list(cands), limit=200)
    return len(candidates)


_HISTORY_NARRATIVE_SYSTEM = """\
You write a SHORT historical narrative summary of a person's older notes
for use as background context in a knowledge-base RAG system. The notes
may have been tagged :stale: (factually outdated) or are simply old
(>180 days). Your output BECOMES additional context the LLM reads
alongside current-truth context.

Output structured org-mode under top-level headings, e.g.:

  * Employment history
  - 2018-2026 — at Unum, claims platform team, focus on data infra.
  - …

  * Project history
  - …

  * Address / location history
  - …

Rules:
  - Use bullet points; no paragraphs.
  - Each bullet ≤ 20 words.
  - Include date ranges when notes mention them; otherwise infer from
    the note mtime span.
  - Pull only NARRATIVE-WORTHY facts: jobs held, places lived, projects
    finished, named people. Skip todo lists, daily-routine notes,
    technical scratchpads.
  - If a section has no content, omit the heading entirely.
  - Output the org-mode body only — no preamble, no fence, no JSON.
"""


def _bare_history_blank() -> str:
    return f"""\
#+title: org-llm history
#+filetags: :llm-history:

* About this file
:PROPERTIES:
:LLM_HISTORY: meta
:END:

LLM-generated narrative summary of older / stale-tagged notes in the
vault. Re-built whenever you run [bold]org-llm history build[/bold].
Tangled to a plain-text file that org-llm prepends to every system
prompt under "HISTORICAL CONTEXT". Stale notes are still valuable —
they're just historical, and this file captures their gist.

#+name: history-narrative
#+begin_src text :tangle {history_tangle_path()}
(no history built yet — run `org-llm history build`)
#+end_src
"""


def ensure_history_file_exists() -> Path:
    p = history_org_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_bare_history_blank())
    return p


def read_history_for_prompt(max_chars: int = 3000) -> str:
    """Tangled history narrative for prompt prepending. Empty when none."""
    target = history_tangle_path()
    org    = history_org_path()
    try:
        if org.exists():
            need = (not target.exists()) or (
                target.stat().st_mtime < org.stat().st_mtime)
            if need:
                tangle(org, target)
    except Exception:
        pass
    if not target.exists():
        return ""
    try:
        body = target.read_text(errors="replace").strip()
    except Exception:
        return ""
    if not body or "no history built" in body[:200]:
        return ""
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…(truncated)"
    return body


def render_history_block() -> str:
    body = read_history_for_prompt()
    if not body:
        return ""
    return f"\n\n{HISTORY_HEADER}\n{body}\n"


def _scan_archive_files() -> list[tuple[str, str, float]]:
    """Walk archive folders the indexer typically skips, returning
    (title, body-excerpt, mtime) tuples. Common archive locations:
    `<org_dir>/archive/`, `<org_dir>/**/archive/`, `*.org_archive` files.

    Returns [] when nothing found.
    """
    org_dir = Path(os.environ.get("ORG_LLM_ORG_DIR") or "~/org").expanduser()
    if not org_dir.exists():
        return []
    out: list[tuple[str, str, float]] = []
    seen: set[Path] = set()
    patterns = ["archive/**/*.org", "**/archive/**/*.org",
                "**/*.org_archive", "Archive/**/*.org"]
    for pat in patterns:
        for p in org_dir.glob(pat):
            try:
                rp = p.resolve()
            except Exception:
                continue
            if rp in seen or not rp.is_file():
                continue
            seen.add(rp)
            try:
                text  = p.read_text(errors="replace")
                mtime = p.stat().st_mtime
            except Exception:
                continue
            # Crude title extraction
            title = p.stem
            for line in text.splitlines()[:30]:
                m = re.match(r"^#\+title:\s*(.+)$", line, re.I)
                if m:
                    title = m.group(1).strip()
                    break
            # First non-trivial paragraph as body excerpt
            excerpt = ""
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith(("#+", ":", "*")):
                    continue
                excerpt += s + " "
                if len(excerpt) >= 240:
                    break
            out.append((title, excerpt[:240], mtime))
            if len(out) >= 50:
                return out
    return out


def build_history(session, *, model: str, base_url: str,
                  sample_limit: int = 60,
                  age_days: int = 180,
                  user_guidance: str = "",
                  include_archives: bool = True) -> str:
    """LLM-build the history narrative from stale-tagged + old notes.

    Samples the oldest non-code nodes that are either tagged :stale:
    or older than `age_days`, gives them to the model with the
    narrative system prompt, and writes the result to the history
    org file's tangle block. Returns the generated narrative or "".

    If `include_archives` is True (default), also walks the user's
    archive folders directly — those typically aren't indexed but
    they're prime narrative material.
    """
    from .db import Node
    cutoff = time.time() - age_days * 86400
    rows = (session.query(Node)
            .filter(~Node.tags.like("%code%"))
            .filter((Node.tags.like("%stale%")) | (Node.mtime < cutoff))
            .order_by(Node.mtime.asc())
            .limit(sample_limit)
            .all())

    samples: list[str] = []
    for n in rows:
        body = (n.body or "").strip().replace("\n", " ")[:240]
        date = ""
        try:
            if n.mtime:
                date = datetime.fromtimestamp(n.mtime).date().isoformat()
        except Exception:
            pass
        samples.append(f"- [{date}] {n.title or '(untitled)'} :: {body}")

    # Archive walk — these files often aren't indexed but are PRIMARY
    # historical material. Pull up to 30, oldest first.
    if include_archives:
        archive_rows = _scan_archive_files()
        archive_rows.sort(key=lambda r: r[2])
        for title, excerpt, mtime in archive_rows[:30]:
            try:
                date = datetime.fromtimestamp(mtime).date().isoformat()
            except Exception:
                date = ""
            samples.append(f"- [{date}] (archive) {title} :: {excerpt}")

    if not samples:
        return ""

    user_prompt = (
        "Notes (oldest first; mtime in brackets):\n"
        + "\n".join(samples)
        + (user_guidance if user_guidance else "")
        + "\n\nWrite the narrative summary as org-mode body text, headed by "
        "appropriate top-level headings."
    )

    try:
        from .llm import chat
        from .ui  import thinking
    except Exception:
        return ""
    import threading
    result: dict = {"resp": ""}
    def _run():
        try:
            result["resp"] = chat(user_prompt, model=model, base_url=base_url,
                                   system=_HISTORY_NARRATIVE_SYSTEM) or ""
        except Exception:
            pass
    try:
        with thinking(f"Narrating history from {len(samples)} notes",
                       model=model):
            t = threading.Thread(target=_run, daemon=True)
            t.start(); t.join(timeout=180.0)
    except Exception:
        t = threading.Thread(target=_run, daemon=True)
        t.start(); t.join(timeout=180.0)
    if t.is_alive():
        return ""
    narrative = (result["resp"] or "").strip()
    if not narrative:
        return ""
    # Strip code-fences if the model added them despite instructions
    if narrative.startswith("```"):
        narrative = "\n".join(narrative.splitlines()[1:])
        if narrative.endswith("```"):
            narrative = narrative[:-3]
    narrative = narrative.strip()

    # Write into history.org's tangle block. Replace the entire file with
    # a fresh template + the narrative so old narratives don't accumulate.
    p = history_org_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"#+title: org-llm history\n"
        f"#+filetags: :llm-history:\n\n"
        f"* About this file\n"
        f":PROPERTIES:\n:LLM_HISTORY: meta\n:END:\n\n"
        f"LLM-generated narrative summary of older / stale-tagged notes.\n"
        f"Built from {len(samples)} sample(s) on {datetime.now().date().isoformat()}.\n"
        f"Re-build with =org-llm history build=.\n\n"
        f"#+name: history-narrative\n"
        f"#+begin_src text :tangle {history_tangle_path()}\n"
        f"{narrative}\n"
        f"#+end_src\n"
    )
    p.write_text(body)
    tangle(p, history_tangle_path())
    return narrative


def apply_stale_tags(session, candidates: list, topic: str = "") -> int:
    """Add `stale` (and optionally `re:<topic>`) tags to the given Nodes.

    Returns count of nodes actually updated. Does NOT rewrite the org
    files on disk — that's the user's job after review (the index
    captures the new tags so retrieval reweights immediately).
    """
    from .db import Node
    norm_topic = re.sub(r"[^a-z0-9_-]+", "", (topic or "").lower())[:32]
    updated = 0
    for n, _ in candidates:
        db_n = session.get(Node, n.id)
        if not db_n:
            continue
        existing = (db_n.tags or "").split()
        if "stale" in existing:
            continue
        new_tags = existing + ["stale"]
        if norm_topic and f"re:{norm_topic}" not in existing:
            new_tags.append(f"re:{norm_topic}")
        db_n.tags = " ".join(new_tags)
        updated += 1
    if updated:
        session.commit()
    return updated
