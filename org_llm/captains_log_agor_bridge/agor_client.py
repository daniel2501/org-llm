"""Tiny HTTP client for the Agor daemon.

Mirrors the patterns in =scripts/agor-mcp-refresh.sh=:

  - admin JWT lives in `~/.agor/cli-token` as `{"accessToken": ...,
    "expiresAt": <ms-since-epoch>}`
  - REST base URL comes from `$AGOR_BASE_URL` (default
    `http://localhost:3030`)
  - `GET /sessions/:id`  reads a Session blob (per
    =dist/core/session-CAfhv1qL.d.ts=)
  - `PATCH /sessions/:id` with a partial-Session JSON body updates
    fields. The bridge uses this to attach
    `custom_context.captain_log_event_ids` — `custom_context` is a
    `Record<string, unknown>` field on `Session`, designed for
    user-defined JSON. Verified by reading
    =dist/daemon/register-services.js= where the daemon's gateway
    code already writes `custom_context: {gateway_source: ...}`.

Stdlib `urllib.request` only — keeping the bridge dependency-free
matches the rest of org-llm's seam packages (`confusion/`).

Sessions are cached for `_SESSION_TTL_SECONDS` (60s) so a batch of
events for the same session in one turn doesn't fan out to the
daemon. Cache is purely local-process; the SSE-streaming proxy
agent will own its own cache when it integrates this v0.1.

Best-effort by contract: every public method returns either a
result or `None` / a structured `AgorError` — no exception ever
escapes into the caller. Failure to reach Agor degrades the bridge
gracefully (captain's log keeps writing; reconciler can backfill).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_DEFAULT_BASE_URL    = "http://localhost:3030"
_DEFAULT_TOKEN_PATH  = Path("~/.agor/cli-token").expanduser()
_SESSION_TTL_SECONDS = 60.0
_HTTP_TIMEOUT_SECS   = 10.0


# ── public types ────────────────────────────────────────────────


@dataclass
class AgorError:
    """Structured failure mode. Callers branch on `kind` (string) for
    light human-readability and skip parsing HTTP status codes."""
    kind:    str   # "auth" | "not_found" | "network" | "bad_response" | "config"
    detail:  str = ""
    status:  Optional[int] = None


@dataclass
class _CachedSession:
    fetched_at: float
    body:       dict[str, Any]


# ── client ──────────────────────────────────────────────────────


class AgorClient:
    """Per-process Agor daemon client.

    All methods return either the unmarshalled response dict or an
    `AgorError`. No raise paths.
    """

    def __init__(
        self,
        *,
        base_url:   Optional[str] = None,
        token_path: Optional[Path] = None,
        ttl:        float = _SESSION_TTL_SECONDS,
    ) -> None:
        self.base_url   = base_url or os.environ.get(
            "AGOR_BASE_URL", _DEFAULT_BASE_URL,
        )
        self.token_path = Path(token_path) if token_path else _DEFAULT_TOKEN_PATH
        self.ttl        = ttl
        self._cache:   dict[str, _CachedSession] = {}
        self._lock     = threading.Lock()

    # ── auth ────────────────────────────────────────────────────

    def _read_token(self) -> Optional[str]:
        """Read the admin JWT or return None on any failure. We do
        NOT verify the signature; the server is the trust boundary
        — we just need a non-empty bearer."""
        try:
            raw = self.token_path.read_text()
            data = json.loads(raw)
            tok = data.get("accessToken") or ""
            return tok or None
        except Exception:
            return None

    # ── http ────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 *, body: Optional[dict] = None,
                 ) -> tuple[Optional[dict], Optional[AgorError]]:
        """Generic JSON round-trip. `(parsed_dict, None)` on success,
        `(None, AgorError)` on every failure mode. Never raises."""
        token = self._read_token()
        if not token:
            return (None, AgorError(
                kind="config",
                detail=f"no admin token at {self.token_path}",
            ))
        url = f"{self.base_url.rstrip('/')}{path}"
        data = None
        if body is not None:
            try:
                data = json.dumps(body).encode("utf-8")
            except Exception as e:
                return (None, AgorError(
                    kind="bad_response",
                    detail=f"could not encode body: {e}",
                ))
        req = Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=_HTTP_TIMEOUT_SECS) as resp:
                raw = resp.read()
                if not raw:
                    return ({}, None)
                try:
                    return (json.loads(raw.decode("utf-8")), None)
                except Exception as e:
                    return (None, AgorError(
                        kind="bad_response",
                        detail=f"non-JSON response: {e}",
                    ))
        except HTTPError as e:
            kind = "auth" if e.code in (401, 403) else (
                "not_found" if e.code == 404 else "network"
            )
            return (None, AgorError(kind=kind, status=e.code,
                                     detail=str(e)))
        except URLError as e:
            return (None, AgorError(kind="network", detail=str(e.reason)))
        except Exception as e:  # noqa: BLE001
            return (None, AgorError(kind="network", detail=str(e)))

    # ── public surface ──────────────────────────────────────────

    def get_session(self, session_id: str,
                    ) -> tuple[Optional[dict], Optional[AgorError]]:
        """GET /sessions/:id — cached for `self.ttl` seconds.

        Returns the raw Session dict (per
        =dist/core/session-CAfhv1qL.d.ts=). Includes `genealogy.*`,
        `git_state.*`, `worktree_id`, `custom_context`, etc.
        """
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(session_id)
            if hit and (now - hit.fetched_at) < self.ttl:
                return (hit.body, None)
        body, err = self._request("GET", f"/sessions/{session_id}")
        if body is not None:
            with self._lock:
                self._cache[session_id] = _CachedSession(
                    fetched_at=now, body=body,
                )
        return (body, err)

    def patch_session(self, session_id: str, updates: dict[str, Any],
                       ) -> tuple[Optional[dict], Optional[AgorError]]:
        """PATCH /sessions/:id with `updates`. Invalidates cache for
        this session (next get_session re-fetches).

        Bridge uses this to set
        `custom_context.captain_log_event_ids = [...]`. The daemon's
        services layer accepts a partial Session blob and merges; we
        verified this by reading the Feathers `service("sessions")
        .patch(id, updates, params)` call sites in
        =dist/daemon/register-services.js=.
        """
        body, err = self._request("PATCH", f"/sessions/{session_id}",
                                    body=updates)
        # Invalidate cache regardless of success — a failed PATCH may
        # still have partially mutated server state (HTTP retries are
        # generally idempotent for the merge use-case but not for
        # list-append; we always re-read).
        with self._lock:
            self._cache.pop(session_id, None)
        return (body, err)

    def query_genealogy(self, parent_session_id: str,
                         ) -> tuple[Optional[dict], Optional[AgorError]]:
        """Read the parent session and resolve its `genealogy.children`
        list into a small tree dict:

          {
            "session_id":  <parent>,
            "genealogy":   {... raw ...},
            "children":    [<child_session_dict>, ...],
          }

        Children are fetched serially (cached). N is small in practice
        (a session typically spawns 0–5 direct children); tree-depth
        traversal is left to callers who care."""
        parent, err = self.get_session(parent_session_id)
        if parent is None:
            return (None, err)
        gen = parent.get("genealogy") or {}
        child_ids = list(gen.get("children") or [])
        children: list[dict] = []
        for cid in child_ids:
            child, child_err = self.get_session(cid)
            if child is not None:
                children.append(child)
            # Skip children that 404 — Agor allows soft-archive cascades.
        return ({
            "session_id": parent_session_id,
            "genealogy":  gen,
            "children":   children,
        }, None)

    # ── spawn surface (write API) ───────────────────────────────
    #
    # The smoke script (=scripts/agor-smoke.sh=) drives the same
    # endpoints. Adding them here so the chat-dispatch CLI verb
    # can reuse them programmatically without shelling out to bash.

    def list_repos(self) -> tuple[Optional[list], Optional[AgorError]]:
        """GET /repos — returns the list of registered Agor repos.
        Each repo dict has at least `id`, `local_path`, `name`,
        `default_branch`."""
        body, err = self._request("GET", "/repos")
        if err is not None:
            return (None, err)
        # The daemon returns either a bare list OR `{data: [...]}` —
        # tolerate both shapes.
        if isinstance(body, list):
            return (body, None)
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                return (data, None)
            # Some endpoints return `{<id>: <repo>, ...}`
            return (list(body.values()), None)
        return ([], None)

    def find_repo_by_path(self, local_path: str,
                          ) -> tuple[Optional[dict], Optional[AgorError]]:
        """Look up a repo by its `local_path' field. Useful for
        callers that know the repo on disk but not its Agor id."""
        repos, err = self.list_repos()
        if err is not None:
            return (None, err)
        for r in repos or []:
            if isinstance(r, dict) and r.get("local_path") == local_path:
                return (r, None)
        return (None, AgorError(kind="not_found",
                                  detail=f"no repo with local_path={local_path}"))

    def create_worktree(self, repo_id: str, name: str,
                          source_branch: Optional[str] = None,
                          ) -> tuple[Optional[dict], Optional[AgorError]]:
        """POST /repos/:id/worktrees. Creates a new git worktree at
        =<.agor>/worktrees/<repo>/<name>=. Returns the worktree dict
        with `worktree_id`, `path`, `worktree_unique_id`.

        `source_branch' defaults to the repo's `default_branch' (the
        daemon will resolve when omitted)."""
        body: dict[str, Any] = {
            "name":          name,
            "ref":           name,
            "createBranch":  True,
            "pullLatest":    False,
            "refType":       "branch",
        }
        if source_branch:
            body["sourceBranch"] = source_branch
        return self._request("POST", f"/repos/{repo_id}/worktrees",
                              body=body)

    def create_session(self, worktree_id: str, *,
                        agentic_tool: str = "opencode",
                        title: Optional[str] = None,
                        ) -> tuple[Optional[dict], Optional[AgorError]]:
        """POST /sessions. Returns dict with `session_id` and
        `mcp_token` (admin-role JWT scoped to this session, default
        24h expiry).

        `agentic_tool' defaults to `\"opencode\"` per the project
        rule that Agor-spawned crew sessions must use FOSS tooling
        only (see `feedback_agor_no_claude.md' in author memory).
        Passing `\"claude-code\"` is a footgun — included as an
        explicit override only because the smoke script uses it
        for tooling validation, not real crew work."""
        body: dict[str, Any] = {
            "worktree_id":   worktree_id,
            "agentic_tool":  agentic_tool,
        }
        if title:
            body["title"] = title
        return self._request("POST", "/sessions", body=body)

    def invalidate(self, session_id: Optional[str] = None) -> None:
        """Drop cached entries. Pass None to clear everything; useful
        for tests and for the (future) v0.1 proxy interceptor that
        knows when an upstream event mutates the session."""
        with self._lock:
            if session_id is None:
                self._cache.clear()
            else:
                self._cache.pop(session_id, None)


__all__ = [
    "AgorClient",
    "AgorError",
]
