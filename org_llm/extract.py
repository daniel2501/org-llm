"""Extract retire-verdicted code per cull-walk verdicts.

Phase 2026-05.01 — physical extraction. Reads a cull verdict
(=docs/wiki/cull.org= by default), matches a target component,
and produces an extraction prompt for an org-llm @-agent
(default =@data=) to generate the migration diff.

Routing: callers should pass the prompt to
=_cloud_chat_with_local_fallback= with the @data persona as the
=system= argument. FOSS cloud model floor =qwen2.5-72b=. No
direct Claude API calls in the verb's path — that's enforced by
the calling CLI command, not this module.

This module is the verdict-parsing + prompt-construction layer.
The =extract= CLI command (in =cli.py=) wires it to the cloud
chat layer + handles =--apply= / =--dry-run= flags.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


# Decisions that mean "physically extract this" — the verb proceeds.
_EXTRACT_DECISIONS = {"RETIRE", "SUBSUMED", "SPLIT"}

# Decisions that mean "do NOT extract" — the verb refuses.
_REJECT_DECISIONS = {
    "KEEP", "CORE", "RESOLVED",
    "CORE_DISABLED_BY_DEFAULT", "CORE_DISABLED",
    "COMMUNITY",
}


class ExtractError(Exception):
    """Raised when a target can't or shouldn't be extracted."""


@dataclass
class Verdict:
    """Parsed cull-walk verdict block for one component."""
    target:           str
    component_heading: str
    decision_keyword: str
    decision_full:    str
    where_lives:      str
    what_does:        str
    what_depends_on:  str
    raw_block:        str


def _normalize_target(target: str) -> tuple[str, str]:
    """Produce (canonical_path, basename_no_ext) for matching."""
    p = Path(target)
    return (str(p), p.stem)


def parse_verdict(verdict_file: Path, target: str) -> Verdict:
    """Find + parse the cull-verdict block matching =target=.

    Matches against =*Where it lives:*= paths in cull.org
    components.

    Raises =ExtractError= when:
      - no matching block found, or
      - decision keyword is in the reject set
        (KEEP / CORE / RESOLVED / CORE_DISABLED / COMMUNITY).
    """
    text = verdict_file.read_text()
    canon_path, stem = _normalize_target(target)

    # Split on level-3 component headings (=*** <name>=).
    components = re.split(r"^\*\*\* ", text, flags=re.MULTILINE)[1:]

    for comp in components:
        comp_text = "*** " + comp
        m = re.search(r"\*Where it lives:\*\s*([^\n]+)", comp_text)
        if not m:
            continue
        where = m.group(1).strip()
        if canon_path in where or stem in where:
            return _build_verdict_from_block(comp_text, target, where)

    raise ExtractError(
        f"no verdict block found for {target!r} in {verdict_file}"
    )


def _build_verdict_from_block(block: str, target: str,
                                where_lives: str) -> Verdict:
    heading = block.split("\n", 1)[0].lstrip("* ").rstrip()
    decision_full = _grab_field(block, "Decision")
    what_does     = _grab_field(block, "What it does")
    what_depends  = _grab_field(block, "What depends on it")
    decision_keyword = _classify_decision(decision_full)

    if decision_keyword in _REJECT_DECISIONS:
        raise ExtractError(
            f"target {target!r} has decision={decision_keyword!r} — "
            f"not eligible for extraction. Component: {heading!r}."
        )
    if decision_keyword not in _EXTRACT_DECISIONS:
        raise ExtractError(
            f"target {target!r} has unrecognized decision keyword "
            f"{decision_keyword!r} in block: "
            f"{decision_full[:120]!r}"
        )

    return Verdict(
        target=target,
        component_heading=heading,
        decision_keyword=decision_keyword,
        decision_full=decision_full,
        where_lives=where_lives,
        what_does=what_does,
        what_depends_on=what_depends,
        raw_block=block,
    )


def _grab_field(block: str, field: str) -> str:
    """Pull =- *Field:*= bullet text up to next bullet or section."""
    pattern = (
        rf"-\s*\*{re.escape(field)}:\*\s*"
        rf"(.+?)(?=\n-\s*\*|\n\*\*\* |\Z)"
    )
    m = re.search(pattern, block, flags=re.DOTALL)
    return m.group(1).strip() if m else ""


def _classify_decision(decision_text: str) -> str:
    """Classify by EARLIEST position, breaking ties with LONGEST match.

    A SPLIT verdict's decision_full often reads
    "SPLIT — track-selection SUBSUMED per scan"; SPLIT is the
    primary decision (position 0), SUBSUMED is action verb later.
    Keyword-order alone wrongly picks SUBSUMED. Position-order
    fixes that. CORE_DISABLED_BY_DEFAULT and plain CORE both
    start at position 0 — longer wins on tie.
    """
    keywords = ("CORE_DISABLED_BY_DEFAULT", "CORE_DISABLED",
                "SUBSUMED", "RETIRE", "SPLIT", "RESOLVED",
                "COMMUNITY", "KEEP", "CORE")
    hits = []
    for kw in keywords:
        idx = decision_text.find(kw)
        if idx >= 0:
            hits.append((idx, -len(kw), kw))
    if not hits:
        return "UNKNOWN"
    hits.sort()
    return hits[0][2]


def build_prompt(verdict: Verdict, target_content: str) -> str:
    """Construct the extraction prompt for the agent."""
    return f"""You are extracting retire-verdicted code per a cull-walk decision.

# Cull verdict
{verdict.raw_block.strip()}

# Target file content
File: {verdict.where_lives}
Decision: {verdict.decision_keyword}

```python
{target_content}
```

# Your task
Produce a unified diff that:
1. Performs the {verdict.decision_keyword} action on this file.
2. For SPLIT: extract only the SUBSUMED portion described in the
   verdict; keep the rest in place.
3. For SUBSUMED: rewrite consumers to use the named replacement
   (or inline SQL if no view defined yet).
4. For RETIRE: delete the file outright + remove every consumer
   that imports from it.
5. Tests must remain green after the diff applies (pytest).
6. Commit message follows the project style: lowercase verb,
   scope in parens, dash for sub-clause, under 70 chars subject.

Output: the unified diff in a fenced ```diff block, followed by
a one-paragraph summary of consumer migrations.
"""


@dataclass
class ExtractionContext:
    """Bundle returned by =prepare_extraction= — ready for the
    cloud-chat call."""
    verdict:      Verdict
    prompt:       str
    target_path:  Path


def prepare_extraction(target: str, verdict_file: Path,
                        repo_root: Path) -> ExtractionContext:
    """Resolve target → verdict → prompt.

    Raises =ExtractError= if the verdict isn't extraction-eligible
    or the target file can't be located.
    """
    verdict = parse_verdict(verdict_file, target)

    # Try direct path first, then verdict's =Where it lives:= path.
    target_path = repo_root / target
    if not target_path.exists():
        m = re.search(r"=([^=]+)=", verdict.where_lives)
        if m:
            target_path = repo_root / m.group(1)
    if not target_path.exists():
        raise ExtractError(
            f"could not resolve target file path for {target!r}; "
            f"tried {repo_root / target} and verdict-derived path"
        )

    target_content = target_path.read_text()
    prompt = build_prompt(verdict, target_content)
    return ExtractionContext(
        verdict=verdict, prompt=prompt, target_path=target_path,
    )
