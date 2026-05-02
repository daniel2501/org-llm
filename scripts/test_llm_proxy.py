#!/usr/bin/env python3
"""End-to-end tests for org_llm.llm_proxy.

Runs without opencode — spins up:
  • the real proxy (org_llm.llm_proxy.start_proxy)
  • a mock "ollama" upstream (stdlib http.server) that records
    every request that reaches it
Then fires opencode-style requests at the proxy and verifies:
  • /sys* → intercepted, mock upstream NOT touched
  • /menu, /help, /config → static slash interceptor fires
  • non-/sys → forwards transparently
  • SSE response shape matches what opencode's @ai-sdk/openai-
    compatible adapter expects (id, created, model, choices,
    delta, finish_reason, usage chunk, [DONE] terminator)
  • audit log records every request
  • cache replays identical prompts

Run via: uv run python scripts/test_llm_proxy.py
or:      org-llm proxy test     (after we wire the CLI)

Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# Add repo root to path so we can `from org_llm import ...`
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from org_llm.llm_proxy import (   # noqa: E402
    AuditLogger, _PrefixCache, start_proxy,
    intercept_sys_commands, intercept_prompt_slim,
    intercept_static_slashes, intercept_response_cache,
)


# ── Mock upstream — pretends to be ollama ──────────────────────────


class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Records every request that reaches it. Returns a static
    pretend-LLM response so the proxy can exercise the forward
    path. The recorded list is shared via the server instance."""

    def log_message(self, *a, **k): pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({   # type: ignore[attr-defined]
            "path":   self.path,
            "method": "POST",
            "body":   body,
        })
        # Respond with a tiny non-streaming chat completion
        payload = json.dumps({
            "id":      "mock-resp",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   "mock-model",
            "choices": [{
                "index":         0,
                "message":       {"role": "assistant",
                                    "content": "(mock upstream response)"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockUpstreamServer(socketserver.ThreadingMixIn,
                          http.server.HTTPServer):
    daemon_threads = True
    requests: list   # populated by handler


def start_mock_upstream() -> tuple[int, MockUpstreamServer]:
    server = MockUpstreamServer(("127.0.0.1", 0), MockUpstreamHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


# ── Test harness ───────────────────────────────────────────────────


PASS = "\x1b[32m✓\x1b[0m"
FAIL = "\x1b[31m✗\x1b[0m"


class Tally:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"  {PASS}  {name}")
            self.passed += 1
        else:
            print(f"  {FAIL}  {name}  — {detail}")
            self.failed += 1


def post_json(url: str, payload: dict, timeout: float = 5.0) -> tuple[int, bytes, dict]:
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


# ── Tests ──────────────────────────────────────────────────────────


def main() -> int:
    print("=== llm_proxy automated test ===\n")
    t = Tally()

    # Mock upstream + proxy in the same process. Cache is fresh
    # per-run so replay tests aren't polluted.
    upstream_port, upstream_srv = start_mock_upstream()
    upstream_url = f"http://127.0.0.1:{upstream_port}"

    audit_path = Path(tempfile.mktemp(suffix=".jsonl"))
    audit = AuditLogger(audit_path)

    proxy_port, proxy_srv = start_proxy(
        upstream_url=upstream_url,
        host="127.0.0.1", port=0,
        audit=audit,
    )
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    print(f"  upstream :{upstream_port}  proxy :{proxy_port}\n")

    # Test 1: /sys command short-circuits
    print("[1] /sys interceptor")
    status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [{"role": "user", "content": "/sysscroll-down 6"}],
    })
    upstream_count_before = len(upstream_srv.requests)
    parsed = json.loads(body)
    t.check("status 200", status == 200, f"got {status}")
    t.check("upstream NOT contacted",
            len(upstream_srv.requests) == upstream_count_before,
            f"upstream got {len(upstream_srv.requests) - upstream_count_before} requests")
    t.check("response has choices[0].message.content",
            "choices" in parsed and parsed["choices"][0]["message"]["content"],
            f"body={body[:100]!r}")
    t.check("response has finish_reason=stop",
            parsed["choices"][0]["finish_reason"] == "stop",
            f"got {parsed['choices'][0].get('finish_reason')}")
    t.check("response has created timestamp",
            "created" in parsed and isinstance(parsed["created"], int),
            f"created={parsed.get('created')}")
    t.check("response has usage",
            "usage" in parsed and parsed["usage"].get("total_tokens"),
            f"usage={parsed.get('usage')}")
    print()

    # Test 2: /sys streaming
    print("[2] /sys streaming SSE shape")
    body_bytes = b""
    req = urllib.request.Request(
        f"{proxy_url}/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "model": "llama3.2", "stream": True,
            "messages": [{"role": "user", "content": "/sysstats"}],
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body_bytes = resp.read()
    chunks = [c for c in body_bytes.split(b"\n\n") if c.strip()]
    t.check("multiple SSE chunks", len(chunks) >= 4, f"got {len(chunks)}")
    t.check("ends with [DONE]",
            any(b"[DONE]" in c for c in chunks[-2:]),
            f"last chunks: {chunks[-2:]}")
    # Parse first 3 data: chunks
    parsed_chunks = []
    for c in chunks:
        if c.startswith(b"data: ") and b"[DONE]" not in c:
            try: parsed_chunks.append(json.loads(c[6:]))
            except Exception: pass
    if parsed_chunks:
        first = parsed_chunks[0]
        t.check("first chunk has role=assistant",
                first["choices"][0]["delta"].get("role") == "assistant",
                f"first delta={first['choices'][0]['delta']}")
        t.check("all chunks have created+id+model",
                all("created" in c and "id" in c and "model" in c
                    for c in parsed_chunks),
                "missing fields")
        # finish_reason should appear in exactly one chunk
        finish = [c for c in parsed_chunks
                  if c["choices"] and c["choices"][0].get("finish_reason") == "stop"]
        t.check("exactly one finish_reason=stop chunk",
                len(finish) == 1, f"got {len(finish)}")
        # usage chunk
        usage = [c for c in parsed_chunks if "usage" in c]
        t.check("has usage chunk", len(usage) == 1, f"got {len(usage)}")
    print()

    # Test 3: Static slash /menu — match on .md body content
    print("[3] /menu static slash (matches on .md body)")
    upstream_count_before = len(upstream_srv.requests)
    menu_body = ("Call `list_slash_commands`. Render the result as-is — "
                 "it's already grouped by prefix family.")
    status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [{"role": "user", "content": menu_body}],
    })
    parsed = json.loads(body)
    t.check("status 200", status == 200)
    t.check("upstream NOT contacted",
            len(upstream_srv.requests) == upstream_count_before)
    content = parsed["choices"][0]["message"]["content"]
    t.check("response content lists slash commands",
            content.count("/") >= 5 and "/sys" in content,
            f"content={content[:120]!r}")
    print()

    # Test 4: Non-/sys forwards transparently
    print("[4] forward path (non-sys query)")
    upstream_count_before = len(upstream_srv.requests)
    status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [{"role": "user", "content": "What is 2+2?"}],
    })
    parsed = json.loads(body)
    t.check("status 200", status == 200)
    t.check("upstream WAS contacted",
            len(upstream_srv.requests) == upstream_count_before + 1,
            f"upstream count diff: {len(upstream_srv.requests) - upstream_count_before}")
    t.check("response is mock-upstream's",
            "mock upstream" in parsed["choices"][0]["message"]["content"].lower(),
            f"content={parsed['choices'][0]['message']['content']!r}")
    print()

    # Test 5: Audit log captures everything
    print("[5] audit log")
    time.sleep(0.1)   # let async writes flush
    audit_lines = audit_path.read_text().splitlines()
    audit_entries = [json.loads(line) for line in audit_lines if line.strip()]
    t.check("audit recorded all requests",
            len(audit_entries) >= 4,
            f"got {len(audit_entries)} audit entries, expected >= 4")
    intercepts = [e for e in audit_entries
                  if e.get("intercepted_by") in (
                      "intercept_sys_commands", "intercept_static_slashes",
                  )]
    t.check("audit shows interceptors fired",
            len(intercepts) >= 3, f"got {len(intercepts)}")
    forwards = [e for e in audit_entries if e.get("intercepted_by") == "forward"]
    t.check("audit shows forward fired", len(forwards) >= 1)
    t.check("intercepts have low duration_ms (< 100)",
            all(e.get("duration_ms", 999) < 100 for e in intercepts),
            f"max={max((e.get('duration_ms', 0) for e in intercepts), default=0)}")
    print()

    # Test 6: --no-llm flag bypass
    print("[6] --no-llm flag interceptor")
    upstream_count_before = len(upstream_srv.requests)
    status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [{"role": "user",
                      "content": "Tell me about quantum mechanics --no-llm"}],
    })
    parsed = json.loads(body)
    t.check("status 200", status == 200)
    t.check("upstream NOT contacted",
            len(upstream_srv.requests) == upstream_count_before,
            f"upstream was hit {len(upstream_srv.requests) - upstream_count_before} times")
    t.check("response mentions --no-llm bypass",
            "no-llm" in parsed["choices"][0]["message"]["content"].lower(),
            f"content={parsed['choices'][0]['message']['content']!r}")
    print()

    # Test 7: Response cache replays identical prompts
    print("[7] response cache")
    pass   # actual test below; keeping label number stable

    # Test 7a: .md-as-skill — exec frontmatter runs subprocess
    print("[7a] .md-as-skill (exec: frontmatter)")
    # Make a temp .opencode/command/echo-test.md and CD into a temp
    # workspace to test isolation.
    import os as _os
    tmp_workspace = Path(tempfile.mkdtemp())
    cmd_dir = tmp_workspace / ".opencode" / "command"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "echo-test.md").write_text(
        "---\n"
        "description: echo whatever args were passed\n"
        "exec: echo hello-from-skill $ARGS\n"
        "---\n"
        "Run the echo skill. $ARGS\n"
    )
    saved_cwd = _os.getcwd()
    _os.chdir(tmp_workspace)
    try:
        upstream_count_before = len(upstream_srv.requests)
        # opencode would inline the body with $ARGS substituted —
        # simulate by sending the body with user args appended.
        status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
            "model": "llama3.2", "stream": False,
            "messages": [{"role": "user",
                          "content": "Run the echo skill. world"}],
        })
        parsed = json.loads(body)
        t.check("status 200", status == 200)
        t.check("upstream NOT contacted",
                len(upstream_srv.requests) == upstream_count_before,
                f"diff={len(upstream_srv.requests) - upstream_count_before}")
        content = parsed["choices"][0]["message"]["content"]
        t.check("output contains echo result",
                "hello-from-skill world" in content,
                f"content={content[:200]!r}")
    finally:
        _os.chdir(saved_cwd)
        import shutil as _shu
        _shu.rmtree(tmp_workspace, ignore_errors=True)
    print()

    # Test 7b: qwen3 /no_think injection
    print("[7b] qwen3 /no_think injection")
    upstream_count_before = len(upstream_srv.requests)
    status, body, _ = post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "qwen3:4b", "stream": False,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user",   "content": "What is 5+5?"},
        ],
    })
    t.check("status 200", status == 200)
    t.check("upstream WAS contacted (no_think mutates and forwards)",
            len(upstream_srv.requests) == upstream_count_before + 1,
            f"diff={len(upstream_srv.requests) - upstream_count_before}")
    last_req = upstream_srv.requests[-1]
    forwarded_body = json.loads(last_req["body"])
    last_user = next((m for m in reversed(forwarded_body["messages"])
                       if m.get("role") == "user"), None)
    t.check("last user message has /no_think directive",
            last_user and "/no_think" in last_user.get("content", ""),
            f"user={last_user}")
    # Idempotence: send a request where /no_think is already in user msg
    upstream_count_before = len(upstream_srv.requests)
    post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "qwen3:4b", "stream": False,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user",   "content": "What is 6+6?\n\n/no_think"},
        ],
    })
    last_req = upstream_srv.requests[-1]
    forwarded_body = json.loads(last_req["body"])
    last_user = next((m for m in reversed(forwarded_body["messages"])
                       if m.get("role") == "user"), None)
    occurrences = last_user.get("content", "").count("/no_think")
    t.check("no_think not duplicated when already present",
            occurrences == 1,
            f"got {occurrences} occurrences")
    # Non-qwen3 model: no injection
    upstream_count_before = len(upstream_srv.requests)
    post_json(f"{proxy_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user",   "content": "What is 7+7?"},
        ],
    })
    last_req = upstream_srv.requests[-1]
    forwarded_body = json.loads(last_req["body"])
    last_user = next((m for m in reversed(forwarded_body["messages"])
                       if m.get("role") == "user"), None)
    t.check("non-qwen3 model: no /no_think injection",
            "/no_think" not in last_user.get("content", ""),
            f"user={last_user}")
    print()

    # Test 8: proxy_local_only catch-all (with custom interceptors)
    print("[8] intercept_local_only catch-all")
    from org_llm.llm_proxy import intercept_local_only, DEFAULT_INTERCEPTORS
    audit2 = AuditLogger(Path(tempfile.mktemp(suffix=".jsonl")))
    upstream2_port, upstream2_srv = start_mock_upstream()
    interceptors2 = list(DEFAULT_INTERCEPTORS) + [intercept_local_only]
    proxy2_port, proxy2_srv = start_proxy(
        upstream_url=f"http://127.0.0.1:{upstream2_port}",
        host="127.0.0.1", port=0, interceptors=interceptors2,
        audit=audit2,
    )
    proxy2_url = f"http://127.0.0.1:{proxy2_port}"
    upstream2_count_before = len(upstream2_srv.requests)
    # Use a unique prompt — test 4 cached "What is 2+2?" earlier
    # and the cache interceptor would short-circuit before our
    # local-only catch-all gets a chance.
    status, body, _ = post_json(f"{proxy2_url}/v1/chat/completions", {
        "model": "llama3.2", "stream": False,
        "messages": [{"role": "user",
                      "content": "Tell me a unique-prompt-for-local-only-test joke"}],
    })
    parsed = json.loads(body)
    t.check("status 200", status == 200)
    t.check("upstream NOT contacted (local-only blocks all)",
            len(upstream2_srv.requests) == upstream2_count_before,
            f"upstream got {len(upstream2_srv.requests) - upstream2_count_before} requests")
    t.check("response mentions local-only mode",
            "local-only" in parsed["choices"][0]["message"]["content"].lower()
            or "no-llm mode" in parsed["choices"][0]["message"]["content"].lower(),
            f"content={parsed['choices'][0]['message']['content']!r}")
    proxy2_srv.shutdown()
    upstream2_srv.shutdown()
    print()
    upstream_count_before = len(upstream_srv.requests)
    payload = {"model": "llama3.2", "stream": False,
               "messages": [{"role": "user", "content": "Cache me!"}]}
    # First call — should forward
    post_json(f"{proxy_url}/v1/chat/completions", payload)
    after_first = len(upstream_srv.requests)
    t.check("first call forwards", after_first == upstream_count_before + 1)
    # Second identical call — should hit cache
    post_json(f"{proxy_url}/v1/chat/completions", payload)
    after_second = len(upstream_srv.requests)
    t.check("second identical call hits cache (no upstream)",
            after_second == after_first,
            f"upstream count: {after_first} → {after_second}")
    print()

    # Test 9: Cloud failover on first-byte timeout
    print("[9] cloud failover on first-byte timeout")
    import org_llm.llm_proxy as _lp

    # 9a: stalling upstream — sends headers then sleeps past the
    # configured TTFB threshold. Cloud upstream responds normally.
    class StallingHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                self.rfile.read(length)
            # Send headers, then stall (don't write any body bytes).
            # The proxy's first-byte read will time out.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            time.sleep(3.0)   # > test timeout below

    class StallingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
    stalling_srv = StallingServer(("127.0.0.1", 0), StallingHandler)
    stalling_port = stalling_srv.server_address[1]
    threading.Thread(target=stalling_srv.serve_forever, daemon=True).start()

    cloud_port, cloud_srv = start_mock_upstream()

    # Inject a fake target + tiny TTFB threshold so the test runs
    # in seconds. _resolve_cloud_failover_target normally hits the
    # DB; override for hermetic tests.
    saved_resolver = _lp._resolve_cloud_failover_target
    saved_timeout  = _lp._first_byte_timeout_secs
    _lp._resolve_cloud_failover_target = lambda: {  # type: ignore[assignment]
        "endpoint": f"http://127.0.0.1:{cloud_port}",
        "model":    "fake-cloud-model",
        "api_key":  "test-key-123",
    }
    _lp._first_byte_timeout_secs = lambda: 0.5  # type: ignore[assignment]

    audit3 = AuditLogger(Path(tempfile.mktemp(suffix=".jsonl")))
    proxy3_port, proxy3_srv = start_proxy(
        upstream_url=f"http://127.0.0.1:{stalling_port}",
        host="127.0.0.1", port=0,
        audit=audit3,
    )
    proxy3_url = f"http://127.0.0.1:{proxy3_port}"
    cloud_count_before = len(cloud_srv.requests)
    try:
        status, body, _ = post_json(f"{proxy3_url}/v1/chat/completions", {
            "model": "llama3.2", "stream": False,
            "messages": [{"role": "user",
                          "content": "Failover test prompt unique-9a"}],
        }, timeout=10.0)
        parsed = json.loads(body)
        t.check("failover status 200", status == 200, f"got {status}")
        t.check("cloud upstream WAS contacted",
                len(cloud_srv.requests) == cloud_count_before + 1,
                f"cloud diff={len(cloud_srv.requests) - cloud_count_before}")
        t.check("response is cloud upstream's",
                "mock upstream" in parsed["choices"][0]["message"]["content"].lower(),
                f"content={parsed['choices'][0]['message']['content']!r}")
        # Cloud request must use the failover model, not the original.
        cloud_body = json.loads(cloud_srv.requests[-1]["body"])
        t.check("failover swapped to cloud model",
                cloud_body.get("model") == "fake-cloud-model",
                f"got model={cloud_body.get('model')!r}")
        # Cloud request hits /chat/completions (the OpenAI-shape path),
        # NOT /v1/chat/completions on the cloud — we re-base.
        t.check("failover targets /chat/completions",
                cloud_srv.requests[-1]["path"] == "/chat/completions",
                f"got path={cloud_srv.requests[-1]['path']!r}")
    except Exception as e:
        t.check("failover request completed", False, f"exception: {e}")

    # Audit must show cloud_failover, not forward.
    time.sleep(0.1)
    audit3_lines = audit3.path.read_text().splitlines()
    audit3_entries = [json.loads(l) for l in audit3_lines if l.strip()]
    failover_rows = [e for e in audit3_entries
                     if e.get("intercepted_by") == "cloud_failover"]
    t.check("audit logs cloud_failover label",
            len(failover_rows) >= 1,
            f"intercepted_by values: {[e.get('intercepted_by') for e in audit3_entries]}")

    # 9b: probe path (GET /api/tags) does NOT failover even when stalling.
    # The handler above is POST-only; for this case use a separate
    # stalling server that handles GET. Skip subtle path: just verify
    # by sending a non-eligible request that should pass through the
    # legacy 502 path. We re-use the same stalling server but with a
    # path that the StallingHandler doesn't implement → returns 501,
    # which still proves the failover path didn't grab it.
    cloud_count_before = len(cloud_srv.requests)
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{proxy3_url}/api/tags", method="GET"),
            timeout=5.0,
        )
    except urllib.error.HTTPError:
        pass   # 501 from StallingHandler — expected
    except Exception:
        pass   # any other status is fine; we only care about cloud not being touched
    t.check("probe paths do NOT trigger failover",
            len(cloud_srv.requests) == cloud_count_before,
            f"cloud got {len(cloud_srv.requests) - cloud_count_before} probe(s)")

    # 9c: failover disabled — cloud must NOT be contacted. The
    # stalling upstream behaviour is downstream of this concern (it
    # may eventually return 200/empty or 502; what matters is that
    # we don't silently route through cloud when the user has
    # disabled failover).
    _lp._resolve_cloud_failover_target = lambda: None  # type: ignore[assignment]
    cloud_count_before = len(cloud_srv.requests)
    try:
        post_json(f"{proxy3_url}/v1/chat/completions", {
            "model": "llama3.2", "stream": False,
            "messages": [{"role": "user",
                          "content": "Failover-disabled prompt unique-9c"}],
        }, timeout=5.0)
    except Exception:
        pass   # local response shape doesn't matter for this assertion
    t.check("cloud NOT contacted when failover disabled",
            len(cloud_srv.requests) == cloud_count_before,
            f"cloud got {len(cloud_srv.requests) - cloud_count_before} request(s)")

    # 9d: cloud target failure → 502, not silent success.
    _lp._resolve_cloud_failover_target = lambda: {  # type: ignore[assignment]
        # Point at a port nothing's listening on so the cloud retry
        # fails fast.
        "endpoint": "http://127.0.0.1:1",
        "model":    "fake-cloud-model",
        "api_key":  "test-key-123",
    }
    got_502 = False
    try:
        post_json(f"{proxy3_url}/v1/chat/completions", {
            "model": "llama3.2", "stream": False,
            "messages": [{"role": "user", "content": "Both fail unique-9d"}],
        }, timeout=5.0)
    except urllib.error.HTTPError as e:
        got_502 = (e.code == 502)
    t.check("cloud failure → 502 (no silent loss)", got_502)

    # Restore real resolver + timeout for any later tests.
    _lp._resolve_cloud_failover_target = saved_resolver  # type: ignore[assignment]
    _lp._first_byte_timeout_secs       = saved_timeout   # type: ignore[assignment]
    proxy3_srv.shutdown()
    stalling_srv.shutdown()
    cloud_srv.shutdown()
    print()

    # Cleanup
    proxy_srv.shutdown()
    upstream_srv.shutdown()
    audit_path.unlink(missing_ok=True)

    # Summary
    print(f"=== {t.passed} passed, {t.failed} failed ===")
    return 0 if t.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
