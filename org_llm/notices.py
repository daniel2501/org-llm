"""See-something-say-something — bottom-up observation log.

Specialists call `notice(...)` to record one-off signals about their
own behaviour or what the user is doing: "asked the same thing four
times this week", "had to retry the LLM 4x to finish one task",
"the user keeps re-running this exact CLI invocation", etc. Nothing
reads the table yet — we're collecting ~4 weeks of evidence in
production before designing the threshold/dispatch layer that turns
salient observations into promotions, recipes, or tool synthesis.

Audit-only sidecar: a write failure NEVER raises into the caller.
The calling agent is doing real work; logging an observation isn't
allowed to break that. Mirrors the swallow-on-failure contract that
`logbook.write_event()` holds for the event log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def notice(
    intent_class: str,
    observation_kind: str,
    payload: dict | None = None,
    source_agent: str = "",
    session_id: str = "",
    dedup_key: str = "",
) -> None:
    """Record one bottom-up observation. Best-effort and safe-to-fail —
    if the DB is locked or migration hasn't run, swallow the error. This
    is an audit-only sidecar; it must never break the calling agent.
    """
    try:
        from .db import DB_PATH, Notice, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return
        engine = make_engine(path)
        ts = datetime.now(timezone.utc).isoformat()
        with Session(engine) as s:
            s.add(Notice(
                timestamp=ts,
                source_agent=source_agent or "",
                intent_class=intent_class,
                observation_kind=observation_kind,
                payload=json.dumps(payload or {}),
                session_id=session_id or "",
                dedup_key=dedup_key or "",
            ))
            s.commit()
    except Exception as e:
        try:
            print(f"[notices] swallowed: {type(e).__name__}: {e}",
                  file=sys.stderr)
        except Exception:
            pass
