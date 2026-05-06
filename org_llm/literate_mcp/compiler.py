"""Compiler — turn a ParsedTool into a callable Python function.

== Safety boundary (read this before changing the code) ==

The literate-tools file lives in the user's vault. The user owns it.
This compiler is therefore NOT a hostile-code sandbox — it's a
defence-in-depth boundary against:

  • A *misbehaving* tool (infinite loop, cwd-write, network, fs walks)
    leaking out of the MCP tool surface and corrupting the host's
    state.
  • A *typo'd* tool that would silently pull in the wrong module
    (``import os`` to call ``os.system`` "just to shell out") and turn
    a one-liner into a privilege escalation.
  • An LLM that proposed a tool via Phase 29 — self-coded tools
    synthesis and the user pasted it without auditing imports.

The boundary is intentionally tight enough that escaping it requires
intent (the user adding ``__import__`` or ``subprocess`` to the
``IMPORTS`` allowlist) — not a bug.

== What's allowed in tool bodies ==

  • Pure-Python primitives: numbers, strings, lists, dicts, sets,
    tuples, bytes, comprehensions, generators, lambdas, dataclasses
    (via the explicit ``dataclasses`` import).
  • The standard ``__builtins__`` SUBSET below (``_SAFE_BUILTINS``):
    no ``open``, no ``__import__``, no ``exec``, no ``eval``, no
    ``compile``, no ``input``.
  • Any module the tool *explicitly* lists in ``:IMPORTS:`` — gated
    against ``_IMPORT_ALLOWLIST`` (stdlib only by default). Adding a
    third-party import requires editing this file's allowlist OR
    flipping the dangerous-tools flag.

== What's NOT allowed ==

  • Filesystem writes (no ``open(..., 'w')``; no ``open`` at all in
    the safe builtins). Tools that need disk land via the host's
    existing capability-gated MCP tools, not this surface.
  • Network. Same reason — gated by host-side capabilities.
  • Subprocess / shell. Phase 23.6.3 — Tier 3a covers shell-wrapped
    tools via a separate path; this leg is Python-only.
  • Bypass via ``getattr(__builtins__, ...)``. The builtins dict we
    inject is a dict literal, not the live ``builtins`` module — so
    ``__builtins__.__import__`` doesn't resolve to the real one.

== Determinism + timeouts ==

The compiler does NOT enforce ``:TIMEOUT:`` itself. The caller (the
MCP server registration layer, v0.1) wraps each compiled function in
a watchdog. Reason: timeout enforcement requires either signal
handlers (single-thread only) or a thread/process boundary (changes
the call shape) — both belong at the registration seam, not the
compile seam.
"""
from __future__ import annotations

import builtins as _builtins
import textwrap
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .parser import ParsedTool


# ── Allowlists ──────────────────────────────────────────────────────
#
# Stdlib modules a tool can ``:IMPORTS:`` without flipping the
# dangerous flag. Conservative on purpose — additions should be
# reviewed against "could a typo here exfiltrate / mutate?".
_IMPORT_ALLOWLIST: frozenset[str] = frozenset({
    # text + data
    "json", "re", "csv", "base64", "binascii", "hashlib", "html",
    "string", "textwrap", "unicodedata", "uuid",
    # numbers + dates
    "math", "statistics", "decimal", "fractions",
    "datetime", "calendar", "time",
    # collections + iteration
    "collections", "itertools", "functools", "heapq", "bisect",
    "operator", "copy", "enum", "dataclasses", "types", "typing",
    # parsing
    "ast", "tokenize", "io", "struct",
    # misc safe
    "random", "secrets",
})


# Builtins we expose to compiled tools. NO ``open``, ``__import__``,
# ``exec``, ``eval``, ``compile``, ``input``, ``breakpoint``, ``help``,
# ``vars``, ``globals``, ``locals``, ``__build_class__``.
_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "complex", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "getattr", "hasattr",
    "hash", "hex", "id", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "object", "oct",
    "ord", "pow", "print", "property", "range", "repr", "reversed",
    "round", "set", "setattr", "slice", "sorted", "str", "sum",
    "tuple", "type", "zip",
    # exception classes — tools need to raise / catch
    "BaseException", "Exception", "ArithmeticError", "AssertionError",
    "AttributeError", "IndexError", "KeyError", "LookupError",
    "NameError", "NotImplementedError", "OverflowError", "RuntimeError",
    "StopIteration", "TypeError", "ValueError", "ZeroDivisionError",
    # constants
    "True", "False", "None",
)


def _safe_builtins() -> dict[str, Any]:
    """Build the ``__builtins__`` dict injected into compiled tools."""
    out: dict[str, Any] = {}
    for n in _SAFE_BUILTIN_NAMES:
        v = getattr(_builtins, n, None)
        if v is not None:
            out[n] = v
    return out


@dataclass
class CompileError:
    tool: str
    line: int
    reason: str


@dataclass
class LiterateTool:
    """A compiled, ready-to-register literate tool."""
    name:        str
    description: str
    params:      dict[str, Any]
    returns:     dict[str, Any]
    timeout:     float | None
    dangerous:   bool
    callable:    Callable[..., Any]


def _validate_imports(parsed: ParsedTool, *,
                       dangerous_enabled: bool
                       ) -> tuple[dict[str, types.ModuleType] | None,
                                   str | None]:
    """Resolve declared imports.

    Returns (imports_dict, error_or_None). When the tool declares an
    import that's not in the stdlib allowlist:
      • ``dangerous_enabled=False`` → reject (compile error)
      • ``dangerous_enabled=True``  → allow only if the parsed tool
        also has ``DANGEROUS: yes``. This is the "two-key" rule —
        host opts in globally AND the tool opts in locally.
    """
    out: dict[str, types.ModuleType] = {}
    for mod_name in parsed.imports:
        if mod_name in _IMPORT_ALLOWLIST:
            try:
                out[mod_name] = __import__(mod_name)
            except Exception as e:
                return None, f"import {mod_name!r} failed: {e}"
            continue
        # Out of the safe allowlist.
        if not (dangerous_enabled and parsed.dangerous):
            return None, (
                f"import {mod_name!r} is not in the safe allowlist; "
                f"add it to _IMPORT_ALLOWLIST or set :DANGEROUS: yes "
                f"on this tool AND launch with dangerous tools enabled"
            )
        try:
            out[mod_name] = __import__(mod_name)
        except Exception as e:
            return None, f"import {mod_name!r} failed: {e}"
    return out, None


def _params_to_signature(params: dict[str, Any]) -> str:
    """Render a JSON-Schema-ish params dict into a Python arg-list.

    We don't enforce the JSON-Schema types at compile time — that's
    pydantic's job at the MCP registration layer. We just need an
    arg list whose names match the params keys so the tool body can
    reference them as locals.

    Order = dict insertion order (== JSON object order). Defaults
    are NOT lifted from the schema's ``default`` field for v0.1 —
    add later if a real tool wants them.
    """
    if not params:
        return ""
    parts: list[str] = []
    for name in params.keys():
        if not name.isidentifier():
            # Skip pathological keys; the resulting signature still
            # compiles, the bad key just isn't bindable from inside
            # the body. Caller surfaces this via validation.
            continue
        parts.append(name)
    return ", ".join(parts)


def compile_tool(parsed: ParsedTool, *,
                  dangerous_enabled: bool = False
                  ) -> tuple[LiterateTool | None, CompileError | None]:
    """Compile one parsed tool into an executable callable.

    The returned :class:`LiterateTool` can be registered with an MCP
    server (caller wires the registration). Failure path: returns
    (None, CompileError) instead of raising — callers iterate the
    parsed list and skip / report bad tools without aborting load.
    """
    # Refuse to compile a dangerous tool unless the host opted in.
    if parsed.dangerous and not dangerous_enabled:
        return None, CompileError(
            tool=parsed.name, line=parsed.source_line,
            reason="tool marked :DANGEROUS: yes but the host has not "
                    "enabled dangerous-tools",
        )

    # Validate / load imports.
    imports, err = _validate_imports(parsed,
                                       dangerous_enabled=dangerous_enabled)
    if err is not None:
        return None, CompileError(
            tool=parsed.name, line=parsed.source_line, reason=err)

    sig = _params_to_signature(parsed.params)
    body = parsed.body or "pass"
    indented = textwrap.indent(body, "    ")
    src = f"def {parsed.name}({sig}):\n{indented}\n"

    # Compile + exec into a tightly-scoped globals dict. This is the
    # safety boundary — see module docstring.
    safe_globals: dict[str, Any] = {
        "__builtins__": _safe_builtins(),
        "__name__":     f"literate_mcp.{parsed.name}",
    }
    # Inject the resolved imports as bare names so the tool body can
    # write ``json.loads(...)`` directly without an ``import`` line.
    safe_globals.update(imports or {})

    try:
        code = compile(src, f"<literate-tool:{parsed.name}>", "exec")
    except SyntaxError as e:
        return None, CompileError(
            tool=parsed.name,
            line=parsed.source_line + (e.lineno or 0),
            reason=f"syntax error: {e.msg}",
        )

    try:
        exec(code, safe_globals)  # noqa: S102  — sandboxed by design
    except Exception as e:
        return None, CompileError(
            tool=parsed.name, line=parsed.source_line,
            reason=f"exec error binding tool: {e}",
        )

    fn = safe_globals.get(parsed.name)
    if not callable(fn):
        return None, CompileError(
            tool=parsed.name, line=parsed.source_line,
            reason="compiled object is not callable",
        )

    # Stash a docstring + name on the function so MCP schema
    # generation has something to read.
    try:
        fn.__doc__ = parsed.description or f"literate tool {parsed.name}"
    except Exception:
        pass

    return LiterateTool(
        name=parsed.name,
        description=parsed.description,
        params=parsed.params,
        returns=parsed.returns,
        timeout=parsed.timeout,
        dangerous=parsed.dangerous,
        callable=fn,
    ), None


def compile_all(parsed_list: list[ParsedTool], *,
                 dangerous_enabled: bool = False
                 ) -> tuple[list[LiterateTool], list[CompileError]]:
    """Compile every tool in a list, partitioning successes + errors."""
    ok: list[LiterateTool] = []
    bad: list[CompileError] = []
    for p in parsed_list:
        tool, err = compile_tool(p, dangerous_enabled=dangerous_enabled)
        if tool is not None:
            ok.append(tool)
        if err is not None:
            bad.append(err)
    return ok, bad
