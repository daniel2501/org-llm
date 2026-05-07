"""Layer-3 Pattern A: Superset URL → chat-prompt prelude.

When the user asks org-llm a question while looking at a Superset
chart or dashboard, the URL captures most of the scope state (time
range, metric, group_by, viz_type, native filter key, dashboard
slug). This module turns the URL into a one-line context prefix
that gets prepended to the chat prompt — so the agent answers in
the scope the user is looking at, without restating constraints.

Example:

    user opens captains-bridge with time_range=Last+week, then asks
    in the chat prompt: "why is gemma3 so slow lately?"

With `org-llm ask --dashboard-url <url> "..."`, this module produces
a prelude like:

    [Superset context: viewing dashboard 'captains-bridge';
    time_range='Last week'] why is gemma3 so slow lately?

Two URL shapes are supported:

  Dashboard:  http://host/superset/dashboard/<slug>/?...
  Explore:    http://host/explore/?slice_id=N&form_data={...}

Native filter state behind ?native_filters_key=KEY requires a
roundtrip to Superset's filter_state API to enrich. With a
configured client, fetch_filter_state() does the lookup and
extends the prelude.

This is the cheap path. The expensive path (chat reads the *current*
filter state by re-fetching on every turn) is a v3.1 follow-up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# /superset/dashboard/<slug>/  (slug ends at next slash or end)
_DASH_PATH_RE = re.compile(r"/superset/dashboard/([^/]+)/?")


@dataclass(frozen=True)
class SupersetUrlState:
    """Everything we extracted from a Superset URL via parse alone.

    Fields are None / empty when not present. `has_state` tells the
    caller whether the URL carried *any* useful context — if False,
    the prelude should be empty and the chat path proceeds as normal.

    `form_data_key` is the most common URL shape produced by Superset's
    explore page (`?form_data_key=KEY&slice_id=N`) — the form_data is
    stored server-side under the key. Pure URL parse can't resolve it;
    the enricher below handles it via /api/v1/explore/form_data/<key>.
    """

    slug: Optional[str] = None
    slice_id: Optional[int] = None
    time_range: Optional[str] = None
    native_filters_key: Optional[str] = None
    form_data_key: Optional[str] = None
    form_data: dict[str, Any] = field(default_factory=dict)

    @property
    def has_state(self) -> bool:
        return any(
            (
                self.slug,
                self.slice_id,
                self.time_range,
                self.native_filters_key,
                self.form_data_key,
                self.form_data,
            )
        )


def parse_url(url: str) -> SupersetUrlState:
    """Pull whatever Superset state is encoded in the URL alone.

    Doesn't hit the network. Native filter state behind a
    `?native_filters_key=KEY` is left as the opaque key — see
    `fetch_filter_state` for the enrichment path.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    slug = None
    m = _DASH_PATH_RE.search(parsed.path)
    if m:
        slug = m.group(1)

    slice_id: Optional[int] = None
    if "slice_id" in qs:
        try:
            slice_id = int(qs["slice_id"][0])
        except (ValueError, IndexError):
            slice_id = None

    time_range = qs.get("time_range", [None])[0]
    nfk = qs.get("native_filters_key", [None])[0]
    fdk = qs.get("form_data_key", [None])[0]

    form_data: dict[str, Any] = {}
    if "form_data" in qs:
        try:
            form_data = json.loads(qs["form_data"][0])
            if not isinstance(form_data, dict):
                form_data = {}
        except (json.JSONDecodeError, IndexError, TypeError):
            form_data = {}

    return SupersetUrlState(
        slug=slug,
        slice_id=slice_id,
        time_range=time_range,
        native_filters_key=nfk,
        form_data_key=fdk,
        form_data=form_data,
    )


# ---------- prelude formatting -------------------------------------------


def _format_form_data(fd: dict[str, Any]) -> list[str]:
    """Pull the chart-shaping fields out of an explore-page form_data.

    Keep the parts list short — captain doesn't want a 500-char
    prelude. Just the metric / group_by / viz_type the user is
    looking at, plus a clean time_range if present here too.
    """
    parts: list[str] = []
    metrics = fd.get("metrics") or []
    if metrics:
        head = metrics[0]
        parts.append(f"metric={head!r}" if isinstance(head, str) else f"metric={head}")
    if fd.get("groupby"):
        parts.append(f"groupby={fd['groupby']}")
    if fd.get("x_axis"):
        parts.append(f"x_axis={fd['x_axis']!r}")
    if fd.get("viz_type"):
        parts.append(f"viz={fd['viz_type']!r}")
    if fd.get("time_range") and fd["time_range"] != "No filter":
        parts.append(f"time_range={fd['time_range']!r}")
    return parts


def format_prompt_prelude(
    state: SupersetUrlState,
    *,
    dashboard_title: Optional[str] = None,
    chart_name: Optional[str] = None,
    extra_filters: Optional[dict[str, Any]] = None,
) -> str:
    """One-line bracketed prelude for the chat prompt.

    Empty string if the URL had no useful state — caller can
    unconditionally prepend without inserting noise.

    `dashboard_title` and `chart_name` come from a Superset client
    lookup (Registry doesn't know dashboard titles). `extra_filters`
    comes from `fetch_filter_state` enrichment of a
    native_filters_key. Both are optional.
    """
    if not state.has_state and not extra_filters:
        return ""

    parts: list[str] = []
    if state.slug:
        title = dashboard_title or state.slug
        parts.append(f"viewing dashboard {title!r}")
    if state.slice_id is not None:
        label = chart_name or f"#{state.slice_id}"
        parts.append(f"exploring chart {label!r}")
    if state.time_range:
        parts.append(f"time_range={state.time_range!r}")
    parts.extend(_format_form_data(state.form_data))
    if state.native_filters_key and not extra_filters:
        parts.append(
            f"native filters present (key={state.native_filters_key[:8]}…, "
            f"unparsed)"
        )
    # If form_data_key was in the URL but we couldn't enrich it (no
    # base_url, or Superset offline), at least flag that the chart's
    # configured state isn't reflected in the prelude.
    if state.form_data_key and not state.form_data:
        parts.append(
            f"form_data behind key={state.form_data_key[:8]}… "
            f"(call `superset_prelude_for_url` with `base_url=` to enrich)"
        )
    if extra_filters:
        for k, v in extra_filters.items():
            parts.append(f"{k}={v!r}")

    if not parts:
        return ""
    return f"[Superset context: {'; '.join(parts)}] "


# ---------- enrichment via Superset API ----------------------------------


def fetch_filter_state(
    base_url: str,
    state: SupersetUrlState,
    auth: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    """Resolve a native_filters_key into a flat {filter_name: value} dict.

    Returns an empty dict when there's no key, when Superset is
    unreachable, or when the key has no stored state. Never raises —
    the chat path proceeds without the enrichment if anything fails.
    """
    if not state.native_filters_key or not state.slug:
        return {}
    try:
        import requests  # local import: optional dep for the chat path
    except ImportError:
        return {}

    auth = auth or ("admin", "admin")
    sess = requests.Session()
    try:
        # Bearer login — same flow as superset_import.OrgDashboard.post.
        tok = sess.post(
            f"{base_url}/api/v1/security/login",
            json={
                "username": auth[0],
                "password": auth[1],
                "provider": "db",
                "refresh": True,
            },
            timeout=5,
        ).json().get("access_token")
        if not tok:
            return {}
        hdrs = {"Authorization": f"Bearer {tok}"}
        # Resolve slug → dashboard id (filter_state needs the int id).
        d = sess.get(
            f"{base_url}/api/v1/dashboard/{state.slug}",
            headers=hdrs,
            timeout=5,
        )
        if not d.ok:
            return {}
        dash_id = d.json()["result"]["id"]
        fs = sess.get(
            f"{base_url}/api/v1/dashboard/{dash_id}/"
            f"filter_state/{state.native_filters_key}",
            headers=hdrs,
            timeout=5,
        )
        if not fs.ok:
            return {}
        # Superset's filter_state body is JSON-stringified.
        body = fs.json()
        raw = body.get("value") or body.get("result", {}).get("value")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        if not isinstance(raw, dict):
            return {}
        # The shape is {filter_id: {extraFormData: {...}, ownState: {...}, ...}}.
        # We pluck the human-meaningful bits: time_range and any
        # filter `value` arrays.
        out: dict[str, Any] = {}
        for fid, fst in raw.items():
            if not isinstance(fst, dict):
                continue
            label = fst.get("label") or fst.get("filterName") or fid
            efd = fst.get("extraFormData") or {}
            tr = efd.get("time_range")
            if tr:
                out["time_range"] = tr
            v = efd.get("filters")
            if isinstance(v, list) and v and isinstance(v[0], dict):
                vals = v[0].get("val")
                if vals:
                    out[label] = vals
        return out
    except Exception:
        # Defensive: filter enrichment is best-effort; never break the
        # chat path on a network blip.
        return {}


def fetch_form_data(
    base_url: str,
    key: str,
    auth: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    """Resolve a `?form_data_key=KEY` (the common explore-page URL
    shape) into the actual form_data dict via Superset's
    `/api/v1/explore/form_data/<key>` endpoint.

    The endpoint returns `{"form_data": "<json string>"}`; we
    JSON-decode the inner string. Best-effort: returns {} on any
    network/auth failure so the chat path stays clean.
    """
    if not key:
        return {}
    try:
        import requests
    except ImportError:
        return {}
    auth = auth or ("admin", "admin")
    sess = requests.Session()
    try:
        tok = sess.post(
            f"{base_url}/api/v1/security/login",
            json={
                "username": auth[0],
                "password": auth[1],
                "provider": "db",
                "refresh": True,
            },
            timeout=5,
        ).json().get("access_token")
        if not tok:
            return {}
        r = sess.get(
            f"{base_url}/api/v1/explore/form_data/{key}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=5,
        )
        if not r.ok:
            return {}
        body = r.json()
        # Two shapes seen in the wild: {"form_data": "<json string>"}
        # and {"result": "<json string>"}. The string-or-dict logic
        # below covers both — and we just got handed a literal dict.
        raw = body.get("form_data")
        if raw is None:
            raw = body.get("result")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception:
        return {}


def fetch_dashboard_title(
    base_url: str,
    slug: str,
    auth: Optional[tuple[str, str]] = None,
) -> Optional[str]:
    """Look up the human dashboard title for a slug. Returns None on
    any failure — the prelude falls back to using the slug itself."""
    if not slug:
        return None
    try:
        import requests
    except ImportError:
        return None
    auth = auth or ("admin", "admin")
    sess = requests.Session()
    try:
        tok = sess.post(
            f"{base_url}/api/v1/security/login",
            json={
                "username": auth[0],
                "password": auth[1],
                "provider": "db",
                "refresh": True,
            },
            timeout=5,
        ).json().get("access_token")
        if not tok:
            return None
        d = sess.get(
            f"{base_url}/api/v1/dashboard/{slug}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=5,
        )
        if not d.ok:
            return None
        return d.json()["result"].get("dashboard_title")
    except Exception:
        return None


def superset_prelude_for_url(
    url: str,
    *,
    base_url: Optional[str] = None,
    auth: Optional[tuple[str, str]] = None,
    enrich: bool = False,
) -> str:
    """High-level convenience: URL → prelude string in one call.

    `form_data_key` enrichment is *automatic* whenever a `base_url`
    is provided — that's the default URL shape qutebrowser hands you
    when you copy from Superset's explore page, and pure URL parse
    yields a useless prelude (just a slice_id). One ~100ms round-trip
    transforms it into a rich `metric=...; groupby=...; viz=...`
    prelude. Enrichment failures (offline Superset, bad auth) fall
    through silently — caller still gets the URL-parse-only prelude.

    `enrich=True` additionally fetches the dashboard title and
    native-filter values for dashboard URLs (the heavier extras).
    `enrich=False` (default) keeps those off but still does the
    cheap form_data_key resolution.
    """
    state = parse_url(url)
    if not state.has_state:
        return ""

    # Cheap-and-load-bearing: form_data_key enrichment whenever
    # we have a base_url. The URL-only prelude is meaningless here
    # because the form_data lives behind the key, server-side.
    if base_url and state.form_data_key and not state.form_data:
        fetched = fetch_form_data(base_url, state.form_data_key, auth=auth)
        if fetched:
            # Re-create the state with the enriched form_data baked in.
            state = SupersetUrlState(
                slug=state.slug,
                slice_id=state.slice_id,
                time_range=state.time_range or fetched.get("time_range"),
                native_filters_key=state.native_filters_key,
                form_data_key=state.form_data_key,
                form_data=fetched,
            )

    title = None
    extra: dict[str, Any] = {}
    if enrich and base_url:
        title = fetch_dashboard_title(base_url, state.slug or "", auth=auth)
        extra = fetch_filter_state(base_url, state, auth=auth)
    return format_prompt_prelude(
        state,
        dashboard_title=title,
        extra_filters=extra,
    )
