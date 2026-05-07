"""Tests for DEC-015 v0.2 — SSE streaming on the OpenAI-compat
proxy endpoint.

The Emacs chat surface (`doom/org-llm-chat.el`) sends `stream: true`
to the running llm-proxy and consumes Server-Sent Events to render
each token as it arrives. These tests verify the proxy actually
emits the SSE shape the elisp client expects:

  - `Content-Type: text/event-stream`
  - one `data: {...}\\n\\n` chunk per token / finish marker
  - terminated by `data: [DONE]\\n\\n`
  - `stream: false` still returns plain JSON (v0.1 path preserved)

We use `intercept_sys_commands` (a built-in interceptor that returns
`_empty_assistant_response`) so we don't need a live upstream model;
the SSE emitter is the same one the forward path produces, so this
covers the wire shape end-to-end.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from org_llm import llm_proxy


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def started_proxy(tmp_path, monkeypatch):
    """Start a real proxy on an OS-assigned port with the
    sys-commands interceptor wired up so we have a deterministic
    SSE-emitting endpoint without a live upstream."""
    # Redirect the port file so we don't clobber the user's running
    # proxy (or pick up its port by accident).
    monkeypatch.setenv(
        "ORG_LLM_PROXY_PORT_FILE", str(tmp_path / "proxy-port"),
    )
    port, server = llm_proxy.start_proxy(
        upstream_url="http://127.0.0.1:1",   # bogus; never reached
        interceptors=[llm_proxy.intercept_sys_commands],
    )
    try:
        # Give serve_forever a moment to spin up.
        time.sleep(0.05)
        yield port, server
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


def _post_chat(port: int, body: dict, timeout: float = 5.0
                 ) -> tuple[int, dict, bytes]:
    """Send a POST to /v1/chat/completions; return (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, raw
    finally:
        conn.close()


def _sys_payload(stream: bool) -> dict:
    return {
        "model": "claude-sonnet-4.6",
        "stream": stream,
        "messages": [
            {"role": "user", "content": "/sys ping"},
        ],
    }


# ── SSE shape on stream: true ─────────────────────────────────────────────


def test_sse_content_type_when_stream_true(started_proxy):
    """`stream: true` → `Content-Type: text/event-stream`."""
    port, _ = started_proxy
    status, headers, _ = _post_chat(port, _sys_payload(stream=True))
    assert status == 200
    ctype = headers.get("content-type", "")
    assert "text/event-stream" in ctype, (
        f"expected text/event-stream, got {ctype!r}"
    )


def test_sse_body_emits_data_chunks(started_proxy):
    """Body is a sequence of `data: {json}\\n\\n` lines."""
    port, _ = started_proxy
    _, _, raw = _post_chat(port, _sys_payload(stream=True))
    text = raw.decode("utf-8", errors="replace")
    # At least one structured `data: {...}` chunk before [DONE].
    assert "data: {" in text, (
        f"expected JSON data chunks; got body={text[:200]!r}"
    )
    # Every chunk separator is the SSE-mandated double-newline.
    assert "\n\n" in text


def test_sse_terminates_with_done_marker(started_proxy):
    """SSE stream ends with `data: [DONE]\\n\\n`."""
    port, _ = started_proxy
    _, _, raw = _post_chat(port, _sys_payload(stream=True))
    text = raw.decode("utf-8", errors="replace")
    assert text.rstrip().endswith("data: [DONE]"), (
        f"stream did not end with [DONE]; tail={text[-80:]!r}"
    )


def test_sse_chunks_parse_as_openai_compat(started_proxy):
    """Each non-terminator `data:` chunk parses as JSON with the
    `chat.completion.chunk` shape (id/object/choices)."""
    port, _ = started_proxy
    _, _, raw = _post_chat(port, _sys_payload(stream=True))
    text = raw.decode("utf-8", errors="replace")
    chunks = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        chunks.append(json.loads(payload))
    assert chunks, "expected at least one parseable data chunk"
    for ch in chunks:
        assert ch.get("object") == "chat.completion.chunk", ch
        assert "id" in ch
        assert "choices" in ch
    # The role-marker delta must show up so clients know an
    # assistant message is starting.
    assert any(
        any(c.get("delta", {}).get("role") == "assistant"
            for c in ch.get("choices", []))
        for ch in chunks
    ), "expected at least one delta.role=='assistant' chunk"


def test_sse_helper_collects_content(started_proxy):
    """`_sse_collect_content_and_tool_calls` aggregates the SSE
    stream into the assistant text — the same helper the elisp
    client mirrors."""
    port, _ = started_proxy
    _, _, raw = _post_chat(port, _sys_payload(stream=True))
    content, tool_calls = (
        llm_proxy._sse_collect_content_and_tool_calls(raw)
    )
    # `intercept_sys_commands` echoes the canonical
    # _SYS_NOOP_CONTENT — so we should see SOMETHING in content.
    assert content, "SSE stream produced no content"
    assert tool_calls == [], (
        f"unexpected tool_calls in plain-content stream: {tool_calls}"
    )


# ── non-streaming path preserved (v0.1 fallback) ──────────────────────────


def test_stream_false_returns_plain_json(started_proxy):
    """`stream: false` still returns a single JSON body — the v0.1
    `--call-backend-proxy' path must keep working."""
    port, _ = started_proxy
    status, headers, raw = _post_chat(port, _sys_payload(stream=False))
    assert status == 200
    ctype = headers.get("content-type", "")
    assert "application/json" in ctype, (
        f"expected application/json for stream=false, got {ctype!r}"
    )
    obj = json.loads(raw.decode("utf-8"))
    assert obj.get("object") == "chat.completion"
    choices = obj.get("choices") or []
    assert choices and "message" in choices[0]
    assert isinstance(
        choices[0]["message"].get("content"), str
    )


def test_stream_omitted_defaults_to_non_streaming(started_proxy):
    """No `stream` field at all → non-streaming JSON (defensive — the
    proxy must not assume streaming)."""
    port, _ = started_proxy
    payload = _sys_payload(stream=False)
    payload.pop("stream", None)
    status, headers, _ = _post_chat(port, payload)
    assert status == 200
    assert "application/json" in headers.get("content-type", "")


# ── chunk ordering (role first, content next, finish_reason, [DONE]) ──────


def test_sse_chunk_ordering_matches_openai(started_proxy):
    """Standard ordering: role-marker → content → finish_reason →
    usage → [DONE]. Mirrors what real OpenAI/Ollama responses emit
    so opencode + the elisp client never see ambiguous frames."""
    port, _ = started_proxy
    _, _, raw = _post_chat(port, _sys_payload(stream=True))
    text = raw.decode("utf-8", errors="replace")

    seen_role = seen_content = seen_finish = seen_done = False
    seen_role_at = seen_content_at = seen_finish_at = seen_done_at = -1
    idx = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            seen_done = True
            seen_done_at = idx
            idx += 1
            continue
        obj = json.loads(payload)
        for ch in obj.get("choices", []):
            delta = ch.get("delta") or {}
            if delta.get("role") == "assistant" and not seen_role:
                seen_role = True
                seen_role_at = idx
            if isinstance(delta.get("content"), str) and not seen_content:
                seen_content = True
                seen_content_at = idx
            if ch.get("finish_reason") and not seen_finish:
                seen_finish = True
                seen_finish_at = idx
        idx += 1

    assert seen_role, "missing delta.role chunk"
    assert seen_content, "missing delta.content chunk"
    assert seen_finish, "missing finish_reason chunk"
    assert seen_done, "missing [DONE] terminator"
    assert seen_role_at < seen_content_at < seen_finish_at < seen_done_at, (
        f"chunks out of order: role@{seen_role_at} "
        f"content@{seen_content_at} finish@{seen_finish_at} "
        f"done@{seen_done_at}"
    )
