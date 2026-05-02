"""HTTP proxy between opencode (or any OpenAI-compatible client)
and the upstream LLM endpoint (ollama, OpenRouter, etc.).

Why this exists
---------------
opencode dispatches user messages to the LLM the moment they're
submitted — there's no plugin-side hook that fires *before* the
HTTP request leaves opencode. For the org-llm `/sys*` family of
commands (sidebar scroll, menu listing, model swap, cloud
relaunch, etc.) the LLM call is pure waste — the plugin handles
the action locally. Intercepting at the HTTP layer is the only
place we can keep ollama from waking up at all.

Architecture
------------
We start a small `ThreadingHTTPServer` on localhost when
`org-llm launch` runs, point opencode's provider config at it
(replacing the upstream baseURL), and run a chain of
**interceptors** against every incoming chat-completions request:

    interceptor(request_body) -> ProxyResponse | None

If an interceptor returns a `ProxyResponse`, the proxy short-
circuits: that response is sent back to opencode, the upstream
is never touched. If all interceptors return None, the request
is forwarded transparently to the upstream and the response
streamed back.

The first shipped interceptor is `intercept_sys_commands`, which
detects `/sys*` in the most-recent user message and returns an
empty assistant response — so opencode renders a blank assistant
turn (immediately), the action runs via the plugin, and ollama
stays asleep.

Future interceptors (rough wishlist):
  • route `/menu`, `/help`, `/config` to handwritten responses
    (no-LLM equivalents of the corresponding `.md` slash files)
  • detect long-running prefill and proactively switch to cloud
  • cache known-stable system-prompt prefixes
  • rewrite tool-call schemas the local model gets wrong
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ── Types ───────────────────────────────────────────────────────────


@dataclass
class ProxyResponse:
    """A short-circuited response an interceptor returns instead of
    forwarding upstream. `body_chunks` is the SSE stream content
    when `streaming` is True, or a single JSON-encoded payload
    when False. Headers and status are sent as-is."""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body_chunks: list[bytes] = field(default_factory=list)
    streaming: bool = False


@dataclass
class ProxyRequest:
    """Parsed request passed to interceptors."""
    path: str
    method: str
    headers: dict[str, str]
    body: bytes
    parsed_json: Optional[dict] = None     # populated when JSON
    upstream: Optional[str] = None         # set by _handle so interceptors that need to call upstream directly (e.g. intercept_synth_tool_call) can do so without forwarding the request the normal way


Interceptor = Callable[[ProxyRequest], Optional[ProxyResponse]]


# ── /sys* interceptor ───────────────────────────────────────────────


def _last_user_text(parsed: dict) -> str:
    """Pull the textual content of the most-recent user-role
    message from an OpenAI Chat Completions request payload.
    Handles both string and list-of-parts content shapes."""
    messages = parsed.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text")
                        if isinstance(t, str):
                            texts.append(t)
                return "".join(texts)
            return ""
    return ""


_SYS_NOOP_CONTENT = "✓ handled locally — no LLM call"


def _empty_assistant_response(model: str, streaming: bool,
                                content: str = _SYS_NOOP_CONTENT) -> ProxyResponse:
    """Build a no-op assistant response. Shape matches what real
    Ollama / OpenAI streaming responses emit so opencode's
    session.processor accepts the turn and releases the queue:
       1. role marker (no content)
       2. content chunk(s)
       3. finish_reason chunk
       4. usage chunk (with prompt/completion/total tokens)
       5. [DONE]
    Each chunk includes `created` timestamp + `id`. Earlier
    iterations omitted `created` and `usage` — opencode's session
    queue stayed stuck after the first response, refusing to
    process subsequent messages. Real models emit exactly this
    shape, so matching it removes ambiguity. """
    created = int(time.time())
    chunk_id = f"sys-noop-{created}"
    if streaming:
        def chunk(delta: dict, finish: Optional[str] = None,
                   usage: Optional[dict] = None) -> bytes:
            payload = {
                "id":      chunk_id,
                "object":  "chat.completion.chunk",
                "created": created,
                "model":   model,
                "choices": [{
                    "index":         0,
                    "delta":         delta,
                    "finish_reason": finish,
                }],
            }
            if usage is not None:
                payload["choices"] = []   # usage-only chunk has empty choices
                payload["usage"] = usage
            return ("data: " + json.dumps(payload) + "\n\n").encode()

        chunks = [
            chunk({"role": "assistant"}),
            chunk({"content": content}),
            chunk({}, finish="stop"),
            chunk({}, usage={
                "prompt_tokens":     1,
                "completion_tokens": 1,
                "total_tokens":      2,
            }),
            b"data: [DONE]\n\n",
        ]
        return ProxyResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     # `Connection: close` tells the client (opencode
                     # / our test harness) that the stream is done
                     # after [DONE]. Without it, http.server's
                     # default keep-alive holds the socket open
                     # waiting for another request, and clients
                     # block on reading until the timeout fires.
                     "Connection":    "close"},
            body_chunks=chunks,
            streaming=True,
        )
    payload = {
        "id":      chunk_id,
        "object":  "chat.completion",
        "created": created,
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     1,
            "completion_tokens": 1,
            "total_tokens":      2,
        },
    }
    body = json.dumps(payload).encode()
    return ProxyResponse(
        status=200,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body))},
        body_chunks=[body],
        streaming=False,
    )


def intercept_sys_commands(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Short-circuit /sys* user messages. Returns an empty
    assistant response so opencode renders cleanly without
    contacting the upstream LLM."""
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json)
    if not text.strip().startswith("/sys"):
        return None
    streaming = bool(req.parsed_json.get("stream"))
    model     = req.parsed_json.get("model") or "unknown"
    return _empty_assistant_response(model, streaming)


# ── /sysexport interceptor ─────────────────────────────────────────


# Why the export lives here (not in the plugin):
#
# The plugin previously did the export and tried to surface a
# confirmation in chat via session.prompt({noReply:true}). But:
#   • noReply: true does NOT actually suppress the LLM in opencode
#     1.14.32 — the model picks up the injected line as a user turn
#     and wastes tokens replying to "✓ Chat exported · …" (observed
#     2026-05-02, 11.6s LLM call hallucinating about the file).
#   • Toast-only feedback dismisses too quickly to read the path.
#
# Doing the export in the proxy fixes both: the proxy already
# returns the assistant turn for /sys* commands, so we just enrich
# THAT turn's text with the file path and (optionally) the sidebar
# snapshot inline. opencode renders the assistant message; no
# second user-message round-trip; no LLM call; the path stays
# visible in chat history for as long as the session lives.

_SIDEBAR_FILE_REL = ".opencode/sidebar-status.json"


def _format_export_messages(messages: list) -> tuple[list[str], int]:
    """Render the request's chat history as markdown. Returns
    (lines, count). Skips system/tool messages — the export is for
    sharing chat with another assistant, system prompts are noise.
    Mirrors the format the plugin used to produce so existing
    consumers keep working."""
    out: list[str] = []
    count = 0
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        out.append(f"## {role}")
        content = msg.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif ptype == "tool_use":
                    out.append(f"### tool_call: {part.get('name', '?')}")
                    out.append("```json")
                    try:
                        out.append(json.dumps(part.get("input", {}), indent=2))
                    except Exception:
                        out.append(str(part.get("input")))
                    out.append("```")
                elif ptype == "tool_result":
                    out.append("### tool_result")
                    out.append("```")
                    res = part.get("content") or part.get("result")
                    if isinstance(res, str):
                        out.append(res)
                    else:
                        try:
                            out.append(json.dumps(res, indent=2))
                        except Exception:
                            out.append(str(res))
                    out.append("```")
        out.append("")
        count += 1
    return out, count


def _format_sidebar_snapshot(org_dir: Path) -> list[str]:
    """Read .opencode/sidebar-status.json and render it as markdown
    matching the layout the plugin used to produce. Returns a
    bullet block with VAULT / ACTIVE / HEALTH / ARCHIVE sections."""
    out: list[str] = ["---", "# Sidebar status snapshot", ""]
    try:
        path = org_dir / _SIDEBAR_FILE_REL
        sidebar = json.loads(path.read_text())
        v   = sidebar.get("vault")    or {}
        a   = sidebar.get("active")   or {}
        m   = sidebar.get("model")    or {}
        h   = sidebar.get("hardware") or {}
        mcp = sidebar.get("mcp")      or {}
        acti = sidebar.get("activity") or {}
        tags = sidebar.get("top_tags") or []
        vit  = sidebar.get("vitals")   or []

        out.append("## VAULT")
        out.append(f"- nodes: {v.get('n_nodes', '?')}  "
                   f"({v.get('n_embedded', '?')} indexed, "
                   f"{v.get('pct_embedded', '?')}%)")
        out.append(f"- files: {v.get('n_files', '?')}")
        out.append(f"- org_dir: `{v.get('org_dir', '?')}`")
        out.append("")

        out.append("## ACTIVE")
        out.append(f"- palette: {a.get('palette', '?')}")
        for k in (a.get("knobs") or []):
            out.append(f"- {k.get('name', '?')}: {k.get('level', '?')}")
        out.append(f"- model: {m.get('active', '?')}  "
                   f"(provider: {m.get('provider', '?')}, "
                   f"route: {m.get('route', '?')})")
        out.append("")

        out.append("## HEALTH")
        for vi in vit:
            out.append(f"- {vi.get('name', '?')}: {vi.get('label', '?')}  "
                       f"[{vi.get('status', '?')}]")
        out.append(f"- mcp: {mcp.get('tool_count', '?')} tools "
                   f"(configured: {mcp.get('configured', False)})")
        if h.get("free_ram_gb") is not None:
            out.append(f"- ram: {h['free_ram_gb']} GB free")
        if h.get("vram_gb") is not None:
            out.append(f"- vram: {h['vram_gb']} GB")
        out.append("")

        out.append("## ARCHIVE")
        out.append(f"- last {acti.get('window_days', 7)}d: "
                   f"{acti.get('nodes', 0)} nodes, "
                   f"{acti.get('files', 0)} files")
        if tags:
            out.append("- top tags:")
            for t in tags:
                out.append(f"  - #{t.get('name', '?')}  ({t.get('count', 0)})")
        out.append("")
    except Exception as e:
        out.append(f"*(sidebar JSON unavailable: {e})*")
        out.append("")
    return out


def intercept_sysexport_command(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Specialised handler for `/sysexport [sidebar|full|all]`.
    Writes the chat history to a markdown file and returns the
    file path (and optionally the inline sidebar snapshot) as the
    assistant turn — visible in chat, no LLM call, no plugin-side
    chat-injection round-trip. Ordered BEFORE intercept_sys_commands
    in DEFAULT_INTERCEPTORS so /sysexport hits this handler first
    and falls through to the generic /sys* noop only if /sysexport
    isn't matched."""
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json).strip()
    # `/sysexport`, `/sysexport sidebar`, `/sysexport full`, `/sysexport all`
    parts = text.split()
    if not parts or parts[0] != "/sysexport":
        return None
    include_sidebar = (
        len(parts) >= 2 and parts[1].lower() in ("sidebar", "full", "all")
    )

    org_dir = Path(os.environ.get("ORG_LLM_ORG_DIR")
                    or (Path.home() / "org"))
    out_dir = org_dir / ".opencode"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out_path = out_dir / f"chat-export-{ts}.md"

    messages = req.parsed_json.get("messages") or []
    body_lines, msg_count = _format_export_messages(messages)
    header = [
        "# org-llm chat export",
        f"*{ts}Z*",
        "",
    ]
    sidebar_lines: list[str] = []
    if include_sidebar:
        sidebar_lines = _format_sidebar_snapshot(org_dir)

    file_lines = header + body_lines + sidebar_lines
    try:
        out_path.write_text("\n".join(file_lines))
    except Exception as e:
        # On write failure, return an error noop so the user sees
        # what went wrong instead of silently nothing.
        streaming = bool(req.parsed_json.get("stream"))
        model     = req.parsed_json.get("model") or "unknown"
        return _empty_assistant_response(
            model, streaming,
            content=f"✗ /sysexport failed: {e}",
        )

    # Build the assistant turn text. Always include the confirmation
    # line. If the user asked for sidebar, also embed the rendered
    # snapshot inline so it's visible in chat — that's the whole
    # point of `/sysexport sidebar`.
    sidebar_blurb = " + sidebar snapshot" if include_sidebar else ""
    confirm = (
        f"✓ Chat exported · {msg_count} message(s){sidebar_blurb}\n"
        f"→ {out_path}"
    )
    if include_sidebar:
        # Embed the same sidebar markdown that's in the file. Two
        # blank lines between confirmation and snapshot for clean
        # rendering.
        confirm = confirm + "\n\n" + "\n".join(sidebar_lines)

    streaming = bool(req.parsed_json.get("stream"))
    model     = req.parsed_json.get("model") or "unknown"
    return _empty_assistant_response(model, streaming, content=confirm)


# ── /sysscreenshot interceptor ─────────────────────────────────────


# Like intercept_sysexport_command, this short-circuits the
# /sysscreenshot user command at the proxy layer so:
#   • The local LLM is never engaged (intercept_sys_commands above
#     catches it too, but this one runs FIRST and returns the
#     screenshot path as the assistant content).
#   • The plugin doesn't have to inject anything via session.prompt
#     — that path triggers an LLM follow-up on opencode 1.14.32
#     even with noReply: true, so injected output paths used to
#     generate spurious "I see you took a screenshot at /path"
#     follow-up turns from the model.
#
# Architectural pattern: any /sys* command that needs to surface
# OUTPUT (vs. just trigger a side-effect with no useful return)
# should follow this shape — handle it in the proxy, return the
# output as the assistant turn. The plugin's runAndInject path is
# fine for commands the user can stomach a chatty LLM follow-up on
# (sysdoctor, sysstats, sysmodels, sysrecent — long output the LLM
# may helpfully summarise) but that's not a guarantee anyone
# should rely on.


def intercept_sysscreenshot_command(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Handle /sysscreenshot fully on the proxy side. Spawns
    `org-llm screenshot --label opencode` (which honours the
    user's screenshot_tool config), captures the resulting path,
    and returns it as the assistant turn — no LLM call, no chat
    injection round-trip."""
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json).strip()
    parts = text.split()
    if not parts or parts[0] != "/sysscreenshot":
        return None

    streaming = bool(req.parsed_json.get("stream"))
    model     = req.parsed_json.get("model") or "unknown"

    # Allow optional --label or --tool overrides typed inline.
    extra_args = parts[1:]
    cmd = ["org-llm", "screenshot"]
    if "--label" not in extra_args and "-l" not in extra_args:
        cmd.extend(["--label", "opencode"])
    cmd.extend(extra_args)

    import subprocess as _sp
    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=30)
    except _sp.TimeoutExpired:
        return _empty_assistant_response(
            model, streaming,
            content="✗ /sysscreenshot timed out after 30s. "
                     "If using the emacs backend, make sure "
                     "(server-start) is running in your Emacs.",
        )
    except Exception as e:
        return _empty_assistant_response(
            model, streaming,
            content=f"✗ /sysscreenshot failed to spawn: {e}",
        )

    # `org-llm screenshot` prints "✓ screenshot via X: <path>" or
    # an error line on failure. Surface stdout (with `▶` prefix
    # markers stripped — those are CLI on_screen() decorators) as
    # the assistant content so the user sees the result inline.
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        body = (f"✗ /sysscreenshot exited {result.returncode}. "
                f"{err or out or '(no output)'}")
    else:
        # Strip the leading `▶` indent that on_screen() adds.
        cleaned = "\n".join(
            line.lstrip("▶").lstrip()
            for line in out.splitlines()
        ) if out else "✓ screenshot captured."
        body = cleaned

    return _empty_assistant_response(model, streaming, content=body)


# ── Force-tool-call prefix: `:tool <name> [query]` ─────────────────


# Lets the user pin the LLM to a specific tool without negotiating.
# Typing `:tool search_notes phase 18 notes` rewrites the request
# to set `tool_choice: {type: "function", function: {name:
# "search_notes"}}` and strips the `:tool <name>` prefix from the
# user message so the LLM composes args from "phase 18 notes" alone.
#
# Why: the LLM's default "should I call a tool, and if so which?"
# decision step adds latency AND occasional wrong-tool picks
# (search when the user wanted code_search, etc.). With the prefix,
# the model is committed — it MUST call that tool. Useful as a
# typed shortcut for power users who know which tool fits.
#
# This is a MUTATION interceptor — it modifies req.body in place
# and returns None so the request still forwards (to local or
# cloud, whichever the chain decides). The cloud-failover and
# cloud-first paths inherit the pinned tool_choice for free.
#
# If the named tool isn't in the request's tools array, returns
# an error response naming the available tools so the user can
# correct the typo without the LLM in the loop.


def intercept_force_tool_call(req: ProxyRequest) -> Optional[ProxyResponse]:
    """`:tool <name>` prefix → force `tool_choice` for that function.
    Mutates req in place; returns None to fall through to the
    forward path (or an error response if the tool isn't valid)."""
    if not req.path.endswith("/chat/completions"):
        return None
    parsed = req.parsed_json
    if not parsed:
        return None
    import re as _re_mod
    prefix_re = _re_mod.compile(r'^\s*:tool\s+([A-Za-z_][\w\-]*)\s*')
    text = _last_user_text(parsed)
    m = prefix_re.match(text)
    if not m:
        return None
    tool_name = m.group(1)

    # Validate the tool is actually in this request's tools array.
    tools = parsed.get("tools") or []
    available = {
        t.get("function", {}).get("name")
        for t in tools
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }
    available.discard(None)
    if tool_name not in available:
        avail_list = ", ".join(sorted(available))[:300] or "(none — request has no tools)"
        return _empty_assistant_response(
            parsed.get("model") or "unknown",
            bool(parsed.get("stream")),
            content=(
                f"✗ :tool {tool_name} — not in this session's tools.\n"
                f"Available: {avail_list}"
            ),
        )

    # Mutate: pin tool_choice + strip the prefix from the user's text.
    parsed["tool_choice"] = {
        "type": "function",
        "function": {"name": tool_name},
    }
    msgs = parsed.get("messages") or []
    for i in reversed(range(len(msgs))):
        msg = msgs[i]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = prefix_re.sub("", content, count=1)
        elif isinstance(content, list):
            for part in content:
                if (isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and prefix_re.match(part["text"])):
                    part["text"] = prefix_re.sub(
                        "", part["text"], count=1)
                    break
        break

    # Re-encode.
    req.body = json.dumps(parsed).encode()
    req.headers = {k: v for k, v in req.headers.items()
                    if k.lower() != "content-length"}
    return None  # fall through to forward


# ── Synthetic tool calls for tool-incapable models ──────────────────


# Models that don't natively support OpenAI-style tool calling.
# Mirrors `_NO_TOOL_CALL_STEMS` in cli.py — duplicated here because
# cli.py imports llm_proxy, not the other way round, and reversing
# that would force a circular import at startup. Keep them in sync
# by hand; the lists are short and rarely change.
_PROXY_NO_TOOL_STEMS = {
    "gemma", "gemma2", "gemma3",
    "phi3", "phi3.5",
    "llava", "bakllava",
    "deepseek-coder",
}

# Match a `<tool_call>{...}</tool_call>` block emitted by the model
# in response to the synthetic-tools system prompt. The JSON inside
# may span multiple lines (DOTALL) and we want the inner-most match
# so a model that emits prose-then-tool gets the tool block alone.
import re as _re                                          # noqa: E402

_TOOL_CALL_BLOCK_RE = _re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    _re.DOTALL,
)


def _is_no_tool_model(model_id: str) -> bool:
    """True iff the request's model is on the proxy's deny-list."""
    if not model_id:
        return False
    bare = model_id.split("/")[-1]      # strip provider prefix
    stem = bare.split(":")[0].lower()   # strip :tag suffix
    return stem in _PROXY_NO_TOOL_STEMS


def _build_synth_tools_prompt(tools: list) -> str:
    """Render an OpenAI-tools list as a system-prompt block that
    instructs a non-tool-capable model to emit tool calls as
    `<tool_call>{...}</tool_call>` blocks. Conservative phrasing —
    tested on gemma3 to produce a single clean block when a tool
    is the right answer, plain prose otherwise."""
    lines = [
        "You have access to the following tools. To call a tool,",
        "respond with ONLY this exact format and STOP:",
        "",
        "  <tool_call>{\"name\": \"<tool_name>\", \"arguments\": "
        "{...}}</tool_call>",
        "",
        "Do not narrate. Do not explain. Do not add prose around the",
        "tool_call block. If no tool is needed, answer the user",
        "directly without the tool_call wrapper.",
        "",
        "Available tools:",
    ]
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        params = fn.get("parameters") or {}
        try:
            params_str = json.dumps(params, separators=(",", ":"))
        except Exception:
            params_str = "{}"
        lines.append(f"- {name}: {desc}")
        if params_str and params_str != "{}":
            # Show the JSON-Schema inline so the model sees required
            # field names. Trimmed at 400 chars to avoid bloating the
            # context with deep schemas.
            lines.append(f"  schema: {params_str[:400]}")
    return "\n".join(lines)


def _extract_tool_call(text: str) -> Optional[dict]:
    """Pull the FIRST `<tool_call>{json}</tool_call>` block out of
    `text` and return it as a parsed dict. Returns None if no block
    is found or the JSON inside is malformed. Tolerant of trailing
    prose — gemma3 sometimes adds a confirmation line after the
    block, which we just ignore."""
    if not text:
        return None
    match = _TOOL_CALL_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(1))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _build_synth_tool_response(
    model: str, streaming: bool,
    *, content: str = "",
    tool_calls: Optional[list] = None,
) -> ProxyResponse:
    """Build an assistant-turn response that may include OpenAI-shape
    tool_calls. Mirrors `_empty_assistant_response` but allows the
    `message.tool_calls` array (or its delta-equivalent in streaming
    mode) so opencode picks up the synthesised tool invocation as a
    real tool call. Used by intercept_synth_tool_call after pulling
    the tool block out of a non-tool model's reply."""
    created = int(time.time())
    chunk_id = f"synth-tool-{created}"
    if streaming:
        # opencode tolerates tool_calls in either the message OR the
        # delta. We emit a single non-streaming-shaped delta block
        # so the model's "thinking" looks instant — the synth path
        # already buffered the full response upstream, streaming
        # the synthesised tool call piecemeal would just be theatre.
        msg: dict = {"role": "assistant"}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if content:
            msg["content"] = content
        chunks = [
            ("data: " + json.dumps({
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": msg,
                              "finish_reason": None}],
            }) + "\n\n").encode(),
            ("data: " + json.dumps({
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls" if tool_calls
                                                              else "stop"}],
            }) + "\n\n").encode(),
            b"data: [DONE]\n\n",
        ]
        return ProxyResponse(
            status=200,
            headers={"Content-Type":  "text/event-stream",
                     "Cache-Control": "no-cache",
                     "Connection":    "close"},
            body_chunks=chunks,
            streaming=True,
        )
    msg2: dict = {"role": "assistant"}
    if tool_calls:
        msg2["tool_calls"] = tool_calls
    if content:
        msg2["content"] = content
    payload = {
        "id": chunk_id, "object": "chat.completion",
        "created": created, "model": model,
        "choices": [{
            "index": 0,
            "message": msg2,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                   "total_tokens": 2},
    }
    body = json.dumps(payload).encode()
    return ProxyResponse(
        status=200,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body))},
        body_chunks=[body],
        streaming=False,
    )


def intercept_synth_tool_call(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Approximate tool calling for models that don't natively
    support it. When the request targets a deny-listed model AND
    opencode passed `tools`, we:
      1. Strip `tools` from the request body (Ollama would otherwise
         silently ignore them for these models).
      2. Inject a system prompt at the FRONT of `messages` that
         describes each tool and instructs the model to emit
         `<tool_call>{...}</tool_call>` when it wants to call one.
      3. Force `stream: false` so we get a single complete response
         we can parse — streaming would require buffering plus
         partial-block detection, which is out of scope for the
         first cut.
      4. Issue the upstream call directly (req.upstream is set by
         the handler).
      5. Extract any `<tool_call>` block and reshape into proper
         OpenAI tool_calls.
      6. Return a ProxyResponse — short-circuiting the normal forward
         path.

    Caveats: quality varies with the model. gemma3 is the primary
    target. Single-tool calls work; multi-tool turns may misfire.
    Falls back to plain-text passthrough when no tool_call block is
    detected."""
    if not req.path.endswith("/chat/completions"):
        return None
    parsed = req.parsed_json
    if not parsed:
        return None
    model_id = parsed.get("model") or ""
    if not _is_no_tool_model(model_id):
        return None
    tools = parsed.get("tools") or []
    if not isinstance(tools, list) or not tools:
        return None
    if not req.upstream:
        # No upstream URL set → can't issue our own forward call.
        # Fall through; opencode will get whatever ollama returns
        # without the synth prompt (probably plain chat).
        return None

    # 1+2+3: build mutated request body.
    sys_prompt = _build_synth_tools_prompt(tools)
    new_body = {k: v for k, v in parsed.items() if k != "tools"}
    new_body.pop("tool_choice", None)
    new_body["stream"] = False
    msgs = list(new_body.get("messages") or [])
    new_body["messages"] = [{"role": "system", "content": sys_prompt}] + msgs
    body_bytes = json.dumps(new_body).encode()
    streaming = bool(parsed.get("stream"))

    # 4: forward to upstream. Inject /v1 if the upstream is ollama-
    # shape and the path lacks it (mirrors `_forward`'s logic).
    upstream = req.upstream
    path = req.path
    if (path in ("/chat/completions", "/embeddings", "/models")
            and "/v1" not in upstream and "/api" not in upstream):
        path = "/v1" + path
    url = upstream + path
    forward_headers = {
        k: v for k, v in req.headers.items()
        if k.lower() not in ("host", "content-length", "connection")
    }
    forward_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body_bytes, method="POST", headers=forward_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            raw = resp.read()
    except Exception as e:
        # Upstream failed — bail and let opencode handle the error
        # path. Returning None lets the chain fall through to the
        # normal forward (which will hit the same error and
        # surface it the usual way).
        return _empty_assistant_response(
            model_id, streaming,
            content=f"(synth-tool upstream error: {e})",
        )

    # 5: parse upstream response, extract assistant content.
    try:
        upstream_obj = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return _empty_assistant_response(
            model_id, streaming,
            content="(synth-tool: upstream returned non-JSON)",
        )
    choices = upstream_obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return _empty_assistant_response(
            model_id, streaming,
            content="(synth-tool: upstream had no choices)",
        )
    msg0 = choices[0].get("message") or {}
    text = msg0.get("content") if isinstance(msg0.get("content"), str) else ""

    tool_call_obj = _extract_tool_call(text or "")
    if tool_call_obj is None:
        # No tool block — return the plain text as a normal
        # assistant turn. opencode treats this as "the model chose
        # not to call a tool", which is correct.
        return _build_synth_tool_response(
            model_id, streaming, content=text or "",
        )

    # 6: reshape into OpenAI tool_calls structure. opencode expects:
    #   tool_calls: [{ id, type: "function", function: { name, arguments } }]
    # Arguments must be a JSON-encoded STRING (per the OpenAI spec),
    # not an object — opencode parses it back when dispatching.
    name = tool_call_obj.get("name") or ""
    args_obj = (tool_call_obj.get("arguments")
                  or tool_call_obj.get("args") or {})
    try:
        args_str = json.dumps(args_obj)
    except Exception:
        args_str = "{}"
    tool_calls = [{
        "id":   f"call_synth_{int(time.time() * 1000)}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }]
    return _build_synth_tool_response(
        model_id, streaming, tool_calls=tool_calls,
    )


# ── Prefix caching (T1.4) ───────────────────────────────────────────


# What this is: a key-value cache keyed by SHA256 of the request
# payload (full messages + tools + model). If we see an identical
# request twice, we return the cached response without contacting
# the upstream.
#
# What this is NOT: token-level KV-cache reuse (that's an ollama-
# internal optimisation we can't poke from the proxy). This is
# response-level caching: when the same prompt arrives twice, we
# replay the earlier response instead of re-running the model.
#
# Where it helps:
#   • opencode's startup probes (`/api/tags` etc — already cacheable
#     on a different layer; this layer focuses on chat completions)
#   • idempotent "what's the doctor analysis" type queries the user
#     re-runs while debugging
#   • repeat /menu /help if those slashes ever flow through (they
#     shouldn't post-T1.3, but a stray case is now cheap)
#
# Where it WON'T help:
#   • normal chat — every user message is a unique prompt, so
#     prompt-hash never collides
#
# Eviction: simple LRU bounded by entry count (default 128).
# TTL: configurable, default 5 min — long enough to catch
# back-to-back debug cycles, short enough that vault state
# changes invalidate stale answers.

import collections


@dataclass
class _CachedResponse:
    timestamp:   float
    response:    ProxyResponse


class _PrefixCache:
    """Bounded LRU response cache. Thread-safe (proxy handler
    threads share one cache instance via the server). """
    def __init__(self, max_entries: int = 128, ttl_s: float = 300.0):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._data: "collections.OrderedDict[str, _CachedResponse]" = collections.OrderedDict()

    def get(self, key: str) -> Optional[ProxyResponse]:
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if now - entry.timestamp > self.ttl_s:
                self._data.pop(key, None)
                return None
            # LRU touch
            self._data.move_to_end(key)
            return entry.response

    def put(self, key: str, response: ProxyResponse) -> None:
        with self._lock:
            self._data[key] = _CachedResponse(time.time(), response)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


_default_cache = _PrefixCache()


def intercept_response_cache(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Return the cached response if we've seen this exact prompt
    payload recently. Cache writes happen post-forward via the
    handler's _record_response_for_cache hook."""
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    # Only cache non-streaming for now — streaming responses arrive
    # as a sequence of chunks that we'd have to reassemble. Worth
    # doing later; non-streaming covers the common slash-command
    # path which IS the bulk of the value.
    if req.parsed_json.get("stream"):
        return None
    key = _hash_prompt(req.parsed_json)
    if not key:
        return None
    cached = _default_cache.get(key)
    if cached is None:
        return None
    return cached


# ── Static slash responses (T1.3) ───────────────────────────────────


# Many of opencode's project-defined slash commands (in
# .opencode/command/*.md) are LLM-rendered status/info wrappers
# that don't actually need a model — the answer is deterministic
# from local data. We intercept those at the HTTP layer and
# return a handwritten markdown response, exactly the same UX
# pattern as the /sys* short-circuit but for opencode-side
# slashes the user types.
#
# Each entry: a regex matched against the user's last message
# and a function that builds the assistant content. The function
# can shell out, read files, query the DB — whatever produces
# the deterministic answer.

import re as _re
import subprocess as _sp


_StaticBuilder = Callable[[str], str]   # (user_text) -> markdown body


def _build_menu(_user: str) -> str:
    """Mirror of the plugin-side /sysmenu but rendered server-
    side so opencode's LLM-driven /menu also gets the no-LLM
    treatment. Reads .opencode/command/*.md from CWD.

    Output uses LCARS-style box-drawing frames (╭─╮│╰─╯) — the
    same format the plugin's /sysmenu uses, which we've
    confirmed opencode's chat surface renders correctly. Plain
    bulleted markdown was being silently filtered (treated as
    autocomplete pollution); framed content survives.

    Colorful via emoji section markers + grouped frames per
    family. The user mentally prepends "/" — we tell them in the
    closing footer. """
    cwd = Path.cwd()
    cmd_dir = cwd / ".opencode" / "command"

    out: list[str] = []

    def _frame(title: str, lines: list[str]) -> list[str]:
        """Box-drawing frame around `lines`. Width auto-fits to
        the longest line + padding. Same shape as the plugin's
        showToast frame helper. """
        width = max((len(l) for l in lines), default=0)
        width = max(width + 2, len(title) + 6, 56)
        title_pad = width - len(title) - 4
        top = f"╭─ {title} {'─' * max(1, title_pad)}╮"
        bot = f"╰{'─' * width}╯"
        mid = [f"│ {l.ljust(width - 2)} │" for l in lines]
        return [top, *mid, bot]

    if cmd_dir.is_dir():
        names = sorted(p.stem for p in cmd_dir.glob("*.md"))
        families: dict[str, list[str]] = {}
        for n in names:
            family = n.split("-")[0] if "-" in n else "misc"
            families.setdefault(family, []).append(n)

        # Strip leading "/" from listed names — opencode's chat
        # renderer hides lines with multiple "/" patterns. Inside
        # the box-drawing frame the alignment cues + emoji header
        # carry enough visual weight that the slash-less names
        # still read as commands.
        for family in sorted(families):
            count = len(families[family])
            cmds = ", ".join(families[family])
            # Wrap long comma-lists at ~50 chars by chunking into
            # multiple lines so the frame doesn't blow out wide.
            wrapped: list[str] = []
            line = ""
            for cmd in families[family]:
                proposed = (line + ", " + cmd) if line else cmd
                if len(proposed) > 50:
                    wrapped.append(line)
                    line = cmd
                else:
                    line = proposed
            if line:
                wrapped.append(line)
            out.extend(_frame(f"📁 /{family}-* ({count})", wrapped))
            out.append("")

    # Closing card — the org-llm /sys* family's no-LLM equivalents.
    out.extend(_frame("⚡ /sys* — no-LLM equivalents", [
        "/sysmenu     this listing (TUI version)",
        "/sysstats    vault counts",
        "/sysmodels   configured models",
        "/sysdoctor   system health",
        "/syscloud    relaunch via cloud",
        "/sysmodel <name>  switch local model + relaunch",
    ]))
    out.append("")
    out.append("Prepend / to invoke any command. Project commands handled by")
    out.append("the LLM unless their .md has `exec:` frontmatter.")
    return "\n".join(out)


def _build_help(_user: str) -> str:
    """Plain-text help. See _build_menu for why no markdown."""
    rule = "─" * 48
    return ("\n".join([
        "HELP",
        rule,
        "",
        "/sysmenu      list all slash commands",
        "/sysstats     vault counts",
        "/sysmodels    configured models",
        "/sysdoctor    system health",
        "/sysreclaim   free Ollama RAM",
        "/syscloud     relaunch via cloud",
        "",
        "Type /sys <subcommand> to invoke any org-llm CLI command.",
    ]))


def _build_config(_user: str) -> str:
    """Dump the current org-llm config as plain text. Plain
    formatting (no fenced code block, no markdown) so opencode's
    chat renderer displays the whole table — see _build_menu."""
    try:
        out = _sp.run(["org-llm", "config"], capture_output=True,
                       text=True, timeout=5)
        body = out.stdout.strip() or "(empty)"
    except Exception as e:
        body = f"(could not run org-llm config: {e})"
    # Trim ANSI sequences if present
    body = _re.sub(r"\x1b\[[0-9;]*m", "", body)
    rule = "─" * 48
    return f"ORG-LLM CONFIG\n{rule}\n\n{body}"


# Each entry: (matcher, builder). Matcher is called against the
# trimmed user text; first match wins.
#
# Matchers detect TWO forms:
#   1. The literal slash (`/menu`) — user typed it directly
#      somewhere opencode forwards as-is.
#   2. The resolved `.md` prompt body — opencode's project slash
#      commands inline the file content as the user message,
#      not the slash name. We detect by distinctive phrases
#      from the cli.py-generated `.md` bodies.
#
# Distinctive phrases: chosen to be specific enough that no
# normal chat query collides. `list_slash_commands` is a tool
# name only the menu prompt mentions; `get_config`+`set_config`
# co-occur only in the config prompt; etc.
def _is_menu_prompt(t: str) -> bool:
    if t == "/menu":
        return True
    return ("list_slash_commands" in t and
            "grouped" in t.lower())

def _is_help_prompt(t: str) -> bool:
    return t == "/help"

def _is_config_prompt(t: str) -> bool:
    if t == "/config":
        return True
    return ("get_config" in t and "set_config" in t)

_STATIC_SLASH_HANDLERS: list[tuple[Callable[[str], bool], _StaticBuilder]] = [
    (_is_menu_prompt,   _build_menu),
    (_is_help_prompt,   _build_help),
    (_is_config_prompt, _build_config),
]


def intercept_static_slashes(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Catch a small set of opencode-side slashes that are
    LLM-rendered today but trivially deterministic. Bypasses
    ollama entirely; returns the handwritten content as a
    chat-completions response opencode renders as the assistant
    turn.

    NOTE on matching: opencode's project slashes inline the `.md`
    file body as the user message text — so an invocation of
    `/menu` arrives as the prompt body ("Call `list_slash_commands`…")
    NOT the slash literal. Each matcher in `_STATIC_SLASH_HANDLERS`
    handles BOTH forms (literal slash + .md body signature). We
    must NOT pre-filter on text.startswith("/") here — that
    would block the .md-body path. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json).strip()
    if not text:
        return None
    for matcher, builder in _STATIC_SLASH_HANDLERS:
        if matcher(text):
            try:
                content = builder(text)
            except Exception as e:
                content = f"(static handler failed: {e})"
            streaming = bool(req.parsed_json.get("stream"))
            model = req.parsed_json.get("model") or "unknown"
            # Reuse the same multi-chunk SSE shape as the /sys
            # interceptor — the empty-content single-chunk shape
            # caused opencode's session.processor to hang for 5
            # minutes. Three-chunk delta (role, content, finish)
            # is what real models emit and what opencode parses
            # without corner cases.
            return _empty_assistant_response(model, streaming, content=content)
    return None


# ── Probe cache (T2.0) ──────────────────────────────────────────────


# /api/tags, /api/show, /v1/models — opencode hits these at startup
# and on model picker open. They're stable across a session; cache
# the response for 5 minutes so consecutive probes return instantly.
_probe_cache: dict[str, tuple[float, ProxyResponse]] = {}
_PROBE_TTL = 300.0   # seconds


def intercept_probe_cache(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Cache responses to ollama's status-probe endpoints. ollama
    has no caching layer for these and answers from disk every
    time. opencode triggers them on session start AND when the
    user opens the model picker — visible latency. Caching for
    5 min covers the typical "open opencode, futz, close" cycle.

    Probes intercepted (path-based):
       /api/tags    — list pulled models
       /api/show    — model metadata
       /v1/models   — OpenAI-compatible model list
    """
    if req.method != "GET":
        return None
    if not any(p in req.path for p in ("/api/tags", "/api/show", "/v1/models")):
        return None
    now = time.time()
    cached = _probe_cache.get(req.path)
    if cached is not None and now - cached[0] < _PROBE_TTL:
        return cached[1]
    return None


def _probe_cache_record(path: str, status: int, body: bytes,
                        headers: dict[str, str]) -> None:
    """Called from _forward when a probe URL just got a fresh
    response. Stores it for next time. Only caches successful
    JSON responses. """
    if status != 200:
        return
    if not any(p in path for p in ("/api/tags", "/api/show", "/v1/models")):
        return
    _probe_cache[path] = (time.time(), ProxyResponse(
        status=status,
        headers=dict(headers),
        body_chunks=[body],
        streaming=False,
    ))


# ── Time-state grounding (T3.0) ─────────────────────────────────────


def intercept_time_grounding(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Inject current date/time into the system prompt. Without
    this, the LLM has no temporal reference — it'll happily say
    "today is" some random date from training. Real-time aware
    answers are a free win.

    The injection is appended to the FIRST system message, or
    prepended as a new system message if none exists. Idempotent
    via a sentinel string the interceptor checks before adding."""
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    messages = req.parsed_json.get("messages") or []
    if not isinstance(messages, list):
        return None

    SENTINEL = "[org-llm:time]"
    import datetime as _dt
    now = _dt.datetime.now()
    grounding = (f"\n\n{SENTINEL} Current date: {now.strftime('%Y-%m-%d %A')}. "
                 f"Current time: {now.strftime('%H:%M %Z').strip()}.")

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            existing = msg.get("content")
            if isinstance(existing, str):
                if SENTINEL in existing:
                    return None
                msg["content"] = existing.rstrip() + grounding
                req.body = json.dumps(req.parsed_json).encode()
                req.headers = {k: v for k, v in req.headers.items()
                                if k.lower() != "content-length"}
                return None
            break    # found a system msg with non-string content; skip

    # No system message — prepend one with the grounding sentinel.
    messages.insert(0, {"role": "system", "content": grounding.strip()})
    req.body = json.dumps(req.parsed_json).encode()
    req.headers = {k: v for k, v in req.headers.items()
                    if k.lower() != "content-length"}
    return None


# ── PII redaction (T3.1) ────────────────────────────────────────────


_PII_PATTERNS = [
    # API keys (common formats)
    (_re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),     "[REDACTED:openai-key]"),
    (_re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}\b"),    "[REDACTED:slack-token]"),
    (_re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-pat]"),
    (_re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),      "[REDACTED:github-pat]"),
    (_re.compile(r"\bAKIA[A-Z0-9]{16}\b"),          "[REDACTED:aws-key]"),
    # Email addresses (conservative — keeps domain)
    (_re.compile(r"\b[A-Za-z0-9._-]+@([A-Za-z0-9.-]+)\b"),
        r"[REDACTED:email]@\1"),
    # Bearer tokens (common in error messages, debug logs)
    (_re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b"),
        "Bearer [REDACTED:token]"),
]


def intercept_pii_redact(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Redact API keys, emails, and bearer tokens from chat
    completion requests before they leave localhost. Only
    triggers when the upstream is NOT localhost — local ollama
    is trusted; cloud endpoints (OpenRouter etc.) get redacted
    content. This is opt-in safety: false negatives possible
    (every regex scheme is best-effort), but false positives
    are bounded (we never invent secrets, only mask existing
    matches).

    No-op when the chain is going to localhost. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None

    # Heuristic for "going to non-localhost": the proxy server's
    # `upstream` attribute starts with "http://127.0.0.1" or
    # "http://localhost" → no redaction. Otherwise redact.
    # We get this via the handler's context; for now check via
    # an environment variable set by cli.py at proxy startup.
    if os.environ.get("ORG_LLM_PROXY_TRUSTED_LOCAL", "1") == "1":
        return None

    messages = req.parsed_json.get("messages") or []
    if not isinstance(messages, list):
        return None
    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        new = content
        for pattern, repl in _PII_PATTERNS:
            new = pattern.sub(repl, new)
        if new != content:
            msg["content"] = new
            changed = True
    if changed:
        req.body = json.dumps(req.parsed_json).encode()
        req.headers = {k: v for k, v in req.headers.items()
                        if k.lower() != "content-length"}
    return None


# ── Tool-call repair (T2.2) ─────────────────────────────────────────


def intercept_tool_call_repair(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Pre-process the tools array in the REQUEST to dedupe
    common malformations small models choke on. Scope is
    deliberately narrow: this isn't a JSON Schema validator, just
    a band-aid for the most common breakages we see in practice:
       • `tools` containing duplicates by `function.name`
       • Missing `description` (some models refuse to call them)
       • Empty `parameters` (small models skip them entirely)

    Doesn't post-process the model RESPONSE — that's a separate
    job (response repair) we'd add as a wrapper around _forward
    later. This is the request-side cleanup. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    tools = req.parsed_json.get("tools")
    if not isinstance(tools, list) or not tools:
        return None

    seen_names: set[str] = set()
    cleaned: list = []
    changed = False
    for tool in tools:
        if not isinstance(tool, dict):
            cleaned.append(tool)
            continue
        fn = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(fn, dict):
            cleaned.append(tool)
            continue
        name = fn.get("name")
        if name in seen_names:
            changed = True
            continue   # drop dup
        if isinstance(name, str):
            seen_names.add(name)
        if not fn.get("description"):
            fn["description"] = f"Tool: {name}"
            changed = True
        if "parameters" not in fn:
            fn["parameters"] = {"type": "object", "properties": {}}
            changed = True
        cleaned.append(tool)

    if changed:
        req.parsed_json["tools"] = cleaned
        req.body = json.dumps(req.parsed_json).encode()
        req.headers = {k: v for k, v in req.headers.items()
                        if k.lower() != "content-length"}
    return None


# ── Multi-model routing (T2.3) ──────────────────────────────────────


# Routing rules: substring/regex in user message → preferred model.
# Override happens BEFORE forward. The user's configured model is
# the default; routing nudges to a more-fitting variant when the
# query type is obvious.
_ROUTING_RULES: list[tuple["_re.Pattern", str]] = [
    # Code patterns → coder model
    (_re.compile(r"```|def \w+\(|class \w+|function \w+\(|"
                  r"\bimport \w+|<\w+>|/\*|//|#!/", _re.MULTILINE),
        "qwen2.5-coder"),
]


def intercept_model_routing(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Route to a more-fitting model based on user message content.
    Conservative — only kicks in for obvious patterns (code
    blocks, function definitions). The user's configured model
    is the fallback. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json)
    if not text:
        return None
    current_model = req.parsed_json.get("model") or ""
    for pattern, target in _ROUTING_RULES:
        if pattern.search(text) and current_model != target:
            req.parsed_json["model"] = target
            req.body = json.dumps(req.parsed_json).encode()
            req.headers = {k: v for k, v in req.headers.items()
                            if k.lower() != "content-length"}
            return None
    return None


# ── .md-as-skill (T2.1) ─────────────────────────────────────────────


import shlex


def _parse_md_skill(md_path: Path) -> Optional[tuple[str, str, str]]:
    """Read an .md slash-command file. If its YAML frontmatter has
    an `exec:` field, return (body, exec_template, slashname)
    where:
      • body is the .md content AFTER frontmatter (this is what
        opencode inlines as the user message text)
      • exec_template is the shell command to run, with `$ARGS`
        as a placeholder for any user-supplied args
      • slashname is the file stem (e.g. `capture` for capture.md)
    Returns None when no frontmatter or no `exec:` field.

    The .md format we expect:
        ---
        description: ...
        exec: org-llm capture $ARGS
        ---
        Body prose here. Optional $ARGS in body is forwarded to
        the user's typed args.
    """
    try:
        text = md_path.read_text()
    except Exception:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    frontmatter = text[4:end]
    body = text[end + 5:].strip()

    exec_template = ""
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("exec:"):
            exec_template = line.split(":", 1)[1].strip()
            # Strip surrounding quotes if user wrapped in YAML strings
            if exec_template.startswith(("'", '"')) and exec_template.endswith(exec_template[0]):
                exec_template = exec_template[1:-1]
            break
    if not exec_template:
        return None
    return (body, exec_template, md_path.stem)


def _match_skill_to_text(body: str, text: str) -> Optional[str]:
    """Determine if `text` (the user message arriving at the proxy)
    matches `body` (an .md skill body). Return the extracted args
    when matched, None otherwise.

    Match rules (most specific first):
      • body has `$ARGS` → split at $ARGS, match prefix + suffix,
        args are the middle.
      • body has no `$ARGS` → text must contain the body verbatim.

    Whitespace-tolerant. opencode may or may not trim or pad the
    inlined body; both sides get .strip() before matching. """
    body_t = body.strip()
    text_t = text.strip()
    if not body_t or not text_t:
        return None

    if "$ARGS" in body_t:
        prefix, _, suffix = body_t.partition("$ARGS")
        prefix = prefix.strip()
        suffix = suffix.strip()
        if not text_t.startswith(prefix):
            return None
        if suffix and not text_t.endswith(suffix):
            return None
        args = text_t[len(prefix):]
        if suffix:
            args = args[:-len(suffix)]
        return args.strip()

    if body_t in text_t:
        # Anything outside the body is treated as args (rare —
        # most .md without $ARGS expect no args).
        return text_t.replace(body_t, "").strip()
    return None


# Built-in registry of slash-name → handler. Each entry is one of:
#   • A shell command string (with optional $ARGS placeholder).
#     Substituted, shlex'd, run, stdout returned.
#   • A python callable taking the user's typed args (str) and
#     returning the response content (str). For dynamic content
#     that's expensive to build via shell (e.g. _build_menu reads
#     the .md directory and formats with custom layout).
#
# Used as FALLBACK by intercept_md_skills: when a project slash's
# .md body matches an incoming request but the .md has no `exec:`
# frontmatter, we check this registry. Lets users get no-LLM
# behavior for common slashes WITHOUT having to author exec:
# frontmatter on every file.
_BUILTIN_SKILLS: dict[str, "str | Callable[[str], str]"] = {
    # Static-content builders (already have functions for these)
    "menu":      lambda args: _build_menu(args),
    "help":      lambda args: _build_help(args),
    "config":    lambda args: _build_config(args),
    # CLI wrappers — the args, if any, get appended to the command
    "stats":     "org-llm stats $ARGS",
    "models":    "org-llm models $ARGS",
    "doctor":    "org-llm doctor --power-boost --no-diagnose $ARGS",
    "discover":  "org-llm discover $ARGS",
    "db":        "org-llm db $ARGS",
    "context":   "org-llm stats $ARGS",   # alias for status overview
    "embed":     "org-llm embed $ARGS",
    "grants":    "org-llm grants $ARGS",
    "history":   "org-llm history $ARGS",
    "health":    "org-llm doctor --power-boost --no-diagnose $ARGS",
    "dbt":       "org-llm dbt $ARGS",
    "dbt-status":   "org-llm dbt status $ARGS",
    "dbt-build":    "org-llm dbt build $ARGS",
    "dbt-run":      "org-llm dbt run $ARGS",
    "dbt-test":     "org-llm dbt test $ARGS",
    "dbt-compile":  "org-llm dbt compile $ARGS",
    "dbt-doctor":   "org-llm dbt doctor $ARGS",
    "code-index":   "org-llm code index $ARGS",
}


def _run_skill(spec: "str | Callable[[str], str]",
                args: str, slashname: str) -> str:
    """Execute a registered skill (either a shell template or
    a python builder) with the user's args. Returns the content
    string ready to be wrapped in an assistant response.

    Errors are swallowed — return a friendly diagnostic the user
    can see in chat instead of crashing the proxy. """
    if callable(spec):
        try:
            return spec(args)
        except Exception as e:
            return f"(builder for /{slashname} failed: {e})"
    # Shell template
    cmd_str = spec.replace("$ARGS", args)
    try:
        argv = shlex.split(cmd_str)
    except Exception as e:
        return f"(exec parse failed: {e})\nCommand: {cmd_str}"
    import subprocess as _sp
    try:
        result = _sp.run(argv, capture_output=True,
                          text=True, timeout=15)
        output = (result.stdout or "").strip()
        if not output and result.stderr:
            output = f"(stderr)\n{result.stderr.strip()}"
        if not output:
            output = "(no output)"
        # Strip ANSI from CLI output — opencode chat doesn't
        # render the escape codes.
        output = _re.sub(r"\x1b\[[0-9;]*m", "", output)
        rule = "─" * 48
        return f"/{slashname}  ({cmd_str.strip()})\n{rule}\n\n{output}"
    except Exception as e:
        return f"(exec failed: {e})\nCommand: {cmd_str}"


def intercept_md_skills(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Run a `.md`-defined skill as a subprocess instead of an
    LLM call. Resolution order:

      1. .md frontmatter has `exec:` → run that command.
      2. The .md's slash name is in `_BUILTIN_SKILLS` → run the
         registered handler. Lets the user get no-LLM behavior
         for common CLI wrappers without authoring `exec:`
         on every .md.
      3. Otherwise: fall through to the LLM.

    Why: most slash commands wrap a CLI invocation (capture →
    `org-llm capture`, stats → `org-llm stats`, doctor →
    `org-llm doctor`). Routing through the LLM adds 5-60s of
    latency on CPU-only inference for output the LLM is just
    re-rendering.

    Scans `.opencode/command/*.md` on every request — cheap (~1ms
    for ~70 files), avoids stale cache when user edits a .md
    mid-session. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json)
    if not text or not text.strip():
        return None

    cmd_dir = Path.cwd() / ".opencode" / "command"
    if not cmd_dir.is_dir():
        return None

    for md_path in cmd_dir.glob("*.md"):
        # Try exec-frontmatter path first.
        skill = _parse_md_skill(md_path)
        if skill is not None:
            body, exec_template, slashname = skill
            args = _match_skill_to_text(body, text)
            if args is None:
                continue
            content = _run_skill(exec_template, args, slashname)
        else:
            # No exec: frontmatter — check the built-in registry.
            slashname = md_path.stem
            if slashname not in _BUILTIN_SKILLS:
                continue
            # Read body without frontmatter for the matcher.
            try:
                full = md_path.read_text()
            except Exception:
                continue
            if full.startswith("---\n"):
                end = full.find("\n---\n", 4)
                body = full[end + 5:].strip() if end > 0 else full
            else:
                body = full.strip()
            args = _match_skill_to_text(body, text)
            if args is None:
                continue
            content = _run_skill(_BUILTIN_SKILLS[slashname], args, slashname)

        streaming = bool(req.parsed_json.get("stream"))
        model = req.parsed_json.get("model") or "unknown"
        return _empty_assistant_response(model, streaming, content=content)

    return None


# ── Qwen3 thinking-mode disabler (T1.6) ─────────────────────────────


def intercept_qwen3_no_think(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Inject `/no_think` into qwen3* requests so the model skips
    its long internal reasoning chain and just answers the user.
    Hybrid thinking is on by default in qwen3 and generates 5-10×
    more tokens than needed for simple queries (e.g. 236 tokens
    of reasoning to say "YES"). On CPU-only inference that's
    minutes of latency for a one-word answer.

    Per Qwen3 docs, `/no_think` belongs in the LAST USER MESSAGE,
    not the system message. The directive turns off the hidden
    `<think>...</think>` wrapper for that turn. Earlier iteration
    appended to the system message — visible in the request but
    qwen3's chat template ignored it there.

    Idempotent: skips when /no_think is already in the last user
    message. Returns None always — this is a request-mutation
    interceptor, not a short-circuit. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    model = (req.parsed_json.get("model") or "").lower()
    if "qwen3" not in model:
        return None

    messages = req.parsed_json.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return None

    DIRECTIVE = "/no_think"

    def _content_str(c) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = p.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            return "".join(parts)
        return ""

    # Find the LAST user message and append directive there.
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return None

    msg = messages[last_user_idx]
    existing = _content_str(msg.get("content"))
    if DIRECTIVE in existing:
        return None     # idempotent

    # Append directive on a new line at end of user content.
    msg["content"] = existing.rstrip() + "\n\n" + DIRECTIVE

    # Belt-and-suspenders: also set `enable_thinking: false` in
    # the request's options so even if Ollama's chat template
    # ignores `/no_think`, the parameter still disables thinking
    # mode at the runtime level. Ollama merges request-level
    # options into the model's defaults.
    options = req.parsed_json.setdefault("options", {})
    if isinstance(options, dict):
        options["enable_thinking"] = False

    # Re-encode body for the forward path.
    req.body = json.dumps(req.parsed_json).encode()
    req.headers = {k: v for k, v in req.headers.items()
                    if k.lower() != "content-length"}
    return None


# ── Prompt slimming (T1.2) ──────────────────────────────────────────


# Heuristic: if the user's last message looks "simple" (short,
# no code, no URL, no question about a tool) AND the request has
# tools defined, we strip the tools array. The MCP catalog is
# the bulk of the prefill cost on small-context models — dropping
# it cuts prefill tokens by ~70% and speeds up small-talk-style
# responses dramatically.
_SLIM_PROBABLE_TOOL_TRIGGERS = (
    "search", "find", "look up", "list ", "show ", "tell me",
    "explain", "summarize", "fetch", "open ", "read ", "edit ",
    "write ", "create ", "delete ", "update ", "run ", "execute",
    "doctor", "stats", "models", "config", "embed", "capture",
    # MCP tool names (match against any tool name we surface)
    "search_notes", "ask_notes", "list_slash_commands",
    "proactive_doctor", "capture_note", "discover_recent",
)


def _looks_like_simple_chat(user_text: str) -> bool:
    """True when the user message reads like a casual question
    that doesn't need MCP tools. Cheap heuristic — false negatives
    (still slimming when tools were needed) cost a tool-call
    error which the LLM recovers from. False positives (slimming
    skipped when not needed) just leave performance on the table.
    Tuned for false-negative-bias since the failure mode is
    'LLM apologises and asks user to rephrase' — recoverable. """
    t = user_text.strip().lower()
    if len(t) > 240:
        return False                       # long prompts often need tools
    if any(c in t for c in ("```", "http://", "https://", "/", "<", "{")):
        return False                       # code/url/path/JSON → likely tool work
    for trigger in _SLIM_PROBABLE_TOOL_TRIGGERS:
        if trigger in t:
            return False
    return True


# ── Prompt prefix cache (Phase 18, interceptor #15) ──────────────────
#
# Two related optimisations rolled into one mutator:
#
#   • For local Ollama: ensure `options.keep_alive` is set generously
#     so the model + KV cache survive between turns. Ollama's default
#     keep_alive is 5 minutes; opencode sessions often span longer
#     gaps where the user reads a response before replying. Bumping
#     to 30m means the second turn re-uses the prefix's prefilled
#     KV-cache instead of cold-loading the model.
#
#   • For cloud Claude (Anthropic native or via OpenRouter passthrough):
#     mark the largest system-message block with `cache_control:
#     {type: "ephemeral"}`. Anthropic's prompt-caching API discounts
#     cached input tokens by ~90% when the same system prompt repeats
#     within 5 minutes. opencode resends the same 22 KB MCP catalog +
#     workspace context every turn — that's exactly the workload
#     prompt caching is built for.
#
# Always returns None: this is a request-mutation interceptor that
# happens before the forwarder. Idempotent — both legs check before
# mutating so re-runs (e.g. if interceptors get re-ordered) don't
# double-add markers or grow keep_alive unboundedly.


def _is_local_ollama_request(req: ProxyRequest) -> bool:
    """Heuristic: an Ollama-shape request that targets local upstream.
    True when the request's `Authorization` header is missing AND the
    `options` field exists or the model name lacks an organisation
    prefix (e.g. `llama3.2` vs `anthropic/claude-opus-4-7`)."""
    if any(k.lower() == "authorization" for k in (req.headers or {})):
        return False
    if not req.parsed_json:
        return False
    if "options" in req.parsed_json:
        return True
    model = (req.parsed_json.get("model") or "")
    # An openrouter-shape model has a `/` (provider/slug); local
    # ollama models are just `llama3.2` / `qwen2.5-coder:7b`.
    return "/" not in model


def _looks_like_anthropic_relay(req: ProxyRequest) -> bool:
    """True when the request's model is a Claude variant (native
    Anthropic API or OpenRouter `anthropic/claude-*`). The cache_control
    marker is harmless on non-Anthropic endpoints — they ignore the
    field — but adding it only when meaningful keeps the audit log
    interpretable."""
    if not req.parsed_json:
        return False
    model = (req.parsed_json.get("model") or "").lower()
    return "claude" in model


def _system_prefix_hash(parsed: dict) -> Optional[str]:
    """SHA256 of the system messages + tools array, ignoring user/
    assistant turns. Identical across turns of the same session so
    repeats are easy to count in the audit log."""
    if not parsed:
        return None
    sys_blocks = []
    for m in (parsed.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "system":
            sys_blocks.append(m.get("content") or "")
    payload = {
        "system":  sys_blocks,
        "tools":   parsed.get("tools") or [],
        "model":   parsed.get("model")  or "",
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _proxy_keep_alive() -> str:
    """Read `proxy_prompt_keep_alive` (e.g. "30m"). Default 30 minutes."""
    return _proxy_cfg_str("proxy_prompt_keep_alive", "30m") or "30m"


def _prompt_cache_enabled() -> bool:
    return _proxy_cfg_str("proxy_prompt_cache_enabled",
                            "true").strip().lower() != "false"


def intercept_prompt_cache(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Mutate outgoing chat-completions to maximise prefix reuse.

    Two passes — both safe to run on the same request:

    1. *keep_alive injection* (local Ollama only). Ensures the model
       stays loaded long enough that the next turn can reuse the
       prefix's KV cache. Skips when the user has explicitly set
       `keep_alive` (any value, including "0") — respects the user
       override.

    2. *Anthropic cache_control* (Claude-shape requests only). Marks
       the LAST system message with `cache_control: {type:"ephemeral"}`
       so Anthropic / OpenRouter discounts cached input tokens.
       Idempotent: skips when any system message already has
       cache_control.

    Always returns None. The forwarder sees the mutated request."""
    if not _prompt_cache_enabled():
        return None
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    parsed = req.parsed_json
    mutated = False

    # Leg 1: keep_alive for local ollama.
    if _is_local_ollama_request(req):
        opts = parsed.setdefault("options", {})
        if isinstance(opts, dict) and "keep_alive" not in opts:
            opts["keep_alive"] = _proxy_keep_alive()
            mutated = True

    # Leg 2: cache_control for Claude-shape outbound.
    if _looks_like_anthropic_relay(req):
        msgs = parsed.get("messages") or []
        if isinstance(msgs, list):
            already_marked = any(
                isinstance(m, dict)
                and m.get("role") == "system"
                and (m.get("cache_control")
                       or (isinstance(m.get("content"), list)
                            and any(isinstance(p, dict)
                                      and p.get("cache_control")
                                      for p in m["content"])))
                for m in msgs
            )
            if not already_marked:
                # Walk in reverse to find the LAST system message — the
                # furthest-back cacheable prefix maximises the cached
                # span while leaving room for per-turn deltas above.
                for i in range(len(msgs) - 1, -1, -1):
                    m = msgs[i]
                    if (isinstance(m, dict)
                            and m.get("role") == "system"):
                        m["cache_control"] = {"type": "ephemeral"}
                        mutated = True
                        break

    if mutated:
        req.body = json.dumps(parsed).encode()
        req.headers = {k: v for k, v in req.headers.items()
                        if k.lower() != "content-length"}
    return None


def intercept_prompt_slim(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Mutate the request body in place to strip the tools array
    from simple-chat prompts. Returns None always — this isn't a
    short-circuit interceptor; it modifies the request before
    forwarding. Mutation happens via `req.body` and `req.parsed_json`
    so subsequent interceptors (including the forwarder) see the
    slimmed version.

    Rule:
      • path must be /chat/completions
      • request must have a `tools` array with at least one entry
      • last user message must look like simple chat
    All three → drop `tools` and any `tool_choice`. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    parsed = req.parsed_json
    tools = parsed.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    user_text = _last_user_text(parsed)
    if not _looks_like_simple_chat(user_text):
        return None
    # Mutate. Drop tools + tool_choice. Re-encode body.
    parsed.pop("tools", None)
    parsed.pop("tool_choice", None)
    new_body = json.dumps(parsed).encode()
    req.body = new_body
    # Update Content-Length so the forward path sends the right
    # number of bytes. urllib will re-set this from data length,
    # but the headers dict still has the old value.
    req.headers = {k: v for k, v in req.headers.items()
                    if k.lower() != "content-length"}
    return None


# ── --no-llm flag (T1.5) ────────────────────────────────────────────


def intercept_no_llm_flag(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Short-circuit any chat completion whose last user message
    contains the literal `--no-llm` flag. Returns a confirmation
    so the user knows the request was bypassed.

    UX: the user can append `--no-llm` to any prompt — chat or
    /sys* or even a project slash that uses $ARGS — to force
    the request through the proxy without engaging ollama. Useful
    for:
      • Testing the proxy ("did this hit ollama?")
      • Stopping a hung/slow LLM mid-iteration without /q'ing
      • Handing the user a kill-switch for any individual prompt

    LIMITATION: opencode's project slashes (the `.md` files in
    `.opencode/command/`) only forward user arguments to the LLM
    if the .md body explicitly references `$ARGS`. If the .md is
    static prose like the default `/menu`, typing `/menu --no-llm`
    sends the SAME body whether or not the user added the flag —
    we can't see it. In that case the user types the .md prompt
    body itself with `--no-llm` appended, or uses /sys* (we
    intercept those regardless of args). """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    text = _last_user_text(req.parsed_json)
    if "--no-llm" not in text:
        return None
    streaming = bool(req.parsed_json.get("stream"))
    model     = req.parsed_json.get("model") or "unknown"
    return _empty_assistant_response(model, streaming,
        content=("✓ --no-llm flag detected — request bypassed at proxy. "
                 "No LLM call made. Strip the flag to invoke the LLM normally."))


def intercept_local_only(req: ProxyRequest) -> Optional[ProxyResponse]:
    """Catch-all interceptor for `proxy_local_only` mode. When the
    chain reaches this last stop, the request didn't match any
    earlier handler — meaning under normal mode it'd forward to
    ollama. In local-only mode we instead return a no-LLM
    response so ollama never gets called.
    Only enabled by the caller (cli.py) when sidebar config
    has `proxy_local_only=true`. Default chain doesn't include
    it, so behaviour is opt-in. """
    if not req.path.endswith("/chat/completions"):
        return None
    if not req.parsed_json:
        return None
    streaming = bool(req.parsed_json.get("stream"))
    model     = req.parsed_json.get("model") or "unknown"
    return _empty_assistant_response(model, streaming,
        content=("✓ no-LLM mode active (proxy_local_only=true). Request "
                 "intercepted before reaching ollama. Disable via "
                 "`org-llm config proxy_local_only false` to allow LLM."))


DEFAULT_INTERCEPTORS: list[Interceptor] = [
    # ── Short-circuit interceptors (return early, never forward) ──
    intercept_no_llm_flag,        # T1.5  --no-llm hard-bypass
    intercept_sysexport_command,  # P18.5 /sysexport — write file + inline sidebar
    intercept_sysscreenshot_command,  # P18.6 /sysscreenshot — capture via configured backend
    intercept_sys_commands,       # T1.0  /sys*
    # Synthetic tool calls run BEFORE the generic forward but AFTER
    # the /sys* short-circuits — for /sys* we don't want to engage
    # the upstream at all, even with rewritten prompts.
    intercept_synth_tool_call,    # P18.6 gemma-class tool-call synth
    intercept_md_skills,          # T2.1  .md `exec:` → subprocess
    intercept_static_slashes,     # T1.3  /menu /help /config
    intercept_probe_cache,        # T2.0  /api/tags etc cache
    intercept_response_cache,     # T1.4  identical-prompt cache
    # ── Mutation interceptors (modify request, fall through) ──
    intercept_force_tool_call,    # P18.6 `:tool <name>` prefix pins tool_choice
    intercept_pii_redact,         # T3.1  redact secrets to non-localhost
    intercept_time_grounding,     # T3.0  inject current date/time
    intercept_model_routing,      # T2.3  route by content (code → coder)
    intercept_tool_call_repair,   # T2.2  dedupe tools, fix descriptions
    intercept_qwen3_no_think,     # T1.6  /no_think for qwen3
    intercept_prompt_cache,       # P18.3 keep_alive + Anthropic cache_control
    intercept_prompt_slim,        # T1.2  strip tools from simple chat
    # NOTE: intercept_local_only is NOT in the default chain. cli.py
    # appends it conditionally based on `proxy_local_only` config.
]


# ── Observability (T1.1) ────────────────────────────────────────────


@dataclass
class AuditEntry:
    """One JSONL line per request. Each field is captured at the
    request-handling boundary in `_ProxyHandler._handle`.

    Fields are deliberately denormalised — log-readers shouldn't
    have to join across files. `prompt_hash` is the SHA256 of the
    full message array (canonicalised) so identical-prompt
    repeats are easy to identify (cache-hit candidates, prompt
    leaks, etc.). `user_text` is truncated to 240 chars: enough
    to recognise a query at a glance, short enough to keep lines
    grep-friendly. """
    ts:              float            # wall-clock seconds
    method:          str
    path:            str
    model:           Optional[str]
    prompt_hash:     Optional[str]
    user_text:       Optional[str]
    intercepted_by:  Optional[str]    # interceptor func name or "forward"
    status:          Optional[int]
    duration_ms:     float
    bytes_out:       int
    error:           Optional[str]


def _hash_prompt(parsed: dict) -> Optional[str]:
    """Stable SHA256 of the messages + tools + model fields. Used
    to identify repeat prompts (cache eligibility, prompt-leak
    auditing). Returns None when the request isn't a chat
    completion."""
    if not parsed:
        return None
    payload = {
        "messages": parsed.get("messages") or [],
        "tools":    parsed.get("tools")    or [],
        "model":    parsed.get("model")    or "",
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


class AuditLogger:
    """Append-only JSONL writer. Default path is in the user's
    XDG data dir. Thread-safe; the proxy's handler threads all
    write through one shared instance.

    The file rotates manually via the user (or via a future
    `org-llm audit rotate` command) — this writer doesn't
    truncate or compress on its own. Disk-space cost: typical
    org-llm session is ≤1 KB per request × ≤200 requests/day =
    ~200 KB/day. A year of usage is ~70 MB. """
    def __init__(self, path: Optional[Path] = None):
        if path is None:
            base = Path(os.environ.get("XDG_DATA_HOME") or
                         (Path.home() / ".local" / "share"))
            path = base / "org-llm" / "llm-audit.jsonl"
        self.path = path
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Don't fail proxy startup over an unwritable audit
            # path — we'll silently drop entries instead.
            pass

    def write(self, entry: AuditEntry) -> None:
        line = json.dumps(asdict(entry), default=str) + "\n"
        with self._lock:
            try:
                with self.path.open("a") as f:
                    f.write(line)
            except Exception:
                # Best-effort. Audit logging never blocks request
                # handling; an I/O failure here should not propagate.
                pass


# Default singleton — a launch-time proxy reuses this unless the
# caller passes a custom AuditLogger to start_proxy.
_default_audit = AuditLogger()


# Future interceptors (each its own module-level builder fn that
# closes over its config; users compose into the chain at proxy
# startup). Roadmap:
#   • cloud_failover: detect first-byte timeout against local,
#     transparently retry against cloud. Needs first-byte timing
#     + dual-transport streaming — larger than this initial patch.
#   • static_slash_responses: handwritten markdown for /menu /help
#     /config etc. so opencode's LLM-driven slashes are zero-cost.
#   • observability: log every request/response with timings to a
#     JSONL audit trail at ~/.local/share/org-llm/llm-audit.jsonl.
#   • prompt_slimming: strip the MCP catalog from system prompts
#     for queries we know don't need tools.
#   • tool_call_repair: fix common LLM-generated tool-call schema
#     mistakes inline before opencode sees them.


# ── Cloud failover (Phase 18) ──────────────────────────────────────
#
# When the local upstream stops responding before its first byte
# arrives within `proxy_first_byte_timeout_ms`, fail over to the
# user's configured cloud provider for THIS request only. The forward
# path detects the timeout, aborts the local connection, and re-issues
# the same request body (with `model` swapped) against
# `cloud_endpoint_url` with the cloud API key.
#
# Scope:
#   • Only fires for POST /v1/chat/completions and /api/chat (the
#     paths opencode uses for chat). Non-chat probes (GET /api/tags
#     etc.) keep their default short timeout and do NOT failover —
#     those should fail fast locally so opencode picks a different
#     provider.
#   • Single retry only. If cloud also fails, propagate the original
#     error to opencode (502 upstream proxy error).
#   • Only fires when `proxy_cloud_failover_enabled` is true AND the
#     user has cloud config (cloud_endpoint_url + a resolvable API
#     key). Without that we fall back to existing behaviour.
#
# Knobs:
#   • proxy_cloud_failover_enabled   (default true)  — kill switch
#   • proxy_first_byte_timeout_ms    (default 8000)  — TTFB before
#     we declare the local upstream stalled. 0 disables the timeout
#     (effectively disables failover).


# Paths the failover eligibility check matches against `req.path`
# (i.e. the path opencode SENT to the proxy, before the /v1
# auto-injection that happens during forwarding). opencode at
# different versions has sent variants of all three forms — the
# bare `/chat/completions` was the gap that masked T6 cloud-
# failover entirely (audit log showed connection-refused errors
# coming back as plain `forward`, never invoking the failover
# path).
_CHAT_PATHS = ("/v1/chat/completions", "/chat/completions", "/api/chat")


def _proxy_cfg_str(key: str, default: str = "") -> str:
    """Best-effort read of a single Config row. Returns `default` on
    any DB issue — proxy startup must not depend on the user having a
    fully-populated config."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return default
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, key)
            return (row.value if row and row.value is not None
                    else default)
    except Exception:
        return default


def _first_byte_timeout_secs() -> float:
    """Read proxy_first_byte_timeout_ms; clamp to ≥0. 0 = disabled.
    Default 30000ms — covers cold-start prefill on thermal-throttled
    CPUs with heavy MCP tool context. Lower defaults (we shipped
    8000 originally) preempt local before it can produce the first
    byte. See db.py's MODEL_DEFAULTS comment for the rationale."""
    raw = _proxy_cfg_str("proxy_first_byte_timeout_ms", "30000")
    try:
        ms = int(raw)
    except ValueError:
        ms = 30000
    return max(0.0, ms / 1000.0)


def _cloud_failover_enabled() -> bool:
    return _proxy_cfg_str("proxy_cloud_failover_enabled",
                            "true").strip().lower() != "false"


def _cloud_first_enabled() -> bool:
    """When true, chat completions skip local entirely and go
    straight to cloud. Default false. Useful when local hardware
    can't realistically serve the configured chat_model in time
    (low free RAM, thermal throttling, no GPU) — instead of waiting
    `proxy_first_byte_timeout_ms` for local to fail over, we route
    directly to cloud and save the wait. Pairs with the cloud-
    failover retry chain so context-overflow on cloud still gets
    a compressed-tools fallback. Toggle via:
        org-llm config proxy_cloud_first true
    """
    return _proxy_cfg_str("proxy_cloud_first",
                            "false").strip().lower() == "true"


def _resolve_cloud_failover_target() -> Optional[dict]:
    """Build the (endpoint, headers, model) tuple for a cloud failover
    or return None if cloud isn't configured / failover disabled.

    Returns dict shape::

        {"endpoint": "https://...", "model": "openai/...",
         "api_key":  "sk-or-v1-..."}

    Reads `cloud_endpoint_url`, `cloud_provider`, `cloud_model`, and
    pulls the API key from the credentials store first (preferred —
    that's where `org-llm cloud --connect` writes it) with the DB
    `cloud_api_key`/`runpod_api_key` as a backup.
    """
    if not _cloud_failover_enabled():
        return None
    endpoint = _proxy_cfg_str("cloud_endpoint_url", "").rstrip("/")
    if not endpoint:
        return None
    provider = _proxy_cfg_str("cloud_provider", "")
    model    = (_proxy_cfg_str("cloud_model", "")
                or _proxy_cfg_str("chat_model", "")
                or "openai/gpt-oss-20b:free")
    # FOSS-first gate: if the user-configured cloud_model is
    # proprietary AND `proprietary_models_enabled` is false, refuse
    # to fail over to it. We don't silently swap to a different model
    # because that'd surprise the user mid-stream — instead, the
    # request lands in the legacy 502 path and they see a clear
    # "cloud failover skipped: proprietary model gated" line in the
    # captain's log.
    try:
        from . import cloud as _cloud_mod
        if not _cloud_mod.proprietary_models_enabled():
            tier = _cloud_mod._classify_license_tier(  # type: ignore[attr-defined]
                "", model)
            if tier == "proprietary":
                return None
    except Exception:
        pass
    db_key   = (_proxy_cfg_str("cloud_api_key", "")
                or _proxy_cfg_str("runpod_api_key", ""))
    api_key = ""
    if provider:
        try:
            from . import creds as _creds
            api_key = _creds.read_secret(_creds.cloud_slug(provider)) or ""
        except Exception:
            api_key = ""
    api_key = api_key or db_key
    return {"endpoint": endpoint, "model": model, "api_key": api_key}


def _compress_tool_schema(s) -> dict:
    """Strip descriptions/examples from a JSON-schema fragment,
    keeping only the type info the model needs to invoke a tool.
    Used by the cloud-failover compressed-tools retry path so
    opencode's full 64-tool MCP inventory fits in a 32k context."""
    if not isinstance(s, dict):
        return {}
    t = s.get("type")
    if t == "object":
        props = s.get("properties") or {}
        new_props = {}
        if isinstance(props, dict):
            for k, v in props.items():
                if not isinstance(v, dict):
                    continue
                slim: dict = {"type": v.get("type", "string")}
                # Keep enum (small, critical for valid invocations).
                if "enum" in v:
                    slim["enum"] = v["enum"]
                # Recurse on nested object/array shapes.
                if v.get("type") in ("object", "array"):
                    slim = _compress_tool_schema(v) or slim
                new_props[k] = slim
        out: dict = {"type": "object", "properties": new_props}
        if isinstance(s.get("required"), list):
            out["required"] = s["required"]
        return out
    if t == "array":
        items = s.get("items")
        if isinstance(items, dict):
            return {"type": "array", "items": _compress_tool_schema(items)}
        return {"type": "array"}
    # Scalars: just keep type + enum.
    out = {"type": t or "string"}
    if "enum" in s:
        out["enum"] = s["enum"]
    return out


def _compress_tools_for_failover(tools: list) -> list:
    """Compress a tools array so it fits in a smaller context budget.
    Strategy: trim each tool's description to 80 chars and replace
    the JSON-schema with a stripped version (type info only, no
    descriptions/examples/long enum lists). For 64 tools at ~303
    avg tokens this typically lands ~50 tokens each — total ~3.2k
    instead of ~19k. The model still knows tool NAMES + arg
    SHAPES, which is what it needs to call them; opencode handles
    the actual execution."""
    out: list = []
    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            out.append(t)
            continue
        desc = fn.get("description", "")
        if isinstance(desc, str) and len(desc) > 80:
            desc = desc[:77] + "…"
        new_fn = {
            "name": fn.get("name"),
            "description": desc,
            "parameters": _compress_tool_schema(fn.get("parameters")),
        }
        out.append({"type": "function", "function": new_fn})
    return out


def _build_cloud_request(orig_body: bytes, parsed: Optional[dict],
                           target: dict, orig_path: str
                           ) -> urllib.request.Request:
    """Produce the Request object for the cloud retry. Strategy:
    swap `model` to the cloud model, drop Ollama-specific fields the
    OpenAI-compatible cloud endpoints reject (`options`,
    `keep_alive`), reuse everything else (messages, tools, stream).
    """
    if parsed is not None and isinstance(parsed, dict):
        body_obj = dict(parsed)
        body_obj["model"] = target["model"]
        # Drop Ollama-only fields that OpenAI-compatible endpoints
        # reject. Most cloud providers (OpenRouter, Groq…) treat
        # extra fields as 400; Anthropic ignores them. Defensive
        # strip.
        for key in ("options", "keep_alive", "format"):
            body_obj.pop(key, None)
        # Phase 18.4-iter19: cap max_tokens to fit cloud context.
        # opencode requests max_tokens=32000 by default — fine for
        # local Ollama (large context) but openrouter free/cheap
        # models cap at 32k TOTAL (qwen-2.5-72b returned: "you
        # requested 57006 tokens — 5621 text + 19385 tools + 32000
        # output"). The output reservation alone can't be 32k
        # when the input is 25k. 4096 is plenty for chat replies
        # and leaves room for the tools array. Only override when
        # the configured value is missing or oversized; user can
        # set it lower explicitly.
        mt = body_obj.get("max_tokens")
        if mt is None or (isinstance(mt, (int, float)) and mt > 4096):
            body_obj["max_tokens"] = 4096
        out_body = json.dumps(body_obj).encode()
    else:
        out_body = orig_body
    headers = {"Content-Type": "application/json",
                "User-Agent":   "org-llm/proxy-failover"}
    if target["api_key"]:
        headers["Authorization"] = f"Bearer {target['api_key']}"
    # opencode hits `/v1/chat/completions` and `/api/chat`; cloud
    # providers expect the OpenAI shape on `/chat/completions` (their
    # base URL usually already ends with `/v1`).
    cloud_url = f"{target['endpoint']}/chat/completions"
    return urllib.request.Request(
        cloud_url, data=out_body, method="POST", headers=headers,
    )


# ── Server ──────────────────────────────────────────────────────────


def _emit_failover_toast(model: str) -> None:
    """Write a `cloud-failover` action via the file-action bridge so
    the user sees that cloud failover just fired. Best-effort — never
    blocks the response path. The plugin's 250 ms tick picks up the
    action, both toasting AND updating the ACTIVE card's persistent
    failover indicator (auto-fades after 10 minutes).

    Discovered need 2026-05-02 during T6 hands-on: opencode's session
    UI shows the CONFIGURED local model regardless of what actually
    responded, so a transparent failover looks identical to "local
    answered slowly" — the user had to grep the audit log to know
    cloud was used. iter13 added a one-off toast (~6s); iter15
    extends it with a persistent ACTIVE-card row so the indicator
    stays visible for the next several minutes.
    """
    try:
        org_dir = Path(os.environ.get("ORG_LLM_ORG_DIR")
                        or (Path.home() / "org"))
        action_path = org_dir / ".opencode" / "sidebar-action.json"
        action_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "v":      1,
            "ts":     int(time.time() * 1000),
            "action": "cloud-failover",
            "model":  model,
        }
        action_path.write_text(json.dumps(payload))
    except Exception:
        # Never block the response. If the action file can't be
        # written (permissions, disk full, etc.), the user just
        # doesn't see the toast — they'll spot it in the audit log
        # if they look.
        pass


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    # `server` is set by socketserver at request time; we attach
    # `upstream` and `interceptors` to the server instance below.
    server: "_ProxyServer"   # type: ignore[assignment]

    # Suppress per-request access logging — too chatty for the
    # opencode launch console. Errors still print via log_error.
    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        return

    def do_GET(self) -> None:        # noqa: N802 (stdlib API)
        self._handle()

    def do_POST(self) -> None:       # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:     # noqa: N802
        self._handle()

    def do_PUT(self) -> None:        # noqa: N802
        self._handle()

    def _handle(self) -> None:
        t0 = time.time()
        body = b""
        length_str = self.headers.get("Content-Length", "0")
        try:
            length = int(length_str) if length_str else 0
        except ValueError:
            length = 0
        if length > 0:
            body = self.rfile.read(length)

        # Try to parse JSON for interceptor inspection. Don't fail
        # the proxy if parsing fails — just forward as bytes.
        parsed: Optional[dict] = None
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype and body:
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None

        req = ProxyRequest(
            path=self.path,
            method=self.command,
            headers={k: v for k, v in self.headers.items()},
            body=body,
            parsed_json=parsed,
            upstream=self.server.upstream,
        )
        # Compute cache key from the ORIGINAL request — before any
        # mutation interceptors (time grounding etc.) touch the
        # prompt. Without this, the cached entry's key (mutated
        # prompt) wouldn't match the next read's key (original).
        # Stashed on self so _forward can use it post-mutation.
        if (self.path.endswith("/chat/completions")
                and self.command == "POST" and parsed
                and not parsed.get("stream")):
            self._original_cache_key = _hash_prompt(parsed)
        else:
            self._original_cache_key = None

        # Capture for the audit entry — populated below as the
        # request flows through the interceptor chain or the
        # forwarder. Default values cover the "forward to upstream"
        # path; interceptors override `intercepted_by`.
        intercepted_by = "forward"
        status: Optional[int] = None
        bytes_out = 0
        error_msg: Optional[str] = None

        try:
            # Run interceptor chain. First match wins; if none
            # match, we forward upstream.
            for interceptor in self.server.interceptors:
                try:
                    resp = interceptor(req)
                except Exception as e:
                    resp = None
                    error_msg = f"interceptor {interceptor.__name__}: {e}"
                if resp is not None:
                    intercepted_by = getattr(interceptor, "__name__",
                                              "anonymous_interceptor")
                    status = resp.status
                    bytes_out = sum(len(c) for c in resp.body_chunks)
                    self._write_proxy_response(resp)
                    return

            # Nothing intercepted → forward upstream. Use the
            # POSSIBLY-MUTATED req.body rather than the original
            # `body` — interceptors like intercept_qwen3_no_think
            # and intercept_prompt_slim modify req.body in place
            # and rely on the forward path to send the new bytes.
            # _forward populates status/bytes_out/error via instance
            # vars so the audit entry can capture them after return.
            self._parsed_for_audit = parsed
            self._forward(req.body)
            status = getattr(self, "_last_status", None)
            bytes_out = getattr(self, "_last_bytes", 0)
            error_msg = getattr(self, "_last_error", None)
            # _forward sets _last_intercept = "forward" by default;
            # the cloud-failover path overwrites it to "cloud_failover".
            intercepted_by = getattr(self, "_last_intercept",
                                       intercepted_by) or intercepted_by
        finally:
            audit = self.server.audit
            if audit is not None:
                audit.write(AuditEntry(
                    ts             = t0,
                    method         = self.command,
                    path           = self.path,
                    model          = (parsed or {}).get("model"),
                    prompt_hash    = _hash_prompt(parsed or {}),
                    user_text      = (_last_user_text(parsed or {})[:240]
                                       or None),
                    intercepted_by = intercepted_by,
                    status         = status,
                    duration_ms    = (time.time() - t0) * 1000.0,
                    bytes_out      = bytes_out,
                    error          = error_msg,
                ))

    def _write_proxy_response(self, resp: ProxyResponse) -> None:
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.end_headers()
        # If the response carries Connection: close, also flip
        # http.server's keep-alive flag so the socket actually
        # closes after writing. Without this, the client blocks
        # on read() waiting for either more data or the keep-alive
        # timeout to fire.
        if any(k.lower() == "connection" and v.lower() == "close"
               for k, v in resp.headers.items()):
            self.close_connection = True
        for chunk in resp.body_chunks:
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def _forward(self, body: bytes) -> None:
        upstream = self.server.upstream

        # Phase 18.6 cloud-first bypass: when the user has
        # `proxy_cloud_first=true`, skip the local upstream entirely
        # for chat completions. Sends the request straight to cloud
        # via the same _failover_to_cloud machinery (so we get the
        # ctx-overflow → compressed-tools → no-tools retry chain),
        # saving the full proxy_first_byte_timeout_ms wait.
        # Use this when local hardware can't realistically serve the
        # configured chat_model in time — low free RAM, thermal
        # throttling, no GPU. Toggle:
        #     org-llm config proxy_cloud_first true
        chat_eligible_path = (
            self.command == "POST"
            and any(self.path.endswith(p) for p in _CHAT_PATHS)
        )
        if chat_eligible_path and _cloud_first_enabled():
            cloud_target = _resolve_cloud_failover_target()
            if cloud_target is not None:
                # Set the audit fields the way the wrapper expects.
                self._last_status    = None
                self._last_bytes     = 0
                self._last_error     = None
                self._last_intercept = "cloud_first"
                if self._failover_to_cloud(
                    body, cloud_target,
                    reason="cloud_first enabled (skipping local)",
                ):
                    # Mark as cloud_first in audit (failover_to_cloud
                    # would otherwise label this as cloud_failover).
                    self._last_intercept = "cloud_first"
                    return
                # _failover_to_cloud failed before writing → fall
                # through to local as a backup (e.g. cloud creds
                # broken). Better to attempt local than to 502 the
                # client; user can always toggle the knob off.

        # Phase 18.4-iter10: normalise path against upstream API
        # version. Ollama's OpenAI-compat endpoint is at /v1/...; if
        # opencode sends bare /chat/completions the bare path 404s on
        # ollama (verified via curl). opencode's behavior has shifted
        # at least once between sending /v1/... and bare /...; instead
        # of chasing the moving target, the proxy auto-injects /v1
        # when the path lacks it AND the upstream is ollama-shape
        # (localhost / no /api/ prefix already).
        path = self.path
        if (path in ("/chat/completions", "/embeddings", "/models")
                and "/v1" not in upstream
                and "/api" not in upstream):
            path = "/v1" + path
        url = f"{upstream}{path}"
        # Strip headers urllib will set itself or that don't make
        # sense to forward (Host, Content-Length).
        forward_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
        }
        req = urllib.request.Request(
            url, data=body if body else None,
            method=self.command, headers=forward_headers,
        )
        # Audit-stat capture. The handler reads these after
        # _forward returns (see _handle's finally block).
        self._last_status    = None
        self._last_bytes     = 0
        self._last_error     = None
        self._last_intercept = "forward"   # overwritten if cloud takes over
        # Cache-eligibility: only successful, non-streaming
        # /chat/completions responses. Use the ORIGINAL cache key
        # captured before mutators ran — see _handle for context.
        cache_key: Optional[str] = getattr(self, "_original_cache_key", None)
        cache_buf: Optional[list[bytes]] = [] if cache_key else None
        cache_headers: dict[str, str] = {}
        # Probe-cache eligibility: GET requests to /api/tags etc.
        # Separate buf so we accumulate bytes regardless of
        # cache_key path above.
        probe_buf: Optional[list[bytes]] = None
        if (self.command == "GET"
                and any(p in self.path for p in
                        ("/api/tags", "/api/show", "/v1/models"))):
            probe_buf = []

        # Cloud-failover eligibility: only chat completions, only
        # when failover is configured. Chooses the request's read
        # timeout: tight (TTFB threshold) for chat so we can fail
        # over fast; permissive (300s) otherwise so probes and
        # other requests behave as before.
        chat_eligible = (
            self.command == "POST"
            and any(self.path.endswith(p) for p in _CHAT_PATHS)
        )
        fb_timeout = _first_byte_timeout_secs() if chat_eligible else 0.0
        cloud_target = (_resolve_cloud_failover_target()
                          if chat_eligible and fb_timeout > 0 else None)
        # urlopen timeout governs both connect AND every read() call;
        # we extend it after the first byte arrives so legit slow
        # streaming doesn't trip the failover path.
        local_timeout = fb_timeout if cloud_target else 300

        try:
            with urllib.request.urlopen(req, timeout=local_timeout) as resp:
                # Try to read the first byte under the failover-tight
                # timeout. Once we have it we know the local upstream
                # is alive — extend the socket timeout so subsequent
                # streaming reads aren't subject to the same threshold.
                first_chunk = b""
                try:
                    first_chunk = resp.read(1)
                except (socket.timeout, TimeoutError) as e:
                    if cloud_target is not None:
                        if self._failover_to_cloud(body,
                                                     cloud_target,
                                                     reason=f"first-byte timeout ({fb_timeout:.1f}s)"):
                            return
                    raise socket.timeout(str(e)) from e
                # First byte arrived — relax the socket timeout so
                # legitimate slow streaming runs uninterrupted. The
                # private-attribute walk is fragile but it's the only
                # path urllib gives us; if the layout shifts we just
                # keep the original tight timeout (graceful degrade).
                if cloud_target is not None:
                    try:
                        resp.fp.raw._sock.settimeout(300.0)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                self._last_status = resp.status
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection",
                                       "content-length"):
                        continue
                    self.send_header(k, v)
                    cache_headers[k] = v
                self.end_headers()
                # Flush the byte we already read off the socket.
                if first_chunk:
                    try:
                        self.wfile.write(first_chunk)
                        self.wfile.flush()
                        self._last_bytes += len(first_chunk)
                        if cache_buf is not None:
                            cache_buf.append(first_chunk)
                        if probe_buf is not None:
                            probe_buf.append(first_chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        self._last_bytes += len(chunk)
                        if cache_buf is not None:
                            cache_buf.append(chunk)
                        if probe_buf is not None:
                            probe_buf.append(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if (cache_key and cache_buf and resp.status == 200
                        and self.server.cache is not None):
                    self.server.cache.put(cache_key, ProxyResponse(
                        status      = resp.status,
                        headers     = cache_headers,
                        body_chunks = cache_buf,
                        streaming   = False,
                    ))
                # Probe cache (/api/tags etc) — keyed on path.
                if (probe_buf is not None and resp.status == 200):
                    _probe_cache_record(self.path, resp.status,
                                          b"".join(probe_buf), cache_headers)
        except urllib.error.HTTPError as e:
            self._last_status = e.code
            self._last_error  = f"http {e.code}"
            self.send_response(e.code)
            for k, v in (e.headers or {}).items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(e.read() or b"")
            except Exception:
                pass
        except (urllib.error.URLError, ConnectionError, socket.timeout,
                  TimeoutError) as e:
            # Local upstream unreachable / stalled. Try cloud failover
            # for chat requests when the target is configured. Falls
            # through to 502 if cloud also fails or isn't an option.
            if cloud_target is not None and self._failover_to_cloud(
                    body, cloud_target, reason=f"upstream error: {e}"):
                return
            # If cloud failover was attempted (cloud_target was set)
            # but failed, _failover_to_cloud has already populated
            # _last_error with "failover failed: …". Preserve it.
            # When no cloud_target, surface the local upstream error
            # plainly. Either way diagnostic info reaches the audit.
            local_msg = f"upstream: {e}"
            cloud_msg = getattr(self, "_last_error", None)
            if cloud_target is not None and cloud_msg and cloud_msg.startswith("failover failed"):
                self._last_error = f"{cloud_msg}; {local_msg}"
            else:
                self._last_error = local_msg
            self.send_error(502, f"upstream proxy error: {e}")
        except Exception as e:
            self._last_error = f"upstream: {e}"
            self.send_error(502, f"upstream proxy error: {e}")

    def _cloud_urlopen(self, req: "urllib.request.Request", timeout: float):
        """Open a request to a cloud endpoint with the system CA bundle.
        Mirrors cloud.py's `_urlopen` helper — Guix-shipped Pythons
        don't include certifi, so bare `urllib.request.urlopen` against
        an HTTPS endpoint hits CERTIFICATE_VERIFY_FAILED.

        Phase 18.4-iter17: the previous fallback wrapper swallowed
        cert errors raised by `cloud._urlopen` itself, then re-tried
        with bare `urlopen`, which double-failed with the SAME SSL
        error — masking the real issue (e.g. cloud.py module not
        seeing the CA bundle in this proxy thread's process state).
        Now: import + call directly. If the helper raises, the
        original exception propagates to `_failover_to_cloud`'s
        catch site so the audit log records the actual cause."""
        from .cloud import _urlopen as _cloud_urlopen_helper
        return _cloud_urlopen_helper(req, timeout=timeout)

    def _failover_to_cloud(self, body: bytes, target: dict,
                              *, reason: str) -> bool:
        """Re-issue the chat request against the configured cloud
        provider and stream its response back. Returns True iff we
        committed bytes (status line + body) to the client. Caller
        must NOT write anything to `self.wfile` after a True return.

        On False, the local error path remains responsible for the
        502 fallback — typical when cloud also fails before sending
        any bytes. Best-effort: a partial cloud stream that drops
        mid-flight returns True (we already wrote headers).
        """
        parsed = getattr(self, "_parsed_for_audit", None)
        if parsed is None:
            try:
                parsed = json.loads(body.decode()) if body else None
            except Exception:
                parsed = None
        cloud_req = _build_cloud_request(body, parsed, target, self.path)
        try:
            with self._cloud_urlopen(cloud_req, timeout=300) as resp:
                self._last_status    = resp.status
                self._last_intercept = "cloud_failover"
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection",
                                       "content-length"):
                        continue
                    self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        self._last_bytes += len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return True
                self._last_error = (f"failover: local stalled "
                                     f"({reason}); served via cloud "
                                     f"({target['model']})")
                # Phase 18.4-iter13: notify the user via the file-
                # action bridge that a cloud failover just happened.
                # Without this, opencode's "model: llama3.2 / via
                # ollama · local" sidebar gives no clue the answer
                # actually came from the cloud — the failover is
                # transparent but the visibility cost is real.
                _emit_failover_toast(target['model'])
                return True
        except urllib.error.HTTPError as e:
            # Cloud responded with HTTP error — surface it. We've
            # not written anything yet; let the caller fall to 502.
            # Phase 18.4-iter18: capture the FIRST 400 chars of the
            # response body so the audit log explains WHY (cloud
            # provider error JSON usually includes a 'message' field
            # naming the bad parameter). Earlier we logged just the
            # status code, leaving the user without diagnostic info.
            try:
                err_body = (e.read() or b"").decode("utf-8", "replace")[:400]
            except Exception:
                err_body = ""

            # Phase 18.6: 400 "context length" two-step retry.
            #
            # Cloud free-tier models cap at 32k context. opencode's
            # 64-tool MCP inventory alone is ~19k tokens, plus 11k
            # conversation + 4k output reservation = >32k easily.
            # On HTTP 400 with a "context length" message, retry:
            #
            #   1. With COMPRESSED tools (descriptions trimmed to
            #      80 chars, schemas stripped of examples and long
            #      enums). Drops 64 tools from ~19k → ~3k tokens.
            #      Model still knows tool names and arg shapes;
            #      opencode still executes the calls. This is the
            #      preferred path: real tool use, terser docs.
            #
            #   2. With NO tools at all. Last resort. Model can't
            #      invoke anything but at least returns a chat
            #      reply. Audit logs as "cloud_failover_no_tools";
            #      compressed retries log as
            #      "cloud_failover_compressed_tools".
            ctx_overflow = (
                e.code == 400
                and ("context length" in err_body.lower()
                     or "maximum context" in err_body.lower()
                     or "too many tokens" in err_body.lower())
            )

            def _do_retry(retry_parsed: dict, label: str) -> bool:
                """Retry the cloud request with a modified parsed
                body. `label` becomes `_last_intercept` so the
                audit log reveals which retry path served the
                response. Returns True iff bytes were committed
                to the wfile (so the caller knows not to fall
                through to a 502)."""
                retry_req = _build_cloud_request(
                    body, retry_parsed, target, self.path,
                )
                with self._cloud_urlopen(retry_req, timeout=300) as resp2:
                    self._last_status    = resp2.status
                    self._last_intercept = label
                    self.send_response(resp2.status)
                    for k, v in resp2.headers.items():
                        if k.lower() in ("transfer-encoding",
                                          "connection",
                                          "content-length"):
                            continue
                        self.send_header(k, v)
                    self.end_headers()
                    while True:
                        chunk = resp2.read(4096)
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            self._last_bytes += len(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return True
                    self._last_error = (
                        f"failover: local stalled ({reason}); "
                        f"cloud full-tools rejected (ctx overflow); "
                        f"served via {label} ({target['model']})"
                    )
                    _emit_failover_toast(target['model'])
                    return True

            if ctx_overflow and parsed and parsed.get("tools"):
                # Step 1: compressed tools.
                compressed = dict(parsed)
                compressed["tools"] = _compress_tools_for_failover(
                    parsed["tools"],
                )
                try:
                    if _do_retry(compressed,
                                  "cloud_failover_compressed_tools"):
                        return True
                except urllib.error.HTTPError:
                    # Compressed still too big or other 4xx — fall
                    # through to no-tools.
                    pass
                except Exception:
                    # Network error mid-retry — fall through too.
                    pass

                # Step 2: no tools.
                no_tools = dict(parsed)
                no_tools.pop("tools", None)
                no_tools.pop("tool_choice", None)
                try:
                    if _do_retry(no_tools, "cloud_failover_no_tools"):
                        return True
                except Exception as e2:
                    self._last_error = (
                        f"failover failed: cloud http {e.code} (ctx "
                        f"overflow), both retries failed: "
                        f"{type(e2).__name__}: {e2}"
                    )
                    return False

            self._last_error = (f"failover failed: cloud http {e.code}"
                                  + (f" — {err_body}" if err_body else "")
                                  + f" after {reason}")
            return False
        except Exception as e:
            self._last_error = (f"failover failed: {type(e).__name__}: "
                                  f"{e} after {reason}")
            return False


class _ProxyServer(socketserver.ThreadingMixIn,
                    http.server.HTTPServer):
    """Threading variant so streaming SSE responses don't block
    other requests (opencode pipelines model probes alongside the
    chat completion stream)."""
    daemon_threads = True
    upstream:     str
    interceptors: list[Interceptor]
    audit:        Optional[AuditLogger]
    cache:        Optional[_PrefixCache]

    def handle_error(self, request, client_address) -> None:
        """Suppress benign client-disconnect tracebacks. opencode
        cancels in-flight streams when the user presses Esc /
        switches sessions / kills a request mid-flight; that
        manifests as ConnectionResetError or BrokenPipeError when
        we try to read the next request on the keep-alive socket
        OR write the next chunk to a closed peer.
        These are normal — the user has moved on, we have nothing
        more to send. The default `handle_error` prints the full
        traceback to stderr which then leaks onto the launching
        terminal and looks alarming. Real errors (parse errors,
        out-of-memory, unexpected exception types) still bubble
        through. """
        import sys
        exc_type, exc_val, _tb = sys.exc_info()
        if exc_type is not None and issubclass(exc_type, (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
        )):
            return                          # silent — not our problem
        # For anything else, fall back to the default (prints
        # traceback). We could route through AuditLogger here in
        # future for structured error capture.
        super().handle_error(request, client_address)


def start_proxy(
    upstream_url: str,
    interceptors: Optional[list[Interceptor]] = None,
    host: str = "127.0.0.1",
    port: int = 0,
    audit: Optional[AuditLogger] = None,
) -> tuple[int, _ProxyServer]:
    """Start the proxy on `host:port` (port=0 → OS-assigned).
    Returns (actual_port, server). Caller can `server.shutdown()`
    on tear-down or just leave the daemon thread running until
    process exit.

    `audit` defaults to the module-level `_default_audit` writing
    JSONL to `~/.local/share/org-llm/llm-audit.jsonl`. Pass
    `audit=AuditLogger(other_path)` for tests, or any object with
    a `.write(AuditEntry)` method to redirect (e.g. send to a
    metrics aggregator). Pass `audit=False` (well, None — but the
    semantics of None are "use default"); if you truly want to
    disable, pass an instance whose path lives in /dev/null. """
    server = _ProxyServer((host, port), _ProxyHandler)
    server.upstream     = upstream_url.rstrip("/")
    server.interceptors = list(interceptors or DEFAULT_INTERCEPTORS)
    server.audit        = audit if audit is not None else _default_audit
    server.cache        = _default_cache
    actual_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return actual_port, server
