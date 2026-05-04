"""Primer: adding or editing a phase entry in ``dev-tracker.org``.

Reads ``docs/wiki/dev-tracker.org`` at call time and extracts the
"How to use this file" section (the property-block + tag
conventions). Adds the recommended-ship-order template that every
multi-sub-phase entry follows.
"""

from __future__ import annotations

from ._repo import extract_section, read

_SOURCE = "docs/wiki/dev-tracker.org"


def render() -> str:
    tracker = read(_SOURCE)
    how_to = extract_section(tracker, "How to use this file")

    return f"""\
PRIMER: dev-tracker-entry
Source (always-fresh, read at fetch time): {_SOURCE}

When adding or editing a phase entry in dev-tracker.org:

1. Heading shape:
     ** TODO Phase <NN>[.<sub>] — <one-line title>   :tag:tag:tag:
   Workflow states defined in the file's #+TODO header:
   TODO / NEXT / STARTED / HOLD / WAITING / DONE / CANCELLED.

2. :PROPERTIES: block (every entry):
     :PRIORITY:        A | B | C
     :GATE:            1 | 2 | 3 | 1,2 | 1,2,3
     :EFFORT:          <half-day | ~Nd | multi-day breakdown>
     :PHASE_CANDIDATE: yes | no
     :SOURCE:          <ISO date> — <brief origin>

3. Body shape for multi-sub-phase entries:
     *Generative principle:* …
     *Why this is gate-1/2/3 healthy:* …
     *** Recommended ship order (N sub-phases)
     1. *Phase NN.1 — …* description, ~Nd.
     2. …
     *** Connections
     - <other phase / wiki page / file>
     *Scheduling:* …

4. Cross-link by ID where possible:
   - dev-tracker itself: [[id:9096622a-689c-4bb1-8e32-1654bce48d1c][dev-tracker.org]]
   - roadmap:           [[id:43e1b335-656b-45ba-8d6d-7d9b9f2e9c4d][roadmap.org]]
   - architecture:      [[id:30d4ee70-0472-4bcb-a02e-efde52bef3c6][architecture.org]]

--- "How to use this file" (verbatim from {_SOURCE}) ---
{how_to}"""
