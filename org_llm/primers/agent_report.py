"""Primer: writing a post-action report under ``docs/notes/``.

Reads ``docs/templates/agent-report.org`` wholesale at call time.
The template is the contract; this primer wraps it with a short
header and the canonical exemplar paths.
"""

from __future__ import annotations

from ._repo import read

_TEMPLATE = "docs/templates/agent-report.org"

_EXEMPLARS = (
    "docs/notes/2026-05-04-promotions-slice-report.org",  # code slice
    "docs/notes/2026-05-04-promotions-wiki-report.org",   # wiki draft
)


def render() -> str:
    template = read(_TEMPLATE)
    exemplars = "\n".join(f"  - {p}" for p in _EXEMPLARS)

    return f"""\
PRIMER: agent-report
Source (always-fresh, read at fetch time): {_TEMPLATE}

When you finish a discrete action (code slice, wiki draft, design
pass, fix), write a structured report at
docs/notes/YYYY-MM-DD-<slug>.org using the template below.

The structure is fixed: Objective → Dev-tracking → What landed →
Decisions → Verification (when applicable) → Open questions →
Non-goals → Gotchas. Objective and Dev-tracking are load-bearing
and never deleted. Verification may be omitted for pure-design /
pure-doc work where there's nothing to verify.

Canonical exemplars to read before writing your first one:
{exemplars}

--- Template (verbatim from {_TEMPLATE}) ---
{template}"""
