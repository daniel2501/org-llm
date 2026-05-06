"""Parser for literate MCP tools — Phase 23.6 — literate MCP tools.

Reads ``~/org/org-llm-tools.org`` (env-overridable via
``ORG_LLM_LITERATE_TOOLS_PATH``) and yields parsed
:class:`ParsedTool` records.

File shape (one tool per top-level heading)::

    * word_count
    :PROPERTIES:
    :DESCRIPTION: Count words in a string
    :PARAMS:      {"text": {"type": "string"}}
    :RETURNS:     {"type": "integer"}
    :TIMEOUT:     5
    :IMPORTS:     re, json
    :DANGEROUS:   no
    :END:

    Optional free-text notes between drawer + src block.

    #+begin_src python :tangle no
    return len(text.split())
    #+end_src

Pure-string parser — no exec, no imports of user code, no DB
contact. The output is a dataclass that ``compiler.compile_tool``
turns into a callable.

Mirrors the ``_load_agents_from_org`` shape used for Phase 23.5
— user-supplied agents library (extension path leg 1).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_PATH = Path("~/org/org-llm-tools.org").expanduser()


def literate_tools_path() -> Path:
    """Where the literate tools file lives. Env override for tests."""
    return Path(os.environ.get("ORG_LLM_LITERATE_TOOLS_PATH")
                or str(_DEFAULT_PATH))


# Identifier-shape check for tool names. We intentionally restrict to
# Python-identifier syntax so the heading title can be used verbatim
# as the function name in the compiled callable.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class ParsedTool:
    """A single tool definition lifted from the literate file.

    Fields:
      name        — heading title (becomes the compiled function name)
      description — :DESCRIPTION: drawer property; the LLM reads this
      params      — parsed JSON object from :PARAMS: (key → JSON-Schema
                    fragment). Empty dict = no params.
      returns     — parsed JSON object from :RETURNS: (optional;
                    informational only — not enforced by the compiler).
      timeout     — :TIMEOUT: in seconds (None = no explicit cap).
      imports     — modules listed in :IMPORTS: (CSV). Empty = no
                    imports beyond the safe builtins.
      dangerous   — :DANGEROUS: yes/true/1 → registration is gated on
                    the host's dangerous-tools-enabled flag.
      body        — the raw Python source from the babel block (the
                    function body — caller wraps it in `def name(...)`).
      source_line — 1-based line number of the heading; for error
                    messages.
    """
    name:        str
    description: str
    params:      dict[str, Any] = field(default_factory=dict)
    returns:     dict[str, Any] = field(default_factory=dict)
    timeout:     float | None   = None
    imports:     tuple[str, ...] = ()
    dangerous:   bool = False
    body:        str = ""
    source_line: int = 0


# ── Heading + drawer + src-block regexes ────────────────────────────
#
# Top-level headings only (one star). Phase 23.6.1 doesn't need
# nested tools; if a use-case emerges later we can lift this.

_HEADING_RE = re.compile(r"^\* +(\S.*?)\s*$", re.MULTILINE)

# PROPERTIES drawer: ":PROPERTIES:" through ":END:" with arbitrary
# property lines in between. Captured slab is the inner body.
_DRAWER_RE = re.compile(
    r"^:PROPERTIES:\s*\n(.*?)^:END:\s*$",
    re.MULTILINE | re.DOTALL,
)
_PROP_RE = re.compile(r"^:([A-Z_]+):\s*(.*?)\s*$", re.MULTILINE)

# Python babel block. We accept any header arg-list ("python :tangle
# no", "python", "python :tangle yes", etc.) — the body is what
# matters. Only Python is supported in this leg; the wiki spec
# already explicitly tags Python-via-babel as the slot this fills
# (Tier 3c with sandbox boundary; v0.1 covers the parsing /
# compiling shape).
_PY_BLOCK_RE = re.compile(
    r"#\+begin_src\s+python\b[^\n]*\n(.*?)\n#\+end_src",
    re.DOTALL | re.IGNORECASE,
)


def _truthy(s: str | None) -> bool:
    v = (s or "").strip().lower()
    return v in ("yes", "true", "t", "1", "on")


def _parse_csv(s: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (s or "").split(",") if p.strip())


def _parse_json_obj(s: str | None) -> dict[str, Any]:
    """Parse a JSON-object string from a property drawer.

    Returns an empty dict on missing / blank / malformed input —
    invalid JSON in :PARAMS: is a soft failure, never an exception.
    The compiler still won't register a tool whose declared
    parameter set doesn't make sense, but parsing stays robust.
    """
    raw = (s or "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _line_for(text: str, char_offset: int) -> int:
    """1-based line number for a character offset (for diagnostics)."""
    return text.count("\n", 0, char_offset) + 1


def parse_text(text: str) -> list[ParsedTool]:
    """Parse a literate-tools org *string* into ParsedTool records.

    File-IO-free for testability. Walks top-level headings, extracts
    the immediately-following PROPERTIES drawer + python babel block,
    and yields one record per heading that has BOTH (no body, no
    drawer → silently skipped, since the heading might be a section
    divider rather than a tool).
    """
    out: list[ParsedTool] = []
    headings = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        name = m.group(1).strip()
        # Trim trailing org tags like ":tool:" if the user adds them.
        name = re.sub(r"\s+:[A-Za-z0-9_:@]+:\s*$", "", name).strip()
        if not _NAME_RE.match(name):
            continue
        end = (headings[i + 1].start() if i + 1 < len(headings)
                else len(text))
        slab = text[m.end():end]

        # Properties (optional but typical)
        drawer_m = _DRAWER_RE.search(slab)
        props: dict[str, str] = {}
        if drawer_m:
            for pm in _PROP_RE.finditer(drawer_m.group(1)):
                props[pm.group(1).strip().upper()] = pm.group(2).strip()

        # Python babel block
        block_m = _PY_BLOCK_RE.search(slab)
        if not block_m:
            # Heading with no python source = not a tool.
            continue
        body = block_m.group(1)
        # Strip a single trailing newline (the closing #+end_src on
        # its own line eats a newline at write time).
        if body.endswith("\n"):
            body = body[:-1]

        timeout_raw = props.get("TIMEOUT", "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else None
        except ValueError:
            timeout = None

        out.append(ParsedTool(
            name=name,
            description=props.get("DESCRIPTION", "").strip(),
            params=_parse_json_obj(props.get("PARAMS")),
            returns=_parse_json_obj(props.get("RETURNS")),
            timeout=timeout,
            imports=_parse_csv(props.get("IMPORTS")),
            dangerous=_truthy(props.get("DANGEROUS")),
            body=body,
            source_line=_line_for(text, m.start()),
        ))
    return out


def parse_file(path: Path) -> list[ParsedTool]:
    """Parse a literate-tools file from disk. Missing file → []."""
    if not path.exists():
        return []
    return parse_text(path.read_text())
