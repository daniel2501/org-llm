"""Operational pruning for Superset dashboards created by org-llm.

Layer 2 of the Superset dashboard design
(`docs/wiki/2026-05-06-superset-dashboard-design.org` § "Sharp edges" →
"Re-import pruning") flagged that each one-off dashboard creates a
database link, datasets, charts, and a dashboard in Superset's
metadata DB. Without a pruning verb that DB grows unboundedly. This
module is the verb.

Recognition strategy — *(b) slug convention*. We treat any dashboard
whose slug matches `oneoff-…` or `pinned-…` as org-llm-owned. Picked
over the other options for these reasons:

  - (a) UUID namespace requires bulk-fetching every dashboard's UUID
    and re-deriving expected uuid5 values for known slug shapes;
    it's not actually deterministic at recognition time because we
    don't know which slugs ever existed.
  - (c) Description prefix is visible in the Superset UI and a user
    accidentally clearing the description silently hides the
    dashboard from prune. Brittle.
  - (d) Custom property in YAML (`extra: {org_llm: {...}}`) is the
    cleanest semantically, but requires a coupled change to
    `superset_import.py` *and* a sweep over already-imported
    dashboards. Defer to v1.1 if (b) gets ambiguous.

The slug-convention check is one regex on the dashboard list response
— no extra round-trips. If this prune ever deletes a user-authored
dashboard the user must have named it `oneoff-` or `pinned-`
deliberately, which is a strong-enough signal of intent.

Usage from the CLI:

    org-llm superset prune --older-than 30d --dry-run
    org-llm superset prune --older-than 14d --apply

Wired via `org_llm.cli:superset_app`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Slug-convention regex. Matches `oneoff-<...>` or `pinned-<...>` —
# the two slug families the preprocessor + persistent-console designs
# emit. Anchored at the start so a user-authored dashboard that
# happens to *contain* "oneoff" elsewhere in its slug is not caught.
ORG_LLM_SLUG_RE = re.compile(r"^(oneoff|pinned)-")

# Default pruning cutoff. 30 days matches the design doc's example;
# the CLI flag overrides this.
DEFAULT_OLDER_THAN = "30d"


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)


def parse_duration(spec: str) -> timedelta:
    """Parse `Nd`/`Nh`/`Nw` into a timedelta.

    Examples: `7d` → 7 days, `24h` → 1 day, `2w` → 14 days.
    Bare integers are not accepted on purpose — explicitness > saving
    one keystroke and ambiguity-cost is high (is `30` minutes? days?).
    """
    m = _DURATION_RE.match(spec)
    if m is None:
        raise ValueError(
            f"invalid duration {spec!r}; expected forms like 7d, 24h, 2w"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    # _DURATION_RE constrains units to h/d/w, so this branch is
    # unreachable — kept as a defensive assert.
    raise ValueError(f"unsupported duration unit {unit!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def is_org_llm_dashboard(dash: dict[str, Any]) -> bool:
    """Recognize a dashboard as org-llm-created via slug convention.

    `dash` is one item from Superset's `/api/v1/dashboard/?q=...`
    `result` array, so it has at minimum a `slug` field (may be None
    for hand-authored dashboards that never got a slug).
    """
    slug = dash.get("slug") or ""
    return bool(ORG_LLM_SLUG_RE.match(slug))


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------


@dataclass
class PruneCandidate:
    """One dashboard considered for pruning."""

    id: int
    slug: str
    title: str
    created_at: datetime
    chart_ids: list[int]
    dataset_ids: list[int]
    age: timedelta
    will_delete: bool

    def age_human(self) -> str:
        """Human-readable age string (rounded to days when ≥1d)."""
        days = self.age.days
        if days >= 1:
            return f"{days}d"
        hours = self.age.seconds // 3600
        return f"{hours}h"


# ---------------------------------------------------------------------------
# Superset HTTP client (thin)
# ---------------------------------------------------------------------------


class SupersetClient:
    """Minimum-viable Superset REST client for prune.

    Mirrors the auth flow in `OrgDashboard.post`: login → CSRF, both
    on the same `requests.Session`. Kept independent of OrgDashboard
    so we can unit-test prune without touching the importer.
    """

    def __init__(
        self,
        url: str,
        auth: tuple[str, str],
        timeout: float = 30.0,
    ) -> None:
        try:
            import requests  # local import: lib is optional in the dep tree
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "requests is required for superset prune; "
                "install via `pip install requests`"
            ) from e
        self._requests = requests
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._login(auth)

    def _login(self, auth: tuple[str, str]) -> None:
        login = self.session.post(
            f"{self.url}/api/v1/security/login",
            json={
                "username": auth[0],
                "password": auth[1],
                "provider": "db",
                "refresh": True,
            },
            timeout=self.timeout,
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"
        csrf = self.session.get(
            f"{self.url}/api/v1/security/csrf_token/",
            timeout=self.timeout,
        )
        csrf.raise_for_status()
        self.session.headers["X-CSRFToken"] = csrf.json()["result"]

    # ---- list / detail ----------------------------------------------------

    def list_dashboards(self) -> list[dict[str, Any]]:
        """List all dashboards. Paginates through Superset's 100/page
        API limit so we don't silently miss old ones."""
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            r = self.session.get(
                f"{self.url}/api/v1/dashboard/",
                params={"q": f"(page:{page},page_size:100)"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()
            batch = body.get("result", [])
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_dashboard_charts(self, dashboard_id: int) -> list[int]:
        """Return chart ids attached to a dashboard."""
        r = self.session.get(
            f"{self.url}/api/v1/dashboard/{dashboard_id}/charts",
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        ids: list[int] = []
        for c in body.get("result", []):
            cid = c.get("id")
            if cid is not None:
                ids.append(int(cid))
        return ids

    def get_dashboard_datasets(self, dashboard_id: int) -> list[int]:
        """Return dataset ids attached to a dashboard's charts."""
        r = self.session.get(
            f"{self.url}/api/v1/dashboard/{dashboard_id}/datasets",
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        ids: list[int] = []
        for d in body.get("result", []):
            did = d.get("id")
            if did is not None:
                ids.append(int(did))
        return ids

    # ---- delete -----------------------------------------------------------

    def delete_chart(self, chart_id: int) -> None:
        r = self.session.delete(
            f"{self.url}/api/v1/chart/{chart_id}",
            timeout=self.timeout,
        )
        r.raise_for_status()

    def delete_dashboard(self, dashboard_id: int) -> None:
        r = self.session.delete(
            f"{self.url}/api/v1/dashboard/{dashboard_id}",
            timeout=self.timeout,
        )
        r.raise_for_status()

    def delete_dataset(self, dataset_id: int) -> None:
        r = self.session.delete(
            f"{self.url}/api/v1/dataset/{dataset_id}",
            timeout=self.timeout,
        )
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Pure helpers (no network, easy to unit-test)
# ---------------------------------------------------------------------------


def _parse_created_at(raw: str | None) -> datetime | None:
    """Parse Superset's created_on / changed_on timestamps.

    The API returns ISO-8601 with no timezone (`2026-05-06T12:34:56.000000`).
    We treat naive datetimes as UTC — Superset's metadata DB stores
    UTC by convention.
    """
    if not raw:
        return None
    try:
        # Strip trailing fractional seconds tail if present.
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_candidates(
    dashboards: Iterable[dict[str, Any]],
    cutoff: timedelta,
    now: datetime | None = None,
    fetch_charts: callable = None,  # type: ignore[valid-type]
    fetch_datasets: callable = None,  # type: ignore[valid-type]
) -> list[PruneCandidate]:
    """Filter + age-check dashboards into prune candidates.

    `fetch_charts` / `fetch_datasets` are callables `(dash_id) -> list[int]`
    so this function stays pure-ish for tests; the CLI passes
    `client.get_dashboard_charts` / `client.get_dashboard_datasets`.
    """
    now = now or datetime.now(timezone.utc)
    out: list[PruneCandidate] = []
    for d in dashboards:
        if not is_org_llm_dashboard(d):
            continue
        created = _parse_created_at(d.get("created_on") or d.get("changed_on"))
        if created is None:
            # No timestamp → treat as "always old", but mark won't-delete
            # to be conservative. User can hand-delete.
            continue
        age = now - created
        chart_ids = (
            list(fetch_charts(d["id"])) if fetch_charts is not None else []
        )
        dataset_ids = (
            list(fetch_datasets(d["id"])) if fetch_datasets is not None else []
        )
        out.append(
            PruneCandidate(
                id=int(d["id"]),
                slug=d.get("slug") or "",
                title=d.get("dashboard_title") or "",
                created_at=created,
                chart_ids=chart_ids,
                dataset_ids=dataset_ids,
                age=age,
                will_delete=age >= cutoff,
            )
        )
    return out


def execute_prune(
    client: "SupersetClient",
    candidates: Iterable[PruneCandidate],
) -> dict[str, int]:
    """Apply deletions for candidates flagged `will_delete`.

    Order matters for FK integrity in Superset's metadata DB:
        1. charts (they reference datasets)
        2. dashboards (they reference charts)
        3. datasets (now safe — nothing left referencing them)

    We never delete the database link — it's shared across all
    org-llm dashboards.

    Returns counts: `{"charts": N, "dashboards": M, "datasets": K}`.
    Failures bubble up as the underlying `requests.HTTPError`; the
    caller decides whether to keep going.
    """
    counts = {"charts": 0, "dashboards": 0, "datasets": 0}
    seen_dataset_ids: set[int] = set()

    targets = [c for c in candidates if c.will_delete]

    # 1. Charts first.
    for cand in targets:
        for cid in cand.chart_ids:
            client.delete_chart(cid)
            counts["charts"] += 1

    # 2. Dashboards next.
    for cand in targets:
        client.delete_dashboard(cand.id)
        counts["dashboards"] += 1

    # 3. Datasets last — dedupe across candidates, since multiple
    #    org-llm dashboards may point at the same registry dataset.
    for cand in targets:
        for did in cand.dataset_ids:
            if did in seen_dataset_ids:
                continue
            seen_dataset_ids.add(did)
            client.delete_dataset(did)
            counts["datasets"] += 1

    return counts


# ---------------------------------------------------------------------------
# Auth helper — username:password split shared with the CLI
# ---------------------------------------------------------------------------


def split_auth(spec: str) -> tuple[str, str]:
    """Parse `--auth USER:PASS` into (user, pass).

    A literal colon in the password is allowed (we split on the first
    `:` only). Empty user or empty pass raises — Superset rejects
    blank-username login anyway, but we want a clear error before
    the network round-trip.
    """
    if ":" not in spec:
        raise ValueError(
            "auth must be in the form USER:PASS (got no colon)"
        )
    user, _, password = spec.partition(":")
    if not user or not password:
        raise ValueError("auth USER and PASS must both be non-empty")
    return user, password


__all__ = [
    "ORG_LLM_SLUG_RE",
    "DEFAULT_OLDER_THAN",
    "PruneCandidate",
    "SupersetClient",
    "build_candidates",
    "execute_prune",
    "is_org_llm_dashboard",
    "parse_duration",
    "split_auth",
]
