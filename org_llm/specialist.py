"""Specialist runtime — direct FOSS API + tool-use, no opencode.

Per the rule "Agor crew uses no Claude" + the round-12 finding that
opencode silently drops completions for non-default FOSS models, this
module replaces opencode as the agentic_tool runtime for org-llm
specialists. It calls the FOSS model directly via OpenRouter (or any
OpenAI-compatible endpoint) with tool-use enabled, parses the model's
tool_calls, applies them to the filesystem, and loops until the model
finishes.

Used by:
- The two-phase pilot harness (round-13+) — bypasses Agor's opencode
  agentic_tool entirely; uses Agor for worktree + session metadata only.
- The `org-llm specialist run` CLI verb — wraps this module for one-shot
  invocations from shell or Doom org buffer.
- Future: an `agentic_tool="org-llm-specialist"` option in Agor itself
  (filed as upstream feature request).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── Default tool spec — file-edit ────────────────────────────────────────
EDIT_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Edit a file by replacing one exact occurrence of old_string "
            "with new_string. Both strings must match the file content "
            "exactly (whitespace included). The match must be unique — "
            "if old_string appears multiple times, include enough "
            "surrounding context to make it unique."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                          "description": "Absolute file path"},
                "old_string": {"type": "string",
                                "description": "Exact text to find"},
                "new_string": {"type": "string",
                                "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create a new file or overwrite an existing one with new "
            "content. Use sparingly — prefer edit_file for changes to "
            "existing files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file's content. Returns the full text.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

DEFAULT_TOOLS = [EDIT_FILE_TOOL, WRITE_FILE_TOOL, READ_FILE_TOOL]


# ── Task / result dataclasses ────────────────────────────────────────────
@dataclass
class SpecialistTask:
    """One specialist invocation."""
    handle: str
    persona: str
    instruction: str
    workdir: Path
    model: str = "qwen/qwen3-coder-30b-a3b-instruct"
    target_files: list[Path] = field(default_factory=list)
    max_iterations: int = 8
    max_budget_usd: float = 1.0
    tools: list[dict] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    api_endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    api_key_pass_slug: str = "org-llm/cloud/openrouter/api-key"


@dataclass
class SpecialistResult:
    handle: str
    success: bool
    iterations: int
    edits_applied: list[dict]
    text_output: str
    cost_usd: float
    duration_seconds: float
    error: Optional[str] = None
    raw_messages: list[dict] = field(default_factory=list)


# ── HTTP / API helpers ───────────────────────────────────────────────────
def _read_api_key(pass_slug: str) -> str:
    return subprocess.run(
        ["pass", pass_slug],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _chat_completions(api_endpoint: str, api_key: str, model: str,
                       messages: list, tools: list,
                       temperature: float = 0.1,
                       max_tokens: int = 3000) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        api_endpoint, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try: err_body = e.read().decode()
        except Exception: err_body = "(no body)"
        return {"_http_error": e.code, "_err_body": err_body}


# ── Tool dispatch ────────────────────────────────────────────────────────
def _dispatch_tool_call(name: str, args: dict, workdir: Path) -> tuple[bool, str]:
    """Execute a tool call. Returns (ok, observation_text)."""
    if name == "edit_file":
        path = args.get("path", "")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        # Require path to be inside workdir (safety)
        try:
            target.resolve().relative_to(workdir.resolve())
        except ValueError:
            return False, f"refused: path outside workdir {workdir}"
        if not target.exists():
            return False, f"file not found: {path}"
        text = target.read_text()
        if old not in text:
            return False, f"old_string not found in file"
        if text.count(old) > 1:
            return False, (f"old_string matches {text.count(old)} times — "
                            f"add surrounding context to make it unique")
        new_text = text.replace(old, new, 1)
        target.write_text(new_text)
        return True, f"applied: 1 replacement in {path}"

    if name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        try:
            target.resolve().relative_to(workdir.resolve())
        except ValueError:
            return False, f"refused: path outside workdir {workdir}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return True, f"wrote {len(content)} chars to {path}"

    if name == "read_file":
        path = args.get("path", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        try:
            target.resolve().relative_to(workdir.resolve())
        except ValueError:
            return False, f"refused: path outside workdir {workdir}"
        if not target.exists():
            return False, f"file not found: {path}"
        text = target.read_text()
        return True, text

    return False, f"unknown tool: {name}"


# ── Main entry point ─────────────────────────────────────────────────────
def run_specialist(task: SpecialistTask) -> SpecialistResult:
    """Run one specialist task. Multi-turn tool-use loop.

    Loops until: (a) model emits content with no tool_calls (done),
    (b) max_iterations reached, (c) budget exceeded.
    """
    api_key = _read_api_key(task.api_key_pass_slug)
    workdir = task.workdir.resolve()

    # Initial message: persona + instruction + file contents
    file_blocks = []
    for fp in task.target_files:
        if fp.exists():
            file_blocks.append(f"<file path=\"{fp}\">\n{fp.read_text()}\n</file>")
    files_section = "\n\n".join(file_blocks) if file_blocks else ""

    system_msg = task.persona
    user_msg = task.instruction
    if files_section:
        user_msg += f"\n\nRELEVANT FILES:\n{files_section}"
    user_msg += (
        f"\n\nWorkdir (path edits must stay within this): {workdir}\n"
        f"Use the available tools (edit_file, write_file, read_file) to do "
        f"your work. When done, write a brief one-line summary as your "
        f"final assistant message (no tool_calls)."
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    edits_applied: list[dict] = []
    cost_total = 0.0
    final_text = ""
    error: Optional[str] = None
    t0 = time.time()

    for iteration in range(task.max_iterations):
        if cost_total >= task.max_budget_usd:
            error = f"budget cap reached: ${cost_total:.4f}"
            break

        resp = _chat_completions(
            task.api_endpoint, api_key, task.model,
            messages, task.tools,
        )
        if "_http_error" in resp:
            error = f"HTTP {resp['_http_error']}: {resp['_err_body'][:300]}"
            break

        usage = resp.get("usage") or {}
        cost_total += float(usage.get("cost") or 0)
        choices = resp.get("choices") or []
        if not choices:
            error = "no choices in response"
            break
        msg = choices[0].get("message") or {}
        finish_reason = choices[0].get("finish_reason")

        # Append assistant message to history
        asst_entry: dict = {"role": "assistant"}
        if msg.get("content"):
            asst_entry["content"] = msg["content"]
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            asst_entry["tool_calls"] = tool_calls
        if "content" not in asst_entry and not tool_calls:
            asst_entry["content"] = ""
        messages.append(asst_entry)

        if not tool_calls:
            # Done
            final_text = msg.get("content") or ""
            break

        # Execute each tool call + append tool result
        for tc in tool_calls:
            tc_id = tc.get("id")
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            ok, observation = _dispatch_tool_call(fn_name, args, workdir)
            if ok and fn_name in ("edit_file", "write_file"):
                edits_applied.append({
                    "iteration": iteration,
                    "tool": fn_name,
                    "args": args,
                    "result": observation,
                })
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": observation,
            })

        if finish_reason == "stop" and not tool_calls:
            break
    else:
        error = f"max_iterations ({task.max_iterations}) reached"

    duration = time.time() - t0
    return SpecialistResult(
        handle=task.handle,
        success=(error is None),
        iterations=iteration + 1,
        edits_applied=edits_applied,
        text_output=final_text,
        cost_usd=round(cost_total, 6),
        duration_seconds=round(duration, 2),
        error=error,
        raw_messages=messages,
    )
