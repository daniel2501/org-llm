"""Captain's-log + Agor session-genealogy bridge — v0 module.

Design page: =docs/wiki/captains-log-agor-bridge.org= (committed
2026-05-06 in d18c501). Recommended option C "tee at proxy" with a
shared UUIDv7 `event_id` between sinks.

Surface:
  - `record_session_event(session_id, event_kind, **payload)` —
    high-level "record one thing that happened in this Agor session"
    convenience. Returns the new `event_id`. Both sinks attempted.
  - `query_session_events(session_id)` — list of `Event` rows the
    bridge attached to this Agor session, joined back to the
    captain's-log History table for body+outcome enrichment.
  - `query_genealogy(parent_session_id)` — small `SessionTree` dict
    (parent + direct children + their attached event_ids).
  - `tee(...)` — the lower-level primitive (see `tee.py`).
  - `Event`, `new_event_id` — the canonical record + id minter.
  - `AgorClient`, `AgorError` — HTTP client (override + inspect).

Critical scope rule (per the implementation brief): this package
does NOT touch =org_llm/llm_proxy.py= (concurrent agent on
DEC-015 v0.2 SSE streaming) and does NOT modify
=org_llm/logbook.py= (Phase 24.3 confusion-detector wiring just
shipped there). Both are only used through their public APIs.

Proxy-interceptor wiring — calling `tee()` on every persona event
out of the LLM proxy seam — is the v0.1 follow-up sprint, NOT this
module's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .agor_client import AgorClient, AgorError
from .event       import Event, new_event_id
from .tee         import (
    deadletter_snapshot,
    get_default_client,
    set_default_client,
    tee,
)


# ── public dataclasses for the read-side API ───────────────────


@dataclass
class SessionTree:
    """Result of `query_genealogy(parent_session_id)`.

    Mirrors the shape returned by `AgorClient.query_genealogy` but
    with the captain's-log event-id list spliced in for direct use
    by audit / review agents (e.g. @riker)."""
    session_id:           str
    parent_session_id:    Optional[str]
    children_session_ids: list[str]
    event_ids:            list[str]
    children:             list["SessionTree"] = field(default_factory=list)


# ── public verb 1 — record ─────────────────────────────────────


def record_session_event(session_id: str,
                          event_kind: str,
                          **payload: Any,
                          ) -> str:
    """Record one event for an Agor session via the bridge tee.

    This is the simple "I just did a thing inside this session,
    write it everywhere" entry point. The caller need not know that
    two sinks exist — that's the bridge's job.

    Conventional `payload` keys (all optional, all stored verbatim):
      `agent`         persona name (default "")
      `action`        verb describing the event (default = event_kind)
      `prompt`        upstream input / narrative
      `outcome`       "ok" | "error" | "fallback" | …
      `worktree_id`   override (else backfilled from Agor session)
      `commit_sha`    override (else backfilled from Agor session)
      `client`        override AgorClient (test seam)

    Returns the new UUIDv7 `event_id` so callers can store it for
    later joining (e.g. in agent-internal state, a task ledger,
    etc.)."""
    agent        = str(payload.pop("agent",   "") or "")
    action       = str(payload.pop("action",  event_kind) or event_kind)
    prompt       = str(payload.pop("prompt",  "") or "")
    outcome      = str(payload.pop("outcome", "ok") or "ok")
    worktree_id  = str(payload.pop("worktree_id", "") or "")
    commit_sha   = str(payload.pop("commit_sha",  "") or "")
    client       = payload.pop("client", None)

    ev = tee(
        action          = action,
        agent           = agent,
        prompt          = prompt,
        outcome         = outcome,
        agor_session_id = session_id,
        kind            = event_kind,
        worktree_id     = worktree_id,
        commit_sha      = commit_sha,
        payload         = dict(payload),
        client          = client,
    )
    return ev.event_id


# ── public verb 2 — query session events ────────────────────────


def _query_history_rows_by_session(session_id: str,
                                     ) -> list[dict[str, Any]]:
    """Look up captain's-log History rows whose JSON `args` blob
    contains the given Agor `session_id`. Best-effort: returns []
    if the DB is unreachable.

    Strategy: the bridge stores `session_id` inside the `args` JSON
    blob (see tee._write_to_captains_log). We use a SQL LIKE on the
    serialised blob. Cheap because History is small + per-kind
    rotated. A future indexed shadow table (=captains_log_index= in
    the design page) is the v0.1 optimisation."""
    rows: list[dict[str, Any]] = []
    try:
        from org_llm.db import DB_PATH, History, make_engine
        from sqlalchemy.orm import Session as SaSession
        import os as _os
        path = _os.environ.get("ORG_LLM_DB") or str(DB_PATH)
        from pathlib import Path as _Path
        if not _Path(path).exists():
            return rows
        engine = make_engine(_Path(path))
        needle = f'"session_id": "{session_id}"'
        with SaSession(engine) as s:
            q = (s.query(History)
                  .filter(History.args.like(f"%{needle}%"))
                  .order_by(History.id.asc()))
            for r in q.all():
                import json as _json
                args_blob: dict[str, Any] = {}
                try:
                    args_blob = _json.loads(r.args or "{}")
                except Exception:
                    pass
                rows.append({
                    "event_id":    str(args_blob.get("event_id", "") or ""),
                    "session_id":  str(args_blob.get("session_id", "") or ""),
                    "worktree_id": str(args_blob.get("worktree_id", "") or ""),
                    "commit_sha":  str(args_blob.get("commit_sha", "") or ""),
                    "kind":        r.kind or "",
                    "command":     r.command or "",
                    "outcome":     r.outcome or "",
                    "response":    r.response or "",
                    "ts":          r.timestamp or "",
                    "payload":     args_blob,
                })
    except Exception:
        pass
    return rows


def query_session_events(session_id: str) -> list[Event]:
    """Return the bridge `Event`s attached to one Agor session,
    sorted by event_id (UUIDv7 sorts chronologically).

    Joins both sources:
      - captain's-log SQLite History rows whose `args` JSON contains
        the session_id (the bridge tee guarantees this);
      - Agor session's `custom_context.captain_log_event_ids` for
        events that wrote to Agor but never reached the captain's
        log (rare but possible in degraded states).

    Best-effort: returns [] on hard errors."""
    rows = _query_history_rows_by_session(session_id)
    by_id: dict[str, Event] = {}
    for r in rows:
        eid = r.get("event_id") or ""
        if not eid:
            continue
        by_id[eid] = Event(
            event_id    = eid,
            session_id  = r.get("session_id") or session_id,
            kind        = r.get("kind") or "",
            worktree_id = r.get("worktree_id") or "",
            commit_sha  = r.get("commit_sha")  or "",
            payload     = r.get("payload")     or {},
            ts          = r.get("ts") or "",
        )
    # Cross-check against Agor's pointer list. Anything Agor knows
    # about that History doesn't is added as a stub Event so the
    # caller sees the full set.
    try:
        client = get_default_client()
        sess, _err = client.get_session(session_id)
        if sess:
            cc = sess.get("custom_context") or {}
            ids = list(cc.get("captain_log_event_ids") or [])
            for eid in ids:
                if eid and eid not in by_id:
                    by_id[eid] = Event(
                        event_id    = eid,
                        session_id  = session_id,
                        kind        = "agor-only",
                        worktree_id = str(sess.get("worktree_id") or ""),
                        commit_sha  = str((sess.get("git_state") or {})
                                            .get("current_sha") or ""),
                        payload     = {"_source": "agor_custom_context"},
                    )
    except Exception:
        pass
    return sorted(by_id.values(), key=lambda e: e.event_id)


# ── public verb 3 — query genealogy ────────────────────────────


def query_genealogy(parent_session_id: str,
                     *,
                     client: Optional[AgorClient] = None,
                     ) -> Optional[SessionTree]:
    """Return a `SessionTree` rooted at `parent_session_id`.

    One level of children (matching Agor's
    `genealogy.children: SessionID[]` field — see
    =dist/core/session-CAfhv1qL.d.ts=). Recursive descent is left
    to callers that need it; we return the per-node `event_ids` so
    a recursive walker only has to call `query_genealogy` on each
    child id.
    """
    cli = client or get_default_client()
    tree, err = cli.query_genealogy(parent_session_id)
    if tree is None:
        return None

    parent_blob = None
    parent_blob, _ = cli.get_session(parent_session_id)
    parent_blob = parent_blob or {}
    parent_cc   = parent_blob.get("custom_context") or {}
    parent_eids = list(parent_cc.get("captain_log_event_ids") or [])
    parent_gen  = parent_blob.get("genealogy") or {}

    children_trees: list[SessionTree] = []
    for child in tree.get("children", []) or []:
        cc = child.get("custom_context") or {}
        children_trees.append(SessionTree(
            session_id           = str(child.get("session_id") or ""),
            parent_session_id    = parent_session_id,
            children_session_ids = list((child.get("genealogy") or {})
                                          .get("children") or []),
            event_ids            = list(cc.get("captain_log_event_ids") or []),
        ))

    return SessionTree(
        session_id           = parent_session_id,
        parent_session_id    = (parent_gen.get("parent_session_id")
                                  or None),
        children_session_ids = list(parent_gen.get("children") or []),
        event_ids            = parent_eids,
        children             = children_trees,
    )


__all__ = [
    "record_session_event",
    "query_session_events",
    "query_genealogy",
    "tee",
    "Event",
    "SessionTree",
    "AgorClient",
    "AgorError",
    "new_event_id",
    "deadletter_snapshot",
    "get_default_client",
    "set_default_client",
]
