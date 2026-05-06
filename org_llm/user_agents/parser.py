"""Parser for user-supplied agents — Phase 23.5 — user-supplied agents library.

Reads ``~/org/org-llm-agents.org`` (env-overridable via
``ORG_LLM_USER_AGENTS_PATH``) and yields parsed
:class:`ParsedUserAgent` records.

File shape (one persona per top-level heading; only personas with
the ``:agent:`` tag are picked up — same convention as the existing
tangled-mirror flow in ``cli._load_agents_from_org``)::

    * @journalist                                                   :agent:
    :PROPERTIES:
    :DESCRIPTION:    Long-form drafts + interview-style summaries
    :SKILLS:         writing,interviewing,structure
    :TOOL_ALLOWLIST: read_file,search_notes,capture_note
    :MODEL:          chat_model
    :END:

    Optional free-text notes between drawer + src block.

    #+begin_src text :name system-prompt
    You are @journalist. Long-form drafts...
    #+end_src

The handle (heading title) accepts both ``@journalist`` and bare
``journalist`` shapes — the leading ``@`` is stripped. Two ways to
declare the system prompt:

  1. A ``#+begin_src text :name system-prompt`` babel block — the
     idiomatic form, mirrors the literate-MCP-tools convention.
  2. The heading body (whatever's between the PROPERTIES drawer and
     the next heading, with the babel block stripped if present).
     Falls back to this when no babel block is present, so the
     existing tangled mirror of ``org-llm-agents.org`` (which uses
     prose body for the system prompt) parses cleanly too.

Pure-string parser — no exec, no imports of user code, no DB
contact. The output is a dataclass that ``validator.validate_one``
turns into a :class:`UserAgent` record.

Mirrors :mod:`org_llm.literate_mcp.parser` — see that module for
the regex shape this borrows from.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_PATH = Path("~/org/org-llm-agents.org").expanduser()


def user_agents_path() -> Path:
    """Where the user-agents file lives. Env override for tests."""
    return Path(os.environ.get("ORG_LLM_USER_AGENTS_PATH")
                or str(_DEFAULT_PATH))


# Identifier-shape check for handles. Accepts ``@spock``, ``spock``,
# and ``spock_v2``. Rejects shell-meta or whitespace. This restricts
# handles to safe ``@<name>`` routing keys.
_HANDLE_RE = re.compile(r"^@?([A-Za-z_][A-Za-z0-9_-]*)$")


@dataclass
class ParsedUserAgent:
    """A single user-supplied agent definition lifted from the file.

    Fields:
      handle         — heading title with leading ``@`` stripped;
                       used as the canonical ``birth_name`` for the
                       registered agent.
      description    — :DESCRIPTION: drawer property; one-liner the
                       launcher / list_agents surface.
      system_prompt  — multi-line system prompt (from a babel block
                       named ``system-prompt`` OR the heading body).
      skills         — :SKILLS: csv; informational, mirrors the
                       documentation surface in agents.org.
      tool_allowlist — :TOOL_ALLOWLIST: csv; the tool surface the
                       persona is allowed to call (see Phase 23.6 —
                       literate MCP tools, Tier 2 for groups + deny).
      model          — :MODEL: optional default model role
                       (e.g. ``chat_model`` / ``reason_model``).
      aliases        — :ALIASES: csv; additional ``@<name>``s that
                       route to the same persona.
      triggers       — :TRIGGERS: csv; keywords for the lightning-
                       fast keyword router (cli.py § _AGENT_TRIGGERS).
      source_line    — 1-based line number of the heading; for
                       error messages.
    """
    handle:         str
    description:    str           = ""
    system_prompt:  str           = ""
    skills:         tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    model:          str           = ""
    aliases:        tuple[str, ...] = ()
    triggers:       tuple[str, ...] = ()
    source_line:    int           = 0
    raw_properties: dict[str, str] = field(default_factory=dict)


# ── Heading + drawer + src-block regexes ────────────────────────────
#
# Top-level (single star) headings only. The :agent: tag filter
# happens after extraction; we match every top-level heading first
# so the regex stays simple.
_HEADING_RE = re.compile(r"^\* +(\S.*?)\s*$", re.MULTILINE)

# PROPERTIES drawer — same shape as literate-MCP's parser.
_DRAWER_RE = re.compile(
    r"^:PROPERTIES:\s*\n(.*?)^:END:\s*$",
    re.MULTILINE | re.DOTALL,
)
_PROP_RE = re.compile(r"^:([A-Z_]+):\s*(.*?)\s*$", re.MULTILINE)

# Babel block named ``system-prompt`` — accepts text / org / md
# language tags; the language doesn't matter, only the ``:name``.
# The ``:name system-prompt`` part is the load-bearing identifier.
_PROMPT_BLOCK_RE = re.compile(
    r"#\+begin_src\s+\S+[^\n]*:name\s+system-prompt[^\n]*\n"
    r"(.*?)\n#\+end_src",
    re.DOTALL | re.IGNORECASE,
)

# Generic babel block — anything between #+begin_src and #+end_src.
# Used for stripping the prompt block out of "body fallback" mode so
# we don't pick up the same text twice.
_ANY_SRC_BLOCK_RE = re.compile(
    r"#\+begin_src\b.*?#\+end_src",
    re.DOTALL | re.IGNORECASE,
)


def _parse_csv(s: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (s or "").split(",") if p.strip())


def _line_for(text: str, char_offset: int) -> int:
    """1-based line number for a character offset (for diagnostics)."""
    return text.count("\n", 0, char_offset) + 1


def _heading_tags(raw_heading: str) -> list[str]:
    """Extract org-mode tag list from a heading title.

    ``"@journalist                              :agent:writer:"`` →
    ``["agent", "writer"]``.
    """
    m = re.search(r"\s+:([A-Za-z0-9_:@-]+):\s*$", raw_heading)
    if not m:
        return []
    return [t for t in m.group(1).split(":") if t]


def _heading_title(raw_heading: str) -> str:
    """Strip trailing org-mode tags from a heading title."""
    return re.sub(r"\s+:[A-Za-z0-9_:@-]+:\s*$", "", raw_heading).strip()


def parse_text(text: str) -> list[ParsedUserAgent]:
    """Parse a user-agents org *string* into ParsedUserAgent records.

    File-IO-free for testability. Walks top-level headings, filters
    to those tagged ``:agent:``, extracts the PROPERTIES drawer +
    system-prompt babel block (or body fallback), and yields one
    record per persona.
    """
    out: list[ParsedUserAgent] = []
    headings = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        raw_heading = m.group(1).strip()
        tags = _heading_tags(raw_heading)
        if "agent" not in tags:
            # Not an agent heading — skip silently. The file may
            # contain documentation sections, group headers, etc.
            continue
        title = _heading_title(raw_heading)
        handle_m = _HANDLE_RE.match(title)
        if not handle_m:
            # Heading title isn't a valid handle — skip. The
            # validator surfaces a diagnostic if the caller wants
            # it; here we keep the parser tolerant.
            continue
        handle = handle_m.group(1)

        end = (headings[i + 1].start() if i + 1 < len(headings)
                else len(text))
        slab = text[m.end():end]

        # Properties drawer (optional but typical).
        drawer_m = _DRAWER_RE.search(slab)
        props: dict[str, str] = {}
        if drawer_m:
            for pm in _PROP_RE.finditer(drawer_m.group(1)):
                props[pm.group(1).strip().upper()] = pm.group(2).strip()

        # System prompt — prefer the named babel block, fall back to
        # heading body (with all babel blocks stripped) to stay
        # compatible with the existing tangled-mirror file shape.
        prompt = ""
        block_m = _PROMPT_BLOCK_RE.search(slab)
        if block_m:
            prompt = block_m.group(1)
            if prompt.endswith("\n"):
                prompt = prompt[:-1]
        else:
            # Strip the PROPERTIES drawer + any stray src blocks
            # from the slab so body-mode prompts aren't polluted by
            # them. The remaining text is the system prompt.
            body = slab
            if drawer_m:
                body = body.replace(drawer_m.group(0), "", 1)
            body = _ANY_SRC_BLOCK_RE.sub("", body)
            prompt = body.strip()

        out.append(ParsedUserAgent(
            handle=handle,
            description=props.get("DESCRIPTION", "").strip(),
            system_prompt=prompt,
            skills=_parse_csv(props.get("SKILLS")),
            tool_allowlist=_parse_csv(props.get("TOOL_ALLOWLIST")),
            model=(props.get("MODEL", "").strip()
                   or props.get("MODEL_ROLE", "").strip()),
            aliases=_parse_csv(props.get("ALIASES")),
            triggers=_parse_csv(props.get("TRIGGERS")),
            source_line=_line_for(text, m.start()),
            raw_properties=props,
        ))
    return out


def parse_file(path: Path) -> list[ParsedUserAgent]:
    """Parse a user-agents file from disk. Missing file → []."""
    if not path.exists():
        return []
    return parse_text(path.read_text())
