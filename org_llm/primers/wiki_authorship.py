"""Primer: writing a wiki page under ``docs/wiki/``.

Reads ``docs/wiki/wiki-conventions.org`` at call time; extracts
Rule 2a (link every code-file mention) and Rule 2b (Summary +
Expanded). Generates a fresh UUID for the new page's
``:ID:`` property.
"""

from __future__ import annotations

import uuid

from ._repo import extract_section, read

_SOURCE = "docs/wiki/wiki-conventions.org"
_VERIFIER = "scripts/link_wiki_files.py"


def render() -> str:
    conventions = read(_SOURCE)
    rule_2a = extract_section(conventions, "Rule 2a: Link every code-file mention")
    rule_2b = extract_section(
        conventions, "Rule 2b: Every concept gets a summary + expanded version"
    )
    fresh_id = uuid.uuid4()

    return f"""\
PRIMER: wiki-authorship
Source (always-fresh, read at fetch time): {_SOURCE}

Before writing a docs/wiki/*.org page:

1. Add a fresh :ID: in the :PROPERTIES: block at the top:
   :ID:       {fresh_id}
   (One UUID per page. Generate with `python3 -c "import uuid; print(uuid.uuid4())"`.)

2. Follow Rule 2b — every concept leads with *Summary.* (1–3
   sentences) then *Expanded.* with the full prose. The summary
   IS the load-bearing claim, not the first paragraph by accident.

3. Follow Rule 2a — every in-repo path mention is rendered as
   an org-mode link, never verbatim. The verifier
   `python {_VERIFIER}` sweeps docs/wiki/ and converts strays.
   Run it after a bulk edit.

4. Cross-link by ID for other wiki pages: [[id:UUID][label]].
   Use file: links for code: [[file:../../org_llm/foo.py][=org_llm/foo.py=]].

5. Index the new page in docs/wiki/00-index.org under the right
   concept-map section AND in the status table at the bottom.

--- Rule 2a (verbatim from {_SOURCE}) ---
{rule_2a}
--- Rule 2b (verbatim from {_SOURCE}) ---
{rule_2b}"""
