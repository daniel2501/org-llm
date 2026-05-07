"""Tests for org_llm.superset_filter — Layer-3 Pattern A.

URL parsing is pure; enrichment via Superset's API is mocked. A live
test against a real Superset is gated behind ORG_LLM_TEST_LIVE_SUPERSET.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from org_llm.superset_filter import (
    SupersetUrlState,
    fetch_dashboard_title,
    fetch_filter_state,
    fetch_form_data,
    format_prompt_prelude,
    parse_url,
    superset_prelude_for_url,
)


# ---------- parse_url -----------------------------------------------------


def test_parse_dashboard_url_with_native_filters() -> None:
    url = "http://127.0.0.1:8088/superset/dashboard/captains-bridge/?native_filters_key=ABCDEF12345"
    s = parse_url(url)
    assert s.slug == "captains-bridge"
    assert s.native_filters_key == "ABCDEF12345"
    assert s.slice_id is None
    assert s.time_range is None
    assert s.form_data == {}
    assert s.has_state


def test_parse_explore_url_with_form_data() -> None:
    fd = {
        "viz_type": "echarts_timeseries_bar",
        "metrics": ["llm_avg_ms"],
        "groupby": ["model"],
        "x_axis": "model",
        "time_range": "Last week",
    }
    url = (
        "http://127.0.0.1:8088/explore/?slice_id=42"
        f"&form_data={json.dumps(fd)}"
    )
    s = parse_url(url)
    assert s.slice_id == 42
    assert s.slug is None
    assert s.form_data == fd
    assert s.has_state


def test_parse_explore_url_with_form_data_key() -> None:
    """The qutebrowser-copied URL shape — form_data lives server-side
    behind an opaque key. Pure parse just captures the key; enrichment
    via fetch_form_data resolves it."""
    url = "http://127.0.0.1:8088/explore/?form_data_key=cdUlQJx2v0c&slice_id=5&standalone=1"
    s = parse_url(url)
    assert s.slice_id == 5
    assert s.form_data_key == "cdUlQJx2v0c"
    assert s.form_data == {}  # not yet enriched
    assert s.has_state


def test_parse_url_with_explicit_time_range() -> None:
    url = "http://127.0.0.1:8088/superset/dashboard/captains-bridge/?time_range=Last+30+days"
    s = parse_url(url)
    assert s.slug == "captains-bridge"
    assert s.time_range == "Last 30 days"


def test_parse_url_no_state_returns_clean_default() -> None:
    s = parse_url("http://127.0.0.1:8088/")
    assert not s.has_state
    assert s == SupersetUrlState()


def test_parse_url_garbage_form_data_doesnt_crash() -> None:
    """If the URL's form_data isn't valid JSON, fall back to empty
    dict — the rest of the prelude still has value (slug, slice_id)."""
    url = "http://x/explore/?slice_id=7&form_data=not_json{"
    s = parse_url(url)
    assert s.slice_id == 7
    assert s.form_data == {}


def test_parse_url_form_data_not_a_dict_falls_back() -> None:
    """form_data that's a JSON list / string is rejected — the only
    Superset shape we care about is dict."""
    url = 'http://x/explore/?form_data=["a","b"]'
    s = parse_url(url)
    assert s.form_data == {}


# ---------- format_prompt_prelude ----------------------------------------


def test_prelude_empty_when_no_state() -> None:
    assert format_prompt_prelude(SupersetUrlState()) == ""


def test_prelude_dashboard_with_title() -> None:
    s = SupersetUrlState(slug="captains-bridge", time_range="Last week")
    out = format_prompt_prelude(s, dashboard_title="Captain's Bridge — vital signs")
    assert "viewing dashboard" in out
    assert "Captain's Bridge" in out
    assert "Last week" in out
    assert out.startswith("[Superset context:")
    assert out.endswith("] ")  # trailing space so it joins cleanly with the user's query


def test_prelude_dashboard_falls_back_to_slug_without_title() -> None:
    s = SupersetUrlState(slug="captains-bridge")
    out = format_prompt_prelude(s)
    assert "captains-bridge" in out


def test_prelude_explore_chart_with_form_data() -> None:
    s = SupersetUrlState(
        slice_id=42,
        form_data={
            "viz_type": "echarts_timeseries_bar",
            "metrics": ["llm_avg_ms"],
            "groupby": ["model"],
            "x_axis": "model",
        },
    )
    out = format_prompt_prelude(s, chart_name="Model cost & latency")
    assert "Model cost & latency" in out
    assert "metric='llm_avg_ms'" in out
    assert "groupby=['model']" in out
    assert "viz='echarts_timeseries_bar'" in out


def test_prelude_native_filter_key_marked_unparsed() -> None:
    """Without enrichment, the prelude flags that there are filters we
    didn't resolve — agent knows to ask if they matter."""
    s = SupersetUrlState(slug="x", native_filters_key="LONGOPAQUEKEY12345")
    out = format_prompt_prelude(s)
    assert "native filters present" in out
    assert "unparsed" in out


def test_prelude_extra_filters_replaces_unparsed_marker() -> None:
    s = SupersetUrlState(slug="x", native_filters_key="LONGOPAQUEKEY12345")
    out = format_prompt_prelude(s, extra_filters={"model": ["phi3.5", "gemma3"]})
    assert "model=['phi3.5', 'gemma3']" in out
    assert "unparsed" not in out


def test_prelude_extra_filters_alone_works() -> None:
    """When URL parse yields nothing but extra_filters has values
    (e.g. user passed filters explicitly), the prelude still emits."""
    s = SupersetUrlState()
    out = format_prompt_prelude(s, extra_filters={"time_range": "Last week"})
    assert out
    assert "time_range='Last week'" in out


def test_prelude_drops_no_filter_time_range() -> None:
    """Superset emits time_range='No filter' when none is set —
    suppress that from the prelude."""
    s = SupersetUrlState(form_data={"time_range": "No filter", "viz_type": "table"})
    out = format_prompt_prelude(s)
    assert "No filter" not in out
    assert "viz='table'" in out


# ---------- enrichment via Superset API (mocked) -------------------------


class _FakeResp:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self._body = body or {}
        self.ok = 200 <= status < 300
        self.status_code = status

    def json(self) -> dict:
        return self._body


class _FakeSession:
    """Minimal requests.Session double for fetch_filter_state."""

    def __init__(self, plan: dict[str, _FakeResp]) -> None:
        self._plan = plan
        self.calls: list[tuple[str, str]] = []

    def post(self, url: str, json=None, timeout=None):
        self.calls.append(("POST", url))
        return self._plan.get(url, _FakeResp(404))

    def get(self, url: str, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self._plan.get(url, _FakeResp(404))


def test_fetch_filter_state_returns_empty_without_key() -> None:
    s = SupersetUrlState(slug="x")  # no native_filters_key
    assert fetch_filter_state("http://x", s) == {}


def test_fetch_filter_state_extracts_named_filter_values() -> None:
    s = SupersetUrlState(slug="captains-bridge", native_filters_key="KEY1")
    plan = {
        "http://x/api/v1/security/login":
            _FakeResp(200, {"access_token": "T"}),
        "http://x/api/v1/dashboard/captains-bridge":
            _FakeResp(200, {"result": {"id": 1}}),
        "http://x/api/v1/dashboard/1/filter_state/KEY1":
            _FakeResp(200, {
                "value": json.dumps({
                    "f-1": {
                        "label": "Model",
                        "extraFormData": {
                            "filters": [{"col": "model", "val": ["phi3.5"]}],
                        },
                    },
                    "f-2": {
                        "label": "TimeRange",
                        "extraFormData": {"time_range": "Last week"},
                    },
                }),
            }),
    }
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_filter_state("http://x", s)
    assert out == {"Model": ["phi3.5"], "time_range": "Last week"}


def test_fetch_filter_state_swallows_network_errors() -> None:
    """Best-effort: any error returns {} so the chat path stays clean."""
    s = SupersetUrlState(slug="x", native_filters_key="K")
    plan = {"http://x/api/v1/security/login": _FakeResp(500)}
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_filter_state("http://x", s)
    assert out == {}


def test_fetch_form_data_resolves_key_to_dict() -> None:
    """v3.1: GET /api/v1/explore/form_data/<key> returns the form_data
    JSON-encoded inside `{"form_data": "<json>"}`. Parser must JSON-decode
    the inner string."""
    inner = json.dumps({
        "viz_type": "echarts_timeseries_bar",
        "metrics": ["llm_calls"],
        "groupby": ["outcome"],
        "x_axis": "model",
    })
    plan = {
        "http://x/api/v1/security/login":
            _FakeResp(200, {"access_token": "T"}),
        "http://x/api/v1/explore/form_data/KEY1":
            _FakeResp(200, {"form_data": inner}),
    }
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_form_data("http://x", "KEY1")
    assert out == {
        "viz_type": "echarts_timeseries_bar",
        "metrics": ["llm_calls"],
        "groupby": ["outcome"],
        "x_axis": "model",
    }


def test_fetch_form_data_returns_empty_when_key_missing() -> None:
    assert fetch_form_data("http://x", "") == {}


def test_fetch_form_data_swallows_failures() -> None:
    plan = {"http://x/api/v1/security/login": _FakeResp(500)}
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_form_data("http://x", "KEY")
    assert out == {}


def test_superset_prelude_auto_enriches_form_data_key() -> None:
    """v3.1: when base_url is provided and the URL has form_data_key,
    auto-fetch the form_data — even with enrich=False (default).

    The form_data_key path is the load-bearing case; pure URL parse
    yields a useless prelude (just slice_id). Without this auto-enrich,
    the user's qutebrowser-copied URL produces nothing actionable."""
    url = "http://x/explore/?form_data_key=KEY1&slice_id=5"
    inner = json.dumps({
        "viz_type": "echarts_timeseries_bar",
        "metrics": ["llm_calls"],
        "groupby": ["outcome"],
        "x_axis": "model",
        "time_range": "Last week",
    })
    plan = {
        "http://x/api/v1/security/login":
            _FakeResp(200, {"access_token": "T"}),
        "http://x/api/v1/explore/form_data/KEY1":
            _FakeResp(200, {"form_data": inner}),
    }
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = superset_prelude_for_url(url, base_url="http://x")
    # Rich prelude: chart + metric + groupby + x_axis + viz + time_range.
    assert "exploring chart '#5'" in out
    assert "metric='llm_calls'" in out
    assert "groupby=['outcome']" in out
    assert "x_axis='model'" in out
    assert "viz='echarts_timeseries_bar'" in out
    assert "time_range='Last week'" in out
    # The "behind key …" hint should NOT appear — we successfully enriched.
    assert "behind key" not in out


def test_prelude_form_data_key_unenriched_flagged_for_enrichment() -> None:
    """When base_url is None or fetch fails, the prelude flags that
    the form_data behind the key wasn't reached — agent at least knows
    its scope is incomplete."""
    s = SupersetUrlState(slice_id=5, form_data_key="ABCDEFGH123")
    out = format_prompt_prelude(s)
    assert "form_data behind key" in out
    assert "ABCDEFGH" in out


def test_fetch_dashboard_title_returns_title_on_ok() -> None:
    plan = {
        "http://x/api/v1/security/login":
            _FakeResp(200, {"access_token": "T"}),
        "http://x/api/v1/dashboard/captains-bridge":
            _FakeResp(200, {"result": {"dashboard_title": "Captain's Bridge"}}),
    }
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_dashboard_title("http://x", "captains-bridge")
    assert out == "Captain's Bridge"


def test_fetch_dashboard_title_returns_none_on_failure() -> None:
    plan = {"http://x/api/v1/security/login": _FakeResp(401)}
    sess = _FakeSession(plan)
    with patch("requests.Session", return_value=sess):
        out = fetch_dashboard_title("http://x", "missing")
    assert out is None


# ---------- end-to-end convenience ---------------------------------------


def test_superset_prelude_for_url_no_state_returns_empty() -> None:
    assert superset_prelude_for_url("http://x/") == ""


def test_superset_prelude_for_url_url_only_no_enrich() -> None:
    url = "http://x/superset/dashboard/cb/?time_range=Last+week"
    out = superset_prelude_for_url(url)
    assert "viewing dashboard 'cb'" in out
    assert "time_range='Last week'" in out


@pytest.mark.skipif(
    not os.environ.get("ORG_LLM_TEST_LIVE_SUPERSET"),
    reason="set ORG_LLM_TEST_LIVE_SUPERSET=1 to hit a real Superset",
)
def test_superset_prelude_against_live_superset() -> None:
    """End-to-end: parse a real captains-bridge URL, enrich with API
    (must include the dashboard's actual title)."""
    out = superset_prelude_for_url(
        "http://127.0.0.1:8088/superset/dashboard/captains-bridge/",
        base_url="http://127.0.0.1:8088",
        enrich=True,
    )
    assert out
    # Title from the imported template.
    assert "Captain's Bridge" in out
