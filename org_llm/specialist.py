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


# ── Extended tool defs for round-15 dial sweeps (D6, D8) ─────────────────
FIND_CANONICAL_ID_TOOL = {
    "type": "function",
    "function": {
        "name": "find_canonical_id",
        "description": (
            "Search the workdir for org-mode files whose top-level "
            "=:ID:= property is the canonical owner of the given "
            "label/concept. Returns a list of {file, id, title} matches "
            "or an empty list. Use this when you need a UUID for an "
            "[[id:UUID][label]] cross-link and the prefetch didn't "
            "include it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": (
                        "Concept label to search for (e.g. 'agents.org', "
                        "'recipes', 'DEC-005'). Matched against filenames + "
                        "titles."),
                }
            },
            "required": ["label"],
        },
    },
}

GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search for a regex pattern across files in the workdir. "
            "Returns matching lines with file paths + line numbers, "
            "capped at 50 hits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": (
                        "Subdir or file (relative to workdir). Defaults to "
                        "workdir if omitted."),
                },
            },
            "required": ["pattern"],
        },
    },
}

LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": (
            "List entries of a directory inside workdir. Returns names "
            "+ types (file/dir), capped at 100 entries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Subdir relative to workdir.",
                },
            },
            "required": ["path"],
        },
    },
}

BROAD_TOOLS = DEFAULT_TOOLS + [FIND_CANONICAL_ID_TOOL, GREP_TOOL, LIST_DIR_TOOL]


# ── Elisp tools (R15+) ───────────────────────────────────────────────────
# Org-llm is an Emacs distro; specialists working on .el files / org-mode
# parsing / vault hygiene need to actually run elisp, not just edit it.
# These tools shell to `emacs --batch` so they're stateless per call.

EVAL_ELISP_TOOL = {
    "type": "function",
    "function": {
        "name": "eval_elisp",
        "description": (
            "Evaluate one or more elisp forms in a fresh `emacs --batch` "
            "process. Returns stdout + stderr + the eval result. Useful "
            "for: testing a function you just wrote, parsing org-mode "
            "with native facilities, validating that a .el file loads "
            "cleanly. The process inherits no user config — load files "
            "you need via the optional `load_files` argument."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Elisp code to evaluate. Wrap in `progn` if "
                        "you need multiple forms."),
                },
                "load_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of .el files (relative to workdir "
                        "or absolute, must be inside workdir) to `load` "
                        "before evaluating `code`."),
                },
            },
            "required": ["code"],
        },
    },
}

LOAD_ELISP_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "load_elisp_file",
        "description": (
            "Load an elisp file in `emacs --batch` and report whether it "
            "loaded without errors. Quick smoke test for a .el file you "
            "just wrote/edited — confirms syntax + provides any byte-"
            "compile warnings via stderr."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                          "description": "Path to .el file inside workdir."},
            },
            "required": ["path"],
        },
    },
}

ELISP_TOOLS = [EVAL_ELISP_TOOL, LOAD_ELISP_FILE_TOOL]
BROAD_TOOLS_PLUS_ELISP = BROAD_TOOLS + ELISP_TOOLS


# ── OS / shell tools (R16+) — Claude-Code parity surface ────────────────
RUN_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Run a shell command in the workdir. Captures stdout, stderr, "
            "and exit code. Times out at 30s. Use for grep, find, sed, awk, "
            "diff, patch, wc, sort, uniq, etc. — and for invoking project "
            "tools like `python`, `pytest`, or `git`. Output capped at 4KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run. Quotes / pipes / "
                                   "redirection are supported.",
                },
            },
            "required": ["command"],
        },
    },
}

RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "Run a Python snippet via `python -c`. Workdir is cwd. Captures "
            "stdout/stderr/rc. Timeout 30s. Use for quick checks, AST "
            "parsing, regex testing, import smoke checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
            },
            "required": ["code"],
        },
    },
}

RUN_PYTEST_TOOL = {
    "type": "function",
    "function": {
        "name": "run_pytest",
        "description": (
            "Run pytest on a path (file or directory) inside workdir. "
            "Returns pass/fail counts + first 30 lines of output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "expr": {
                    "type": "string",
                    "description": "Optional `-k` filter expression",
                },
            },
            "required": ["path"],
        },
    },
}

GIT_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": (
            "Read-only git introspection in the workdir. Subcommand is one "
            "of: status, diff, log, show, blame. Args are passed verbatim "
            "after the subcommand. No mutating subcommands."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "enum": ["status", "diff", "log", "show", "blame"],
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra args (e.g. ['--stat'] or ['-3'])",
                },
            },
            "required": ["subcommand"],
        },
    },
}

OS_TOOLS = [RUN_SHELL_TOOL, RUN_PYTHON_TOOL, RUN_PYTEST_TOOL, GIT_STATUS_TOOL]


# ── Type-aware org tools (R16 Tier S L7) ─────────────────────────────────
ADD_PROPERTY_TOOL = {
    "type": "function",
    "function": {
        "name": "add_property",
        "description": (
            "Atomically write a key/value into the :PROPERTIES: drawer of an "
            "org heading identified by its :ID:. Creates the drawer if "
            "absent. Validates the file's org AST after write; reverts on "
            "failure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "heading_id": {"type": "string",
                                "description": ":ID: UUID of the heading"},
                "key": {"type": "string",
                          "description": "Property key (no leading colon)"},
                "value": {"type": "string"},
            },
            "required": ["file", "heading_id", "key", "value"],
        },
    },
}

SET_TODO_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "set_todo_state",
        "description": (
            "Set the TODO state of an org heading. Validates the new state "
            "is a recognized value (TODO, NEXT, STARTED, HOLD, WAITING, "
            "DONE, CANCELLED). Validates AST after."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "heading_id": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["TODO", "NEXT", "STARTED", "HOLD", "WAITING",
                              "DONE", "CANCELLED"],
                },
            },
            "required": ["file", "heading_id", "state"],
        },
    },
}

VALIDATE_ORG_TOOL = {
    "type": "function",
    "function": {
        "name": "validate_org",
        "description": (
            "Run `org-element-parse-buffer` on an org file via emacs --batch. "
            "Returns OK if AST parses cleanly, otherwise the error."
        ),
        "parameters": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
    },
}

ORG_TOOLS = [ADD_PROPERTY_TOOL, SET_TODO_STATE_TOOL, VALIDATE_ORG_TOOL]


# Composite surface — Claude-Code parity for R16
BROAD_TOOLS_FULL = (BROAD_TOOLS_PLUS_ELISP + OS_TOOLS + ORG_TOOLS)


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
    # When True, edit_file/write_file refuse paths outside target_files
    # (in addition to the always-on workdir-scope check). Round-15 D9.
    scope_strict: bool = False
    # R16 L8 — when True, edit_file/write_file run org-element-parse-buffer
    # post-write on .org files; revert if AST is broken.
    validate_after_edit: bool = False
    # R16 L19 — when True, prepend "iter X/N, $A used / $B cap, $C left"
    # to each iteration's user message for budget-aware decisiveness.
    inject_budget_status: bool = False
    # R16: optional response_format for constrained decoding (XGrammar /
    # JSON-schema). Passed verbatim to the chat-completions API. Used
    # for tasks where output must conform to a strict schema.
    response_format: Optional[dict] = None


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
    # O1+O2: timeline of significant events for live observability and
    # post-hoc audit. Each entry is a small dict with a "t" timestamp and
    # an "event" name. Harnesses can mirror this to events.jsonl.
    events: list[dict] = field(default_factory=list)
    # O5: per-tool call counts so the harness can spot which tools each
    # model actually reaches for under D8 broad surface.
    tool_use_breakdown: dict = field(default_factory=dict)


# ── HTTP / API helpers ───────────────────────────────────────────────────
def _read_api_key(pass_slug: str) -> str:
    return subprocess.run(
        ["pass", pass_slug],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# Models whose internal reasoning chain has consumed the entire max_tokens
# budget before emitting visible content / tool_calls. Per R14 B1 K2-kimi-k2.6
# silent no-op (finish_reason=length, $0.018, empty content) and 2026-05-07
# fix-slate F3f: OpenRouter's `reasoning: {enabled: false}` eliminates the
# thinking phase; tool_calls still fire correctly, completion drops to ~33
# tokens, cost ~4-7x lower, latency 2-4x faster. Allow-list — do NOT blanket
# this for all reasoning models (deepseek-r1 regresses with reasoning off).
_DISABLE_REASONING_MODELS = {
    "moonshotai/kimi-k2.6",
}


def _chat_completions(api_endpoint: str, api_key: str, model: str,
                       messages: list, tools: list,
                       temperature: float = 0.1,
                       max_tokens: int = 8000,
                       response_format: Optional[dict] = None) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if model in _DISABLE_REASONING_MODELS:
        payload["reasoning"] = {"enabled": False}
    if response_format:
        payload["response_format"] = response_format
    body = json.dumps(payload).encode()
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
def _resolve_in_workdir(target: Path, workdir: Path,
                          target_files_set: Optional[set] = None,
                          scope_strict: bool = False
                          ) -> tuple[bool, str, Optional[Path]]:
    """Resolve a path relative to workdir + check scope.

    Path resolution: relative paths are resolved against workdir (NOT
    Python's cwd — models naturally use paths like "docs/wiki/foo.org"
    expecting them to be repo-relative).

    Returns (ok, error_message_if_not_ok, resolved_path_if_ok).

    Always-on: must resolve inside workdir. When scope_strict=True AND a
    target_files_set is provided, must also be in that allow-list.
    Read ops only honor the workdir check; scope_strict applies to
    edit/write only.
    """
    if not target.is_absolute():
        target = workdir / target
    try:
        rp = target.resolve()
        rp.relative_to(workdir.resolve())
    except ValueError:
        return False, f"refused: path outside workdir {workdir}", None
    if scope_strict and target_files_set:
        if rp not in target_files_set:
            allowed = ", ".join(str(p) for p in sorted(target_files_set))
            return False, (f"refused (scope_strict): {rp} not in target_files "
                            f"allow-list [{allowed}]"), None
    return True, "", rp


def _validate_org_file(target: Path) -> tuple[bool, str]:
    """R16 L8 — post-edit validation gate. Run org-element-parse-buffer via
    `emacs --batch`. Returns (ok, error_message). Used by edit_file +
    write_file when target is a .org file and validate_after_edit is set."""
    try:
        cp = subprocess.run(
            ["emacs", "--batch", "-Q", "--eval",
              f'(progn (find-file "{target}") '
              f'(condition-case err '
              f'  (progn (org-element-parse-buffer) (princ "OK")) '
              f'  (error (princ (format "FAIL: %S" err)))))'],
            capture_output=True, text=True, timeout=20,
        )
        out = (cp.stdout or "").strip()
        ok_ = "OK" == out.split()[-1] if out else False
        return ok_, out if not ok_ else ""
    except FileNotFoundError:
        return True, ""   # emacs missing — silently skip validation
    except subprocess.TimeoutExpired:
        return True, ""   # don't block on slow validation


def _dispatch_tool_call(name: str, args: dict, workdir: Path,
                          target_files_set: Optional[set] = None,
                          scope_strict: bool = False,
                          validate_after_edit: bool = False) -> tuple[bool, str]:
    """Execute a tool call. Returns (ok, observation_text).

    When validate_after_edit=True and the edited target is a .org file,
    runs org-element-parse-buffer post-write; reverts on failure (R16 L8).
    """
    if name == "edit_file":
        path = args.get("path", "")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        ok, why, target = _resolve_in_workdir(
            target, workdir, target_files_set, scope_strict)
        if not ok: return False, why
        if not target.exists():
            return False, f"file not found: {path}"
        if target.is_dir():
            return False, f"refused: {path} is a directory, not a file"
        try:
            text = target.read_text()
        except (UnicodeDecodeError, OSError) as exc:
            return False, f"edit_file read failed: {exc.__class__.__name__}: {exc}"
        if old not in text:
            return False, f"old_string not found in file"
        if text.count(old) > 1:
            return False, (f"old_string matches {text.count(old)} times — "
                            f"add surrounding context to make it unique")
        new_text = text.replace(old, new, 1)
        target.write_text(new_text)
        # R16 L8 validation gate
        if validate_after_edit and str(target).endswith(".org"):
            ok_v, msg_v = _validate_org_file(target)
            if not ok_v:
                target.write_text(text)   # revert
                return False, (f"edit reverted — org AST validation failed:\n"
                                f"{msg_v[:300]}\nFix the org structure and "
                                f"retry.")
        return True, f"applied: 1 replacement in {path}"

    if name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        ok, why, target = _resolve_in_workdir(
            target, workdir, target_files_set, scope_strict)
        if not ok: return False, why
        if target.exists() and target.is_dir():
            return False, f"refused: {path} is a directory, not a file"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot for revert if validation triggers
        prior = target.read_text() if target.exists() else None
        target.write_text(content)
        if validate_after_edit and str(target).endswith(".org"):
            ok_v, msg_v = _validate_org_file(target)
            if not ok_v:
                if prior is not None:
                    target.write_text(prior)
                else:
                    target.unlink()
                return False, (f"write reverted — org AST validation failed:\n"
                                f"{msg_v[:300]}")
        return True, f"wrote {len(content)} chars to {path}"

    if name == "read_file":
        # Reads honor workdir scope but NOT scope_strict — specialists need
        # to read context (other org files, the prefetch source) to do work.
        path = args.get("path", "")
        if not path:
            return False, "missing path"
        target = Path(path)
        ok, why, target = _resolve_in_workdir(target, workdir, None, False)
        if not ok: return False, why
        if not target.exists():
            return False, f"file not found: {path}"
        if target.is_dir():
            return False, (f"refused: {path} is a directory, not a file. "
                            f"Use list_dir to enumerate it.")
        try:
            text = target.read_text()
        except (UnicodeDecodeError, OSError) as exc:
            return False, f"read_file: {exc.__class__.__name__}: {exc}"
        return True, text

    if name == "find_canonical_id":
        # Search workdir for org files whose :ID:/title match the label.
        label = (args.get("label") or "").strip()
        if not label:
            return False, "missing label"
        try:
            results = _find_canonical_id_search(workdir, label)
        except Exception as exc:
            return False, f"find_canonical_id failed: {exc}"
        if not results:
            return True, json.dumps([])
        return True, json.dumps(results[:10])

    if name == "grep":
        pattern = args.get("pattern") or ""
        sub_path = args.get("path") or ""
        if not pattern:
            return False, "missing pattern"
        target = (workdir / sub_path).resolve() if sub_path else workdir.resolve()
        try:
            target.relative_to(workdir.resolve())
        except ValueError:
            return False, f"refused: path outside workdir"
        try:
            out = subprocess.run(
                ["grep", "-rEn", "--", pattern, str(target)],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except subprocess.TimeoutExpired:
            return False, "grep: timeout"
        lines = out.splitlines()[:50]
        if not lines:
            return True, "(no matches)"
        return True, "\n".join(lines)

    if name == "list_dir":
        sub_path = args.get("path") or "."
        target = (workdir / sub_path).resolve()
        try:
            target.relative_to(workdir.resolve())
        except ValueError:
            return False, f"refused: path outside workdir"
        if not target.exists() or not target.is_dir():
            return False, f"not a directory: {sub_path}"
        entries = []
        for p in sorted(target.iterdir())[:100]:
            entries.append(("dir/" if p.is_dir() else "file/") + p.name)
        return True, "\n".join(entries) if entries else "(empty)"

    if name == "eval_elisp":
        code = args.get("code") or ""
        load_files = args.get("load_files") or []
        if not code:
            return False, "missing code"
        load_args = []
        for fp in load_files:
            ok, why, p = _resolve_in_workdir(Path(fp), workdir, None, False)
            if not ok: return False, why
            if not p.exists():
                return False, f"load_file not found: {fp}"
            load_args += ["-l", str(p)]
        # Wrap user code so output is captured deterministically.
        wrapped = (
            f"(condition-case err "
            f"  (let ((res {code})) (princ (format \"=> %S\" res)))"
            f"  (error (princ (format \"ERROR: %S\" err))))"
        )
        try:
            cp = subprocess.run(
                ["emacs", "--batch", "-Q", *load_args, "--eval", wrapped],
                capture_output=True, text=True, timeout=30,
            )
            out = (cp.stdout or "") + ((f"\n--stderr--\n{cp.stderr}")
                                         if cp.stderr else "")
            ok = (cp.returncode == 0 and "ERROR:" not in (cp.stdout or ""))
            return ok, out[:4000]
        except FileNotFoundError:
            return False, "emacs binary not on PATH"
        except subprocess.TimeoutExpired:
            return False, "emacs eval: timeout (30s)"

    if name == "load_elisp_file":
        path = args.get("path") or ""
        if not path:
            return False, "missing path"
        ok, why, target = _resolve_in_workdir(Path(path), workdir, None, False)
        if not ok: return False, why
        if not target.exists():
            return False, f"file not found: {path}"
        try:
            cp = subprocess.run(
                ["emacs", "--batch", "-Q", "-l", str(target),
                  "--eval", "(princ \"LOADED_OK\")"],
                capture_output=True, text=True, timeout=30,
            )
            ok = (cp.returncode == 0 and "LOADED_OK" in (cp.stdout or ""))
            out = ("OK: file loaded\n" if ok else "FAIL: load error\n") + \
                   (cp.stdout or "") + \
                   (f"\n--stderr--\n{cp.stderr}" if cp.stderr else "")
            return ok, out[:4000]
        except FileNotFoundError:
            return False, "emacs binary not on PATH"
        except subprocess.TimeoutExpired:
            return False, "emacs load: timeout (30s)"

    if name == "run_shell":
        cmd = args.get("command") or ""
        if not cmd:
            return False, "missing command"
        try:
            cp = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                cwd=str(workdir), timeout=30,
            )
            out = cp.stdout or ""
            err = cp.stderr or ""
            full = out + (("\n--stderr--\n" + err) if err else "")
            full = full[:4000]
            return cp.returncode == 0, f"rc={cp.returncode}\n{full}"
        except subprocess.TimeoutExpired:
            return False, "shell: timeout (30s)"
        except Exception as exc:
            return False, f"shell error: {exc.__class__.__name__}: {exc}"

    if name == "run_python":
        code = args.get("code") or ""
        if not code:
            return False, "missing code"
        try:
            cp = subprocess.run(
                ["python3", "-c", code],
                capture_output=True, text=True,
                cwd=str(workdir), timeout=30,
            )
            out = (cp.stdout or "") + (("\n--stderr--\n" + cp.stderr)
                                          if cp.stderr else "")
            return cp.returncode == 0, f"rc={cp.returncode}\n{out[:4000]}"
        except subprocess.TimeoutExpired:
            return False, "python: timeout (30s)"

    if name == "run_pytest":
        path = args.get("path") or ""
        expr = args.get("expr") or ""
        if not path:
            return False, "missing path"
        ok_, why, target = _resolve_in_workdir(Path(path), workdir, None, False)
        if not ok_: return False, why
        if not target.exists():
            return False, f"pytest target not found: {path}"
        cmd = ["python3", "-m", "pytest", str(target), "-x", "--no-header",
                "--tb=short", "-q"]
        if expr: cmd += ["-k", expr]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=str(workdir), timeout=60)
            lines = (cp.stdout or "").splitlines()[-30:]
            tail = "\n".join(lines)
            err = (cp.stderr or "")[:500]
            return (cp.returncode == 0,
                     f"rc={cp.returncode}\n{tail}"
                     + (f"\n--stderr--\n{err}" if err else ""))
        except subprocess.TimeoutExpired:
            return False, "pytest: timeout (60s)"

    if name == "git_status":
        sub = args.get("subcommand") or ""
        extra = args.get("args") or []
        if sub not in ("status", "diff", "log", "show", "blame"):
            return False, f"refused: subcommand {sub} not in allowed set"
        if not isinstance(extra, list): extra = [str(extra)]
        try:
            cp = subprocess.run(
                ["git", "-C", str(workdir), sub, *extra],
                capture_output=True, text=True, timeout=15,
            )
            out = (cp.stdout or "")[:4000]
            return cp.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, "git: timeout (15s)"

    if name == "validate_org":
        path = args.get("file") or args.get("path") or ""
        if not path:
            return False, "missing file"
        ok_, why, target = _resolve_in_workdir(Path(path), workdir, None, False)
        if not ok_: return False, why
        if not target.exists():
            return False, f"file not found: {path}"
        try:
            cp = subprocess.run(
                ["emacs", "--batch", "-Q", "--eval",
                  f'(progn (find-file "{target}") '
                  f'(condition-case err '
                  f'  (progn (org-element-parse-buffer) (princ "OK")) '
                  f'  (error (princ (format "FAIL: %S" err)))))'],
                capture_output=True, text=True, timeout=30,
            )
            out = (cp.stdout or "").strip()
            ok_ = "OK" in out and "FAIL" not in out
            return ok_, out + (f"\nstderr: {cp.stderr.strip()}"
                                  if cp.stderr.strip() and not ok_ else "")
        except subprocess.TimeoutExpired:
            return False, "emacs validate: timeout"
        except FileNotFoundError:
            return False, "emacs binary not on PATH"

    if name == "add_property":
        path = args.get("file") or ""
        hid = args.get("heading_id") or ""
        key = args.get("key") or ""
        value = args.get("value", "")
        if not (path and hid and key):
            return False, "missing file/heading_id/key"
        ok_, why, target = _resolve_in_workdir(
            Path(path), workdir, target_files_set, scope_strict)
        if not ok_: return False, why
        if not target.exists() or target.is_dir():
            return False, f"not a file: {path}"
        elisp = (
            f'(progn (find-file "{target}") '
            f'(if (org-find-property "ID" "{hid}") '
            f'  (progn (goto-char (org-find-property "ID" "{hid}")) '
            f'    (org-set-property "{key}" "{value}") '
            f'    (save-buffer) '
            f'    (princ (format "OK: set %s=%s on heading {hid}" "{key}" "{value}"))) '
            f'  (princ (format "ERR: heading id {hid} not found"))))'
        )
        try:
            cp = subprocess.run(
                ["emacs", "--batch", "-Q", "--eval", elisp],
                capture_output=True, text=True, timeout=20)
            out = cp.stdout or ""
            ok_ = cp.returncode == 0 and out.startswith("OK:")
            return ok_, out + (f"\nstderr: {cp.stderr[:200]}"
                                  if cp.stderr and not ok_ else "")
        except FileNotFoundError:
            return False, "emacs binary not on PATH"
        except subprocess.TimeoutExpired:
            return False, "add_property: timeout"

    if name == "set_todo_state":
        path = args.get("file") or ""
        hid = args.get("heading_id") or ""
        state = (args.get("state") or "").upper()
        ok_states = ("TODO", "NEXT", "STARTED", "HOLD", "WAITING",
                       "DONE", "CANCELLED")
        if state not in ok_states:
            return False, f"invalid state {state!r}; allowed {ok_states}"
        if not (path and hid):
            return False, "missing file/heading_id"
        ok_, why, target = _resolve_in_workdir(
            Path(path), workdir, target_files_set, scope_strict)
        if not ok_: return False, why
        if not target.exists() or target.is_dir():
            return False, f"not a file: {path}"
        elisp = (
            f'(progn (find-file "{target}") '
            f'(let ((m (org-find-property "ID" "{hid}"))) '
            f'  (if m (progn (goto-char m) '
            f'              (org-todo "{state}") '
            f'              (save-buffer) '
            f'              (princ (format "OK: set state {state}"))) '
            f'        (princ (format "ERR: id {hid} not found")))))'
        )
        try:
            cp = subprocess.run(
                ["emacs", "--batch", "-Q", "--eval", elisp],
                capture_output=True, text=True, timeout=20)
            out = cp.stdout or ""
            ok_ = cp.returncode == 0 and out.startswith("OK:")
            return ok_, out
        except FileNotFoundError:
            return False, "emacs binary not on PATH"
        except subprocess.TimeoutExpired:
            return False, "set_todo_state: timeout"

    return False, f"unknown tool: {name}"


def _find_canonical_id_search(workdir: Path, label: str) -> list[dict]:
    """Workdir-wide search for :ID: properties whose owner matches label.

    Searches docs/wiki/*.org first if present, falls back to whole workdir.
    Matches against (a) filename stem, (b) #+TITLE: line.
    """
    import re
    label_l = label.lower().strip()
    label_l_stripped = label_l.replace(".org", "").strip()
    candidates_root = workdir / "docs/wiki"
    if not candidates_root.exists():
        candidates_root = workdir
    results = []
    for org in candidates_root.rglob("*.org"):
        try:
            text = org.read_text()
        except Exception:
            continue
        m = re.search(r"^:ID:\s+([0-9a-f-]{8,})", text, re.MULTILINE)
        if not m:
            continue
        uid = m.group(1)
        title_m = re.search(r"^#\+TITLE:\s*(.+)$", text, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else org.stem
        # Match: filename stem, title text, or substring presence
        stem_l = org.stem.lower()
        title_l = title.lower()
        score = 0
        if label_l_stripped == stem_l: score += 100
        elif label_l_stripped in stem_l: score += 50
        if label_l in title_l: score += 25
        if score > 0:
            results.append({
                "file": str(org.relative_to(workdir)),
                "id": uid,
                "title": title,
                "score": score,
            })
    results.sort(key=lambda r: -r["score"])
    return results


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
    tool_names = [t["function"]["name"] for t in (task.tools or [])]
    user_msg += (
        f"\n\nWorkdir: {workdir}\n"
        f"Paths can be relative (resolved to workdir) or absolute. "
        f"Available tools: {', '.join(tool_names)}.\n\n"
        f"OPERATING DISCIPLINE — READ CAREFULLY:\n"
        f"- You have {task.max_iterations} iterations max. Use them to ACT, "
        f"not browse.\n"
        f"- Read the prefetch context FIRST. It usually has everything you "
        f"need.\n"
        f"- One read_file is usually enough. Don't fish.\n"
        f"- Skip tools you don't need. (e.g. don't use list_dir/grep/eval_elisp "
        f"unless the task actually requires them.)\n"
        f"- Be decisive. Make the edit. Errors are fine — you can fix them.\n\n"
        f"When done, return a brief one-line summary as your final message "
        f"with no tool_calls."
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    edits_applied: list[dict] = []
    cost_total = 0.0
    final_text = ""
    error: Optional[str] = None
    events: list[dict] = []
    tool_use_breakdown: dict = {}
    t0 = time.time()

    def _emit(name: str, **fields) -> None:
        ev = {"t": round(time.time() - t0, 3), "event": name, **fields}
        events.append(ev)
        # Hook for live mirroring (O2). Harness sets this on the task
        # via task.tools? No — we use a sentinel attr if present.
        cb = getattr(task, "_event_callback", None)
        if callable(cb):
            try: cb(ev)
            except Exception: pass

    _emit("run_start", model=task.model, handle=task.handle,
          target_files=[str(p) for p in task.target_files],
          scope_strict=task.scope_strict,
          tool_count=len(task.tools))

    for iteration in range(task.max_iterations):
        if cost_total >= task.max_budget_usd:
            error = f"budget cap reached: ${cost_total:.4f}"
            _emit("budget_exceeded", cost=round(cost_total, 6))
            break
        _emit("iter_start", iteration=iteration, cum_cost=round(cost_total, 6))

        # R16 L19 — token-budget transparency: prepend a one-line status
        # to the user message of the most recent turn so the model sees
        # how much budget it has left. Only every other iter to avoid noise.
        if task.inject_budget_status and iteration > 0 and iteration % 2 == 0:
            status = (f"[budget-status] iter {iteration}/{task.max_iterations}, "
                      f"${cost_total:.4f} of ${task.max_budget_usd:.2f} used "
                      f"({task.max_iterations - iteration} iters left). "
                      f"Be decisive — make the edits, don't browse.")
            messages.append({"role": "user", "content": status})

        resp = _chat_completions(
            task.api_endpoint, api_key, task.model,
            messages, task.tools,
            response_format=task.response_format,
        )
        if "_http_error" in resp:
            error = f"HTTP {resp['_http_error']}: {resp['_err_body'][:300]}"
            _emit("http_error", code=resp.get("_http_error"))
            break

        usage = resp.get("usage") or {}
        iter_cost = float(usage.get("cost") or 0)
        cost_total += iter_cost
        choices = resp.get("choices") or []
        if not choices:
            error = "no choices in response"
            _emit("no_choices")
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
            _emit("iter_end", iteration=iteration, iter_cost=round(iter_cost, 6),
                  cum_cost=round(cost_total, 6), tool_calls=0,
                  final_text_chars=len(final_text), finish_reason=finish_reason)
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
            tool_use_breakdown[fn_name] = tool_use_breakdown.get(fn_name, 0) + 1
            args_summary = {k: (str(v)[:60] + "..." if len(str(v)) > 60 else v)
                             for k, v in args.items()}
            _emit("tool_call", iteration=iteration, tool=fn_name,
                  args_summary=args_summary)
            try:
                ok, observation = _dispatch_tool_call(
                    fn_name, args, workdir,
                    target_files_set={p.resolve() for p in (task.target_files or [])},
                    scope_strict=task.scope_strict,
                    validate_after_edit=task.validate_after_edit,
                )
            except Exception as exc:
                ok = False
                observation = f"{fn_name}: unhandled {exc.__class__.__name__}: {exc}"
                _emit("tool_exception", iteration=iteration, tool=fn_name,
                      err=observation[:200])
            obs_short = observation[:120] + ("..." if len(observation) > 120 else "")
            _emit("tool_result", iteration=iteration, tool=fn_name,
                  ok=ok, obs=obs_short)
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

        _emit("iter_end", iteration=iteration, iter_cost=round(iter_cost, 6),
              cum_cost=round(cost_total, 6), tool_calls=len(tool_calls),
              finish_reason=finish_reason)
        if finish_reason == "stop" and not tool_calls:
            break
    else:
        error = f"max_iterations ({task.max_iterations}) reached"

    duration = time.time() - t0
    # Silent-no-op detection: a clean exit with neither edits nor narration
    # is a failure mode (model returned nothing actionable). Surfaces e.g.
    # round-14's kimi K2.6 cells as failed instead of indistinguishable
    # from "task already done, no-op correct."
    did_something = bool(edits_applied) or bool((final_text or "").strip())
    if error is None and not did_something:
        error = "silent_noop: no edits and no text_output"
        _emit("silent_noop")
    _emit("run_end", success=(error is None), error=error,
          edits=len(edits_applied), cost=round(cost_total, 6),
          duration=round(duration, 2))
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
        events=events,
        tool_use_breakdown=tool_use_breakdown,
    )
