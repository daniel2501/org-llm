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
import re
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

# R26 P1-18 — MCP-mediated elisp eval via rhblind/emacs-mcp-server v0.7.0.
# Lazy import keeps `org_llm.mcp_emacs` optional (depends on a running
# Emacs daemon + the cloned server). Falls back to empty list when the
# module can't load — specialists keep working with `eval_elisp` /
# `load_elisp_file` (the `emacs --batch` variants).
try:
    from org_llm.mcp_emacs import MCP_EMACS_TOOLS as _MCP_EMACS_TOOLS
except Exception:  # pragma: no cover — optional dep
    _MCP_EMACS_TOOLS = []
ELISP_TOOLS_PLUS_MCP = ELISP_TOOLS + list(_MCP_EMACS_TOOLS)
BROAD_TOOLS_PLUS_ELISP_MCP = BROAD_TOOLS + ELISP_TOOLS_PLUS_MCP


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


# ── Vault RAG tool (R19 Track D) ─────────────────────────────────────────
# Imported lazily inside the dispatch so the rest of the specialist surface
# doesn't pay the qdrant-client / sentence-transformers import cost when
# vault_search isn't on the active toolset.
try:
    from org_llm.vault_rag import VAULT_SEARCH_TOOL  # noqa: F401
except ImportError:
    VAULT_SEARCH_TOOL = None  # type: ignore[assignment]

RAG_TOOLS = [VAULT_SEARCH_TOOL] if VAULT_SEARCH_TOOL else []


# ── org-roam-mcp tools (R26 P1-17) ──────────────────────────────────────
# Sister to vault_search above. Tools: mcp_roam_search_nodes,
# mcp_roam_get_node, mcp_roam_get_backlinks. Backed by org-roam's
# SQLite database (zero new deps; org-roam-mcp upstream wraps the same
# DB). Use for structural / exact-title queries; vault_search handles
# semantic prose queries.
try:
    from org_llm.mcp_org_roam import MCP_ROAM_TOOLS  # noqa: F401
except ImportError:
    MCP_ROAM_TOOLS = []  # type: ignore[assignment]


# ── org-mcp tools (R26 P1-16) ───────────────────────────────────────────
# Wraps laurynas-biveinis/org-mcp v0.9 (MELPA). Tools: mcp_org_list_todos,
# mcp_org_refile_node, mcp_org_create_node, mcp_org_query_agenda,
# mcp_org_search_by_tag. Each shells to `emacs --batch`; falls back to
# upstream org-mode primitives when mcp-server-lib isn't yet installed.
# See org_llm/mcp_org.py for status notes + tool-spec definitions.
try:
    from org_llm.mcp_org import MCP_TOOLS  # noqa: F401
except ImportError:
    MCP_TOOLS = []  # type: ignore[assignment]


# Composite surface — Claude-Code parity for R16; MCP tools added in R26
BROAD_TOOLS_FULL = (BROAD_TOOLS_PLUS_ELISP + OS_TOOLS + ORG_TOOLS + MCP_TOOLS)


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
    # R17: OpenRouter provider routing pin. Per cache audit 2026-05-07:
    # OpenRouter rotates across multiple brokers for the same model
    # (e.g. DeepSeek-V3 hit 3 different brokers on 5 sequential calls)
    # which kills prefix-cache hits. Pinning per route stabilizes both
    # cache + latency. Format: {"order": ["DeepSeek"]} or
    # {"order": ["Moonshot", "Parasail"], "allow_fallbacks": True}.
    provider_pin: Optional[dict] = None
    # R17 quality-judge fix #1 — when True, edit_file rejects any edit
    # whose post-state diff against the file's initial content shows
    # deletion lines. APPEND-ONLY tasks (B11 DEC entry drafting) need
    # this — R16 K1+K7 used edit_file to replace existing content's
    # entire body as old_string, deleting 151 and 146 lines respectively.
    append_only: bool = False
    # R17 quality-judge fix #2 — when True, edit_file rejects any new
    # text containing =verbatim= markers inside [[id:UUID][...]] link
    # labels (org renders =foo= literally inside link descriptions).
    forbid_verbatim_in_labels: bool = False
    # R17 quality-judge fix #3 — cap inlined RELEVANT FILES section to
    # last N chars (tail). Per R16 K7-qwen72b ctx-overflow on B11:
    # full decisions.org breached qwen's 32k context. Set per-task.
    max_file_inline_chars: Optional[int] = None
    # R17 quality-judge fix #5 — when True, edit_file on a .py file runs
    # `ast.parse` post-edit to ensure no FunctionDef has >1 docstring.
    forbid_stacked_docstrings: bool = False
    # R17 fix A5 — per-handle temperature override. None = use default 0.1.
    temperature_override: Optional[float] = None
    # R17 synthesis addition #5 — UUID enforcement gate. When True,
    # edit_file/write_file scan the new content for [[id:X][...]] links;
    # any X not present in the workdir's :ID: registry triggers a revert
    # with explicit error. Closes Claude's UUID-fabrication gap (1/B11
    # cell across both rounds; FOSS variants had 0 fabs).
    forbid_unknown_ids: bool = False
    # R25 P5 / R26 P0-2 — per-task max_tokens cap. When set, overrides
    # the default 8000 in _chat_completions OpenAI payload. Used to cap
    # K2 (kimi-k2.6) prose runaway: R25 K2 produced 22-28k char outputs
    # at default 8000-token ceiling (16 WALL_CAP_KILLED on long cells).
    # _R25_VARIANT_MAX_TOKENS in scripts/_round25_dials.py reads here.
    max_tokens: Optional[int] = None


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
    # R17 cache audit — sum of cached_tokens reported across iterations.
    cached_tokens_total: int = 0
    # R17 cache audit — which OpenRouter broker actually served (Parasail,
    # Moonshot, DeepInfra, Together, etc.). Useful when provider_pin is None
    # to identify which routes the model rotated through.
    provider_seen: Optional[str] = None


# ── HTTP / API helpers ───────────────────────────────────────────────────
_API_KEY_CACHE: dict[str, str] = {}


def _read_api_key(pass_slug: str) -> str:
    """Per-process cache for pass-derived API keys.

    R26 v1 hit a gpg-agent rate-limit storm at PARALLELISM=32 with ~10
    iterations per cell × 100+ cells: thousands of `pass` invocations
    cascaded into rc=2 failures on K8. Cache once per slug per process
    so the gpg decrypt happens at most once per (slug, harness run).
    """
    cached = _API_KEY_CACHE.get(pass_slug)
    if cached is not None:
        return cached
    val = subprocess.run(
        ["pass", pass_slug],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _API_KEY_CACHE[pass_slug] = val
    return val


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


_K20_FN_RE = re.compile(
    r'<function=(\w+)>\s*(.*?)\s*</function>',
    re.DOTALL,
)
_K20_PARAM_RE = re.compile(
    r'<parameter=(\w+)>\s*(.*?)\s*</parameter>',
    re.DOTALL,
)


def _maybe_inject_k20_tool_calls(data: dict) -> None:
    """In-place mutate `data` if K20-style tool calls are in content.

    K20 (Together-fine-tuned Qwen3-Coder-30B-A3B) emits:
        <tool_call>
        <function=NAME>
        <parameter=KEY>VALUE</parameter>
        ...
        </function>
        </tool_call>

    vLLM v0.20.1 hermes/pythonic parsers don't extract this format.
    Parse the text + populate `tool_calls` so the agent loop sees them.
    """
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        return
    if msg.get("tool_calls"):
        return  # parser already populated; nothing to do
    content = msg.get("content") or ""
    if "<function=" not in content:
        return
    parsed = []
    for fn_idx, m in enumerate(_K20_FN_RE.finditer(content)):
        name, body = m.group(1), m.group(2)
        args = {}
        for p in _K20_PARAM_RE.finditer(body):
            args[p.group(1)] = p.group(2).strip()
        parsed.append({
            "id": f"k20_call_{fn_idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    if parsed:
        msg["tool_calls"] = parsed
        # Strip the tool-call XML from content so the model's textual
        # commentary (if any) shows separately.
        msg["content"] = re.sub(r'<tool_call>.*?</tool_call>', '',
                                content, flags=re.DOTALL).strip()


# R28-4: K20 max-context clamp + pod-liveness re-poll.
# K20-v1 LoRA on RunPod ships with --max-model-len 32768 (R27 v6 fix from
# the 8192 default). Even at 32k, we MUST clamp output tokens so input +
# output ≤ ctx, or HTTP 400 fires (R27 cell_layer1/B1/K20-...__s4).
_K20_MAX_CTX = 32768
_K20_SAFETY_BUFFER = 256  # drop a bit for tokenizer edge-cases


def _estimate_tokens(messages: list, tools: list) -> int:
    """Rough char/4 heuristic — adequate for clamp purposes (we only need
    to pick max_tokens that won't blow the context, not exact accounting)."""
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total_chars += len(part.get("text", ""))
    for t in tools:
        # function name + description + JSON schema is non-trivial
        total_chars += len(json.dumps(t)) if isinstance(t, dict) else len(str(t))
    return total_chars // 4 + 200  # +200 for system overhead


def _clamp_max_tokens_for_k20(model: str, messages: list, tools: list,
                              max_tokens: int) -> int:
    """R28-4 — clamp output tokens to (K20_MAX_CTX - input - safety) for
    K20 models. R27 v5 cell ran HTTP 400 because wiki-prompt input was
    >8192 tokens with default ctx=8192 + max_tokens=8000. Now: ctx=32768
    AND clamped output, so we can never exceed ctx regardless of input."""
    if not model.startswith("k20-"):
        return max_tokens
    in_tokens = _estimate_tokens(messages, tools)
    available = _K20_MAX_CTX - in_tokens - _K20_SAFETY_BUFFER
    if available <= 0:
        return 256  # absolute floor; will likely fail but lets caller see cause
    return min(max_tokens, available)


def _k20_pod_liveness_check(api_endpoint: str) -> bool:
    """R28-4 — confirm K20 pod is live + serving k20-v1 model. Called on
    HTTP 404/empty-body to distinguish "pod paused at exit" (R27 v6 race)
    from a real model-not-found error. Returns True if /v1/models lists
    k20-v1, False otherwise (including any network failure)."""
    if "/v1/" not in api_endpoint:
        return False
    base = api_endpoint.split("/v1/")[0]
    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"User-Agent": "org-llm/0.1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            ids = [m.get("id") for m in data.get("data", [])]
            return "k20-v1" in ids
    except Exception:
        return False


def _chat_completions(api_endpoint: str, api_key: str, model: str,
                       messages: list, tools: list,
                       temperature: float = 0.1,
                       max_tokens: int = 8000,
                       response_format: Optional[dict] = None,
                       provider_pin: Optional[dict] = None,
                       max_retries: int = 2) -> dict:
    # R28-4: clamp max_tokens for K20 to avoid HTTP 400 ctx-overflow.
    max_tokens = _clamp_max_tokens_for_k20(model, messages, tools, max_tokens)
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
    if provider_pin:
        # R17 — OpenRouter `provider` field for per-route pinning.
        # Stabilizes prefix cache + latency. See cache audit 2026-05-07.
        payload["provider"] = provider_pin
    body = json.dumps(payload).encode()
    # R17 fix B2 — smart retry on transient HTTP errors (429/502/503/504).
    # Exponential backoff with jitter. Permanent errors (4xx except 429)
    # return immediately.
    import random as _random
    last_err = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            api_endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "org-llm/0.1 (https://github.com/daniel2501/org-llm)"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
                # R27 K20 fix: Together-fine-tuned Qwen3-Coder-30B emits
                # tool calls in text format `<function=NAME><parameter=K>V
                # </parameter>...</function>` which vLLM's hermes parser
                # doesn't recognize. Detect + parse + inject into
                # tool_calls when serving k20-* models and tool_calls is
                # empty. Belt-and-suspenders fix paired with pod-side
                # `--tool-call-parser pythonic` swap.
                if model.startswith("k20-"):
                    _maybe_inject_k20_tool_calls(data)
                return data
        except urllib.error.HTTPError as e:
            try: err_body = e.read().decode()
            except Exception: err_body = "(no body)"
            last_err = {"_http_error": e.code, "_err_body": err_body}
            # R28-4: on K20 HTTP 404 / empty-body, distinguish "pod paused
            # at exit" (race) from real model-404. If pod is not live,
            # mark cell with k20_unavailable so the cell isn't scored 0.
            if model.startswith("k20-") and e.code in (404, 502, 503):
                if not _k20_pod_liveness_check(api_endpoint):
                    return {"_http_error": e.code,
                              "_err_body": err_body,
                              "k20_unavailable": True}
            if e.code in (429, 502, 503, 504) and attempt < max_retries:
                wait = (2 ** attempt) + _random.uniform(0, 1)
                time.sleep(wait)
                continue
            return last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = {"_http_error": 0, "_err_body": f"URLError: {e}"}
            if attempt < max_retries:
                time.sleep((2 ** attempt) + _random.uniform(0, 1))
                continue
            return last_err
    return last_err or {"_http_error": -1, "_err_body": "no attempts made"}


# R17 fix A5 — per-handle temperature override. Passed through from harness.
PERSONA_TEMPERATURE_DEFAULTS = {
    "@atoz":     0.0,   # graph hygiene — precision over creativity
    "@spock":    0.0,   # logic + canon — precision
    "@boothby":  0.0,   # ops + hygiene — precision
    "@data":     0.1,   # code + scribe — slight creativity for naming
    "@riker":    0.2,   # process — moderate
    "@geordi":   0.4,   # analytics + charts — design choices
    "@picard":   0.1,   # captain — light creativity for plan synthesis
}


# ── Tool dispatch ────────────────────────────────────────────────────────
def _resolve_in_workdir(target: Path, workdir: Path,
                          target_files_set: Optional[set] = None,
                          scope_strict: bool = False
                          ) -> tuple[bool, str, Optional[Path]]:
    """Resolve a path relative to workdir + check scope.

    Path resolution: relative paths are resolved against workdir (NOT
    Python's cwd — models naturally use paths like "docs/wiki/foo.org"
    expecting them to be repo-relative).

    R27 B1 — K1 dispatch fix (Option A from r26-synthesis §4). The
    qwen3-coder-30B-A3B specialist defaults to emitting *absolute*
    worktree paths derived from =run_start.target_files= (file_blocks
    inject the absolute path at line ~1420). When the model echoes
    those back, we now strip any absolute prefix that matches a known
    repo root (worktree root OR parent =/home/daniel/repos/org-llm=)
    and treat the remainder as workdir-relative. This catches:
      (1) the canonical bug: model emits worktree-prefix/foo.org
          and we resolve it under workdir cleanly;
      (2) the cross-pollination bug: model emits org-llm-main-prefix/
          foo.org because the prefetch contained that path; we still
          resolve under the worktree.
    Bar (T9): K1 reads abs+rel path on B1 → both succeed.

    Returns (ok, error_message_if_not_ok, resolved_path_if_ok).

    Always-on: must resolve inside workdir. When scope_strict=True AND a
    target_files_set is provided, must also be in that allow-list.
    Read ops only honor the workdir check; scope_strict applies to
    edit/write only.
    """
    workdir_resolved = workdir.resolve()
    # B1 — strip-to-relative prefix normalization. Apply BEFORE the
    # workdir-relative prepend so absolute paths beginning with any
    # known repo root behave identically to relative ones.
    if target.is_absolute():
        target = _strip_known_repo_prefix(target, workdir_resolved)
    if not target.is_absolute():
        target = workdir / target
    try:
        rp = target.resolve()
        rp.relative_to(workdir_resolved)
    except ValueError:
        return False, f"refused: path outside workdir {workdir}", None
    if scope_strict and target_files_set:
        if rp not in target_files_set:
            allowed = ", ".join(str(p) for p in sorted(target_files_set))
            return False, (f"refused (scope_strict): {rp} not in target_files "
                            f"allow-list [{allowed}]"), None
    return True, "", rp


def _strip_known_repo_prefix(target: Path, workdir_resolved: Path) -> Path:
    """R27 B1 — strip any known repo-root prefix from an absolute path,
    returning a workdir-relative remainder when possible.

    Strip order:
      1. The current worktree root (workdir_resolved) — exact prefix.
      2. The parent /home/daniel/repos/org-llm/... main repo root,
         when the model accidentally echoes a main-repo path despite
         working in a worktree clone of the same tree shape.
      3. Any sibling worktree root under /home/daniel/repos/org-llm-
         worktrees/ — also fall through to the workdir-relative tail.

    If no prefix matches, the original absolute path is returned
    unchanged; the downstream =relative_to(workdir)= guard will then
    reject it as "path outside workdir" — same behavior as before.
    """
    try:
        target_resolved = target.resolve()
    except (OSError, RuntimeError):
        target_resolved = target
    target_str = str(target_resolved)
    # 1. Current worktree root — already-correct case; nothing to do.
    workdir_str = str(workdir_resolved)
    if target_str == workdir_str or target_str.startswith(workdir_str + "/"):
        return target_resolved
    # 2 + 3. Strip known repo roots and rebase on workdir.
    KNOWN_ROOTS = (
        "/home/daniel/repos/org-llm-worktrees",
        "/home/daniel/repos/org-llm",
    )
    for root in KNOWN_ROOTS:
        if target_str.startswith(root + "/"):
            tail = target_str[len(root) + 1:]
            # tail may itself start with a worktree dir name like
            # "r27-B1-K1-qwen30-...".  If so, drop that first segment
            # and treat the rest as workdir-relative.
            if root.endswith("worktrees"):
                # drop the worktree-name segment
                slash = tail.find("/")
                if slash != -1:
                    tail = tail[slash + 1:]
                else:
                    tail = ""
            if not tail:
                # caller meant the root itself
                return Path(".")
            return Path(tail)
    return target_resolved


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


def _check_append_only(old_text: str, new_text: str) -> tuple[bool, str]:
    """R17 fix #1 — append-only check. Returns (ok, error_msg).

    The new_text must contain old_text as a STARTING prefix; only
    appending allowed. If old_text is not a prefix, count the deletion
    lines for diagnostic.
    """
    if new_text.startswith(old_text):
        return True, ""
    import difflib
    diff = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        lineterm=""))
    deletions = sum(1 for line in diff
                       if line.startswith("-") and not line.startswith("---"))
    return False, (f"APPEND-ONLY violation: edit produced {deletions} deletion "
                    f"lines. This task is append-only — your new file content "
                    f"must contain the old content as a starting prefix. "
                    f"Only ADD at the end (use edit_file with surgical context "
                    f"from the file's tail).")


def _check_no_verbatim_in_labels(text: str) -> tuple[bool, str]:
    """R17 fix #2 — reject =foo= markers inside [[id:UUID][...]] labels."""
    import re as _re
    bad = _re.findall(r"\[\[id:[0-9a-f-]+\]\[[^\]]*=[^\]]*\]\]", text)
    if bad:
        return False, (f"VERBATIM-IN-LABEL violation: {len(bad)} link(s) have "
                        f"=foo= inside the label. Org-mode renders =foo= "
                        f"literally inside link descriptions. Strip the = "
                        f"markers before placing text inside [[id:UUID][...]].\n"
                        f"First offender: {bad[0][:200]}")
    return True, ""


_KNOWN_IDS_CACHE: dict = {"workdir": None, "ids": set()}


def _build_known_ids(workdir: Path) -> set:
    """Walk workdir's docs/wiki/*.org + docs/notes/*.org files; collect
    every top-level :ID: UUID. Cache per workdir."""
    if _KNOWN_IDS_CACHE["workdir"] == workdir:
        return _KNOWN_IDS_CACHE["ids"]
    import re as _re
    ids = set()
    for sub in ("docs/wiki", "docs/notes", "docs"):
        d = workdir / sub
        if not d.exists(): continue
        for org in d.rglob("*.org"):
            try: text = org.read_text()
            except Exception: continue
            for m in _re.finditer(r"^:ID:\s+([0-9a-f-]{8,})", text, _re.MULTILINE):
                ids.add(m.group(1))
    _KNOWN_IDS_CACHE["workdir"] = workdir
    _KNOWN_IDS_CACHE["ids"] = ids
    return ids


def _check_no_unknown_ids(text: str, workdir: Path) -> tuple[bool, str]:
    """R17 synthesis addition #5 — verify every [[id:X]] in text refers to
    a UUID that exists in workdir's :ID: registry. Closes UUID-fabrication."""
    import re as _re
    known = _build_known_ids(workdir)
    if not known:
        return True, ""   # no registry to compare against; permissive
    inserted = _re.findall(r"\[\[id:([0-9a-f-]{8,})\]", text)
    unknown = [u for u in inserted if u not in known]
    if unknown:
        return False, (f"UUID-fabrication: {len(unknown)} link(s) reference IDs "
                        f"not in the workdir's :ID: registry. The harness "
                        f"reverted your edit. Use find_canonical_id(label) to "
                        f"look up the real UUID, or check the prefetched "
                        f"candidates list. Unknown IDs: {unknown[:3]}")
    return True, ""


def _check_no_stacked_docstrings(path: Path) -> tuple[bool, str]:
    """R17 fix #5 — for Python files, ensure no FunctionDef has 2 docstrings."""
    try: import ast as _ast
    except ImportError: return True, ""
    try: tree = _ast.parse(path.read_text())
    except SyntaxError as exc:
        return False, f"STACKED-DOCSTRING check failed at parse: {exc}"
    bad = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                    _ast.ClassDef)):
            continue
        body = node.body or []
        if (len(body) >= 2 and isinstance(body[0], _ast.Expr)
              and isinstance(body[0].value, _ast.Constant)
              and isinstance(body[0].value.value, str)
              and isinstance(body[1], _ast.Expr)
              and isinstance(body[1].value, _ast.Constant)
              and isinstance(body[1].value.value, str)):
            bad.append(f"{node.name} (line {node.lineno})")
    if bad:
        return False, (f"STACKED-DOCSTRING violation: {len(bad)} function/class "
                        f"have 2 string literals at the top of body (looks like "
                        f"a duplicate docstring stacked on existing one). "
                        f"Offenders: {', '.join(bad[:5])}")
    return True, ""


def _dispatch_tool_call(name: str, args: dict, workdir: Path,
                          target_files_set: Optional[set] = None,
                          scope_strict: bool = False,
                          validate_after_edit: bool = False,
                          append_only: bool = False,
                          forbid_verbatim_in_labels: bool = False,
                          forbid_stacked_docstrings: bool = False,
                          forbid_unknown_ids: bool = False) -> tuple[bool, str]:
    """Execute a tool call. Returns (ok, observation_text).

    When validate_after_edit=True and the edited target is a .org file,
    runs org-element-parse-buffer post-write; reverts on failure (R16 L8).
    R17 also adds append_only / forbid_verbatim_in_labels / forbid_stacked_docstrings checks.
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
        # R26 P2-6 STARTER — pre-emit grammar gate. Reject structural
        # defects BEFORE the edit lands in the diff, so the cell loop
        # forces a retry instead of penalizing post-emit. Default OFF
        # via EDIT_GATE_ENABLED env var (R27 will flip ON).
        # Tier A U3 — gate_edit_with_k20 composes structural gate with
        # the optional K20 LoRA score (behind EDIT_GATE_K20_SCORE=1).
        # When K20 path is OFF, behavior is identical to gate_edit_file.
        from org_llm.edit_gate import (
            edit_gate_enabled,
            gate_edit_with_k20,
            k20_score_gate_enabled,
        )
        if edit_gate_enabled() or k20_score_gate_enabled():
            ok_g, reason = gate_edit_with_k20(path, old, new)
            if not ok_g:
                return False, (
                    f"edit rejected by pre-emit gate: {reason}; "
                    f"please fix and retry"
                )
        new_text = text.replace(old, new, 1)
        target.write_text(new_text)
        # R17 fix #1 — append-only diff shim
        if append_only:
            ok_v, msg_v = _check_append_only(text, new_text)
            if not ok_v:
                target.write_text(text)
                return False, f"edit reverted — {msg_v}"
        # R17 fix #2 — verbatim inside link labels
        if forbid_verbatim_in_labels:
            ok_v, msg_v = _check_no_verbatim_in_labels(new_text)
            if not ok_v:
                target.write_text(text)
                return False, f"edit reverted — {msg_v}"
        # R17 fix #5 — stacked docstrings on Python files
        if forbid_stacked_docstrings and str(target).endswith(".py"):
            ok_v, msg_v = _check_no_stacked_docstrings(target)
            if not ok_v:
                target.write_text(text)
                return False, f"edit reverted — {msg_v}"
        # R17 synthesis addition #5 — UUID-fabrication gate
        if forbid_unknown_ids:
            ok_v, msg_v = _check_no_unknown_ids(new_text, workdir)
            if not ok_v:
                target.write_text(text)
                return False, f"edit reverted — {msg_v}"
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
        if forbid_unknown_ids:
            ok_v, msg_v = _check_no_unknown_ids(content, workdir)
            if not ok_v:
                if prior is not None:
                    target.write_text(prior)
                else:
                    target.unlink()
                return False, f"write reverted — {msg_v}"
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

    # R26 P1-18 — MCP-mediated elisp eval via rhblind/emacs-mcp-server.
    # These coexist with eval_elisp / load_elisp_file above. The MCP
    # variants share a long-lived Emacs daemon (warm state, org-roam DB,
    # buffer cache) instead of spawning `emacs --batch` per call.
    if name.startswith("mcp_emacs_"):
        try:
            from org_llm import mcp_emacs as _me
        except Exception as exc:
            return False, f"mcp_emacs unavailable: {exc}"
        dispatchers = {
            "mcp_emacs_eval_elisp": _me.dispatch_eval_elisp,
            "mcp_emacs_read_buffer": _me.dispatch_read_buffer,
            "mcp_emacs_list_buffers": _me.dispatch_list_buffers,
            "mcp_emacs_find_file": _me.dispatch_find_file,
            "mcp_emacs_execute_command": _me.dispatch_execute_command,
            "mcp_emacs_get_diagnostics": _me.dispatch_get_diagnostics,
        }
        fn = dispatchers.get(name)
        if fn is None:
            return False, f"unknown mcp_emacs tool: {name}"
        return fn(args)

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

    if name == "vault_search":
        # R19 Track D — RAG retrieval over wiki + notes + vault.
        # Lazy-import so the heavy ML deps aren't pulled in unless used.
        query = (args.get("query") or "").strip()
        if not query:
            return False, "missing query"
        try:
            k = int(args.get("k") or 5)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(20, k))
        source = args.get("source")
        if source not in ("wiki", "notes", "vault", None):
            source = None
        try:
            from org_llm.vault_rag import vault_search as _vs
            hits = _vs(query, k=k, source_filter=source)
        except Exception as exc:
            return False, f"vault_search failed: {exc.__class__.__name__}: {exc}"
        return True, json.dumps(hits, ensure_ascii=False)

    if name == "mcp_roam_search_nodes":
        # R26 P1-17 — org-roam-mcp: title/alias/tag substring search.
        query = (args.get("query") or "").strip()
        if not query:
            return False, "missing query"
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(100, limit))
        try:
            from org_llm.mcp_org_roam import search_nodes as _sn
            hits = _sn(query, limit=limit)
        except Exception as exc:
            return False, f"mcp_roam_search_nodes failed: {exc.__class__.__name__}: {exc}"
        return True, json.dumps(hits, ensure_ascii=False)

    if name == "mcp_roam_get_node":
        # R26 P1-17 — org-roam-mcp: fetch single node by :ID:.
        node_id = (args.get("node_id") or "").strip()
        if not node_id:
            return False, "missing node_id"
        try:
            from org_llm.mcp_org_roam import get_node as _gn
            node = _gn(node_id)
        except Exception as exc:
            return False, f"mcp_roam_get_node failed: {exc.__class__.__name__}: {exc}"
        if not node:
            return True, json.dumps({"found": False, "node_id": node_id})
        return True, json.dumps(node, ensure_ascii=False)

    if name == "mcp_roam_get_backlinks":
        # R26 P1-17 — org-roam-mcp: nodes that link TO node_id.
        node_id = (args.get("node_id") or "").strip()
        if not node_id:
            return False, "missing node_id"
        try:
            from org_llm.mcp_org_roam import get_backlinks as _gb
            links = _gb(node_id)
        except Exception as exc:
            return False, f"mcp_roam_get_backlinks failed: {exc.__class__.__name__}: {exc}"
        return True, json.dumps(links, ensure_ascii=False)

    if name.startswith("mcp_org_"):
        # R26 P1-16 — org-mcp wrapper. Each tool dispatches to a function
        # in org_llm/mcp_org.py which shells to `emacs --batch`. Wrappers
        # return (ok, json-or-message) directly so we just forward.
        try:
            from org_llm import mcp_org as _mo
        except ImportError as exc:
            return False, f"org-mcp wrapper unavailable: {exc}"
        if name == "mcp_org_list_todos":
            return _mo.list_todos(workdir, args.get("state_filter"))
        if name == "mcp_org_refile_node":
            return _mo.refile_node(
                workdir, args.get("node_id") or "",
                args.get("target_path") or "")
        if name == "mcp_org_create_node":
            return _mo.create_node(
                workdir, args.get("parent_path") or "",
                args.get("title") or "", args.get("body") or "")
        if name == "mcp_org_query_agenda":
            try:
                days = int(args.get("days") or 7)
            except (TypeError, ValueError):
                days = 7
            return _mo.query_agenda(workdir, days)
        if name == "mcp_org_search_by_tag":
            return _mo.search_by_tag(workdir, args.get("tag") or "")
        return False, f"unknown mcp_org tool: {name}"

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

    # Initial message construction. Per R17 cache audit (2026-05-07):
    # ORDER MATTERS for OpenRouter prefix-caching. Stable bytes FIRST so the
    # cacheable prefix is maximized. Variable bytes (RELEVANT FILES with
    # workdir-specific paths, instruction-with-prefetch-JSON) come LAST.
    file_blocks = []
    for fp in task.target_files:
        if fp.exists():
            content = fp.read_text()
            # R17 fix #3 — cap inlined file at max_file_inline_chars (tail-truncate)
            # so prefetch + files don't blow past model context window.
            # Per R16 K7-qwen72b ctx-overflow on B11.
            if (task.max_file_inline_chars
                  and len(content) > task.max_file_inline_chars):
                content = ("...[truncated to last "
                            f"{task.max_file_inline_chars} chars]...\n"
                            + content[-task.max_file_inline_chars:])
            file_blocks.append(f"<file path=\"{fp}\">\n{content}\n</file>")
    files_section = "\n\n".join(file_blocks) if file_blocks else ""

    system_msg = task.persona
    tool_names = [t["function"]["name"] for t in (task.tools or [])]
    discipline = (
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
        f"Paths can be relative (resolved to workdir) or absolute.\n"
        f"When done, return a brief one-line summary as your final message "
        f"with no tool_calls."
    )
    # Stable prefix: discipline FIRST. Then instruction (per-task but mostly
    # constant after R17 reorder). Variable prefix LAST: workdir + file dump.
    user_msg = discipline + "\n\n" + task.instruction
    if files_section:
        user_msg += f"\n\nWorkdir: {workdir}\n\nRELEVANT FILES:\n{files_section}"
    else:
        user_msg += f"\n\nWorkdir: {workdir}"

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    edits_applied: list[dict] = []
    cost_total = 0.0
    cached_tokens_total: int = 0    # R17 cache audit metric
    provider_seen: Optional[str] = None
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

    # R17 — adaptive iter ceiling. Start with task.max_iterations, but
    # cut early if the model goes silent for 2 consecutive iterations
    # (no edits + no text). Saves wall on thrash cells without harming
    # productive ones.
    silent_iters_in_a_row = 0
    edits_per_iter: list[int] = []

    for iteration in range(task.max_iterations):
        if cost_total >= task.max_budget_usd:
            error = f"budget cap reached: ${cost_total:.4f}"
            _emit("budget_exceeded", cost=round(cost_total, 6))
            break
        # R17 adaptive cut: if the model has been silent for 2 iterations
        # in a row AND we've made no edits yet, abort the cell early.
        if (silent_iters_in_a_row >= 2 and not edits_applied
              and iteration >= 3):
            error = "adaptive_abort: 2 consecutive silent iterations, no edits"
            _emit("adaptive_abort", iteration=iteration,
                  silent_streak=silent_iters_in_a_row)
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

        # R17 fix A5 — per-handle temperature variance
        temp = (task.temperature_override
                  if task.temperature_override is not None
                  else PERSONA_TEMPERATURE_DEFAULTS.get(task.handle, 0.1))
        # R26 P0-2 — per-task max_tokens override (default 8000 if None)
        mt_kwargs = ({"max_tokens": task.max_tokens}
                     if task.max_tokens is not None else {})
        resp = _chat_completions(
            task.api_endpoint, api_key, task.model,
            messages, task.tools,
            response_format=task.response_format,
            provider_pin=task.provider_pin,
            temperature=temp,
            **mt_kwargs,
        )
        if "_http_error" in resp:
            error = f"HTTP {resp['_http_error']}: {resp['_err_body'][:300]}"
            _emit("http_error", code=resp.get("_http_error"))
            break

        usage = resp.get("usage") or {}
        iter_cost = float(usage.get("cost") or 0)
        cost_total += iter_cost
        # R17 — capture cache + provider per-iter for post-hoc audit
        ptd = (usage.get("prompt_tokens_details") or {})
        cached_tokens_iter = ptd.get("cached_tokens") or 0
        cached_tokens_total += cached_tokens_iter
        provider_seen = resp.get("provider") or provider_seen
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
                    append_only=task.append_only,
                    forbid_verbatim_in_labels=task.forbid_verbatim_in_labels,
                    forbid_stacked_docstrings=task.forbid_stacked_docstrings,
                    forbid_unknown_ids=task.forbid_unknown_ids,
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

        # R17 — track productivity for adaptive abort
        edits_this_iter = sum(1 for tc in tool_calls
                                 if (tc.get("function") or {}).get("name")
                                    in ("edit_file", "write_file"))
        edits_per_iter.append(edits_this_iter)
        if edits_this_iter == 0:
            silent_iters_in_a_row += 1
        else:
            silent_iters_in_a_row = 0

        # R17 fix A4 — self-correction nudge: if any tool result this iter
        # contained a revert or violation, append a coaching message that
        # the model will see at the start of next iter.
        recent_tool_msgs = messages[-len(tool_calls):]
        if any("reverted" in (m.get("content") or "").lower()
                  or "violation" in (m.get("content") or "").lower()
                  for m in recent_tool_msgs):
            messages.append({
                "role": "user",
                "content": ("[harness] Your last edit was reverted by a "
                              "validation check. Read the error message above "
                              "carefully — the harness rejected your edit for "
                              "a specific reason. Try again with a smaller, "
                              "more surgical edit that respects the rule."),
            })
            _emit("self_correction_nudge", iteration=iteration)

        _emit("iter_end", iteration=iteration, iter_cost=round(iter_cost, 6),
              cum_cost=round(cost_total, 6), tool_calls=len(tool_calls),
              finish_reason=finish_reason,
              edits_this_iter=edits_this_iter,
              silent_streak=silent_iters_in_a_row)
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
          duration=round(duration, 2),
          cached_tokens=cached_tokens_total,
          provider=provider_seen)
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
        cached_tokens_total=cached_tokens_total,
        provider_seen=provider_seen,
    )
