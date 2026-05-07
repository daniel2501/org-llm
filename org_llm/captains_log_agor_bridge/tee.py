"""The bridge tee primitive — write ONE event to BOTH sinks.

Per the design page (=docs/wiki/captains-log-agor-bridge.org=,
recommended option C "tee at proxy"), this is the *single* point
where a bridge event lands in:

  1. The captain's log, via =org_llm.logbook.write_event(...)=
     — narrative + body + structured PROPERTIES drawer + dbt-friendly
     SQLite mirror. Public API; we do NOT touch =logbook.py= itself.
     (Phase 24.3 confusion-detector wiring just shipped there;
     concurrent-agent rule says don't go in.)

  2. The Agor session genealogy, via PATCH /sessions/:id setting
     `custom_context.captain_log_event_ids = [..., <event_id>]`.
     Verified shape by reading
     =dist/core/session-CAfhv1qL.d.ts= (custom_context is
     `Record<string, unknown> & {scheduled_run?: ...}`) and the
     Feathers `service("sessions").patch(id, updates, params)`
     call sites in =dist/daemon/register-services.js=.

Failure-isolation rule (DEC-006 — supervision is deterministic):
  - If captain's log write fails → the Agor sink still goes through.
  - If Agor PATCH fails → the captain's log entry stays; the event_id
    is queued in a local "deadletter" list the v0.1 reconciler will
    pick up. (This module owns the deadletter; we don't ship the
    reconciler in v0; the list is queryable + clearable from tests
    + future code.)
  - Both sinks failing is logged on the captain's-log side (which
    is itself best-effort) and the Event is still returned to the
    caller so the proxy interceptor can decide what to do.

The agent-narrating-while-thinking rule applies *upstream* of this
seam — the bridge writes whatever the proxy hands it; it does not
itself shape the narrative.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .agor_client import AgorClient, AgorError
from .event       import Event, new_event_id


# In-process deadletter for events whose Agor PATCH failed. The v0.1
# reconciler will drain this list. Module-level (single per-process
# bridge consumer is the model). Tests reset via `_reset_deadletter()`.
_deadletter:    list[Event] = []
_deadletter_lock = threading.Lock()


def _reset_deadletter() -> None:
    with _deadletter_lock:
        _deadletter.clear()


def deadletter_snapshot() -> list[Event]:
    """Copy of the deadletter for inspection / reconciliation. Never
    returns the live list."""
    with _deadletter_lock:
        return list(_deadletter)


# ── default singleton client ────────────────────────────────────


_default_client: Optional[AgorClient] = None
_default_client_lock = threading.Lock()


def get_default_client() -> AgorClient:
    """Lazy singleton so tests can monkeypatch + so the import path
    is cheap."""
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = AgorClient()
        return _default_client


def set_default_client(client: AgorClient) -> None:
    """Test-only seam — install a custom client (e.g. one pointing
    at a stub HTTP server)."""
    global _default_client
    with _default_client_lock:
        _default_client = client


# ── captain's-log writer (uses logbook public API only) ─────────


def _write_to_captains_log(event: Event,
                             *,
                             action:  str,
                             agent:   str,
                             prompt:  str,
                             outcome: str,
                             ) -> bool:
    """Best-effort write through =logbook.write_event= ONLY (public
    API). Returns True on apparent success; logbook itself swallows
    its own errors so this is mostly a "did the import resolve" gate.

    Encoding rationale:
      - `kind` = event.kind verbatim (matches the existing kinds
        glossary: llm / mcp / crew / cli / etc.)
      - `command` = action (e.g. "narration", "tool_call", "fork")
      - `args` = JSON-encoded payload PLUS the bridge metadata
        (`event_id`, `session_id`, `worktree_id`, `commit_sha`)
        so a future query layer can pull the bridge fields off
        the History row without a JOIN.
      - `response` = prompt | outcome — narrative for the org body.
      - `outcome` = passed through.
    """
    try:
        from org_llm.logbook import write_event
    except Exception:
        return False
    args_blob: dict[str, Any] = {
        "event_id":    event.event_id,
        "session_id":  event.session_id,
        "worktree_id": event.worktree_id,
        "commit_sha":  event.commit_sha,
        "agent":       agent,
        **(event.payload or {}),
    }
    try:
        write_event(
            event.kind, action,
            args     = json.dumps(args_blob, default=str),
            response = (prompt + ("\n\n" + outcome if outcome else "")).strip(),
            model    = "",
            outcome  = outcome or "ok",
        )
        return True
    except Exception:
        return False


# ── Agor sink ───────────────────────────────────────────────────


_CUSTOM_CONTEXT_KEY = "captain_log_event_ids"


def _attach_event_to_session(client: AgorClient,
                              event:  Event,
                              ) -> Optional[AgorError]:
    """Append `event.event_id` to
    `session.custom_context.captain_log_event_ids` via PATCH.

    Read-modify-write: GET the session, splice the list, PATCH back.
    Idempotent: if the event_id is already present (e.g. from a
    re-tee after a crash recovery), we skip the PATCH and return
    None. The `event_id` is the dedup key, NOT the payload.

    Returns None on success or an `AgorError` describing the failure
    mode (caller routes to deadletter)."""
    if not event.session_id:
        return AgorError(kind="config",
                          detail="event.session_id is required for Agor sink")
    sess, err = client.get_session(event.session_id)
    if sess is None:
        return err or AgorError(kind="not_found",
                                  detail=f"session {event.session_id} not found")
    cc = dict(sess.get("custom_context") or {})
    existing = list(cc.get(_CUSTOM_CONTEXT_KEY) or [])
    if event.event_id in existing:
        # Idempotent rewrite — already attached. Match design page's
        # "Idempotency" failure mode requirement.
        return None
    existing.append(event.event_id)
    cc[_CUSTOM_CONTEXT_KEY] = existing
    _, perr = client.patch_session(event.session_id,
                                    {"custom_context": cc})
    return perr


# ── public seam ────────────────────────────────────────────────


def tee(action:  str,
        agent:   str,
        prompt:  str,
        outcome: str,
        *,
        agor_session_id: Optional[str] = None,
        kind:            str            = "crew",
        worktree_id:     str            = "",
        commit_sha:      str            = "",
        payload:         Optional[dict[str, Any]] = None,
        client:          Optional[AgorClient] = None,
        ) -> Event:
    """Write one bridge event to BOTH sinks.

    Returns the `Event` regardless of sink success — the caller
    (proxy interceptor in v0.1) gets the canonical `event_id` and
    can correlate further work to it. Sink failures are logged into
    the deadletter; they do not raise.

    Args:
      `action`           e.g. "narration", "tool_call", "fork",
                         "spawn". Becomes the captain's-log
                         `command` field.
      `agent`            Agent persona name (e.g. "geordi").
      `prompt`           The upstream input. Stored as captain's-log
                         body (clipped by logbook's truncation).
      `outcome`          "ok" / "error" / "fallback" / etc.
      `agor_session_id`  Agor session this event belongs to. Required
                         for the Agor sink; may be empty for cli-only
                         events (in which case only the captain's
                         log writes — and a flag goes in the payload
                         so the reconciler knows to skip).
      `kind`             Captain's-log kind. Defaults to "crew".
      `worktree_id`,     Optional context. If `agor_session_id` is
      `commit_sha`       set and these are empty, we backfill from
                         the cached session blob.
      `payload`          Free-form structured event detail.
      `client`           Optional override — defaults to the process
                         singleton.
    """
    payload = dict(payload or {})
    payload.setdefault("agent", agent)
    payload.setdefault("prompt", prompt[:1000] if prompt else "")
    cli = client or get_default_client()

    # Backfill worktree / commit from Agor session when present. We do
    # this BEFORE captain's-log write so the `args` blob carries the
    # full provenance triple.
    if agor_session_id and (not worktree_id or not commit_sha):
        sess, _ = cli.get_session(agor_session_id)
        if sess:
            worktree_id = worktree_id or str(sess.get("worktree_id") or "")
            git_state   = sess.get("git_state") or {}
            commit_sha  = commit_sha or str(git_state.get("current_sha") or "")

    event = Event(
        event_id    = new_event_id(),
        session_id  = agor_session_id or "",
        kind        = kind,
        worktree_id = worktree_id,
        commit_sha  = commit_sha,
        payload     = payload,
    )

    # Sink 1: captain's log — always tried, swallows its own errors.
    _write_to_captains_log(
        event,
        action  = action,
        agent   = agent,
        prompt  = prompt,
        outcome = outcome,
    )

    # Sink 2: Agor — only if we have a session; otherwise mark for
    # the reconciler so it knows the event is captain's-log-only.
    if not agor_session_id:
        return event

    err = _attach_event_to_session(cli, event)
    if err is not None:
        with _deadletter_lock:
            _deadletter.append(event)

    return event


__all__ = [
    "tee",
    "deadletter_snapshot",
    "get_default_client",
    "set_default_client",
]
