"""Canonical bridge `Event` — the *one* record that lives in BOTH
sinks (captain's log narrative + Agor session genealogy graph).

Per the design page (=docs/wiki/captains-log-agor-bridge.org=), the
join key between the two systems is `event_id`, a UUIDv7. UUIDv7 is
chosen instead of UUIDv4 because:

  - it sorts naturally by creation time (so a SQL `ORDER BY event_id`
    on the Agor side gives the same chronology as the org log)
  - it embeds an ms-precision timestamp inline, which doubles as a
    cheap tie-breaker when two events land in the same wall-clock
    second
  - the Python stdlib gained `uuid.uuid7()` in 3.14, so we keep a
    deterministic local fallback for 3.11–3.13 (this codebase pins
    `requires-python = ">=3.11"`).

`session_id` is the *secondary* join — for queries scoped to "what
happened inside one Agor session?" it is sufficient on its own; for
cross-session questions the `event_id` is canonical.

The `Event` dataclass is intentionally tiny: it's the seam, not the
storage. The captain's log keeps its rich org-mode body; the Agor
sink keeps a pointer list (`captain_log_event_ids` in
`session.custom_context`); both reference back through `event_id`.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── UUIDv7 minting ──────────────────────────────────────────────


def _uuid7_bytes() -> bytes:
    """Mint a 16-byte UUIDv7 per RFC 9562 §5.7.

    Layout (big-endian):
      bytes 0..5     unix ms timestamp (48 bits)
      byte  6, 7     version (4 bits = 0x7) + 12 bits random
      byte  8, 9     variant (2 bits = 0b10) + 14 bits random
      bytes 10..15   48 bits random

    Pure-stdlib so the bridge has no extra dependency. Tested against
    the Python 3.14 `uuid.uuid7()` shape; downstream callers should
    treat the return as opaque.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = secrets.token_bytes(10)
    out = bytearray(16)
    out[0] = (ms >> 40) & 0xFF
    out[1] = (ms >> 32) & 0xFF
    out[2] = (ms >> 24) & 0xFF
    out[3] = (ms >> 16) & 0xFF
    out[4] = (ms >> 8) & 0xFF
    out[5] = ms & 0xFF
    # version 7 in high nibble of byte 6
    out[6] = 0x70 | (rand[0] & 0x0F)
    out[7] = rand[1]
    # variant 0b10 in top 2 bits of byte 8
    out[8] = 0x80 | (rand[2] & 0x3F)
    out[9] = rand[3]
    out[10:16] = rand[4:10]
    return bytes(out)


def _format_uuid(b: bytes) -> str:
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def new_event_id() -> str:
    """Mint a new UUIDv7 string for use as the bridge primary key.

    Prefer `uuid.uuid7()` if available (Python 3.14+), else fall back
    to the local big-endian RFC 9562 minter above.
    """
    try:
        import uuid as _uuid  # noqa: WPS433 — local import keeps imports cheap
        if hasattr(_uuid, "uuid7"):
            return str(_uuid.uuid7())  # type: ignore[attr-defined]
    except Exception:
        pass
    return _format_uuid(_uuid7_bytes())


# ── public dataclass ─────────────────────────────────────────────


@dataclass
class Event:
    """One bridge event. Same record lands in BOTH sinks.

    Required:
      `event_id`      UUIDv7 — the *shared* primary key.
      `session_id`    Agor session this event happened inside.
      `kind`          Logical event kind (matches captain's log
                      `kind` column — e.g. "llm", "mcp", "crew").
    Optional:
      `worktree_id`   Agor worktree at the time of the event. We
                      record it on the event because the worktree
                      can be re-pointed mid-session (rare but legal).
      `commit_sha`    `git_state.current_sha` snapshot for the join
                      against Agor's `git_state` column.
      `payload`       Arbitrary structured dict — agent name, tool
                      name, prompt summary, outcome, etc. Captain's
                      log writers flatten this into kwargs; Agor sink
                      stores the event_id pointer and reads payload
                      back via captain's-log SQL when needed.
      `ts`            Wall-clock timestamp (ISO 8601, UTC). Defaults
                      to `now()`. Embedded in event_id too, but kept
                      separately for human-readable queries that
                      don't want to decode UUIDv7.
    """

    event_id:    str
    session_id:  str
    kind:        str
    worktree_id: str = ""
    commit_sha:  str = ""
    payload:     dict[str, Any] = field(default_factory=dict)
    ts:          str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form. Bridge tee writes this through to both
        sinks; tests round-trip through `from_dict`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        """Permissive: unknown keys are ignored, missing keys default."""
        kwargs = {k: d.get(k, "") for k in ("event_id", "session_id",
                                              "kind", "worktree_id",
                                              "commit_sha", "ts")}
        kwargs["payload"] = d.get("payload", {}) or {}
        return cls(**kwargs)


__all__ = [
    "Event",
    "new_event_id",
]
