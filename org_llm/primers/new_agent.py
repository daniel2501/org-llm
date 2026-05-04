"""Primer: shipping a new agent via @agentsmith.

Reads ``docs/wiki/agentsmith.org`` at call time and extracts
"The seven design steps" section, which is the canonical
new-agent shape (domain → name → persona → tools → triggers →
capabilities → recipes/inferrers).
"""

from __future__ import annotations

from ._repo import extract_section, read

_SOURCE = "docs/wiki/agentsmith.org"


def render() -> str:
    agentsmith = read(_SOURCE)
    seven_steps = extract_section(agentsmith, "The seven design steps")

    return f"""\
PRIMER: new-agent
Source (always-fresh, read at fetch time): {_SOURCE}

Every new org-llm agent walks the same seven design steps.
Answer "I don't know — you decide" on any step and @agentsmith
suggests a default based on the user's vault and the existing
roster.

Before drafting:

1. Read agents.org for the current roster and the design rules
   (≥80% trigger overlap → merge candidate; only-invoked-by-
   other-agents → tool, not agent).
2. Read agent-roster.org for naming patterns and icon palettes.
3. Identify whether the proposed agent overlaps with an existing
   one — if so, the right answer is often a mode of an existing
   agent, not a new one.

After drafting, register via the literate file
~/org/org-llm-agents.org and run `org-llm agents --apply`.

--- The seven design steps (verbatim from {_SOURCE}) ---
{seven_steps}"""
