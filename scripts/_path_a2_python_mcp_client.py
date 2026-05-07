#!/usr/bin/env python3
"""Path A2 prototype — Python as direct MCP client.

claude-code disappears from the harness entirely. Python crafts JSON-RPC
MCP requests against Agor's `/mcp` HTTP endpoint, using the captain
pilot session's `mcp_token`. Per the rule "Claude is not part of this
app" — only the top-level Claude Code (the human-user-in-test) and the
labeled K5 baseline arm remain Claude-using.

Test:
  1. Create a captain pilot session (POST /sessions).
  2. Initialize MCP via POST /mcp.
  3. Call agor_execute_tool → agor_sessions_spawn for an opencode +
     qwen30 specialist that prints PROTO_DONE.
  4. Verify the specialist runs end-to-end.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
ARTIFACTS = REPO / "scripts/_path_a2_python_mcp_client_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"


def admin_tok():
    return json.loads(TOKEN_FILE.read_text())["accessToken"]


def relogin():
    pw = subprocess.run(["pass", "org-llm/agor/admin-password"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = os.environ.copy()
    env["PATH"] = (f"{os.path.expanduser('~/.npm-global/bin')}:"
                   f"{os.path.expanduser('~/.guix-profile/bin')}:" + env.get("PATH", ""))
    subprocess.run(["agor", "login", "-e", "admin@agor.live", "-p", pw],
                    capture_output=True, env=env, check=True)


def admin_req(method, path, body=None, retries=1):
    """Admin-token REST call (for session create, polling, etc)."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(BASE + path, data=data, method=method,
                headers={"Authorization": f"Bearer {admin_tok()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries:
                relogin()
                continue
            try: err_body = e.read().decode()
            except Exception: err_body = "(no body)"
            print(f"  HTTPError {e.code}: {err_body[:400]}")
            raise


# ── MCP client (JSON-RPC over HTTP) ──────────────────────────────────────
class McpClient:
    """Minimal MCP HTTP client. Streamable-HTTP transport per MCP spec."""

    def __init__(self, base_url: str, mcp_token: str):
        self.base_url = base_url
        self.mcp_token = mcp_token
        self.session_id: str | None = None  # Mcp-Session-Id header
        self.next_id = 1

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.mcp_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _request(self, method: str, params: dict | None = None) -> dict:
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self.next_id,
        }
        self.next_id += 1
        req = urllib.request.Request(self.base_url, method="POST",
                                       data=json.dumps(body).encode(),
                                       headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                # Capture session id if returned
                sid_hdr = resp.headers.get("Mcp-Session-Id")
                if sid_hdr and not self.session_id:
                    self.session_id = sid_hdr
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            try: err_body = e.read().decode()
            except Exception: err_body = "(no body)"
            print(f"  MCP HTTPError {e.code}: {err_body[:400]}")
            raise
        # Streamable-HTTP can return SSE; parse if so
        if "text/event-stream" in content_type:
            # Parse SSE: lines starting with "data: " contain JSON
            parsed = None
            for line in raw.splitlines():
                if line.startswith("data: "):
                    try:
                        parsed = json.loads(line[6:])
                        break
                    except json.JSONDecodeError:
                        pass
            if parsed is None:
                return {"_raw": raw, "_content_type": content_type}
            return parsed
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw, "_content_type": content_type}

    def _notify(self, method: str, params: dict | None = None) -> None:
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        req = urllib.request.Request(self.base_url, method="POST",
                                       data=json.dumps(body).encode(),
                                       headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()  # discard; notifications don't return
        except urllib.error.HTTPError as e:
            print(f"  MCP notify {method} HTTPError {e.code}")

    def initialize(self) -> dict:
        result = self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "path-a2-python-client", "version": "0.1"},
        })
        # Send the initialized notification
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> dict:
        return self._request("tools/list", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {
            "name": name, "arguments": arguments,
        })


# ── Main ─────────────────────────────────────────────────────────────────
print(f"=== PATH A2 — Python as MCP client (no claude-code) ===\n")
relogin()

# 1. Create a worktree
wt_name = f"path-a2-{EPOCH}"
print(f"[1] creating worktree {wt_name}...")
wt = admin_req("POST", f"/repos/{REPO_ID}/worktrees", {
    "name": wt_name, "ref": wt_name, "createBranch": True,
    "sourceBranch": "trunk", "pullLatest": False, "refType": "branch"})
wt_id = wt["worktree_id"]
print(f"    wt_id={wt_id[:18]}")
time.sleep(2)

# 2. Create captain pilot session (gets mcp_token)
print(f"\n[2] creating captain pilot session (claude-code)...")
cap = admin_req("POST", "/sessions", {
    "worktree_id": wt_id, "agentic_tool": "claude-code",
})
cap_id = cap["session_id"]
mcp_token = cap["mcp_token"]
admin_req("PATCH", f"/sessions/{cap_id}",
            {"permission_config": {"mode": "bypassPermissions"}})
print(f"    captain_id={cap_id[:18]}")
print(f"    mcp_token: {mcp_token[:30]}...")

# Note: the captain pilot session is created but NEVER hosts a claude -p.
# We're using its mcp_token from Python directly.

# 3. Initialize MCP from Python
print(f"\n[3] Python-as-MCP-client: initialize...")
client = McpClient(BASE + "/mcp", mcp_token)
init_result = client.initialize()
print(f"    init result keys: {list(init_result.keys())}")
if "result" in init_result:
    server_info = init_result["result"].get("serverInfo", {})
    print(f"    serverInfo: {server_info}")
elif "error" in init_result:
    print(f"    INIT ERROR: {init_result['error']}")
    sys.exit(1)

# 4. List tools (sanity check)
print(f"\n[4] tools/list (should show agor_search_tools + agor_execute_tool)...")
tools_resp = client.list_tools()
if "result" in tools_resp:
    tools = tools_resp["result"].get("tools", [])
    print(f"    found {len(tools)} tools:")
    for t in tools[:5]:
        print(f"      - {t.get('name')}: {(t.get('description') or '')[:80]}")
elif "error" in tools_resp:
    print(f"    tools/list ERROR: {tools_resp['error']}")

# 5. Call agor_execute_tool → agor_sessions_spawn
print(f"\n[5] tools/call → agor_execute_tool → agor_sessions_spawn (opencode + qwen30)...")
spawn_args = {
    "tool_name": "agor_sessions_spawn",
    "arguments": {
        "agenticTool": "opencode",
        "modelConfig": {"provider": "openrouter",
                          "model": "qwen/qwen3-coder-30b-a3b-instruct"},
        "prompt": ("Print exactly the literal text PROTO_A2_DONE on a line by "
                    "itself, then stop. Do not narrate, do not use any tools."),
        "title": "path-a2-probe",
    },
}
spawn_resp = client.call_tool("agor_execute_tool", spawn_args)
print(f"    spawn result keys: {list(spawn_resp.keys())}")
(ARTIFACTS / "spawn_response.json").write_text(json.dumps(spawn_resp, indent=2))

# Extract spawned session_id from result content
spec_sid = None
if "result" in spawn_resp:
    content = spawn_resp["result"].get("content") or []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "text":
            text = blk.get("text", "")
            # Try to find a session_id in the response text
            import re
            m = re.search(r'"session_id"\s*:\s*"([a-f0-9-]{36})"', text)
            if m:
                spec_sid = m.group(1)
            else:
                m2 = re.search(r'([a-f0-9-]{36})', text)
                if m2: spec_sid = m2.group(1)
            print(f"    spawn response text (first 400): {text[:400]}")
elif "error" in spawn_resp:
    print(f"    SPAWN ERROR: {spawn_resp['error']}")

print(f"\n    extracted specialist sid: {spec_sid}")

# 6. Poll specialist to terminal
if spec_sid:
    admin_req("PATCH", f"/sessions/{spec_sid}",
                {"permission_config": {"mode": "bypassPermissions"}})
    print(f"\n[6] polling specialist {spec_sid[:18]} to terminal...")
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    deadline = time.time() + 240
    final_status = None
    while time.time() < deadline:
        try:
            s = admin_req("GET", f"/sessions/{spec_sid}", retries=1)
            st = s.get("status")
            if st in terminal:
                final_status = st
                break
        except Exception:
            pass
        time.sleep(10)
    print(f"    terminal: {final_status}")

    # 7. Read messages
    print(f"\n[7] reading specialist messages...")
    msgs = admin_req("GET", f"/messages?session_id={spec_sid}", retries=1)
    msg_data = msgs.get("data", [])
    print(f"    total: {len(msg_data)} messages")
    asst = [m for m in msg_data if m.get("role") == "assistant"]
    print(f"    assistant: {len(asst)} messages")
    full_text = ""
    for m in asst:
        c = m.get("content_preview") or m.get("content") or ""
        if isinstance(c, str):
            full_text += c
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    full_text += blk.get("text", "")
    proto_done = "PROTO_A2_DONE" in full_text
    print(f"    full assistant text (first 400): {full_text[:400]}")
    print(f"    PROTO_A2_DONE emitted: {proto_done}")

    verdict = {
        "spawn_succeeded": True,
        "specialist_sid": spec_sid,
        "terminal_status": final_status,
        "assistant_messages": len(asst),
        "proto_a2_done_emitted": proto_done,
    }
else:
    verdict = {"spawn_succeeded": False, "reason": "could not extract session_id from MCP response"}

# Summary
print()
print("=" * 60)
print("PATH A2 VERDICT")
print("=" * 60)
print(json.dumps(verdict, indent=2))

(ARTIFACTS / f"summary-{EPOCH}.json").write_text(json.dumps(verdict, indent=2))

if verdict.get("proto_a2_done_emitted"):
    print()
    print("✓ Python-as-MCP-client SUCCESSFULLY spawned an opencode+qwen30 specialist")
    print("  that ran a prompt and emitted the expected token.")
    print("  → claude-code-as-MCP-client can be RETIRED from the harness.")
    print("  → Claude is now strictly EXTERNAL (top-level pilot + K5 baseline only).")
elif verdict.get("spawn_succeeded"):
    print()
    print("? Spawn succeeded but no PROTO_A2_DONE — specialist may have failed silently.")
else:
    print()
    print("✗ Path A2 needs more work — see error above.")
