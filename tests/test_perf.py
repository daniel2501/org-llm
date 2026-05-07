"""Unit tests for pure helpers in org_llm.perf.

No DB, no filesystem, no network required.
"""
import pytest

from org_llm.perf import _normalize_model, _row_tok_s


# ── _normalize_model ──────────────────────────────────────────────────────────

class TestNormalizeModel:
    def test_strips_trailing_latest(self):
        assert _normalize_model("phi3.5:latest") == "phi3.5"

    def test_strips_latest_case_insensitive(self):
        assert _normalize_model("Phi3.5:LATEST") == "phi3.5"

    def test_lowercases_result(self):
        assert _normalize_model("Mistral-Nemo") == "mistral-nemo"

    def test_no_latest_suffix_unchanged(self):
        assert _normalize_model("llama3") == "llama3"

    def test_empty_string(self):
        assert _normalize_model("") == ""

    def test_none_like_empty(self):
        # name=None is not typed but the guard `(name or "")` handles it
        assert _normalize_model(None) == ""  # type: ignore[arg-type]

    def test_only_latest(self):
        assert _normalize_model(":latest") == ""

    def test_latest_mid_name_not_stripped(self):
        # ':latest' only stripped when it is a trailing suffix
        result = _normalize_model("model:latest:v2")
        assert result == "model:latest:v2"

    def test_strips_whitespace(self):
        assert _normalize_model("  phi3.5:latest  ") == "phi3.5"


# ── _row_tok_s ────────────────────────────────────────────────────────────────

_VALID_RESPONSE = "A" * 20   # 20 chars, clearly ≥ 10, not an error pattern
_VALID_MS = 2000              # 2 s — above 1000 ms threshold


class TestRowTokS:
    # --- returns None cases ---

    def test_outcome_error_returns_none(self):
        assert _row_tok_s(_VALID_RESPONSE, _VALID_MS, outcome="error") is None

    def test_outcome_error_case_insensitive(self):
        assert _row_tok_s(_VALID_RESPONSE, _VALID_MS, outcome="ERROR") is None

    def test_none_response_returns_none(self):
        assert _row_tok_s(None, _VALID_MS) is None

    def test_empty_response_returns_none(self):
        assert _row_tok_s("", _VALID_MS) is None

    def test_none_duration_returns_none(self):
        assert _row_tok_s(_VALID_RESPONSE, None) is None

    def test_zero_duration_returns_none(self):
        assert _row_tok_s(_VALID_RESPONSE, 0) is None

    def test_negative_duration_returns_none(self):
        assert _row_tok_s(_VALID_RESPONSE, -500) is None

    def test_sub_second_duration_returns_none(self):
        assert _row_tok_s(_VALID_RESPONSE, 999) is None

    def test_exactly_1000ms_is_not_filtered(self):
        # 1000 ms is NOT < 1000, so it should pass the guard
        result = _row_tok_s(_VALID_RESPONSE, 1000)
        assert result is not None

    def test_short_response_returns_none(self):
        assert _row_tok_s("Short", _VALID_MS) is None  # 5 chars < 10

    def test_exactly_9_chars_returns_none(self):
        assert _row_tok_s("A" * 9, _VALID_MS) is None

    def test_exactly_10_chars_not_filtered(self):
        result = _row_tok_s("A" * 10, _VALID_MS)
        assert result is not None

    def test_responseerror_prefix_returns_none(self):
        msg = "ResponseError: model requires more memory" + "x" * 30
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_http_prefix_returns_none(self):
        msg = "HTTP 503 Service Unavailable" + "x" * 30
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_error_colon_prefix_returns_none(self):
        msg = "error: something went wrong here and there ok" + "x" * 20
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_traceback_prefix_returns_none(self):
        msg = "Traceback (most recent call last):" + "x" * 30
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_exception_prefix_returns_none(self):
        msg = "Exception: unexpected failure encountered now" + "x" * 20
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_status_code_5xx_returns_none(self):
        msg = "request failed with status code: 503 reason" + "x" * 20
        assert _row_tok_s(msg, _VALID_MS) is None

    def test_status_code_4xx_returns_none(self):
        msg = "request failed with status code: 404 reason" + "x" * 20
        assert _row_tok_s(msg, _VALID_MS) is None

    # --- returns float cases ---

    def test_valid_inputs_return_float(self):
        result = _row_tok_s(_VALID_RESPONSE, _VALID_MS)
        assert isinstance(result, float)

    def test_correct_value(self):
        # 20 chars / 4 chars-per-token = 5 tokens; 5 / 2.0 s = 2.5 tok/s
        result = _row_tok_s(_VALID_RESPONSE, _VALID_MS)
        assert result == pytest.approx(2.5)

    def test_outcome_ok_not_filtered(self):
        result = _row_tok_s(_VALID_RESPONSE, _VALID_MS, outcome="ok")
        assert result is not None

    def test_outcome_none_not_filtered(self):
        result = _row_tok_s(_VALID_RESPONSE, _VALID_MS, outcome=None)
        assert result is not None

    def test_leading_whitespace_before_error_prefix_is_stripped(self):
        # lstrip() is applied before the prefix check
        msg = "   ResponseError: memory" + "x" * 40
        assert _row_tok_s(msg, _VALID_MS) is None
