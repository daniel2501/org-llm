"""@sidecar — silent side-thought capture during deep work.

v0 of the @sidecar persona implementation per
docs/wiki/sidecar-agent-design.org. The contract:

  * `capture(thought)` is synchronous, deterministic, and must
    complete in well under 500 ms. No LLM round-trip on the hot
    path.
  * Enrichment (auto-tag, project attribution, roam-link) is
    DEFERRED — fired off asynchronously after the entry is
    already on disk. If async enrichment isn't reachable, a tiny
    inline tagger runs in the same call and applies a best-effort
    tag set; the entry never blocks on enrichment.

This module deliberately does NOT depend on cli.py / mcp_server.py
/ llm_proxy.py — the v0 hot path is filesystem-only so it survives
any of those layers being slow / down. The Agor / delegate.fork
hooks are best-effort imports; their absence is the default path,
not the error path.

What's NOT here in v0 (TODOs):
  * CLI verb (`org-llm sidecar park ...`) — v0.1 surface lives in
    Emacs / chat / SMS gateway; CLI verb intentionally deferred.
  * Bridge Crew materializer integration (SOUL/IDENTITY/USER
    files) — defer to the bridge_crew.py wave that ships
    long-tail extension agents.
  * Backpressure / queue-depth handling on `enrich_async` — when
    multiple parks fire enrichment in quick succession we just
    spawn threads. A bounded queue / single worker thread is the
    right shape but not load-bearing for v0; revisit when the
    daily-driver hits >50 parks/day.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses        import dataclass, field
from datetime           import datetime
from pathlib            import Path
from typing             import Callable, Optional


# Default file lives under ~/org/ alongside the rest of the
# vault. Kept SEPARATE from inbox.org so review surface can scope
# without globbing. The design doc proposes a "* Sidecar parks"
# top-level heading inside inbox.org; v0 uses a dedicated file
# for simpler atomic appends + simpler tests. Path can be swapped
# back to inbox.org via SidecarConfig once the review surface
# lands.
_DEFAULT_OUTPUT = Path("~/org/sidecar-captures.org").expanduser()


# Latency budget. The capture path SHOULD complete well under
# this; the value here is the hard alarm — if we exceed it we log
# a slow-path notice and keep going (capture never fails on
# budget).
_DEFAULT_LATENCY_MS = 500


# Sentence-anchored trigger patterns. Mirror the design doc's
# "Explicit triggers" section. Used by `extract_park_text` so the
# proxy / Emacs side can pre-strip the trigger before calling
# `capture`. v0 keeps the trigger list narrow on purpose.
_TRIGGERS: tuple[str, ...] = (
    "park this:",
    "park:",
    "side:",
    "remember:",
    "note for later:",
    "for later:",
    "set this aside:",
)


# Trailing parenthetical markers (different shape — text comes
# before the marker). "we should refactor X (side)" → park-text
# is "we should refactor X".
_TRAILING_MARKERS: tuple[str, ...] = ("(park)", "(side)")


@dataclass
class SidecarConfig:
    """All knobs the capture path reads. Pass an instance to
    `capture` / `enrich_async` to override defaults; the bare
    default mirrors what the persona reaches for when no
    user-config is present.

    Fields:
      latency_budget_ms — hard alarm on capture duration. Capture
        never fails on budget; exceeding this just records a
        slow-path notice for @boothby's hygiene scan to surface.

      output_file — where parks land. Default ~/org/
        sidecar-captures.org. Switch to inbox.org once the review
        surface lands and "* Sidecar parks" sub-tree splitting
        is ergonomic.

      enrich_mode — "auto" tries delegate.fork → Agor btw → inline
        in that order. "inline" forces the cheap inline path
        (used in tests). "off" disables enrichment entirely.

      private — when True, suppresses project / active-context
        tags. Maps to the design doc's :private: load-bearing
        marker. v0 implements only the tag suppression; cross-
        device replication suppression and downstream stop-tag
        wiring is deferred.

      session_id — captain's-log session id, copied into the
        entry's :SESSION: property so end-of-session review can
        scope to "today's parks" / "this session's parks". Empty
        string when unset.

      now — clock injection seam for tests; defaults to the real
        wall clock. Exists because every assertion-on-timestamp
        test would otherwise have to monkeypatch datetime.
    """
    latency_budget_ms: int  = _DEFAULT_LATENCY_MS
    output_file:       Path = field(default_factory=lambda: _DEFAULT_OUTPUT)
    enrich_mode:       str  = "auto"   # "auto" | "inline" | "off"
    private:           bool = False
    session_id:        str  = ""
    now:               Callable[[], datetime] = field(
        default_factory=lambda: datetime.now)


# ── public API ────────────────────────────────────────────────────────

def capture(thought: str,
            *,
            context_hint: Optional[str] = None,
            config:       Optional[SidecarConfig] = None,
            ) -> str:
    """Synchronously write a park entry to the output file and
    return the entry's `:ID:`.

    Latency contract: target <500 ms wall clock. The path is
    pure-Python + a single append-only `open(path, "a")` write —
    no DB, no LLM, no MCP round-trip.

    `context_hint` is an optional caller-supplied tag/string the
    agent layer surfaced (e.g. "active project: org-llm"). Stored
    verbatim on the entry as a `:CONTEXT_HINT:` property; not
    parsed in v0. Enrichment may overwrite/augment.

    Returns the entry's UUID — the same string written into
    `:ID:`. The Emacs side uses it to find the freshly-captured
    entry for review / undo / promote.
    """
    cfg   = config or SidecarConfig()
    start = time.perf_counter()

    text  = (thought or "").strip()
    if not text:
        # Refuse empty captures — they'd just clutter the file.
        # Raise instead of returning "" so the caller has a clear
        # failure mode (the persona's own validation should catch
        # this earlier, but guard at the API boundary anyway).
        raise ValueError("@sidecar.capture: refusing empty thought")

    entry_id = str(uuid.uuid4())
    ts       = cfg.now()
    ts_iso   = ts.strftime("%Y-%m-%d %a %H:%M")
    ts_inact = ts.strftime("[%Y-%m-%d %a %H:%M]")

    # Tag set: minimum-viable set on the hot path. Project /
    # roam-link tags get attached later by enrich_async (or the
    # inline fallback) so the capture itself stays deterministic
    # and fast.
    tags: list[str] = ["sidecar", "park"]
    if cfg.private:
        tags.append("private")
    tag_str = ":" + ":".join(tags) + ":"

    # Title is a one-line preview of the thought (first ~64
    # chars, no newlines). Real prose stays in the body. This
    # keeps the heading scannable in `org-agenda` / outline view.
    preview = text.splitlines()[0][:64].strip() or "park"

    entry_lines = [
        f"* {preview}    {tag_str}",
        ":PROPERTIES:",
        f":ID:       {entry_id}",
        f":CREATED:  {ts_inact}",
    ]
    if cfg.session_id:
        entry_lines.append(f":SESSION:  {cfg.session_id}")
    if context_hint and not cfg.private:
        # :private: parks suppress the context hint entirely per
        # the design doc's privacy section.
        entry_lines.append(f":CONTEXT_HINT: {context_hint}")
    entry_lines += [
        ":STATUS:   parked",
        ":END:",
        "",
        text,
        "",
    ]
    entry = "\n".join(entry_lines) + "\n"

    out = cfg.output_file.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Append-only — never rewrite the file. Other agents may be
    # reading it concurrently (the design doc lists @journalist /
    # @boothby / @riker as downstream readers).
    with open(out, "a", encoding="utf-8") as fh:
        if out.stat().st_size == 0:
            fh.write("#+TITLE: Sidecar parks\n")
            fh.write("#+FILETAGS: :sidecar:\n\n")
        fh.write(entry)

    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms > cfg.latency_budget_ms:
        # Log to stderr so the slow-path notice is visible in
        # captain's-log if the user runs with stderr captured;
        # don't raise — capture must succeed regardless.
        try:
            import sys
            print(
                f"[sidecar] capture exceeded latency budget: "
                f"{elapsed_ms:.0f}ms > {cfg.latency_budget_ms}ms "
                f"(id={entry_id})",
                file=sys.stderr,
            )
        except Exception:
            pass

    return entry_id


def enrich_async(entry_id: str,
                 *,
                 config: Optional[SidecarConfig] = None,
                 ) -> threading.Thread:
    """Fire off background enrichment for `entry_id`.

    Tries in order:
      1. `org_llm.delegate.fork`  (filed but unbuilt — falls back)
      2. Agor `agor_sessions_prompt(mode="btw")` if reachable
      3. Tiny inline tagger that re-reads the entry, infers a
         couple tags from the body text, and rewrites the
         heading's tag-string in-place.

    Returns the spawned Thread so callers (and tests) can join /
    inspect. Daemon=True so a forgotten enrichment doesn't keep
    the process alive.

    NOTE v0: no queue / no backpressure. Spawning N threads for N
    parks is fine for the daily-driver scale today. Bounded queue
    is filed under the module-level TODO list.
    """
    cfg = config or SidecarConfig()

    def _runner() -> None:
        if cfg.enrich_mode == "off":
            return
        if cfg.enrich_mode == "auto":
            if _try_delegate_fork(entry_id, cfg):
                return
            if _try_agor_btw(entry_id, cfg):
                return
        # "inline" or all higher tiers unreachable.
        try:
            _inline_enrich(entry_id, cfg)
        except Exception:
            # Enrichment is best-effort — never crash the
            # background thread because the file shape is
            # off-spec or already mutated by another agent.
            pass

    t = threading.Thread(
        target=_runner, name=f"sidecar-enrich-{entry_id[:8]}",
        daemon=True)
    t.start()
    return t


def extract_park_text(message: str) -> Optional[str]:
    """Return the park-content of `message` if it matches a
    sidecar trigger, else None. Anchored to start-of-line per
    the design doc's anti-trigger heuristics — mid-sentence
    'park:' won't fire.

    Used by the proxy / Emacs trigger layer to pre-strip before
    calling `capture`. Kept here (not in cli.py) so the
    @sidecar persona can be tested + reused without dragging in
    the proxy.
    """
    if not message:
        return None
    head = message.lstrip()
    head_lower = head.lower()
    for trig in _TRIGGERS:
        if head_lower.startswith(trig):
            return head[len(trig):].strip() or None
    # Trailing-marker shape: "actual text (park)" / "... (side)"
    stripped = head.rstrip()
    for marker in _TRAILING_MARKERS:
        if stripped.lower().endswith(marker):
            body = stripped[: -len(marker)].rstrip(" \t-—:")
            return body.strip() or None
    return None


# ── private helpers ───────────────────────────────────────────────────

def _try_delegate_fork(entry_id: str, cfg: SidecarConfig) -> bool:
    """Try the delegate.fork pathway. Returns True on success.

    delegate.fork is filed-but-unbuilt today (per docs/wiki/
    side-quests.org § delegate.fork). The hook stays here so
    that when it lands, sidecar enrichment is one-line wired —
    no spec churn.
    """
    try:
        from . import delegate  # type: ignore[attr-defined]
        fork = getattr(delegate, "fork", None)
        if fork is None:
            return False
        fork(
            target="@scribe",
            prompt=f"enrich sidecar park {entry_id}: tag + roam-link",
            context={"entry_id": entry_id, "kind": "sidecar.enrich"},
        )
        return True
    except Exception:
        return False


def _try_agor_btw(entry_id: str, cfg: SidecarConfig) -> bool:
    """Try the Agor `agor_sessions_prompt(mode="btw")` pathway.
    Returns True on success.

    Probes for a reachable Agor daemon via env var
    AGOR_DAEMON_URL; bails out fast if unset (the common case on
    the daily-driver). When set, expects a small in-process
    client; we don't ship one in v0 — this is the seam, not the
    impl.
    """
    if not os.environ.get("AGOR_DAEMON_URL"):
        return False
    try:
        # Importing pi_extension.agor_client (or whatever lands
        # for the Agor seam) is an open question — keep the
        # attempt symbolic in v0. Real wiring goes through here
        # once Agor btw is a stable production primitive.
        from .pi_extension import agor_client  # type: ignore[attr-defined]
        agor_client.sessions_prompt(
            mode="btw",
            target="@scribe",
            prompt=f"enrich sidecar park {entry_id}",
            metadata={"entry_id": entry_id, "kind": "sidecar.enrich"},
        )
        return True
    except Exception:
        return False


def _inline_enrich(entry_id: str, cfg: SidecarConfig) -> None:
    """Tiny deterministic enrichment fallback. Re-reads the
    output file, finds the entry by `:ID:`, scans the body for
    obvious project / topic tokens, and appends inferred tags
    to the heading's tag-string.

    Kept narrow on purpose — anything fancier than substring
    matching belongs in delegate.fork / Agor btw, not in the
    fallback.
    """
    out = cfg.output_file.expanduser()
    if not out.exists():
        return
    raw = out.read_text(encoding="utf-8")
    if entry_id not in raw:
        return

    # Split into a heading list. The append-only writer means the
    # entry is always a contiguous block starting with "* ".
    lines = raw.splitlines(keepends=True)
    # Locate the heading whose PROPERTIES drawer has our :ID:.
    target_heading_idx: Optional[int] = None
    for idx, line in enumerate(lines):
        if line.startswith("* "):
            # Look ahead for :ID:
            for ahead in lines[idx: idx + 12]:
                if entry_id in ahead:
                    target_heading_idx = idx
                    break
            if target_heading_idx == idx:
                break
    if target_heading_idx is None:
        return

    # Body window: from heading until next "* " heading or EOF.
    body_window = []
    for line in lines[target_heading_idx + 1:]:
        if line.startswith("* "):
            break
        body_window.append(line)
    body = "".join(body_window).lower()

    # Inline tag inference — narrow vocab on purpose.
    inferred: list[str] = []
    if cfg.private:
        # :private: stops downstream context-tagging. Match the
        # design doc's load-bearing privacy contract.
        return
    for token, tag in (
        ("org-llm",  "org-llm"),
        ("emacs",    "emacs"),
        ("doom",     "doom"),
        ("paper",    "reading"),
        ("read ",    "reading"),
        ("call ",    "people"),
        ("mom ",     "people"),
        ("idea ",    "idea"),
        ("dev",      "dev"),
        ("phase ",   "phase"),
    ):
        if token in body and tag not in inferred:
            inferred.append(tag)
    if not inferred:
        return

    # Splice the new tags into the heading's tag-string.
    head_line   = lines[target_heading_idx]
    new_head    = _splice_tags(head_line, inferred)
    if new_head == head_line:
        return
    lines[target_heading_idx] = new_head
    out.write_text("".join(lines), encoding="utf-8")


_TAG_RE = re.compile(r"\s+:([\w@:_-]+):\s*$")


def _splice_tags(heading_line: str, new_tags: list[str]) -> str:
    """Insert `new_tags` into the trailing :tag1:tag2: of
    `heading_line` (preserving existing tags + ordering). When
    the heading has no tag-string, append a fresh one.
    """
    line   = heading_line.rstrip("\n")
    m      = _TAG_RE.search(line)
    if m:
        existing = [t for t in m.group(1).split(":") if t]
        merged   = list(existing)
        for t in new_tags:
            if t not in merged:
                merged.append(t)
        new_tagstr = ":" + ":".join(merged) + ":"
        line = line[: m.start()] + " " + new_tagstr
    else:
        new_tagstr = ":" + ":".join(new_tags) + ":"
        line = line.rstrip() + "    " + new_tagstr
    return line + "\n"


# ── persona prompt (consumed by _builtins.py) ─────────────────────────
#
# Lives here (not in cli.py) so the @sidecar persona is a
# self-contained module. The Bridge Crew personas still live in
# cli.py; per the v0 spec we don't move them.

SIDECAR_PERSONA: str = (
    "You are sidecar — the user's silent stenographer for "
    "side-thoughts during deep work. Two modes; switch on "
    "session phase.\n"
    "\n"
    "MODE A — CAPTURE (default; user is busy elsewhere):\n"
    "  - Triggers: 'park this:', 'park:', 'side:', 'remember:', "
    "'note for later:', '(side)' / '(park)' trailing. Text "
    "after the trigger IS the park content.\n"
    "  - Capture verbatim. Do NOT confirm, echo, or ask "
    "follow-ups. Silent capture, silent success.\n"
    "  - Acknowledgment is ONE token only: 'parked'.\n"
    "  - Target: ~/org/sidecar-captures.org. Date stamp; tag "
    ":sidecar:park: plus inferred tags from the active "
    "project (best-effort, async).\n"
    "  - If park-text >~3 lines or looks like a draft, ONE "
    "polite redirect: 'this looks drafty — want @scribe?' "
    "then drop.\n"
    "  - Capture must complete <500ms. If active-project "
    "lookup won't fit, capture WITHOUT context; next "
    "enrichment pass fills it in.\n"
    "\n"
    "MODE B — REVIEW (triggered by 'review my parks' / "
    "'show parks' / 'what did I park'):\n"
    "  - Read parks captured in current session or the "
    "requested window (default: today).\n"
    "  - Surface bulleted, grouped by tag if >5 items. Date, "
    "one-line preview, inferred project link.\n"
    "  - Per item: PROMOTE (delegate to @scribe), LINK (insert "
    "into existing node), DISCARD (mark "
    ":reviewed:discarded:), DEFER (no-op default).\n"
    "  - Never moralise about quantity. Surface, don't "
    "editorialise.\n"
    "\n"
    "You are NOT a drafter — route prose work to @scribe.\n"
    "You are NOT a tracker — @riker reads parks; @sidecar "
    "doesn't write dev-tracker.org."
)


SIDECAR_DESCRIPTION: str = (
    "@sidecar — silent side-thought capture during deep work; "
    "reviews parked items at session close."
)


SIDECAR_TRIGGERS: tuple[str, ...] = (
    "park this:",
    "park:",
    "side:",
    "remember:",
    "note for later:",
    "for later:",
    "set this aside",
    "(park)",
    "(side)",
    "review my parks",
    "show parks",
    "what did i park",
)
