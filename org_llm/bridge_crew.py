"""Bridge Crew → Agor assistant materializer + sync (Phase 24.x / 29.x).

The Bridge Crew is the curated 7-persona core team locked by
DEC-014 — Bridge Crew (extends DEC-009 — Curated core agent
team + extension path). Operationalizing them as Agor
=assistant= worktrees lets each persona run as a long-lived,
stateful collaborator inside the Agor UI rather than only as
an in-process org-llm specialist.

Agor v0.17.3 reality (verified live 2026-05-06):
  * The =worktrees= table has *no* =custom_context= column.
    Earlier prose in this module guessed =worktree.custom_context.assistant=
    from a TypeScript declaration; the actual REST/DB surface synthesises
    =custom_context: null= but doesn't persist a value. Therefore the
    "register persona via Agor's API (isAssistant() returns true)"
    follow-up *cannot ship today* — there's no API to register against.
  * What works today: write SOUL.md / IDENTITY.md / USER.md into
    =<worktree>/.agor-assistants/<persona>/=, and have spawn helpers
    inject those files at session-spawn time. =tracker agor-sync= is
    that integration verb — it materialises files PLUS writes a
    structured manifest (=agor.yaml=) for forward compat AND updates
    a central index (=~/.agor/concepts/bridge-crew.json=) so spawn
    helpers can resolve =@<handle>= to its persona dir without scanning.

When upstream Agor exposes a real assistant primitive (REST
=/assistants= endpoint, =worktrees.custom_context= column, or both),
=tracker agor-sync= gains a registration step on top of the existing
materialize + manifest + index halves. The file layout shipped here
stays canonical regardless.

The persona content (SOUL / IDENTITY / USER text) lives in
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

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


# Subdirectory under the Agor worktree where org-llm writes
# the per-persona SOUL/IDENTITY/USER trio. Chosen as a single
# top-level dir so the materializer is reversible (one rm -rf)
# and doesn't collide with Agor's own =.agor.yml= /
# =.agor-meta= conventions.
ASSISTANTS_SUBDIR = ".agor-assistants"
PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "USER.md")

# Filename of the per-persona composed system prompt written next
# to the SOUL/IDENTITY/USER trio by the materializer (BUG-5
# workaround for Agor v0.17.3, which does NOT auto-load the trio
# at session-spawn). Single file Agor can consume if upstream ever
# adds an auto-load hook; until then, `create_session_with_persona`
# reads it (or the trio directly) and prepends to the initial
# session prompt.
COMPOSED_FILENAME = "composed.md"

# Filename of the per-persona structured manifest written next
# to the SOUL/IDENTITY/USER trio. Forward-compat: when upstream
# Agor exposes /assistants or worktrees.custom_context.assistant,
# `tracker agor-sync` can PATCH this dict into the API.
MANIFEST_FILENAME = "agor.yaml"

# Central index — one file under ~/.agor/concepts/ that maps
# every Bridge Crew handle to its worktree + persona-dir + manifest
# path. Spawn helpers can resolve =@<handle>= without scanning.
DEFAULT_CONCEPTS_DIR = Path("~/.agor/concepts").expanduser()
CENTRAL_INDEX_FILENAME = "bridge-crew.json"

# REST surface (verified live 2026-05-06 against agor-live v0.17.3).
DEFAULT_AGOR_BASE_URL = "http://localhost:3030"
DEFAULT_AGOR_TOKEN_FILE = Path("~/.agor/cli-token").expanduser()


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
        # BUG-5 workaround (Agor v0.17.3): also drop a single
        # `composed.md` next to the trio so a future Agor
        # auto-load hook (if upstream ships one) has one file to
        # read. Today this file is what `create_session_with_persona`
        # would prefer to read. NOT added to the WriteRecord list
        # so the public 21-write contract (7 personas × 3 files)
        # stays stable for callers that count records.
        if commit:
            composed = compose_system_prompt(
                p.handle, worktree, base_system_prompt=None,
            )
            (base / COMPOSED_FILENAME).write_text(composed, encoding="utf-8")
    return out


# ── BUG-5 workaround: SOUL→behavior wiring at session-spawn ────
#
# Agor v0.17.3 does NOT auto-load <worktree>/.agor-assistants/<handle>/
# at session create. Verified against the Session schema in
# `agor-live/dist/core/session-CAfhv1qL.d.ts` — there is no
# `system_prompt`, `append_system_prompt`, or any equivalent
# field on POST /sessions. The closest thing is the SpawnConfig's
# `extraInstructions` (which is *appended* to the spawn prompt,
# not a system-prompt slot, and is only available on `spawn`,
# not on `create`).
#
# Workaround: at session-create time, read SOUL.md / IDENTITY.md
# / USER.md from disk and prepend them to the session's initial
# user prompt. This makes the persona land in the agent's first
# turn — close to a system-prompt override in effect, and the
# only knob v0.17.3 exposes.
#
# When upstream Agor adds a real persona / system_prompt knob
# (a worktree-level auto-load OR a /sessions field), swap the
# prepend for that field; the composed string we already build
# is the same payload either way.


def _read_persona_file(base: Path, fname: str) -> str:
    """Read one persona file, returning '' if missing.

    SOUL.md and IDENTITY.md are required; USER.md is optional
    (a freshly materialized persona always has all three, but
    a partially-hand-edited dir may have dropped one). We tolerate
    a missing USER.md gracefully so the composed prompt still
    boots. SOUL/IDENTITY missing is a hard error in
    `compose_system_prompt`.
    """
    p = base / fname
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").rstrip()


def compose_system_prompt(
    handle: str,
    worktree_path: Path,
    base_system_prompt: str | None = None,
) -> str:
    """Compose the on-disk SOUL/IDENTITY/USER trio into one prompt.

    Reads `<worktree_path>/.agor-assistants/<handle>/{SOUL,IDENTITY,USER}.md`
    and joins them with stable section separators. If
    `base_system_prompt` is non-None, it is appended *after* the
    SOUL/IDENTITY/USER block so caller-supplied overrides
    (env-specific notes, time-of-day hints, etc.) win the last
    word.

    Layout:
        [SOUL.md content]
        --- IDENTITY ---
        [IDENTITY.md content]
        --- USER PREFERENCES ---
        [USER.md content]
        [--- BASE ---
        base_system_prompt]      # only if base_system_prompt is non-None

    Args:
        handle: Bridge Crew handle (no @-prefix).
        worktree_path: the Agor worktree root containing
            `.agor-assistants/<handle>/`.
        base_system_prompt: optional override appended last.

    Raises:
        KeyError: unknown handle (re-raised from `get_persona`).
        FileNotFoundError: persona dir missing OR SOUL.md /
            IDENTITY.md missing on disk. USER.md missing is
            tolerated (the section is omitted).
    """
    persona = get_persona(handle)            # validates handle
    base = worktree_path / ASSISTANTS_SUBDIR / persona.handle
    if not base.exists():
        raise FileNotFoundError(
            f"Bridge Crew persona dir missing: {base}. "
            f"Run `org-llm bridge-crew materialize {worktree_path} --commit` first."
        )
    soul     = _read_persona_file(base, "SOUL.md")
    identity = _read_persona_file(base, "IDENTITY.md")
    user     = _read_persona_file(base, "USER.md")
    if not soul:
        raise FileNotFoundError(f"SOUL.md missing or empty under {base}")
    if not identity:
        raise FileNotFoundError(f"IDENTITY.md missing or empty under {base}")

    sections: list[str] = [soul, "--- IDENTITY ---", identity]
    if user:
        sections += ["--- USER PREFERENCES ---", user]
    if base_system_prompt is not None and base_system_prompt.strip():
        sections += ["--- BASE ---", base_system_prompt.rstrip()]
    # Trailing newline matches the on-disk persona files (which
    # all end in `\n`) so downstream concatenation is clean.
    return "\n".join(sections) + "\n"


def create_session_with_persona(
    persona_handle: str,
    worktree_path: Path,
    *,
    prompt: str | None = None,
    worktree_id: str | None = None,
    agentic_tool: str = "claude-code",
    base_url: str = DEFAULT_AGOR_BASE_URL,
    token_file: Path = DEFAULT_AGOR_TOKEN_FILE,
    timeout: int = 30,
    _post: Any = None,
) -> dict[str, Any]:
    """Create an Agor session with the persona's composed prompt prepended.

    Reads the on-disk SOUL/IDENTITY/USER trio for `persona_handle`,
    composes them via `compose_system_prompt`, and POSTs to
    `/sessions` with the composed text PLUS any caller-supplied
    `prompt` baked into the session's initial prompt body.

    Field choice (verified against agor-live v0.17.3's Session
    schema): there is NO `system_prompt`, `append_system_prompt`,
    `customSystemPrompt`, or similar field on POST /sessions.
    The only writable, prompt-shaped slot is the legacy
    `description` field (Session.description: "may contain first
    prompt"). We populate `description` AND a top-level `prompt`
    field — Agor's executor reads whichever its current build
    supports. When v0.18+ ships a real system-prompt knob, swap
    the wire field name only; the composed payload stays identical.

    Args:
        persona_handle: Bridge Crew handle (no @-prefix).
        worktree_path: existing worktree containing the persona
            files. Required because the persona files are
            worktree-local.
        prompt: optional user-supplied first turn for the session.
            When None, the composed persona prompt is the entire
            initial body. When non-None, the composed persona
            prompt is prepended above the user prompt with a
            `--- TASK ---` separator.
        worktree_id: Agor worktree UUID. When None, derived from
            `worktree_path` by GET /worktrees match (defers).
            v0 callers should always pass it explicitly.
        agentic_tool: matches the smoke harness default.
        base_url / token_file / timeout: Agor REST plumbing.
        _post: dependency injection seam for tests — a callable
            taking (url, headers, body) and returning the parsed
            JSON response. Defaults to a urllib-based implementor.

    Returns: the parsed `/sessions` response dict (`session_id`,
    `mcp_token`, etc.). Caller is responsible for any cleanup.

    Raises:
        FileNotFoundError: persona dir / required files missing
            OR token file missing.
        ValueError: worktree_id missing AND we can't derive it.
    """
    composed = compose_system_prompt(persona_handle, worktree_path)
    if prompt is not None and prompt.strip():
        body_prompt = composed + "\n--- TASK ---\n" + prompt.rstrip() + "\n"
    else:
        body_prompt = composed

    if not worktree_id:
        # v0: require an explicit worktree_id. Auto-discovery via
        # GET /worktrees is a v0.1 follow-up — keeping this strict
        # makes the failure mode obvious during the BUG-5 manual
        # smoke test.
        raise ValueError(
            "create_session_with_persona requires worktree_id. "
            "Discover via GET /worktrees or pass --worktree-id explicitly."
        )

    payload: dict[str, Any] = {
        "worktree_id":  worktree_id,
        "agentic_tool": agentic_tool,
        # Both shapes — Agor's current executor uses the legacy
        # `description` slot per the Session schema; `prompt` is a
        # forward-compat hint for when upstream lands a real field.
        "description":  body_prompt,
        "prompt":       body_prompt,
    }

    if _post is None:
        _post = _post_sessions_urllib
    return _post(
        f"{base_url.rstrip('/')}/sessions",
        token_file=token_file,
        body=payload,
        timeout=timeout,
    )


def _post_sessions_urllib(
    url: str,
    *,
    token_file: Path,
    body: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """urllib-backed POST to /sessions. Pulled out so tests can
    swap in a mock via the `_post` dependency-injection seam on
    `create_session_with_persona`.
    """
    tok = _read_agor_token(token_file)
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type":  "application/json",
        },
        data=json.dumps(body).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


# ── tracker agor-sync (Phase 29.x extension) ───────────────────
#
# `materialize()` writes the SOUL/IDENTITY/USER trio. `agor-sync`
# layers two more deliverables on top:
#   1. a structured manifest (`agor.yaml`) per persona — joins
#      Bridge Crew persona content with `_AGENT_META` (handle,
#      birth_name, aliases, capabilities, recipes, pack), so
#      future spawn helpers + upstream Agor APIs have a single
#      file to read.
#   2. a central index at `~/.agor/concepts/bridge-crew.json`
#      mapping every handle to its worktree + manifest path —
#      idempotent, merges across runs, preserves untouched
#      personas. Spawn helpers can resolve `@<handle>` via this
#      index without rescanning every worktree.
#
# Verified upstream gaps (v0.17.3, see module docstring):
#   * No `/assistants` REST endpoint. The "register a persona"
#     half cannot ship today.
#   * `worktrees.custom_context.assistant` PATCH succeeds
#     silently with no persistence. Don't bother.
# `agor-sync` therefore stays file-only on disk; the manifest
# carries a `forward_compat` block documenting the gap.


@dataclass(frozen=True)
class SyncResult:
    """Per-persona result returned by `sync()`. Used by the CLI
    to render the summary table and (later) by callers that want
    to chain post-sync actions.
    """
    persona:        str
    worktree_id:    Optional[str]      # None for explicit-path mode
    worktree_path:  Path
    files_written:  tuple[Path, ...]   # paths of SOUL/IDENTITY/USER
    manifest_path:  Path
    manifest_written: bool
    index_updated:  bool
    skipped_reason: str = ""           # set when persona was skipped


@dataclass
class AgentMetaJoin:
    """Subset of `_AGENT_META` exposed to the manifest writer.

    Decoupled from `agents._builtins._AgentMeta` so this module
    doesn't import from `agents` (which imports from `cli` —
    circular). Caller looks up `_AGENT_META` and hands us a
    plain projection. Falls back to empty defaults if a Bridge
    Crew handle isn't in `_AGENT_META`.
    """
    birth_name:   str = ""
    aliases:      tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    recipes:      tuple[str, ...] = ()
    pack:         str = "starfleet-core"
    addressable:  bool = True


def lookup_agent_meta(handle: str) -> AgentMetaJoin:
    """Project the `_AGENT_META` row for a Bridge Crew handle into
    an `AgentMetaJoin`. Lazy import to keep the dep graph one-way:
    `cli` → `bridge_crew` → (lazy) `agents._builtins`.

    Bridge Crew handles map to `_AGENT_META` keys via the legacy
    name (e.g. `picard` is keyed under `crew`, `spock` under
    `researcher`). Walks the dict and matches on `birth_name`
    OR alias OR legacy key.
    """
    try:
        from .agents._builtins import _AGENT_META
    except Exception:
        return AgentMetaJoin()
    h = handle.lower()
    for legacy_key, meta in _AGENT_META.items():
        if (legacy_key.lower() == h
                or meta.birth_name.lower() == h
                or h in (a.lower() for a in meta.aliases)):
            return AgentMetaJoin(
                birth_name=meta.birth_name,
                aliases=tuple(meta.aliases),
                capabilities=tuple(meta.capabilities),
                recipes=tuple(meta.recipes),
                pack=meta.pack,
                addressable=meta.addressable,
            )
    return AgentMetaJoin()


def cross_check_meta(personas: Iterable[Persona] | None = None) -> list[str]:
    """Return a list of Bridge Crew handles that are MISSING from
    `_AGENT_META`. Empty list = no drift. Used to surface drift
    in the CLI summary so `agor-sync` doubles as a metadata audit.
    """
    pool = list(personas) if personas is not None else list(BRIDGE_CREW)
    drift: list[str] = []
    for p in pool:
        meta = lookup_agent_meta(p.handle)
        if not meta.birth_name:
            drift.append(p.handle)
    return drift


def agor_yaml_for(persona: Persona, meta: AgentMetaJoin) -> str:
    """Render the per-persona manifest as a small YAML string.

    We write this by hand (no PyYAML dep) — the schema is small
    and stable, and a hand-rolled writer keeps the comment
    header + field order deterministic. Fields tracked here are
    the union of what `_AGENT_META` stores and what a future
    Agor `/assistants` POST body would plausibly need.
    """
    def _yaml_list(items: tuple[str, ...]) -> str:
        if not items:
            return "[]"
        return "[" + ", ".join(items) + "]"

    return (
        "# Generated by `org-llm tracker agor-sync` — do not edit by hand.\n"
        "# Source: org_llm/bridge_crew.py + org_llm/agents/_builtins.py.\n"
        "# Edits should target the wiki canon\n"
        "# (docs/wiki/bridge-crew-agor-assistants.org) then re-run agor-sync.\n"
        f"handle: {persona.handle}\n"
        f"birth_name: {meta.birth_name or persona.handle}\n"
        f"role: \"{persona.role}\"\n"
        f"aliases: {_yaml_list(meta.aliases)}\n"
        f"capabilities: {_yaml_list(meta.capabilities)}\n"
        f"recipes: {_yaml_list(meta.recipes)}\n"
        f"pack: {meta.pack or 'starfleet-core'}\n"
        f"addressable: {'true' if meta.addressable else 'false'}\n"
        "files:\n"
        "  soul: SOUL.md\n"
        "  identity: IDENTITY.md\n"
        "  user: USER.md\n"
        "forward_compat:\n"
        "  # When upstream Agor exposes worktrees.custom_context.assistant\n"
        "  # OR a /assistants REST endpoint, agor-sync will PATCH this manifest\n"
        "  # into the API. Today: file-only.\n"
        "  api_status: \"v0.17.3 — no /assistants endpoint; "
        "no worktrees.custom_context column\"\n"
    )


def central_index_path(concepts_dir: Path | None = None) -> Path:
    """Resolve the central Bridge Crew index path.

    Override via `concepts_dir` (test) or env `AGOR_DATA_DIR`
    (which holds the agor data dir; we suffix `/concepts/`).
    """
    if concepts_dir is not None:
        return concepts_dir / CENTRAL_INDEX_FILENAME
    env_data = os.environ.get("AGOR_DATA_DIR")
    if env_data:
        return Path(env_data).expanduser() / "concepts" / CENTRAL_INDEX_FILENAME
    return DEFAULT_CONCEPTS_DIR / CENTRAL_INDEX_FILENAME


def _now_iso() -> str:
    """ISO-8601 timestamp w/ tz, used in the index `generated_at`."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_central_index(path: Path) -> dict[str, Any]:
    """Load the central index (or return a fresh empty shell).

    Tolerant of missing file / unparseable file — both yield a
    fresh dict so the verb stays usable on a clean install.
    """
    if not path.exists():
        return {"version": 1, "generated_at": "", "personas": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "personas" not in data:
            return {"version": 1, "generated_at": "", "personas": {}}
        data.setdefault("version", 1)
        data.setdefault("generated_at", "")
        if not isinstance(data["personas"], dict):
            data["personas"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "generated_at": "", "personas": {}}


def update_central_index(
    path: Path,
    *,
    handle: str,
    worktree_id: Optional[str],
    worktree_path: Path,
    persona_dir: Path,
    manifest_filename: str = MANIFEST_FILENAME,
    commit: bool = False,
) -> bool:
    """Merge one persona entry into the central index.

    Returns True if the file was actually written (commit=True)
    or *would* change (commit=False — purely a planning probe;
    no I/O).
    """
    data = load_central_index(path)
    new_entry = {
        "worktree_id":   worktree_id or "",
        "worktree_path": str(worktree_path),
        "persona_dir":   str(persona_dir),
        "manifest":      manifest_filename,
    }
    cur = data["personas"].get(handle)
    changed = (cur != new_entry)
    if not commit:
        return changed
    data["personas"][handle] = new_entry
    data["generated_at"] = _now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def sync(
    persona: Persona,
    worktree_path: Path,
    *,
    worktree_id: Optional[str] = None,
    commit: bool = False,
    index_path: Path | None = None,
) -> SyncResult:
    """Sync ONE Bridge Crew persona into one Agor worktree.

    Orchestrates: materialize → manifest write → central-index
    merge. Idempotent: re-running with no input change is a
    no-op on disk. Read-only when `commit=False`.

    Args:
        persona: a `Persona` from `BRIDGE_CREW`.
        worktree_path: existing worktree dir (we refuse to create
            it — that's Agor's job; `--create-missing-worktrees`
            is a v0.1 follow-up).
        worktree_id: optional Agor UUID (set in --all mode where
            we discovered the worktree via REST; None in
            explicit --worktree mode).
        commit: when False, plan-only — no FS or REST writes.
        index_path: override for the central index file (test).

    Returns: SyncResult with the planned-or-executed paths +
    flags for the summary table.
    """
    if commit and not worktree_path.exists():
        return SyncResult(
            persona=persona.handle,
            worktree_id=worktree_id,
            worktree_path=worktree_path,
            files_written=(),
            manifest_path=worktree_path / ASSISTANTS_SUBDIR / persona.handle
                          / MANIFEST_FILENAME,
            manifest_written=False,
            index_updated=False,
            skipped_reason=f"worktree not found: {worktree_path}",
        )

    persona_dir = worktree_path / ASSISTANTS_SUBDIR / persona.handle
    manifest_path = persona_dir / MANIFEST_FILENAME

    # 1. SOUL/IDENTITY/USER trio — delegate to the existing materializer.
    if commit:
        records = materialize(worktree_path, commit=True, personas=(persona,))
    else:
        records = planned_writes(worktree_path, personas=(persona,))
    files = tuple(r.path for r in records)

    # 2. Manifest.
    meta = lookup_agent_meta(persona.handle)
    manifest_body = agor_yaml_for(persona, meta)
    manifest_written = False
    if commit:
        persona_dir.mkdir(parents=True, exist_ok=True)
        existing = ""
        if manifest_path.exists():
            try:
                existing = manifest_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        if existing != manifest_body:
            manifest_path.write_text(manifest_body, encoding="utf-8")
            manifest_written = True
        else:
            # idempotent re-run — flag as "written" since the file
            # exists with the right content. Disambiguate from
            # "actually wrote bytes this run" via `changed`-style
            # callers in the future if needed.
            manifest_written = True

    # 3. Central index.
    idx_path = index_path if index_path is not None else central_index_path()
    index_updated = update_central_index(
        idx_path,
        handle=persona.handle,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        persona_dir=persona_dir,
        commit=commit,
    )

    return SyncResult(
        persona=persona.handle,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        files_written=files,
        manifest_path=manifest_path,
        manifest_written=manifest_written,
        index_updated=index_updated,
    )


# ── REST: worktree auto-discovery (--all mode) ─────────────────


def _read_agor_token(token_file: Path = DEFAULT_AGOR_TOKEN_FILE) -> str:
    """Pull the admin JWT from `~/.agor/cli-token` (Feathers shape).

    Raises FileNotFoundError if the token file is missing — the
    CLI catches this + prints a hint to run `agor login`.
    """
    if not token_file.exists():
        raise FileNotFoundError(
            f"Agor token file not found: {token_file}. "
            "Run `agor login -e admin@agor.live -p $(pass org-llm/agor/admin-password)`."
        )
    blob = json.loads(token_file.read_text(encoding="utf-8"))
    tok = blob.get("accessToken")
    if not tok:
        raise ValueError(f"Token file {token_file} has no .accessToken field.")
    return tok


def list_worktrees(
    *,
    base_url: str = DEFAULT_AGOR_BASE_URL,
    token_file: Path = DEFAULT_AGOR_TOKEN_FILE,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """GET /worktrees and return the data array.

    Pure stdlib (urllib) so this module ships zero new deps.
    `requests` is a heavier import that doesn't pay for itself
    here; the call shape is trivial.
    """
    tok = _read_agor_token(token_file)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/worktrees",
        headers={"Authorization": f"Bearer {tok}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        body = json.loads(r.read().decode("utf-8"))
    if not isinstance(body, dict):
        return []
    data = body.get("data") or []
    return [w for w in data if isinstance(w, dict)]


def discover_worktree_for_handle(
    handle: str,
    worktrees: Iterable[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Pick the first non-archived worktree whose name matches
    one of the conventional patterns:

      * `bridge-crew-<handle>`   (preferred)
      * `<handle>-assistant`     (legacy / alt)
      * `<handle>`               (last resort — exact)

    Returns the matched worktree row, or None.
    """
    h = handle.lower()
    candidates = [
        f"bridge-crew-{h}",
        f"{h}-assistant",
        h,
    ]
    rows = [w for w in worktrees if not w.get("archived")]
    for pat in candidates:
        for w in rows:
            if (w.get("name") or "").lower() == pat:
                return w
    return None
