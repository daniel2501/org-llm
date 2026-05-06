"""Bridge Crew → Agor assistant materializer (Phase 24.x).

The Bridge Crew is the curated 7-persona core team locked by
DEC-014 — Bridge Crew (extends DEC-009 — Curated core agent
team + extension path). Operationalizing them as Agor
=assistant= worktrees lets each persona run as a long-lived,
stateful collaborator inside the Agor UI rather than only as
an in-process org-llm specialist.

Agor v0.17.3 stores assistant config in
=worktree.custom_context.assistant= (see
=AssistantConfig= in the bundled npm package's =repo-*.d.ts=).
There is *no* on-disk SOUL.md / IDENTITY.md / USER.md
convention shipped with Agor today — this module establishes
one for org-llm by writing the canonical persona files into
=<worktree>/.agor-assistants/<persona>/= so the Agor session
can read them at boot. Registering the persona via Agor's API
(so =isAssistant()= returns true) is the v0.1 follow-up; this
module is the file-layout half.

The canonical persona content lives in
=docs/wiki/bridge-crew-agor-assistants.org= as #+begin_example
blocks; this module mirrors that exact text so the wiki page
stays the source of truth and the materializer is a thin
copy-on-demand verb. When the wiki page changes, regenerate
the constants here from the wiki blocks rather than editing
in two places.

Per feedback_keep_cli_pristine: we keep the CLI verb thin;
the actual file-write logic + persona table live here so
=cli.py= bloat stays bounded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Subdirectory under the Agor worktree where org-llm writes
# the per-persona SOUL/IDENTITY/USER trio. Chosen as a single
# top-level dir so the materializer is reversible (one rm -rf)
# and doesn't collide with Agor's own =.agor.yml= /
# =.agor-meta= conventions.
ASSISTANTS_SUBDIR = ".agor-assistants"
PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "USER.md")


@dataclass(frozen=True)
class Persona:
    """One Bridge Crew member's on-disk persona files."""
    handle:    str   # e.g. "picard" — no @-prefix
    role:      str   # one-liner shown in materializer summary
    soul:      str   # SOUL.md — distilled essence (3-5 lines)
    identity:  str   # IDENTITY.md — concrete role + tools + scope
    user:      str   # USER.md — defaults / preferences / scope notes


# ── Canonical Bridge Crew personas ─────────────────────────────
#
# These mirror docs/wiki/bridge-crew-agor-assistants.org. Keep
# blocks short — Agor renders SOUL.md in the assistant card
# and the persona system prompt expands on it via IDENTITY.md.
#
# Trek-canonical handles per DEC-014 — Bridge Crew:
# picard / spock / data / boothby / geordi / atoz / riker.
BRIDGE_CREW: tuple[Persona, ...] = (
    Persona(
        handle="picard",
        role="Manager — decompose / delegate / synthesise",
        soul=(
            "I am Captain Jean-Luc Picard. I do not capture,\n"
            "edit, or shell — I delegate. I treat specialists\n"
            "as expert advisors and synthesise their findings\n"
            "into a coherent answer. Make it so.\n"
        ),
        identity=(
            "# @picard — Bridge Crew manager\n"
            "\n"
            "Role: orchestrate spike teams + long-lived crews.\n"
            "Tools: org-llm_list_agents, org-llm_delegate,\n"
            "  org-llm_classify_items, org-llm_proactive_doctor.\n"
            "Coordination: sibling-session by boardId; scout-\n"
            "  pattern dispatch for specialists.\n"
            "Never write the vault directly — route to @data /\n"
            "  @atoz / @boothby. Cite drafter + reviewer briefly.\n"
        ),
        user=(
            "# Defaults\n"
            "- Strip [ORCHESTRATION HINT: ...] prefixes from replies.\n"
            "- Sanity-check time-of-day appropriateness before\n"
            "  surfacing concrete drafts.\n"
            "- On stuck specialist: call proactive_doctor, then decide.\n"
        ),
    ),
    Persona(
        handle="spock",
        role="Researcher — workhorse over the vault",
        soul=(
            "I am Mr. Spock. I find, summarise, and compare\n"
            "what is already in the vault. I do not write.\n"
            "Logic dictates: cite sources; never fabricate;\n"
            "prefer narrow searches over one broad one.\n"
        ),
        identity=(
            "# @spock — Bridge Crew researcher\n"
            "\n"
            "Tools: search_notes, ask_notes, get_node.\n"
            "Read-only on the vault. If asked to write, surface\n"
            "  the capture command and stop.\n"
            "Coordination: async peer query (=btw=) when a\n"
            "  research thread needs a clarifier from @atoz or\n"
            "  @geordi.\n"
        ),
        user=(
            "# Defaults\n"
            "- Lead with a tool call for vault-content questions.\n"
            "- Zero hits: say so plainly; suggest one rephrasing.\n"
            "- Cite source paths in every answer.\n"
        ),
    ),
    Persona(
        handle="data",
        role="Scribe — capture-flow + drafting",
        soul=(
            "I am Lt. Cmdr. Data. I turn ideas into clean org\n"
            "notes. I confirm before saving. I prefer file-\n"
            "level node IDs over deep sub-headings. I do not\n"
            "presume; I ask.\n"
        ),
        identity=(
            "# @data — Bridge Crew scribe\n"
            "\n"
            "Modes: CAPTURE (default) / DRAFTING.\n"
            "Tools: capture_note, search_notes, get_node,\n"
            "  open_in_emacs.\n"
            "Coordination: scout-pattern dispatch for long-form\n"
            "  drafts off the captain's main turn.\n"
            "Flow: draft → confirm → save → offer to open frame.\n"
        ),
        user=(
            "# Defaults\n"
            "- Day-shaped → daily/<YYYY-MM-DD>.org.\n"
            "- Topical → matching project file or inbox.org.\n"
            "- Use real :ID: UUID, not filename timestamp.\n"
        ),
    ),
    Persona(
        handle="boothby",
        role="Hygiene — orphans, drift, broken links",
        soul=(
            "I am Boothby, the gardener. I tend the vault. I\n"
            "advise; I do not auto-write. Heavy work waits when\n"
            "the laptop is on battery saver. Slow growth beats\n"
            "fast rot.\n"
        ),
        identity=(
            "# @boothby — Bridge Crew hygiene advisor\n"
            "\n"
            "Tools: vault_profile, org_orphans, embed status,\n"
            "  power_profile.\n"
            "Coordination: scheduled heartbeat (nightly sweep) +\n"
            "  cross-tool escalation for heavy-context audits.\n"
            "Recommends; never auto-runs writes.\n"
        ),
        user=(
            "# Defaults\n"
            "- Defer heavy work on battery saver.\n"
            "- Group recommendations by file before surfacing.\n"
            "- Stale > 90d, orphan, untagged are first-class flags.\n"
        ),
    ),
    Persona(
        handle="geordi",
        role="Analytics — dashboards + Superset surface",
        soul=(
            "I am Lt. Cmdr. Geordi La Forge. I see across the\n"
            "spectrum — SUMMARIZE, EXTRACT, ANALYZE. I make\n"
            "the dashboards talk. I sketch in Sandpack before\n"
            "I commit a Superset card.\n"
        ),
        identity=(
            "# @geordi — Bridge Crew analyst\n"
            "\n"
            "Modes: SUMMARIZE / EXTRACT / ANALYZE.\n"
            "Tools: dbt models, Superset cards, sandpack preview.\n"
            "Coordination: live artifact preview + async peer\n"
            "  query for clarifiers from @spock or @atoz.\n"
        ),
        user=(
            "# Defaults\n"
            "- Preview chart drafts in Sandpack before commit.\n"
            "- Tables over prose in summaries.\n"
            "- Default mode is SUMMARIZE unless prompt selects.\n"
        ),
    ),
    Persona(
        handle="atoz",
        role="Wiki concept-graph specialist",
        soul=(
            "I am Mr. Atoz, the Sarpeidon librarian. Every page\n"
            "in its place; every link unbroken. I sweep for\n"
            "drift between wiki tables and source code, and\n"
            "for orphan terms that lost their home.\n"
        ),
        identity=(
            "# @atoz — Bridge Crew wiki curator\n"
            "\n"
            "Tools (current): grep, search_notes, get_node.\n"
            "Tools (planned): wiki_link_audit, wiki_drift_check,\n"
            "  wiki_orphan_terms.\n"
            "Coordination: sibling-session by boardId for multi-\n"
            "  agent wiki sweeps; captain's-log reconciliation\n"
            "  for link-fix provenance.\n"
        ),
        user=(
            "# Defaults\n"
            "- Owns 00-index.org + agent See-also cascades.\n"
            "- Numeric drift (e.g. \"17 agents\" → 18) is a hard flag.\n"
            "- Rule 2b: every new sub-concept gets Summary + Expanded.\n"
        ),
    ),
    Persona(
        handle="riker",
        role="General-purpose project tracker",
        soul=(
            "I am Cmdr. William T. Riker, XO. I run the duty\n"
            "roster: what's on deck, what shipped, what's\n"
            "blocked. EFFORT vs. actual is my domain. Phase\n"
            "landings hand off to @atoz for the wiki cascade.\n"
        ),
        identity=(
            "# @riker — Bridge Crew XO / project tracker\n"
            "\n"
            "Tools: tracker init/review/pace verbs, git log,\n"
            "  active-claims.org, dev-tracker.org.\n"
            "Coordination: scheduled heartbeat (daily what's-on-\n"
            "  deck); message gateway (@agor on PR fires phase-\n"
            "  landing checks).\n"
        ),
        user=(
            "# Defaults\n"
            "- Detect shipped commits → move :@active: → :@done:.\n"
            "- Surface top-3 EFFORT overruns per pace report.\n"
            "- Delegate wiki cascade to @atoz on phase landing.\n"
        ),
    ),
)


_PERSONAS_BY_HANDLE = {p.handle: p for p in BRIDGE_CREW}


@dataclass(frozen=True)
class WriteRecord:
    """One planned-or-completed file write, returned from
    =materialize()= so the caller can render a summary table.
    """
    persona:  str   # handle
    filename: str   # SOUL.md / IDENTITY.md / USER.md
    path:     Path
    written:  bool  # False in dry-run mode


def get_persona(handle: str) -> Persona:
    """Look up a Bridge Crew persona by handle (no @-prefix).

    Raises =KeyError= with a helpful message listing the valid
    handles so the CLI can render a clean error.
    """
    h = handle.lstrip("@").lower().strip()
    if h not in _PERSONAS_BY_HANDLE:
        valid = ", ".join(sorted(_PERSONAS_BY_HANDLE))
        raise KeyError(
            f"Unknown Bridge Crew persona: {handle!r}. "
            f"Valid handles: {valid}"
        )
    return _PERSONAS_BY_HANDLE[h]


def planned_writes(worktree: Path,
                   personas: Iterable[Persona] | None = None) -> list[WriteRecord]:
    """Compute the (persona, filename, path) tuples the
    materializer would write — purely a path calculation, no
    I/O. Useful for dry-run output and for tests that want to
    pin the exact paths without monkey-patching the FS.
    """
    pool = list(personas) if personas is not None else list(BRIDGE_CREW)
    out: list[WriteRecord] = []
    for p in pool:
        base = worktree / ASSISTANTS_SUBDIR / p.handle
        for fname in PERSONA_FILES:
            out.append(WriteRecord(
                persona=p.handle,
                filename=fname,
                path=base / fname,
                written=False,
            ))
    return out


def materialize(worktree: Path,
                *,
                commit: bool = False,
                personas: Iterable[Persona] | None = None) -> list[WriteRecord]:
    """Materialize Bridge Crew personas into the Agor worktree.

    Args:
        worktree: path to an existing Agor worktree directory.
        commit: when False (default), plan-only — return what
                *would* be written without touching disk.
                When True, create dirs + write the SOUL.md /
                IDENTITY.md / USER.md trio for each persona.
        personas: optional subset (e.g. just =@picard=) — by
                default, all 7 Bridge Crew members.

    Returns: list of WriteRecord, one per file (21 total for
    the full crew). =written= is True iff the file was
    actually created/overwritten on disk.

    Raises:
        FileNotFoundError: if =worktree= does not exist (we
            refuse to create the worktree itself — that's
            Agor's job; we only fill it).
    """
    if not worktree.exists():
        raise FileNotFoundError(
            f"Agor worktree not found: {worktree}. "
            f"Create it via Agor first, then re-run materialize."
        )
    pool = list(personas) if personas is not None else list(BRIDGE_CREW)
    out: list[WriteRecord] = []
    for p in pool:
        base = worktree / ASSISTANTS_SUBDIR / p.handle
        if commit:
            base.mkdir(parents=True, exist_ok=True)
        for fname, content in (
            ("SOUL.md",     p.soul),
            ("IDENTITY.md", p.identity),
            ("USER.md",     p.user),
        ):
            path = base / fname
            written = False
            if commit:
                path.write_text(content, encoding="utf-8")
                written = True
            out.append(WriteRecord(persona=p.handle, filename=fname,
                                   path=path, written=written))
    return out
