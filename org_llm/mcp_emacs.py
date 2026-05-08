"""MCP-mediated Emacs evaluation via rhblind/emacs-mcp-server v0.7.0.

This module wraps the rhblind emacs-mcp-server (pure-elisp; Unix-socket
+ JSON-RPC 2.0 transport) so the org-llm specialist (and bench) can
drive a *running* Emacs process instead of spawning a fresh
``emacs --batch`` per call. The running-Emacs path matters because:

- ``emacs --batch`` cannot honor user config (org-roam DB, agenda
  files, registered capture templates) without re-bootstrapping each
  call (~600ms × N cells).
- The MCP server enforces a security/permission layer the bench can
  audit (allowed dangerous fns, sensitive-file blocklist).
- BK6-BK15 elisp tests in the R26 boost plan need a real running
  Emacs to answer "does this defcustom validate / does ert pass".

R26 launch checklist P1-18 wires this in alongside the existing
``eval_elisp`` and ``load_elisp_file`` tools (which still work via
``emacs --batch``); the specialist can choose either based on dial
config. New names are prefixed ``mcp_emacs_*`` to avoid breaking R25+.

Usage pattern::

    # 1. ensure_daemon spawns `emacs --daemon=org-llm-mcp` if needed
    #    and triggers `mcp-server-start-unix` inside it.
    sock = ensure_daemon()              # → Path to socket
    cl = EmacsMCPClient(sock).initialize()
    print(cl.eval_elisp("(+ 1 2)"))     # → "3"
    print(cl.read_buffer("*scratch*"))  # → buffer contents
    cl.close()
    # tear down with shutdown_daemon() at round end if you spawned it

The class is sync + per-cell — instantiation is cheap, but the daemon
process is shared across cells in a round so initialization cost is
amortized.

Install path discovery order:
    1. ``$ORG_LLM_EMACS_MCP_DIR``        (explicit override)
    2. ``~/.local/share/org-llm/emacs-mcp-server/`` (recommended)
    3. ``/tmp/emacs-mcp-server/``        (developer scratch)

The server elisp must be loadable via ``-L <dir> -l mcp-server``.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# ── Server install discovery ─────────────────────────────────────────────

_MCP_INSTALL_CANDIDATES = (
    Path(os.environ.get("ORG_LLM_EMACS_MCP_DIR", "")),
    Path.home() / ".local" / "share" / "org-llm" / "emacs-mcp-server",
    Path("/tmp/emacs-mcp-server"),
)


def find_install_dir() -> Optional[Path]:
    """Return the first install dir that has ``mcp-server.el`` in it."""
    for cand in _MCP_INSTALL_CANDIDATES:
        if cand and cand.is_dir() and (cand / "mcp-server.el").is_file():
            return cand
    return None


# ── Daemon lifecycle ─────────────────────────────────────────────────────

DEFAULT_DAEMON_NAME = "org-llm-mcp"


def _query_socket_path(daemon_name: str) -> Optional[Path]:
    """Ask the daemon (via emacsclient) where its MCP socket lives.

    The actual path depends on user config (``mcp-server-socket-directory``,
    ``mcp-server-socket-name``) and the user's Emacs runtime dir, so the
    only honest way is to ask the running daemon. Returns None if the
    daemon isn't reachable yet.
    """
    try:
        cp = subprocess.run(
            ["emacsclient", "-s", daemon_name, "-e",
              "(mcp-server-get-socket-path)"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    # emacs prints elisp strings as "..." — strip the surrounding quotes.
    if len(out) >= 2 and out.startswith('"') and out.endswith('"'):
        out = out[1:-1]
    if not out or out == "nil":
        return None
    p = Path(out)
    return p if p.exists() and stat.S_ISSOCK(p.stat().st_mode) else None


def ensure_daemon(daemon_name: str = DEFAULT_DAEMON_NAME,
                    install_dir: Optional[Path] = None,
                    timeout_s: float = 15.0) -> Path:
    """Start the Emacs MCP daemon if it isn't running; return socket path.

    Idempotent — if a daemon with this name is already up + the MCP
    server is responding on a Unix socket, returns immediately.

    The actual socket path is discovered via ``emacsclient -s NAME -e
    (mcp-server-get-socket-path)`` rather than hardcoded, since the
    server respects the user's ``mcp-server-socket-directory`` and
    ``mcp-server-socket-name`` settings.
    """
    install = install_dir or find_install_dir()
    if install is None:
        raise RuntimeError(
            "emacs-mcp-server install not found; set ORG_LLM_EMACS_MCP_DIR "
            "or clone to ~/.local/share/org-llm/emacs-mcp-server/")

    # Fast path: already running?
    existing = _query_socket_path(daemon_name)
    if existing is not None:
        return existing

    # Whitelist a small set of dangerous functions that R26 elisp/org
    # cell verifiers need (buffer/window navigation). The remaining
    # truly-dangerous fns (delete-file, shell-command, kill-emacs, …)
    # stay blocked. This list is conservative — extend in user config
    # if a BK test needs more.
    allowlist = ("with-current-buffer save-window-excursion "
                  "save-restriction save-excursion")
    boot = (
        f"(progn "
        f"  (add-to-list 'load-path \"{install}\") "
        f"  (add-to-list 'load-path \"{install}/tools\") "
        f"  (require 'mcp-server) "
        f"  (setq mcp-server-security-allowed-dangerous-functions "
        f"        '({allowlist})) "
        f"  (mcp-server-start-unix))")

    # Spawn detached daemon. --daemon=NAME registers an emacsclient
    # server (separate from the MCP Unix socket).
    subprocess.Popen(
        ["emacs", f"--daemon={daemon_name}", "--eval", boot],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        sock = _query_socket_path(daemon_name)
        if sock is not None:
            return sock
        time.sleep(0.25)

    raise RuntimeError(
        f"emacs-mcp-server daemon '{daemon_name}' failed to expose its "
        f"Unix socket within {timeout_s}s "
        f"(check: emacsclient -s {daemon_name} -e (mcp-server-get-socket-path))")


def shutdown_daemon(daemon_name: str = DEFAULT_DAEMON_NAME) -> bool:
    """Tear down the daemon (round-end). Returns True if a kill was sent."""
    try:
        subprocess.run(
            ["emacsclient", "-s", daemon_name, "-e", "(kill-emacs)"],
            capture_output=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── JSON-RPC 2.0 client ──────────────────────────────────────────────────

@dataclass
class _Response:
    ok: bool
    text: str               # human-readable result/content
    raw: Optional[dict]     # full JSON-RPC envelope


class EmacsMCPClient:
    """Sync JSON-RPC 2.0 client over Unix socket.

    Stateless across calls except for the initialize handshake. Safe to
    instantiate per-cell; reuse if you want to skip the handshake.
    """

    def __init__(self, socket_path: Path | str, timeout_s: float = 30.0):
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s
        self._sock: Optional[socket.socket] = None
        self._next_id = 1
        self._initialized = False

    # ── connection ──
    def connect(self) -> "EmacsMCPClient":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        s.connect(str(self.socket_path))
        self._sock = s
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "EmacsMCPClient":
        return self.connect().initialize()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── transport ──
    def _send_recv(self, msg: dict) -> dict:
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._sock.sendall(line)
        # Read a single newline-terminated JSON message.
        buf = bytearray()
        while b"\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("socket closed before response")
            buf += chunk
        line, _, _ = bytes(buf).partition(b"\n")
        return json.loads(line.decode("utf-8"))

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        msg: dict[str, Any] = {
            "jsonrpc": "2.0", "id": self._next_id, "method": method,
        }
        self._next_id += 1
        if params is not None:
            msg["params"] = params
        return self._send_recv(msg)

    # ── handshake ──
    def initialize(self) -> "EmacsMCPClient":
        if self._initialized:
            return self
        resp = self._call("initialize", {
            "protocolVersion": "draft",
            "capabilities": {},
            "clientInfo": {"name": "org-llm-specialist", "version": "0.1.0"},
        })
        if "result" not in resp:
            err = (resp.get("error") or {}).get("message", "unknown")
            raise RuntimeError(f"initialize failed: {err}")
        # MCP requires an `initialized` notification post-handshake.
        self._notify("notifications/initialized")
        self._initialized = True
        return self

    # ── tool surface ──
    def call_tool(self, tool_name: str, arguments: dict) -> _Response:
        """Invoke a tool by name. Returns (ok, text, raw)."""
        resp = self._call("tools/call", {
            "name": tool_name, "arguments": arguments,
        })
        if "error" in resp:
            err = resp["error"].get("message", "unknown error")
            return _Response(False, f"ERROR: {err}", resp)
        result = resp.get("result") or {}
        # MCP tool responses use {"content": [{"type": "text", "text": ...}]}.
        chunks = []
        for item in (result.get("content") or []):
            if item.get("type") == "text":
                chunks.append(item.get("text", ""))
        return _Response(not result.get("isError", False),
                         "\n".join(chunks),
                         resp)

    # ── high-level helpers ──
    def eval_elisp(self, expression: str) -> str:
        """Evaluate an elisp expression; return its printed result."""
        return self.call_tool("eval-elisp", {"expression": expression}).text

    def read_buffer(self, name: str) -> str:
        """Read a buffer's contents via eval-elisp wrapper.

        rhblind v0.7.0 doesn't expose a dedicated read-buffer tool —
        we fall back to ``with-current-buffer`` + ``buffer-string``.
        """
        expr = (f'(with-current-buffer (get-buffer "{name}") '
                f'(buffer-string))')
        return self.eval_elisp(expr)

    def list_buffers(self) -> list[str]:
        """List all live buffer names."""
        expr = "(mapcar #'buffer-name (buffer-list))"
        text = self.eval_elisp(expr)
        # Result is printed elisp list like ("*scratch*" " *Minibuf-0*" ...).
        # Cheap parse: strip parens, split on space-quote, drop quotes.
        text = text.strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        names = []
        for tok in text.split('"'):
            tok = tok.strip()
            if tok and not tok.startswith("("):
                names.append(tok)
        # Filter junk between strings (the splits leave " " etc.)
        return [n for n in names if n and not n.isspace()]

    def find_file(self, path: str) -> bool:
        """Open a file in the daemon (no-op visit)."""
        expr = f'(progn (find-file "{path}") t)'
        resp = self.call_tool("eval-elisp", {"expression": expr})
        return resp.ok and "t" in resp.text.lower()

    def execute_command(self, name: str, args: Optional[list] = None) -> str:
        """Run an interactive command via ``call-interactively``-equivalent."""
        if args:
            arg_str = " ".join(json.dumps(a) for a in args)
            expr = f"({name} {arg_str})"
        else:
            expr = f"(call-interactively '{name})"
        return self.eval_elisp(expr)

    def get_diagnostics(self, file_path: Optional[str] = None,
                          severity: Optional[str] = None) -> dict:
        """Native rhblind tool — flycheck/flymake diagnostics aggregator."""
        args: dict[str, Any] = {}
        if file_path: args["file_path"] = file_path
        if severity: args["severity"] = severity
        resp = self.call_tool("get-diagnostics", args)
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            return {"raw": resp.text, "ok": resp.ok}


# ── Module-level convenience (for specialist.py dispatch) ────────────────
# These match the existing `eval_elisp` / `load_elisp_file` dispatch
# style: (ok: bool, observation: str). See specialist._dispatch_tool_call.

_GLOBAL_CLIENT: Optional[EmacsMCPClient] = None


def _get_client() -> EmacsMCPClient:
    """Lazily spawn daemon + connect. Cached for the process lifetime."""
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None or _GLOBAL_CLIENT._sock is None:
        sock = ensure_daemon()
        _GLOBAL_CLIENT = EmacsMCPClient(sock).connect().initialize()
    return _GLOBAL_CLIENT


def reset_client() -> None:
    """Tear down the cached client. Used by tests + at round end."""
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is not None:
        _GLOBAL_CLIENT.close()
        _GLOBAL_CLIENT = None


def dispatch_eval_elisp(args: dict) -> tuple[bool, str]:
    """Specialist-side dispatch for ``mcp_emacs_eval_elisp`` tool."""
    code = args.get("code") or args.get("expression") or ""
    if not code:
        return False, "missing code"
    try:
        cl = _get_client()
        resp = cl.call_tool("eval-elisp", {"expression": code})
        return resp.ok, resp.text[:4000]
    except Exception as exc:
        return False, f"mcp_emacs_eval_elisp error: {exc.__class__.__name__}: {exc}"


def dispatch_read_buffer(args: dict) -> tuple[bool, str]:
    name = args.get("name") or ""
    if not name:
        return False, "missing buffer name"
    try:
        cl = _get_client()
        return True, cl.read_buffer(name)[:4000]
    except Exception as exc:
        return False, f"mcp_emacs_read_buffer error: {exc.__class__.__name__}: {exc}"


def dispatch_list_buffers(_args: dict) -> tuple[bool, str]:
    try:
        cl = _get_client()
        names = cl.list_buffers()
        return True, "\n".join(names)
    except Exception as exc:
        return False, f"mcp_emacs_list_buffers error: {exc.__class__.__name__}: {exc}"


def dispatch_find_file(args: dict) -> tuple[bool, str]:
    path = args.get("path") or ""
    if not path:
        return False, "missing path"
    try:
        cl = _get_client()
        ok = cl.find_file(path)
        return ok, "OK" if ok else "FAIL"
    except Exception as exc:
        return False, f"mcp_emacs_find_file error: {exc.__class__.__name__}: {exc}"


def dispatch_execute_command(args: dict) -> tuple[bool, str]:
    name = args.get("name") or ""
    cmd_args = args.get("args") or []
    if not name:
        return False, "missing command name"
    try:
        cl = _get_client()
        return True, cl.execute_command(name, cmd_args)[:4000]
    except Exception as exc:
        return False, f"mcp_emacs_execute_command error: {exc.__class__.__name__}: {exc}"


def dispatch_get_diagnostics(args: dict) -> tuple[bool, str]:
    try:
        cl = _get_client()
        d = cl.get_diagnostics(args.get("file_path"), args.get("severity"))
        return True, json.dumps(d)[:4000]
    except Exception as exc:
        return False, f"mcp_emacs_get_diagnostics error: {exc.__class__.__name__}: {exc}"


# ── Tool specs (for specialist.BROAD_TOOLS_FULL composition) ─────────────

MCP_EMACS_EVAL_ELISP_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_eval_elisp",
        "description": (
            "Evaluate an elisp expression in a *running* Emacs daemon "
            "via the MCP protocol (rhblind/emacs-mcp-server). Unlike "
            "`eval_elisp` (which spawns `emacs --batch` per call and "
            "loses state), this preserves buffers, org-roam DB, agenda "
            "state across calls. Use when you need editor-side truth "
            "(parsed AST, font-lock, derived modes) or when warmup "
            "cost would dominate `emacs --batch`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Elisp expression to evaluate.",
                },
            },
            "required": ["code"],
        },
    },
}

MCP_EMACS_READ_BUFFER_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_read_buffer",
        "description": (
            "Read a named buffer's contents from the running Emacs "
            "daemon. Useful for reading *Messages*, *scratch*, or any "
            "buffer the daemon has opened (e.g. via mcp_emacs_find_file)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                          "description": "Buffer name (e.g. \"*scratch*\")."},
            },
            "required": ["name"],
        },
    },
}

MCP_EMACS_LIST_BUFFERS_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_list_buffers",
        "description": "List all live buffer names in the running Emacs daemon.",
        "parameters": {"type": "object", "properties": {}},
    },
}

MCP_EMACS_FIND_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_find_file",
        "description": (
            "Open a file in the running Emacs daemon (visit-file). "
            "Subsequent mcp_emacs_read_buffer / mcp_emacs_eval_elisp "
            "calls will see the buffer."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

MCP_EMACS_EXECUTE_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_execute_command",
        "description": (
            "Run an interactive Emacs command in the running daemon "
            "(equivalent to M-x NAME)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                          "description": "Command name, e.g. \"org-agenda\""},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    },
}

MCP_EMACS_GET_DIAGNOSTICS_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_emacs_get_diagnostics",
        "description": (
            "Aggregate flycheck/flymake diagnostics across project "
            "buffers in the running Emacs daemon. Optional file_path "
            "scopes to one file; optional severity filters by "
            "\"error\"/\"warning\"/\"info\". Returns JSON."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "severity": {"type": "string",
                              "enum": ["error", "warning", "info"]},
            },
        },
    },
}

MCP_EMACS_TOOLS = [
    MCP_EMACS_EVAL_ELISP_TOOL,
    MCP_EMACS_READ_BUFFER_TOOL,
    MCP_EMACS_LIST_BUFFERS_TOOL,
    MCP_EMACS_FIND_FILE_TOOL,
    MCP_EMACS_EXECUTE_COMMAND_TOOL,
    MCP_EMACS_GET_DIAGNOSTICS_TOOL,
]


# ── Smoke test (run as `python -m org_llm.mcp_emacs`) ────────────────────

def _smoke() -> int:
    print(f"Looking for emacs-mcp-server install...")
    install = find_install_dir()
    if install is None:
        print("FAIL: install not found in any candidate path.")
        return 2
    print(f"  found: {install}")

    print("Spawning daemon...")
    try:
        sock = ensure_daemon()
    except Exception as exc:
        print(f"FAIL: ensure_daemon: {exc}")
        return 3
    print(f"  socket: {sock}")

    print("Connecting + initializing...")
    try:
        cl = EmacsMCPClient(sock).connect().initialize()
    except Exception as exc:
        print(f"FAIL: connect: {exc}")
        return 4

    print("Calling eval-elisp (+ 1 2)...")
    out = cl.eval_elisp("(+ 1 2)")
    print(f"  result: {out!r}")
    if "3" not in out:
        print("FAIL: expected '3' in result.")
        cl.close()
        return 5

    print("Calling list_buffers()...")
    bufs = cl.list_buffers()
    print(f"  found {len(bufs)} buffers; first 5: {bufs[:5]}")

    print("Calling read_buffer(*scratch*)...")
    scratch = cl.read_buffer("*scratch*")
    print(f"  scratch: {scratch[:80]!r}")

    cl.close()
    print("Tearing down daemon...")
    shutdown_daemon()
    print("OK: smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke())
