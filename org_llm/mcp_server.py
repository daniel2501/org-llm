# [[file:../../../org/20260425230731-org_llm.org::*mcp_server.py][mcp_server.py:1]]
from __future__ import annotations
from pathlib import Path
import asyncio
import functools
import inspect
import json
import os
import subprocess

# Imported at module top so FastMCP can resolve `Context | None` annotations
# on tool functions via inspect.get_annotations(eval_str=True). Inner-scope
# imports inside create_mcp_server() leave the symbol unresolvable from the
# tool's __globals__.
from mcp.server.fastmcp import Context

# Phase 24.2 — recovery hooks. The middle layer of the supervision
# trinity (DEC-006 — deterministic supervision). Imported here so
# the @recover_on_failure decorator below can dispatch through the
# registered hook chain (timeout → connection_error →
# missing_argument) on any tool exception. Hooks are deterministic
# and cannot tax the user's turn — backward-compatible default is
# RAISE (re-raise unchanged), so undecorated tools keep their
# existing behaviour.
from .recovery import (
    RecoveryAction   as _RecoveryAction,
    RecoveryContext  as _RecoveryContext,
    recover_from     as _recover_from,
)


def _format_mcp_error(tool_name: str, exc: Exception,
                       match_structured) -> str:
    """Friendly error string for MCP tool exceptions.

    The CLI's structured-rescue registry pattern-matches the exception
    against known shapes (Ollama 404, OOM, DB locked, etc.) and returns
    a deterministic recovery hint. We surface that to the LLM here so
    its next reply can quote the exact fix instead of dumping the
    Python traceback verbatim.
    """
    hint = match_structured(exc)
    head = f"ERROR in {tool_name}: {type(exc).__name__}: {str(exc)[:300]}"
    if hint is None:
        return head
    return (f"{head}\n\n"
            f"WHY: {hint.why}\n"
            f"FIX: {hint.fix}\n"
            f"(structured-rescue confidence: {hint.confidence})")


def _format_skip_message(tool_name: str, exc: BaseException) -> str:
    """Structured 'skipped' string for `RecoveryAction.SKIP`.

    The recovery hook decided this exception is non-load-bearing
    (e.g. a transient telemetry failure). We surface a short note
    so the LLM sees that the tool ran but produced no result
    rather than a silent empty string.
    """
    return (f"SKIPPED in {tool_name}: {type(exc).__name__}: "
             f"{str(exc)[:200]} — supervision deemed this "
             f"non-fatal; proceed with what you have.")


def _format_escalate_message(tool_name: str, exc: BaseException,
                                hint: str) -> str:
    """Structured hint string for `RecoveryAction.ESCALATE`.

    Surfaces the deterministic hint built by the matched hook
    (e.g. missing_argument's 'expected X, got Y' shape) so the LLM
    can self-correct on its next turn. Mirrors the resolver
    pattern: deterministic code prepares the LLM with the right
    next move, no observer agent required.
    """
    head = (f"ERROR in {tool_name}: {type(exc).__name__}: "
              f"{str(exc)[:200]}")
    if not hint:
        return head
    return f"{head}\n\nRECOVERY HINT: {hint}"


def recover_on_failure(fn):
    """Phase 24.2 — recovery hooks: per-tool exception router.

    Wraps a tool function so any exception is dispatched through
    the recovery registry (`recover_from(exc, ctx)`). The four
    possible outcomes:

      - `RETRY`    → call the tool one more time (single retry for
                     v0.1; the hook may have mutated `ctx` to
                     extend a deadline / sleep for backoff /
                     etc., and returns `RAISE` itself once its
                     attempt budget is exhausted).
      - `SKIP`     → return a structured 'skipped' string; the
                     LLM proceeds without the tool's result.
      - `ESCALATE` → return a structured hint string built by the
                     matched hook so the LLM can self-correct on
                     its next turn (e.g. missing-argument fixes).
      - `RAISE`    → re-raise the original exception unchanged
                     (the default for unknown exceptions). The
                     existing `_wrap_tool_decorator` rescue path
                     catches it and runs `match_structured`, so
                     undecorated behaviour is preserved.

    Single retry by design: hooks themselves enforce per-class
    attempt budgets via `ctx.attempts` (timeout: 2 attempts;
    connection: 3 attempts; missing-argument: ESCALATE-only).
    The decorator only re-invokes once because the framework
    rule is 'a single retry is enough for v0.1' — repeated
    re-invocation would let a misbehaving hook spin the tool
    indefinitely. Tools requiring multi-attempt loops belong in
    a dedicated worker (e.g. dbt_run) not the supervision layer.

    Works on both sync and async tool functions. The wrapper
    preserves `__annotations__` + `__wrapped__` so FastMCP's
    pydantic schema introspection still sees the original
    signature.
    """
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _async_recovered(*a, **kw):
            ctx = _RecoveryContext(
                tool=fn.__name__,
                args=dict(kw),
                attempts=1,
                timeout_s=float(kw.get("timeout_s")
                                 or kw.get("timeout") or 0.0),
            )
            try:
                return await fn(*a, **kw)
            except Exception as exc:
                action = _recover_from(exc, ctx)
                if action == _RecoveryAction.RETRY:
                    ctx.attempts += 1
                    # Hooks that extended ctx.timeout_s could
                    # update kw here on a future iteration; for
                    # v0.1 we only carry the timeout hint via
                    # ctx.meta, which the hook already used.
                    return await fn(*a, **kw)
                if action == _RecoveryAction.SKIP:
                    return _format_skip_message(fn.__name__, exc)
                if action == _RecoveryAction.ESCALATE:
                    return _format_escalate_message(
                        fn.__name__, exc, ctx.hint)
                # RAISE — fall through to existing rescue path.
                raise
        # Preserve annotations explicitly: functools.wraps copies
        # __wrapped__ + __annotations__, which is what FastMCP
        # introspects via inspect.get_annotations(eval_str=True).
        return _async_recovered

    @functools.wraps(fn)
    def _sync_recovered(*a, **kw):
        ctx = _RecoveryContext(
            tool=fn.__name__,
            args=dict(kw),
            attempts=1,
            timeout_s=float(kw.get("timeout_s")
                             or kw.get("timeout") or 0.0),
        )
        try:
            return fn(*a, **kw)
        except Exception as exc:
            action = _recover_from(exc, ctx)
            if action == _RecoveryAction.RETRY:
                ctx.attempts += 1
                return fn(*a, **kw)
            if action == _RecoveryAction.SKIP:
                return _format_skip_message(fn.__name__, exc)
            if action == _RecoveryAction.ESCALATE:
                return _format_escalate_message(
                    fn.__name__, exc, ctx.hint)
            # RAISE — fall through to existing rescue path.
            raise

    return _sync_recovered


def _make_engine():
    from .db import DB_PATH, make_engine
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key: str) -> str:
    from .db import Config
    row = session.get(Config, key)
    return row.value if row else ""


def _themed(tool: str, summary: str, body: str = "",
              outcome: str = "ok") -> str:
    """Wrap a tool's return string in the LCARS chat vocabulary so opencode
    output keeps the same visual rhythm as the CLI.

    Format:
        ◀ <tool> — <summary>
        ────────────────────────────────────────────────
        <body>
        ↳ <themed success/error suffix from theme_studio>

    Body is optional — short tool returns (e.g. capture_note returning a
    node ID) get just the header + suffix. Free-text returns from
    search/ask/etc. get the full sandwich. Clients without rich
    rendering still see plain text; clients with monospace blocks
    render the separator cleanly.

    `outcome` is "ok" (default) or "error" — controls which themed
    suffix from the theme_studio cache gets appended. Cold cache
    falls back to the surface defaults baked into the registry.
    """
    head = f"◀ {tool} — {summary}"
    # Suffix lookup: theme_studio.get_themed transparently returns
    # the surface default when the cache is cold or LLM-down, so this
    # stays cheap and never blocks.
    try:
        from . import theme_studio as _ts
        if outcome == "error":
            suffix = _ts.get_themed("mcp_tool_error_suffix", "↳ red alert")
        else:
            suffix = _ts.get_themed("mcp_tool_success_suffix", "↳ done.")
    except Exception:
        suffix = "↳ red alert" if outcome == "error" else "↳ done."
    if not body:
        return f"{head}\n{suffix}"
    sep = "─" * 60
    return f"{head}\n{sep}\n{body.rstrip()}\n{suffix}"


def _theme_label(op: str) -> str:
    """Themed phrase for an op key — borrows the same TREK_MSGS table the
    CLI's spinners use, so MCP progress messages match the on-screen vibe
    (e.g. embed → "Initializing deflector array"). Falls through to the
    default phrase when the key isn't in the table."""
    try:
        from .ui import TREK_MSGS
        return TREK_MSGS.get(op) or TREK_MSGS.get("default") or op
    except Exception:
        return op


async def _report(ctx, progress: float, total: float | None,
                   message: str | None = None) -> None:
    """Best-effort MCP progress notification.

    `ctx.report_progress` requires the caller (e.g. Claude Code) to have
    set a progressToken on the original tool request. When the client
    didn't ask for progress (e.g. opencode today), the call no-ops or
    raises — we swallow so instrumentation never breaks tool execution.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=progress, total=total,
                                    message=message)
    except Exception:
        pass


async def _info(ctx, message: str) -> None:
    """Best-effort MCP log notification — same swallow-everything story."""
    if ctx is None:
        return
    try:
        await ctx.info(message)
    except Exception:
        pass


def create_mcp_server():
    from mcp.server.fastmcp import FastMCP
    from .db import get_session

    engine = _make_engine()

    # ── structured-rescue wrapper for every MCP tool ──────────────────────────
    # When an exception escapes a tool body, FastMCP returns a generic
    # JSON-RPC error and the LLM in the MCP client (opencode, Claude
    # Code, Pi, …) sees an opaque
    # traceback string. Wrap the `server.tool` decorator so every
    # subsequently-registered tool runs through the same rescue path
    # the CLI uses: structured registry first (Ollama 404 → "pull X",
    # OOM → "doctor --power-boost", etc.), then a sanitized error
    # string the LLM can act on.
    def _wrap_tool_decorator(_orig_tool):
        from .rescue import match_structured as _ms
        import functools as _ft

        def _copy_intro(wrapper, fn):
            """Copy enough of `fn`'s introspection metadata onto
            `wrapper` that FastMCP + pydantic can build the tool
            schema. Plain `functools.wraps` doesn't copy
            `__annotations__`, which pydantic inspects to derive
            arg types — and our wrappers carry `*a, **kw` so they
            have no annotations of their own."""
            _ft.update_wrapper(wrapper, fn,
                                updated=())   # don't merge __dict__
            wrapper.__annotations__ = dict(fn.__annotations__)
            wrapper.__wrapped__ = fn
            return wrapper

        def _wrapped_decorator(*args, **kwargs):
            inner = _orig_tool(*args, **kwargs)

            def wrap_fn(fn):
                if asyncio.iscoroutinefunction(fn):
                    async def _async_wrap(*a, **kw):
                        try:
                            return await fn(*a, **kw)
                        except Exception as e:
                            return _format_mcp_error(fn.__name__, e, _ms)
                    return inner(_copy_intro(_async_wrap, fn))
                else:
                    def _sync_wrap(*a, **kw):
                        try:
                            return fn(*a, **kw)
                        except Exception as e:
                            return _format_mcp_error(fn.__name__, e, _ms)
                    return inner(_copy_intro(_sync_wrap, fn))
            return wrap_fn

        return _wrapped_decorator

    server = FastMCP(
        "org-llm",
        instructions=(
            "You are connected to the user's org-roam knowledge base via org-llm.\n"
            "\n"
            "CRITICAL — FOLLOW BEFORE ANYTHING ELSE:\n"
            "1. The user has thousands of indexed notes covering their work,\n"
            "   life, projects, reading, opinions, and history. ANY question\n"
            "   that could be answered from those notes MUST start with a\n"
            "   `search_notes` or `ask_notes` call. NO exceptions.\n"
            "2. NEVER say \"I don't have access to your personal X\", \"I can't\n"
            "   recall\", \"you'd need to tell me yourself\", or \"I have no\n"
            "   memory of past conversations\". Those phrases are BUGS in this\n"
            "   workspace. The user's life IS in the vault. Search FIRST.\n"
            "3. Question patterns that ALWAYS demand a search call before a\n"
            "   reply: \"have I X\", \"did I ever Y\", \"what did I write about Z\",\n"
            "   \"where am I with project W\", \"who is V\", \"remind me about U\",\n"
            "   even \"how do I feel about T\".\n"
            "4. When in doubt, search. Try synonyms (\"Marx\" → \"Das Kapital\" →\n"
            "   \"political economy\") before concluding the vault doesn't cover\n"
            "   the topic. Searches are cheap; missed answers are expensive.\n"
            "\n"
            "Tools beyond search: capture_note (save ideas), run_skill (execute\n"
            "org-babel workflows), code_search (cross-corpus code), org_llm_run\n"
            "(any allow-listed CLI verb with auto-fix), and the dbt_* tools for\n"
            "the analytics layer. Always search before answering questions about\n"
            "the user's notes.\n"
            "\n"
            "LAPTOP HEALTH QUESTIONS — different ground truth, different tools:\n"
            "When the user asks about their LAPTOP (battery, CPU, memory, disk,\n"
            "thermals, Ollama daemon, the auto-embedder), the answer is in\n"
            "sensor_log NOT in the vault. Use:\n"
            "  - `life_support_status`  — fresh probe of all 8 vital systems\n"
            "  - `life_support_history` — last N minutes of timeseries\n"
            "  - `life_support_advice`  — LLM optimisation suggestions from\n"
            "                              the timeseries (deterministic floor)\n"
            "  - `emh_consult`          — Voyager EMH persona; question mode\n"
            "                              for historical questions, or\n"
            "                              diagnose=True for a full systematic\n"
            "                              check. Cites specific probes +\n"
            "                              activity correlations.\n"
            "Use these WHEN the question is about the host system. Don't\n"
            "search_notes for 'why is my CPU pegged' — that's an EMH job.\n"
            "\n"
            "WALK + TEACH — context grows over time:\n"
            "When the user says 'walk me through my notes', 'review my\n"
            "vault', 'what should I think about today?', or asks you to\n"
            "help them tag/organize notes:\n"
            "  - `walk_pick_targets`        — N candidate notes worth\n"
            "                                  a walk\n"
            "  - `walk_extract_facts`       — mine atomic claims from\n"
            "                                  the user's response\n"
            "  - `walk_save_facts(user_approved=True)`\n"
            "                               — persist to active-facts\n"
            "                                  (PERMISSIVE: substantive\n"
            "                                  teaching response IS the\n"
            "                                  consent; no need to\n"
            "                                  re-ask 'should I save?')\n"
            "                                  REJECTS bundled facts\n"
            "                                  (>180c, 'history:' prefix,\n"
            "                                  3+ commas + 'then'). Each\n"
            "                                  fact must be ATOMIC: one\n"
            "                                  role, one project, one\n"
            "                                  date.\n"
            "  - `walk_save_disambiguation(user_approved=True)`\n"
            "                               — for 'X means Y in period A\n"
            "                                  but Z in period B' kinds\n"
            "                                  of context (lands in the\n"
            "                                  Disambiguations section,\n"
            "                                  not active-facts)\n"
            "  - `walk_undo_last_save(confirm=True)`\n"
            "                               — pop the most recent save\n"
            "                                  if user says 'actually\n"
            "                                  no' / 'undo that'\n"
            "  - `walk_remove_fact(line, user_approved=True)`\n"
            "                               — surgically remove ONE\n"
            "                                  fact (any save order),\n"
            "                                  case-sensitive match\n"
            "  - `walk_review_pending`      — list old facts for\n"
            "                                  re-confirmation\n"
            "After saving, ECHO the EXACT lines the save tool returns —\n"
            "do NOT paraphrase or beautify into a prettier summary that\n"
            "diverges from what's actually in the file."
        ),
    )

    # Apply the rescue wrapper *before* any tool registration below.
    server.tool = _wrap_tool_decorator(server.tool)

    # ── search_notes ──────────────────────────────────────────────────────────
    @server.tool()
    def search_notes(query: str, limit: int = 10, keyword: bool = False) -> str:
        """Semantic search over org-roam notes (keyword=True for exact text match)."""
        with get_session(engine) as session:
            url   = _cfg(session, "ollama_url") or "http://localhost:11434"
            model = _cfg(session, "embed_model") or "nomic-embed-text"
            if keyword:
                from .search import keyword_search
                results = keyword_search(session, query, limit=limit)
            else:
                try:
                    from .search import vector_search
                    from .llm import embed
                    qvec    = embed(query, model=model, base_url=url, task="query")
                    results = vector_search(session, qvec, limit=limit)
                except Exception:
                    from .search import keyword_search
                    results = keyword_search(session, query, limit=limit)
        if not results:
            return _themed("search_notes",
                            f"no hits for {query!r}",
                            "Try a synonym or broader phrase.")
        body = "\n\n---\n\n".join(
            f"**{r.title}**\nFile: {Path(r.file_path).name}\n"
            f"Tags: {r.tags or 'none'}\n\n{r.body[:600]}"
            for r in results
        )
        mode = "keyword" if keyword else "semantic"
        return _themed("search_notes",
                        f"{len(results)} {mode} hit(s) for {query!r}",
                        body)

    # ── ask_notes ─────────────────────────────────────────────────────────────
    @server.tool()
    async def ask_notes(question: str, top_k: int = 6,
                          ctx: Context | None = None) -> str:
        """Answer a question using RAG over org notes.

        Three-phase progress: 1/3 embedding the query, 2/3 vector
        search, 3/3 LLM synthesis. Themed via TREK_MSGS["ask"]
        ("Hailing frequencies open")."""
        from .llm import embed, chat
        from .search import vector_search
        label = _theme_label("ask")
        with get_session(engine) as session:
            url         = _cfg(session, "ollama_url") or "http://localhost:11434"
            embed_model = _cfg(session, "embed_model") or "nomic-embed-text"
            chat_model  = _cfg(session, "chat_model")  or "llama3.2"
            await _report(ctx, 1, 3, f"{label} — embedding query")
            try:
                qvec    = embed(question, model=embed_model, base_url=url, task="query")
            except Exception as e:
                return f"Search error: {e}"
            await _report(ctx, 2, 3, f"{label} — vector search (top {top_k})")
            try:
                results = vector_search(session, qvec, limit=top_k)
            except Exception as e:
                return f"Search error: {e}"
            if not results:
                await _report(ctx, 3, 3, f"{label} — no matches")
                return _themed("ask_notes",
                                f"no relevant notes for {question!r}",
                                "Try a different phrasing.")
            await _info(ctx, f"{label}: matched {len(results)} note(s)")
            rag_ctx = "\n\n---\n\n".join(f"# {r.title}\n{r.body[:800]}" for r in results)
        # Fold the user's llm-context overlay into the system prompt
        # so saved facts (from `walk`, `context add`, etc.) ground
        # the answer alongside the RAG-retrieved notes. Phase 13.1
        # surfaced the gap: ask_notes was bypassing this.
        from . import context as _ctx_mod
        overlay = _ctx_mod.read_context_for_prompt(max_chars=4000)
        system = (
            "You are an assistant with access to a personal "
            "org-mode knowledge base. Answer using ONLY the provided "
            "notes plus the user's current-truth overlay below. Be "
            "concise. Cite note titles. When the overlay contradicts "
            "a retrieved note, the overlay wins (it represents the "
            "user's CURRENT truth; the note is historical record).\n\n"
            f"User's current-truth overlay:\n{overlay}"
            if overlay else
            "You are an assistant with access to a personal "
            "org-mode knowledge base. Answer using only the provided "
            "notes. Be concise. Cite note titles."
        )
        await _report(ctx, 3, 3, f"{label} — synthesizing answer with {chat_model}")
        try:
            answer = chat(
                f"Notes:\n\n{rag_ctx}\n\n---\n\nQuestion: {question}",
                model=chat_model, base_url=url, system=system,
            )
            return _themed("ask_notes",
                            f"{len(results)} note(s) feeding the answer",
                            answer)
        except Exception as e:
            return f"LLM error: {e}"

    # ── capture_note ──────────────────────────────────────────────────────────
    @server.tool()
    def capture_note(title: str = "", body: str = "",
                       file: str = "inbox.org",
                       note: str = "", content: str = "",
                       text: str = "") -> str:
        """Capture a new note into the org vault. Returns the node ID
        for linking.

        Aliases for `body`: `note`, `content`, `text` (silently
        accepted; the LLM frequently emits one of these instead of
        the canonical name). If `title` is omitted, derives it from
        the first `* heading` in body. If body has no heading,
        falls back to "Untitled capture".

        For day-shaped captures (today's agenda, weekend todo,
        journal entries) target `daily/<YYYY-MM-DD>.org` so it
        lands as the user's daily file rather than inbox.org.
        """
        # Accept body-aliases
        body = body or note or content or text
        # Title fallback from first heading
        if not title and body:
            for line in body.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("* "):
                    title = stripped[2:].strip()
                    break
            if not title:
                title = "Untitled capture"
        if not title:
            title = "Untitled capture"
        import uuid
        from datetime import datetime
        with get_session(engine) as session:
            org_dir = Path(os.environ.get("ORG_LLM_ORG_DIR")
                           or _cfg(session, "org_dir") or "~/org").expanduser()
        node_id  = str(uuid.uuid4())
        ts       = datetime.now().strftime("%Y%m%d%H%M%S")
        # Path-traversal guard: refuse writes outside org_dir
        org_dir_r = org_dir.resolve()
        target = (org_dir / file).expanduser()
        normalized = Path(os.path.normpath(str(target)))
        try:
            normalized.relative_to(org_dir_r)
        except ValueError:
            return f"Refused: '{file}' resolves outside org_dir ({org_dir_r})."
        org_file = normalized
        org_file.parent.mkdir(parents=True, exist_ok=True)
        entry = (
            f"\n* {title}\n"
            f":PROPERTIES:\n:ID: {node_id}\n:CREATED: [{ts}]\n:END:\n\n"
            f"{body.strip()}\n"
        )
        with open(org_file, "a") as fh:
            fh.write(entry)
        return _themed("capture_note",
                        f"saved '{title}' to {org_file.name}",
                        f"ID: {node_id}\n"
                        f"Path: {org_file}\n"
                        f"Run [bold]org-llm index[/bold] to add to search.")

    # ── get_node ──────────────────────────────────────────────────────────────
    @server.tool()
    def get_node(title: str) -> str:
        """Fetch the full content of a note by title (partial/fuzzy match)."""
        from datetime import datetime
        from .db import Node
        with get_session(engine) as session:
            node = (
                session.query(Node)
                .filter(Node.title.ilike(f"%{title}%"))
                .order_by(Node.mtime.desc())
                .first()
            )
            if not node:
                return _themed("get_node",
                                f"no match for {title!r}",
                                "Try search_notes() with a different phrase.")
            modified = (
                datetime.fromtimestamp(node.mtime).isoformat(timespec="seconds")
                if node.mtime else "?"
            )
            from .db import merged_tags as _merged
            body = (
                f"**{node.title}**\n"
                f"File:     {node.file.path}\n"
                f"Tags:     {_merged(node) or 'none'}\n"
                f"ID:       {node.node_id or 'none'}\n"
                f"Modified: {modified}\n\n"
                f"{node.body}"
            )
            return _themed("get_node", node.title, body)

    # ── list_nodes_by_tag ─────────────────────────────────────────────────────
    @server.tool()
    def list_nodes_by_tag(tag: str, limit: int = 30) -> str:
        """List notes that contain a given tag (looks at both file-source
        tags and LLM auto-tags)."""
        from sqlalchemy import or_
        from .db import File, Node, merged_tags as _merged
        with get_session(engine) as session:
            rows = (
                session.query(Node, File.path)
                .join(File, File.id == Node.file_id)
                .filter(or_(Node.tags.ilike(f"%{tag}%"),
                             Node.auto_tags.ilike(f"%{tag}%")))
                .limit(limit).all()
            )
        if not rows:
            return _themed("list_nodes_by_tag", f"no notes tagged {tag!r}")
        body = "\n".join(
            f"- {n.title}  [{_merged(n)}]  ({Path(p).name})"
            for n, p in rows
        )
        return _themed("list_nodes_by_tag",
                        f"{len(rows)} note(s) tagged {tag!r}", body)

    # ── list_recent_nodes ─────────────────────────────────────────────────────
    @server.tool()
    def list_recent_nodes(days: int = 14) -> str:
        """List notes modified in the last N days, most recent first."""
        from datetime import datetime, timedelta
        from .db import Node
        with get_session(engine) as session:
            since = (datetime.now() - timedelta(days=days)).timestamp()
            nodes = (
                session.query(Node)
                .filter(Node.mtime >= since)
                .order_by(Node.mtime.desc())
                .limit(25).all()
            )
        if not nodes:
            return _themed("list_recent_nodes",
                            f"no activity in the last {days} day(s)")
        body = "\n".join(
            f"- {n.title}  ({datetime.fromtimestamp(n.mtime).date().isoformat() if n.mtime else '?'})"
            for n in nodes
        )
        return _themed("list_recent_nodes",
                        f"{len(nodes)} note(s) modified in last {days}d", body)

    # ── list_dailies ──────────────────────────────────────────────────────────
    @server.tool()
    def list_dailies(limit: int = 10, since_days: int = 0,
                       include_content: bool = True,
                       max_chars_each: int = 4000) -> str:
        """List recent daily-log files from the vault's daily directory.

        Daily files live at `<daily_dir>/*.org` (default
        `<org_dir>/daily/`). Returns the most recent `limit` files,
        newest first. `since_days=0` means no recency filter.

        Output: one line per file with date, path, and first heading
        / #+TITLE (when present) so the caller has enough to decide
        which file(s) to read in full via read_file or get_node."""
        from datetime import datetime, timedelta
        from pathlib import Path
        with get_session(engine) as session:
            org_dir = Path((_cfg(session, "org_dir") or "~/org")).expanduser()
            daily_dir_cfg = (_cfg(session, "daily_dir") or "").strip()
        daily_dir = (Path(daily_dir_cfg).expanduser()
                     if daily_dir_cfg else (org_dir / "daily"))
        if not daily_dir.exists():
            return _themed("list_dailies",
                            f"daily directory not found: {daily_dir}",
                            "Create it with `mkdir -p` and start "
                            "capturing dailies, or set `daily_dir` "
                            "via `org-llm config daily_dir <path>`.")
        files = sorted(daily_dir.glob("*.org"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if since_days > 0:
            cutoff = (datetime.now() - timedelta(days=since_days)).timestamp()
            files = [f for f in files if f.stat().st_mtime >= cutoff]
        files = files[:max(1, limit)]
        if not files:
            return _themed("list_dailies",
                            f"no daily files in {daily_dir}"
                            + (f" (last {since_days}d)"
                                if since_days > 0 else ""))
        rows = []
        for f in files:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            title = ""
            content = ""
            try:
                full = f.read_text(errors="replace")
                lines = full.splitlines()
                for line in lines[:8]:
                    s = line.strip()
                    if s.lower().startswith("#+title:"):
                        title = s.split(":", 1)[1].strip()
                        break
                    if s.startswith("* "):
                        title = s[2:].strip()
                        break
                if include_content:
                    content = full[:max_chars_each]
                    if len(full) > max_chars_each:
                        content += f"\n... [truncated; full file is {len(full)} chars]"
            except Exception:
                pass
            row = (f"- {f.stem}  ({mtime.date().isoformat()})"
                    f"{'  — ' + title if title else ''}\n  {f}")
            if include_content and content:
                row += "\n  ```org\n" + "\n".join(
                    "  " + ln for ln in content.splitlines()) + "\n  ```"
            rows.append(row)
        body = "\n".join(rows)
        title_line = (f"{len(files)} daily file(s) in {daily_dir}"
                       + (" with content" if include_content else ""))
        return _themed("list_dailies", title_line, body)

    # ── get_vault_stats ───────────────────────────────────────────────────────
    @server.tool()
    def get_vault_stats() -> str:
        """Return statistics about the indexed org-roam vault."""
        from .db import Node, File
        with get_session(engine) as session:
            n_files    = session.query(File).count()
            n_nodes    = session.query(Node).count()
            n_embedded = session.query(Node).filter(Node.embedding.isnot(None)).count()
            from sqlalchemy import or_
            # "Tagged" = has file-source tags OR LLM auto-tags (either is
            # enough to be discoverable via tag search).
            n_tagged   = session.query(Node).filter(
                or_(Node.tags != "", Node.auto_tags != "")
            ).count()
            org_dir    = _cfg(session, "org_dir") or "~/org"
        pct_e = int(n_embedded / n_nodes * 100) if n_nodes else 0
        pct_t = int(n_tagged   / n_nodes * 100) if n_nodes else 0
        body = (
            f"Vault: {org_dir}\n"
            f"  {n_files} files  |  {n_nodes} nodes\n"
            f"  {n_embedded}/{n_nodes} embedded ({pct_e}%)\n"
            f"  {n_tagged}/{n_nodes} tagged ({pct_t}%)"
        )
        return _themed("get_vault_stats",
                        f"{n_nodes} nodes across {n_files} files", body)

    # ── metrics_describe / metrics_query ──────────────────────────────────────
    # Semantic-layer surface — Phase 27 (Superset). Agents ask for
    # numbers by metric name; the registry compiles SQL and runs it.
    # Same definitions feed Superset dataset YAML, so the dashboard and
    # the @analyst agent answer with identical numbers.
    @server.tool()
    def metrics_describe() -> str:
        """List all org-llm metrics + dimensions, grouped by source.

        Use this BEFORE metrics_query to discover what numbers are
        available without writing raw SQL. Returns a human-readable
        index — feed it back into your prompt to pick a metric name.
        """
        from .metrics import Registry
        reg = Registry.load()
        return _themed(
            "metrics_describe",
            f"{len(reg.metrics)} metric(s), {len(reg.dimensions)} dimension(s)",
            reg.describe(),
        )

    @server.tool()
    @recover_on_failure
    def metrics_query(
        metric: str,
        group_by: str = "",
        where: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        join: str = "",
    ) -> str:
        """Run one semantic-layer metric query against the org-llm DB.

        Args:
          metric: metric name (call metrics_describe to discover).
          group_by: comma-separated dimension names, e.g. "model,call_kind".
          where: filters as KEY=VALUE,KEY=VALUE (use "" for none).
          since/until: bounds on the source's time column (ISO date).
          limit: row cap (default 50).
          join: opt into a sanctioned cross-source join by name (see
            metrics_describe for declared joins). When set, group_by
            dimensions can come from either source covered by the join.
            Requires ORG_LLM_REGISTRY_V1_JOINS=1 in the runtime env;
            without the flag, cross-source queries still fail loud.

        Returns a markdown table. group_by dimensions and where keys
        must come from the metric's source — or, with `join`, from
        either source covered by the named join.
        """
        from .metrics import Registry, RegistryError
        reg = Registry.load()
        gb = [g.strip() for g in group_by.split(",") if g.strip()] or None
        wh: dict[str, str] = {}
        if where:
            for clause in where.split(","):
                clause = clause.strip()
                if not clause:
                    continue
                if "=" not in clause:
                    return f"metrics_query error: bad filter '{clause}' (use KEY=VALUE)"
                k, v = clause.split("=", 1)
                wh[k.strip()] = v.strip()
        try:
            rows = reg.query(
                metric=metric,
                group_by=gb,
                where=wh or None,
                since=since or None,
                until=until or None,
                limit=limit,
                join=join or None,
            )
        except RegistryError as e:
            return f"metrics_query error: {e}"
        if not rows:
            return _themed("metrics_query",
                           f"metric:{metric} → 0 rows", "(no rows)")
        cols = list(rows[0].keys())
        header = " | ".join(cols)
        sep = " | ".join("---" for _ in cols)
        body_lines = [
            " | ".join(str(r[c]) for c in cols) for r in rows
        ]
        body = "\n".join([header, sep, *body_lines])
        return _themed(
            "metrics_query",
            f"metric:{metric} → {len(rows)} row(s)",
            body,
        )

    # ── list_skills ───────────────────────────────────────────────────────────
    @server.tool()
    def list_skills() -> str:
        """List all registered org-llm skill workflows (org-babel :skill: blocks)."""
        from .skills import Skill
        with get_session(engine) as session:
            skills = session.query(Skill).all()
        if not skills:
            return _themed("list_skills",
                            "no skills registered",
                            "Add :skill: blocks to org files and run "
                            "[bold]org-llm skill-index[/bold].")
        body = "\n".join(
            f"- {s.name}  (lang: {s.lang}, model: {s.model_key})"
            for s in skills
        )
        return _themed("list_skills", f"{len(skills)} skill(s)", body)

    # ── run_skill ─────────────────────────────────────────────────────────────
    @server.tool()
    def run_skill(name: str, input: str) -> str:
        """Execute a registered org-babel skill workflow with the given input."""
        from .skills import Skill, run_skill as _run
        from .db import Config
        with get_session(engine) as session:
            skill = session.query(Skill).filter(Skill.name == name).first()
            if not skill:
                available = [s.name for s in session.query(Skill).all()]
                return f"Skill '{name}' not found. Available: {', '.join(available) or 'none'}"
            url = _cfg(session, "ollama_url") or "http://localhost:11434"
            cfg = {r.key: r.value for r in session.query(Config).all()}
        try:
            return _run(skill, input_text=input, cfg=cfg, base_url=url)
        except Exception as e:
            return f"Skill '{name}' failed: {e}"

    # ── tangle_file ───────────────────────────────────────────────────────────
    # Phase 24.2 — recovery hook site: tangle_file shells out to
    # `emacsclient`, which can `TimeoutExpired` (slow tangle of a
    # huge org file) or hit `ConnectionError`-style failures when
    # the daemon is mid-restart. Both are transient and benefit
    # from the timeout / connection_error hooks' retry budgets.
    @server.tool()
    @recover_on_failure
    def tangle_file(file_path: str) -> str:
        """Run org-babel-tangle on an org file via emacsclient.

        If `emacsclient` reports no server is running, this tool tries to
        start `emacs --daemon` once and retries — so the LLM can call
        tangle_file without first asking the user to start Emacs.
        """
        import subprocess, shutil, time
        def _try_tangle() -> tuple[int, str, str]:
            r = subprocess.run(
                ["emacsclient", "--eval", f'(org-babel-tangle-file "{file_path}")'],
                capture_output=True, text=True, timeout=30,
            )
            return r.returncode, r.stdout, r.stderr
        try:
            rc, stdout, stderr = _try_tangle()
            if rc == 0:
                return f"Tangled: {file_path}\n{stdout.strip()}"
            # emacsclient prints "can't find socket" / "no socket" when the
            # daemon isn't running. Try to autostart it once.
            if "socket" in stderr.lower() or "server" in stderr.lower():
                if shutil.which("emacs"):
                    subprocess.Popen(
                        ["emacs", "--daemon"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    # Brief poll: daemon usually up within ~3 s
                    for _ in range(8):
                        time.sleep(0.4)
                        try:
                            rc2, stdout2, stderr2 = _try_tangle()
                            if rc2 == 0:
                                return (f"Tangled (auto-started emacs --daemon): "
                                        f"{file_path}\n{stdout2.strip()}")
                        except Exception:
                            continue
            return f"Tangle failed: {stderr.strip() or stdout.strip()}"
        except FileNotFoundError:
            return "emacsclient not found. Ensure Emacs server is running: (server-start) in config.el."
        except Exception as e:
            return f"Tangle error: {e}"

    # ── get_config ────────────────────────────────────────────────────────────
    # ── access: read_file, list_directory, open_url, qute_command ───────────
    # Phase 24.2 — recovery hook site: read_file is the canonical
    # filesystem op the LLM reaches for. A wrong-shape call
    # (`read_file()` with no `path` arg, or a typo of the kwarg)
    # is the most likely failure mode — perfect ESCALATE territory
    # for the missing_argument hook so the LLM self-corrects.
    @server.tool()
    @recover_on_failure
    def read_file(path: str) -> str:
        """Read a file from anywhere on the user's filesystem.

        Only paths the user has explicitly authorised via `org-llm grant`
        are reachable. If the request is denied, the response names the
        exact `org-llm grant` command the user must run to allow it.
        """
        from .access import read_file as _read
        result = _read(path)
        if not result.ok:
            return result.error
        return result.content

    # Phase 24.2 — recovery hook site: parallel to read_file,
    # the same missing-argument and transient-IO failure modes
    # apply.
    @server.tool()
    @recover_on_failure
    def list_directory(path: str) -> str:
        """List entries in a directory. Same allow-list gating as read_file."""
        from .access import list_directory as _list
        result = _list(path)
        if not result.ok:
            return result.error
        return result.content

    @server.tool()
    def list_grants() -> str:
        """Show the user's currently-authorised file-access prefixes."""
        from .access import allowlist, auto_grant_roots, browser_enabled
        grants = allowlist()
        roots  = auto_grant_roots()
        lines = ["Currently authorised file prefixes (read_file/list_directory):"]
        if grants:
            lines += [f"  - {g}" for g in grants]
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Auto-grant roots (LLM may self-grant under these via request_access):")
        if roots:
            lines += [f"  - {r}" for r in roots]
        else:
            lines.append("  (none — request_access will refuse all paths)")
        lines.append("")
        lines.append(f"Browser access: {'enabled' if browser_enabled() else 'disabled'}")
        lines.append("")
        lines.append("To grant a path manually: org-llm grant <path>")
        lines.append("To trust an auto-root:    org-llm grant-root <path>")
        lines.append("To enable browser:        org-llm grant-browser")
        return "\n".join(lines)

    @server.tool()
    def request_access(path: str, reason: str = "") -> str:
        """Request to read files under PATH. Auto-granted IFF:

           • PATH lies under one of the user's auto-grant roots
             (`org-llm grant-root <root>`), AND
           • PATH is not on the sensitive deny-list (SSH, GPG, cloud creds).

        On success, PATH is added to the regular allow-list — subsequent
        `read_file` calls succeed without going through this tool. On
        refusal, the message tells you exactly which `org-llm grant` or
        `grant-root` command the user must run for you to proceed.

        Use this when you need a file outside your current grants. Always
        give a brief `reason` so the user can audit why later.
        """
        from .access import request_self_grant
        result = request_self_grant(path, reason)
        return result.message

    # Phase 24.2 — recovery hook site: open_url shells out to a
    # browser process; transient ConnectionError / TimeoutError
    # from the IPC handshake are exactly what the connection +
    # timeout hooks were built for.
    @server.tool()
    @recover_on_failure
    def open_url(url: str) -> str:
        """Open a URL in qutebrowser (or the user's default browser).

        Disabled by default; the user enables it with `org-llm grant-browser`.
        Only http(s) and file:// URLs are accepted; javascript:/data: refused.
        """
        from .access import open_url as _open
        ok, msg = _open(url)
        return msg

    @server.tool()
    def export_manager_history(limit: int = 200,
                                 out_path: str = "") -> str:
        """Write the manager (crew_log) audit trail to a markdown
        file. Use this when the user asks to see what the manager
        has been doing, or to bundle a forensic record of the crew's
        actions for a specific session.

        - limit: max entries (newest first). Default 200.
        - out_path: where to write. Default
          `<org_dir>/.opencode/manager-history-<timestamp>.md`.

        Returns the absolute path of the written file. Pairs with
        the sidebar's MANAGER row (live last-3) and the
        `org-llm crew-log` CLI verb (terminal inspection)."""
        import time as _t
        from pathlib import Path
        from .llm_proxy import _format_manager_history
        with get_session(engine) as session:
            org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        if not out_path:
            ts = _t.strftime("%Y-%m-%dT%H-%M-%S", _t.gmtime())
            out_dir = org_dir / ".opencode"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(out_dir / f"manager-history-{ts}.md")
        else:
            out_path = str(Path(out_path).expanduser())
        lines = _format_manager_history(limit=limit)
        try:
            Path(out_path).write_text("\n".join(lines))
        except Exception as e:
            return _themed("export_manager_history",
                            f"[red]✗[/red] write failed: {e}",
                            f"target: {out_path}")
        return _themed("export_manager_history",
                        f"[green]✓[/green] wrote {limit} entries",
                        f"path: {out_path}")

    @server.tool()
    def export_sidebar_snapshot(out_path: str = "") -> str:
        """Write the current sidebar state (VAULT / ACTIVE / HEALTH /
        ARCHIVE plus MANAGER recent activity) to a markdown file.

        Use this when the user asks 'export the sidebar' or wants a
        snapshot of the current operational state. Returns the
        absolute path of the written file."""
        import time as _t
        from pathlib import Path
        from .llm_proxy import (_format_sidebar_snapshot,
                                  _format_manager_history)
        with get_session(engine) as session:
            org_dir = Path(_cfg(session, "org_dir") or "~/org").expanduser()
        if not out_path:
            ts = _t.strftime("%Y-%m-%dT%H-%M-%S", _t.gmtime())
            out_dir = org_dir / ".opencode"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(out_dir / f"sidebar-snapshot-{ts}.md")
        else:
            out_path = str(Path(out_path).expanduser())
        lines = ["# org-llm sidebar snapshot",
                  f"*{_t.strftime('%Y-%m-%dT%H-%M-%SZ', _t.gmtime())}*",
                  ""]
        lines += _format_sidebar_snapshot(org_dir)
        lines += _format_manager_history(limit=10)
        try:
            Path(out_path).write_text("\n".join(lines))
        except Exception as e:
            return _themed("export_sidebar_snapshot",
                            f"[red]✗[/red] write failed: {e}",
                            f"target: {out_path}")
        return _themed("export_sidebar_snapshot",
                        f"[green]✓[/green] sidebar exported",
                        f"path: {out_path}")

    @server.tool()
    def infer_capture_style(dir_path: str = "",
                              force_refresh: bool = False) -> str:
        """Detect the dominant org-mode capture style in a directory
        — DETERMINISTIC, ~10ms, CACHED across calls.

        Reads the 3 most recent .org files in `dir_path` (default
        `<daily_dir>` or `<org_dir>/daily/`), counts header-cookie
        (`* [ ] heading`), header-todo (`** TODO heading`),
        bullet-cookie (`- [ ] item`), and plain-bullet patterns,
        and returns the dominant shape plus useful adjuncts
        (priorities, org-roam links).

        Use this BEFORE any list-shaped capture so the new entry
        mirrors the user's existing convention. Cached by directory
        path + newest-mtime, so a second call is free until the
        user edits a file in that directory.

        Replaces the old "read 3 sample dailies and have the LLM
        infer format" pattern that cost 3 cloud round-trips per
        turn (~30s)."""
        from .style_infer import infer_style, style_summary
        if not dir_path:
            with get_session(engine) as session:
                org_dir = (_cfg(session, "org_dir") or "~/org")
                daily_dir = (_cfg(session, "daily_dir") or "").strip()
            dir_path = (daily_dir
                         if daily_dir
                         else str(Path(org_dir).expanduser() / "daily"))
        info = infer_style(dir_path, sample_size=3,
                            force_refresh=force_refresh)
        summary = style_summary(info)
        body = (f"{summary}\n\n"
                f"Samples: {info['samples_examined']} files in "
                f"{info['dir']}\n"
                f"Counts: {info['raw_counts']}")
        return _themed("infer_capture_style",
                        f"shape={info['dominant_shape']}"
                        + (" (cached)" if info['cached'] else ""),
                        body)

    @server.tool()
    def list_agents() -> str:
        """List the active org-llm agents — birth-name, aliases,
        role, model.

        Use this BEFORE delegate() so you know who's available and
        what they're good at. Each agent's BIRTH-NAME is canonical
        (Trek-themed for the OOB set: picard, spock, data, geordi,
        riker, janeway, scotty, soong, keiko); ALIASES are the
        functional names (crew, researcher, scribe, …) — both work
        when you call delegate(). Picard is the manager; the rest
        are domain specialists. Users can add/edit agents via
        ~/org/org-llm-agents.org (`org-llm agents --tangle`).
        """
        from .cli import _resolve_active_agents
        from .db  import Config as _Cfg
        import os as _os
        from pathlib import Path as _P
        org_dir = _P(_os.environ.get("ORG_LLM_ORG_DIR")
                      or (_P.home() / "org"))
        with get_session(engine) as session:
            cfg = {r.key: r.value for r in session.query(_Cfg).all()}
        rows = []
        for a in _resolve_active_agents(org_dir):
            if not a.addressable:
                continue
            role = a.model_role or "chat_model"
            resolved = ((cfg.get(role) or cfg.get("chat_model")
                          or "(unset)").strip())
            alias_str = (f"  [aliases: {', '.join(a.aliases)}]"
                          if a.aliases else "")
            rows.append(f"- {a.birth_name}  ({role} → {resolved})"
                         f"{alias_str}\n  {a.description}")
        return _themed("list_agents",
                        f"{len(rows)} agent(s)", "\n".join(rows))

    # Phase 24.2 — recovery hook site: delegate is the single
    # highest-leverage MCP tool (every cloud sub-LLM call funnels
    # through here). Connection drops to the cloud + cloud-side
    # timeouts are the most common transient failure modes, and
    # the in-body try/except already returns themed strings rather
    # than re-raising — so this decorator is a *safety net* for
    # exceptions that escape that try/except (e.g. config-loader
    # crashes, agent-resolver bugs, missing-argument shape errors
    # the LLM emitted).
    @server.tool()
    @recover_on_failure
    def delegate(agent: str, prompt: str, context: str = "",
                  model_override: str = "", timeout_s: int = 120) -> str:
        """Consult a specialist agent and return its response.

        The manager pattern: `crew` (the top-level manager agent)
        uses delegate() to consult domain experts —
        delegate('classifier', 'filter to routine items: …') returns
        the classifier's answer as a string. The manager then
        composes the experts' input into the final reply.

        - agent: name from list_agents() (e.g. 'classifier',
          'researcher', 'reviewer')
        - prompt: the focused question for the specialist
        - context: optional preamble (recent conversation slice,
          relevant data) appended to the user message
        - model_override: force a different model (e.g.
          'qwen2.5:1.5b' for a fast classifier pass) — bypasses
          the agent's default model_role
        - timeout_s: bail out after this many seconds; the manager
          can retry with a smaller model on timeout

        Cloud is preferred when configured (faster + tool-capable);
        falls back to local ollama otherwise."""
        from .cli    import _resolve_active_agents
        from .db     import log_crew_action as _log
        from .db     import render_baselines as _render_baselines
        from pathlib import Path as _P
        import os    as _os
        import time  as _t
        org_dir = _P(_os.environ.get("ORG_LLM_ORG_DIR")
                      or (_P.home() / "org"))
        active = _resolve_active_agents(org_dir)
        # Accept birth-name OR any alias. `agent` becomes the
        # canonical birth_name for downstream logging + role
        # checks regardless of which form the caller passed.
        spec_obj = None
        typed_lc = (agent or "").strip().lower()
        for _a in active:
            if (_a.birth_name.lower() == typed_lc
                    or any(al.lower() == typed_lc for al in _a.aliases)):
                spec_obj = _a
                break
        if spec_obj is None:
            available = ", ".join(sorted(_a.birth_name for _a in active))
            _log("delegate", agent_to=agent, prompt=prompt,
                 outcome="error",
                 result=f"no such agent: {agent}")
            return _themed("delegate",
                            f"[red]✗[/red] no such agent: {agent}",
                            f"available: {available}")
        agent = spec_obj.birth_name
        # Prepend agent baselines (CURRENT TIME, vault-first,
        # vault_style, etc.) to the sub-LLM's system prompt so the
        # delegated specialist gets the same context the proxy
        # would inject for a direct @-routed turn. Without this
        # the sub-LLM sees only the bare agent prompt and
        # hallucinates format (this was the "delegate to scribe →
        # flat bullets instead of nested headers" gap).
        role_for_baselines = ("manager" if agent == "picard"
                               else "specialist")
        baselines_block = _render_baselines(agent, role_for_baselines)
        sys_prompt = spec_obj.persona
        if baselines_block:
            sys_prompt = baselines_block + "\n\n" + sys_prompt
        role = spec_obj.model_role or "chat_model"
        with get_session(engine) as session:
            role_model = (_cfg(session, role)
                           or _cfg(session, "chat_model")
                           or "")
        model = model_override or role_model
        if not model:
            _log("delegate", agent_to=agent, prompt=prompt,
                 outcome="error",
                 result=f"could not resolve model for {agent}")
            return _themed("delegate",
                            f"[red]✗[/red] could not resolve model "
                            f"for {agent}")
        t0 = _t.monotonic()
        # Phase 24.1 — pre-flight resolver enrichment. Pure-code
        # disambiguation of repo/path/agent tokens BEFORE the
        # cloud sees the prompt, so the sub-LLM doesn't have to
        # guess where a relative path lives or which @handle the
        # user meant. The block sits ahead of the user message
        # so it's the first thing the sub-LLM reads. Failure is
        # always silent — we'd rather lose enrichment than
        # block a delegate call.
        try:
            from .resolvers import resolve_all, format_resolved_context
            _resolved_block = format_resolved_context(
                resolve_all(prompt, context))
        except Exception:
            _resolved_block = ""
        # Compose user prompt
        user_msg = (f"{context}\n\n{prompt}".strip()
                     if context else prompt)
        if _resolved_block:
            user_msg = f"{_resolved_block}\n\n{user_msg}"
        with get_session(engine) as session:
            from . import creds as _creds
            cloud_provider   = _cfg(session, "cloud_provider")
            cloud_endpoint   = _cfg(session, "cloud_endpoint_url")
            cloud_model      = _cfg(session, "cloud_model")
            cloud_fast_model = _cfg(session, "cloud_fast_model")
            ollama_url       = (_cfg(session, "ollama_url")
                                or "http://localhost:11434")
            api_key          = ""
            if cloud_provider:
                try:
                    api_key = _creds.read_secret(
                        _creds.cloud_slug(cloud_provider)) or ""
                except Exception:
                    api_key = ""
        # Cloud path preferred when fully configured
        use_cloud = bool(cloud_endpoint and api_key
                          and (cloud_model or "/" in model))
        try:
            if use_cloud:
                import urllib.request as _ur
                from .cloud import _urlopen as _ssl_urlopen
                # If model_override doesn't carry a provider prefix
                # AND we're going to cloud, swap to a cloud model.
                # Fast-role agents (classifier, tag, summarize) get
                # `cloud_fast_model` when configured — saves 5-10s
                # per delegate call vs the full chat model.
                _FAST_ROLES = {"fast_model", "tag_model",
                                "summarize_model"}
                if "/" in model:
                    send_model = model
                elif role in _FAST_ROLES and cloud_fast_model:
                    send_model = cloud_fast_model
                else:
                    send_model = cloud_model
                payload = json.dumps({
                    "model": send_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    "stream":      False,
                    "max_tokens":  1024,
                    "temperature": 0.3,
                }).encode()
                req = _ur.Request(
                    cloud_endpoint.rstrip("/") + "/chat/completions",
                    data=payload, method="POST",
                    headers={
                        "Content-Type":  "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent":    "org-llm/delegate",
                    })
                with _ssl_urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                obj = json.loads(raw)
                msg = (obj.get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content") or ""
                dt_ms = int((_t.monotonic() - t0) * 1000)
                outcome = "ok" if content.strip() else "empty"
                _log("delegate", agent_to=agent, model=send_model,
                     prompt=prompt, result=content,
                     duration_ms=dt_ms, outcome=outcome)
                return _themed("delegate",
                                f"@{agent} ({send_model}, cloud, {dt_ms}ms)",
                                content.strip()
                                or "(empty response from cloud)")
            else:
                # Local ollama fallback
                from .llm import chat as _chat
                bare = (model[len("ollama/"):]
                        if model.startswith("ollama/") else model)
                content = _chat(user_msg, bare, ollama_url,
                                 system=sys_prompt, timeout=timeout_s)
                dt_ms = int((_t.monotonic() - t0) * 1000)
                outcome = "ok" if content.strip() else "empty"
                _log("delegate", agent_to=agent, model=bare,
                     prompt=prompt, result=content,
                     duration_ms=dt_ms, outcome=outcome)
                return _themed("delegate",
                                f"@{agent} ({bare}, local, {dt_ms}ms)",
                                content.strip()
                                or "(empty response from local)")
        except Exception as e:
            dt_ms = int((_t.monotonic() - t0) * 1000)
            err = str(e)
            outcome = ("timeout" if "timed out" in err.lower()
                        or "timeout" in err.lower() else "error")
            _log("delegate", agent_to=agent, model=model,
                 prompt=prompt, result=err,
                 duration_ms=dt_ms, outcome=outcome)
            return _themed("delegate",
                            f"[red]✗[/red] @{agent} failed: {e}",
                            f"Manager: consider model_override "
                            f"(smaller/faster) or proactive_doctor "
                            f"to diagnose.")

    @server.tool()
    def classify_items(items: list[str], criterion: str,
                        timeout_s: int = 60) -> str:
        """Filter a list of items by a criterion using a fast LLM pass.

        Replaces the old `delegate('classifier', …)` pattern: same
        rule sets, same JSON output shape, but called as a first-
        class MCP tool with no agent-registry indirection.

        Default rule sets the LLM applies when the criterion matches:
          - ROUTINE: recurs in multiple source files OR has no end-
            state OR is a standing weekly/daily habit. REJECT one-off
            events, one-shot learning projects, anything tied to a
            specific date or person.
          - URGENT: explicit deadline within 7 days OR blocking
            another item OR flagged with [#A] / SCHEDULED past.
          - RECURRING: appears 3+ times in the source.

        Returns JSON-shaped string:
          {"kept":     [{"item": "...", "why": "..."}, ...],
           "rejected": [{"item": "...", "why": "..."}, ...],
           "criterion_used": "the rule applied"}

        Routes through cloud_fast_model when available (saves 5-10s
        per call vs the chat model); falls back to local ollama
        with the configured fast model.
        """
        import time as _t
        from .db import log_crew_action as _log
        if not isinstance(items, list) or not items:
            return _themed("classify_items",
                            "[yellow]∅[/yellow] no items to classify",
                            json.dumps({"kept": [], "rejected": [],
                                        "criterion_used": criterion}))
        # Ground-truth identity inlined here so dropping
        # @classifier from the agent registry doesn't lose the
        # contract.
        sys_prompt = (
            "You are a fast classifier. The caller hands you a "
            "LIST of items and a CRITERION. Your only job: return "
            "the subset that matches, with a one-clause "
            "justification per item.\n"
            "\n"
            "Default rule sets (apply when the criterion matches):\n"
            "  - ROUTINE = recurs across multiple source files OR "
            "has no end-state OR is a standing weekly/daily "
            "habit. REJECT one-off events, one-shot learning, "
            "anything tied to a specific date or person.\n"
            "  - URGENT = explicit deadline within 7 days OR "
            "blocks another item OR is [#A] / SCHEDULED past.\n"
            "  - RECURRING = appears 3+ times in the source.\n"
            "\n"
            "OUTPUT SHAPE: STRICT JSON, no prose around it:\n"
            "  {\"kept\":     [{\"item\":\"…\", \"why\":\"…\"}, …],\n"
            "   \"rejected\": [{\"item\":\"…\", \"why\":\"…\"}, …],\n"
            "   \"criterion_used\": \"the rule you applied\"}\n"
            "\n"
            "Be ruthless on rejection — when in doubt, reject. "
            "Speed > exhaustiveness."
        )
        user_msg = (f"CRITERION: {criterion}\n\n"
                    f"ITEMS ({len(items)}):\n"
                    + "\n".join(f"  - {it}" for it in items))
        t0 = _t.monotonic()
        with get_session(engine) as session:
            from . import creds as _creds
            cloud_provider   = _cfg(session, "cloud_provider")
            cloud_endpoint   = _cfg(session, "cloud_endpoint_url")
            cloud_fast_model = _cfg(session, "cloud_fast_model")
            cloud_model      = _cfg(session, "cloud_model")
            ollama_url       = (_cfg(session, "ollama_url")
                                or "http://localhost:11434")
            fast_model_local = _cfg(session, "fast_model")
            api_key = ""
            if cloud_provider:
                try:
                    api_key = _creds.read_secret(
                        _creds.cloud_slug(cloud_provider)) or ""
                except Exception:
                    api_key = ""
        use_cloud = bool(cloud_endpoint and api_key
                          and (cloud_fast_model or cloud_model))
        try:
            if use_cloud:
                import urllib.request as _ur
                from .cloud import _urlopen as _ssl_urlopen
                send_model = cloud_fast_model or cloud_model
                payload = json.dumps({
                    "model":       send_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    "stream":      False,
                    "max_tokens":  1024,
                    "temperature": 0.1,
                }).encode()
                req = _ur.Request(
                    cloud_endpoint.rstrip("/") + "/chat/completions",
                    data=payload, method="POST",
                    headers={
                        "Content-Type":  "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent":    "org-llm/classify_items",
                    })
                with _ssl_urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                obj = json.loads(raw)
                msg = (obj.get("choices") or [{}])[0].get("message") or {}
                content = (msg.get("content") or "").strip()
            else:
                from .llm import chat as _chat
                bare = (fast_model_local
                        or _cfg(get_session(engine).__enter__(),
                                "chat_model")
                        or "")
                if bare.startswith("ollama/"):
                    bare = bare[len("ollama/"):]
                if not bare:
                    return _themed("classify_items",
                                    "[red]✗[/red] no fast model configured",
                                    "Set cloud_fast_model or fast_model.")
                content = _chat(user_msg, bare, ollama_url,
                                 system=sys_prompt,
                                 timeout=timeout_s).strip()
            dt_ms = int((_t.monotonic() - t0) * 1000)
            # Strip a markdown fence if the model wrapped the JSON.
            if content.startswith("```"):
                import re as _re_local
                content = _re_local.sub(r"^```[a-zA-Z]*\n?", "", content)
                content = _re_local.sub(r"\n?```\s*$", "", content)
            try:
                json.loads(content)   # validate
                result_str = content
                outcome = "ok"
            except Exception:
                result_str = json.dumps({
                    "kept": [], "rejected": [],
                    "criterion_used": criterion,
                    "_error": "model output was not valid JSON",
                    "_raw":   content[:400],
                })
                outcome = "bad-json"
            _log("classify", agent_from="manager",
                 agent_to="classifier(tool)",
                 model=("cloud_fast" if use_cloud else "local"),
                 prompt=criterion[:200],
                 result=result_str[:300],
                 duration_ms=dt_ms, outcome=outcome)
            return _themed("classify_items",
                            f"classified {len(items)} item(s) "
                            f"({outcome}, {dt_ms}ms)",
                            result_str)
        except Exception as e:
            dt_ms = int((_t.monotonic() - t0) * 1000)
            _log("classify", agent_from="manager",
                 agent_to="classifier(tool)", prompt=criterion[:200],
                 result=str(e), duration_ms=dt_ms, outcome="error")
            return _themed("classify_items",
                            f"[red]✗[/red] classify failed: {e}")

    @server.tool()
    def open_in_emacs(path: str, line: int = 0,
                       new_frame: bool = True) -> str:
        """Open `path` in Emacs via emacsclient.

        By default creates a NEW client frame so the user's existing
        windows aren't disturbed. Pass new_frame=False to open in
        whatever frame is currently selected.

        line > 0 jumps to that 1-based line after opening. Useful
        after `capture_note` to drop the cursor on the new heading.

        The path must be inside the MCP file allow-list (same gate
        as read_file) — refuses otherwise.

        Use this AFTER capture_note succeeds so the user can review /
        edit / link the new note immediately. Pair with
        close_emacs_frames when the user is done with the captured
        note's frame."""
        from .access import is_allowed
        import shutil as _sh
        if not _sh.which("emacsclient"):
            return _themed("open_in_emacs",
                            "[red]✗[/red] emacsclient not on PATH")
        allowed, resolved = is_allowed(path)
        if not allowed:
            return _themed("open_in_emacs",
                            f"[red]✗[/red] access denied: {resolved}",
                            "Call request_access(parent_dir) first.")
        if not resolved.exists():
            return _themed("open_in_emacs",
                            f"[red]✗[/red] file not found: {resolved}")
        # Build the elisp form. find-file-other-frame creates a new
        # FRAME (not just a window), which is what the user wants for
        # ephemeral note editing — easy to delete-frame later.
        if new_frame:
            form = (f'(progn (find-file-other-frame {json.dumps(str(resolved))})'
                    + (f' (goto-line {int(line)})' if line > 0 else '')
                    + ' "ok")')
        else:
            form = (f'(progn (find-file {json.dumps(str(resolved))})'
                    + (f' (goto-line {int(line)})' if line > 0 else '')
                    + ' "ok")')
        try:
            r = subprocess.run(
                ["emacsclient", "--eval", form],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return _themed("open_in_emacs",
                            f"[red]✗[/red] emacsclient failed: {e}")
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            return _themed("open_in_emacs",
                            f"[red]✗[/red] emacs rejected the call: {err}")
        return _themed("open_in_emacs",
                        f"[green]✓[/green] opened {resolved}"
                        + (f" (line {line})" if line > 0 else "")
                        + (" in new frame" if new_frame else ""))

    @server.tool()
    def close_emacs_frames(buffer_pattern: str = "") -> str:
        """Close Emacs client frames whose selected buffer matches
        `buffer_pattern` (substring match, case-insensitive).

        With no pattern, closes ALL client frames (frames created via
        `emacsclient -c`) — leaves the user's primary daemon frame
        alone. With a pattern like "Weekend To-Do" closes just the
        frame opened for that note.

        Use this when the user signals they're done with a captured
        note's frame, or as a "cleanup" step before opening a new
        ephemeral one."""
        import shutil as _sh
        if not _sh.which("emacsclient"):
            return _themed("close_emacs_frames",
                            "[red]✗[/red] emacsclient not on PATH")
        # Build a quoted pattern; empty pattern means "match all".
        pat_lit = json.dumps(buffer_pattern)
        form = (
            f'(let ((pat {pat_lit}) (n 0)) '
            f'  (dolist (f (frame-list)) '
            f'    (when (and (frame-parameter f (quote client)) '
            f'               (let ((b (buffer-name '
            f'                        (window-buffer '
            f'                         (frame-selected-window f))))) '
            f'                 (and b '
            f'                      (or (string-empty-p pat) '
            f'                          (string-match-p '
            f'                           (regexp-quote pat) b))))) '
            f'      (ignore-errors (delete-frame f t)) '
            f'      (setq n (1+ n)))) '
            f'  (format "%d" n))'
        )
        try:
            r = subprocess.run(
                ["emacsclient", "--eval", form],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return _themed("close_emacs_frames",
                            f"[red]✗[/red] emacsclient failed: {e}")
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            return _themed("close_emacs_frames",
                            f"[red]✗[/red] emacs rejected the call: {err}")
        # Output is a quoted string from %d — strip quotes for prose.
        n = (r.stdout or "").strip().strip('"')
        if not n or n == "0":
            return _themed("close_emacs_frames",
                            f"[dim]no client frames matched "
                            f"{buffer_pattern!r}[/dim]")
        return _themed("close_emacs_frames",
                        f"[green]✓[/green] closed {n} client frame(s)"
                        + (f" matching {buffer_pattern!r}"
                            if buffer_pattern else ""))

    # Phase 24.2 — recovery hook site: same shell-IPC failure
    # surface as open_url. Decorated so a flaky qutebrowser RPC
    # gets a single retry instead of bubbling to the LLM as a
    # bare ConnectionError.
    @server.tool()
    @recover_on_failure
    def browser_command(command: str) -> str:
        """Send a colon-command (e.g. ':open URL', ':tab-next') to qutebrowser.

        Requires `org-llm grant-browser` AND qutebrowser already installed.
        """
        from .access import qute_command
        ok, msg = qute_command(command)
        return msg

    @server.tool()
    def add_context(fact: str, topic: str = "") -> str:
        """Add a current-truth fact to the user's context file.

        Use this PROACTIVELY whenever the user states they've changed
        something durable about their world — a new job, a move, a
        finished project, a renamed person. The fact is appended to
        the org-llm context file, tangled to plain text, and prepended
        to every future system prompt under "USER CONTEXT". Older
        notes that mention the prior fact remain in the index but the
        new fact takes precedence.

        Topic should be a short word like "employment", "address",
        "project-status" — used for the secondary tag when stale-
        marking related notes.

        Returns the path the fact was written to.
        """
        from . import context as _ctx
        if not fact or not fact.strip():
            return "Empty fact — nothing to add."
        try:
            p = _ctx.add_fact(fact.strip(), source=f"mcp:{topic or 'general'}")
            return f"Added to {p}. Re-tangled. The next ask/code call will see this fact."
        except Exception as e:
            return f"add_context failed: {e}"

    @server.tool()
    def get_context() -> str:
        """Read the current LLM context — what the user has marked as
        current truth. Use this BEFORE answering any question that
        depends on facts about the user's life/work/projects, so you
        cite current rather than stale info."""
        from . import context as _ctx
        body = _ctx.read_context_for_prompt(max_chars=8000)
        return body or "(no context registered yet)"

    @server.tool()
    def find_stale_notes(keywords: list[str], limit: int = 20) -> str:
        """Find notes whose title or body contains any of `keywords`.

        Useful right after add_context: surface notes likely contradicted
        by the new fact so you can offer to tag them stale.
        """
        from . import context as _ctx
        with get_session(engine) as session:
            cands = _ctx.find_stale_candidates(session, keywords, limit=limit)
        if not cands:
            return f"No notes found containing: {keywords}"
        lines = [f"Found {len(cands)} candidate(s):"]
        for n, kw in cands:
            lines.append(f"  - [{kw}] {n.title or '(untitled)'}  (id={n.node_id})")
        return "\n".join(lines)

    # Phase 24.2 — recovery hook site: org_llm_run shells out to
    # the CLI itself with a configurable timeout. Subprocess
    # `TimeoutExpired` translates to TimeoutError-like failures
    # the timeout hook can absorb with a single 2× retry; cloud
    # subcommand backends can also throw transient
    # ConnectionError shapes.
    @server.tool()
    @recover_on_failure
    async def org_llm_run(command_string: str, timeout: int = 60,
                            ctx: Context | None = None) -> str:
        """Run an arbitrary `org-llm` subcommand from natural-language intent.

        Pass a free-form command string (e.g. "ask connect synthwave to my
        recent notes" or "personalize --apply"). The same three-layer
        recovery chain the CLI uses kicks in:

          1. Deterministic shell-quoting repair.
          2. LLM intent reconstruction (if you mangled the syntax).
          3. SRE-style fix (config / doctor / models repairs).

        Refuses dangerous verbs: `mcp` (would recurse), `claude` / `launch`
        (would try to take over the terminal), `install-tools` /
        `install` / `setup` (long interactive flows with binary
        installs), and `grant*` / `revoke*` (security boundary —
        the user must explicitly grant access). Returns combined
        stdout/stderr from the run, capped at 8000 chars.

        While the subprocess runs, emits MCP progress notifications
        every 2s with elapsed time so clients see something is alive.
        """
        import shlex, subprocess, asyncio, time as _t
        # Verbs the in-opencode LLM is NEVER allowed to run via org_llm_run:
        #   - mcp     — would recursively start another MCP server
        #   - launch / claude — would try to take over the user's terminal
        #   - install-tools / install (legacy alias) — network installs of
        #     binaries; needs explicit user consent in a real terminal
        #   - setup   — long interactive flow with prompts; not for MCP
        #   - grant*/revoke* — security boundary; only the user can grant
        DANGEROUS = {"mcp", "claude", "launch",
                     "install-tools", "install",   # rename + back-compat
                     "setup",
                     "grant", "grant-root", "grant-browser",
                     "revoke", "revoke-root", "revoke-browser"}
        try:
            argv = shlex.split(command_string or "")
        except ValueError as e:
            return f"Could not parse command_string: {e}"
        if not argv:
            return "Empty command_string."
        verb = argv[0]
        if verb in DANGEROUS:
            return (f"Refused: '{verb}' is not safe to run from MCP. "
                    f"The user must run it themselves in a terminal.")
        label = _theme_label(verb if verb in (
            "embed", "index", "search", "ask", "init", "models",
            "tag", "capture", "code", "cloud", "assess", "launch") else "default")
        await _info(ctx, f"{label}: org-llm {' '.join(argv)}")
        # Run the subprocess in a thread-pool executor so we can interleave
        # progress heartbeats from the asyncio loop. Without this, a 60s
        # `org-llm doctor` blocks the loop and no progress notifications
        # ever reach the wire.
        loop = asyncio.get_event_loop()
        started = _t.monotonic()
        try:
            fut = loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["org-llm", *argv],
                    capture_output=True, text=True, timeout=timeout,
                ),
            )
            tick = 0
            while not fut.done():
                await asyncio.sleep(2.0)
                tick += 1
                elapsed = _t.monotonic() - started
                # Total is unknown — pass `timeout` so the client can render
                # an ETA bar against the user's specified budget.
                await _report(ctx, elapsed, float(timeout),
                               f"{label} — {elapsed:.0f}s elapsed of "
                               f"{timeout}s budget")
            proc = await fut
        except FileNotFoundError:
            return "org-llm binary not on PATH inside the MCP server env."
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s."
        await _report(ctx, float(timeout), float(timeout),
                       f"{label} complete (exit {proc.returncode})")
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if len(out) > 8000:
            out = out[:8000] + "\n…(truncated)"
        suffix = ""
        if proc.returncode != 0:
            suffix = f"\n[exit {proc.returncode}]"
            # On non-zero exit, ask the same in-app LLM for recovery
            # bullets so the in-opencode LLM can act on them. Best-
            # effort: if the local LLM is unreachable, we just return
            # the bare exit code and let the caller decide.
            try:
                from .cli import _llm_recovery_advice as _adv
                advice = _adv(
                    f"`org-llm {' '.join(argv)}` failed (exit "
                    f"{proc.returncode}). Output tail:\n{out[-1000:]}",
                    context="invoked via MCP org_llm_run from opencode",
                    max_bullets=3,
                )
                if advice:
                    suffix += f"\n[LLM recovery advice]\n{advice}"
            except Exception:
                pass
        return out + suffix

    @server.tool()
    def get_config() -> str:
        """Return current org-llm configuration with env-override visibility.

        Each line shows: `key = value  [source]`  where source is one of
        env (overridden by ORG_LLM_<KEY>), config (DB row), or default.
        Use this to see what's ACTUALLY in effect — env vars trump DB
        rows at read time, so a config table query alone misses overrides.
        """
        from .db import Config, MODEL_DEFAULTS
        from .literate_config import env_var_for
        with get_session(engine) as session:
            rows = {r.key: r.value or "" for r in session.query(Config).all()}
        all_keys = sorted(set(rows) | set(MODEL_DEFAULTS))
        lines: list[str] = []
        for k in all_keys:
            env_val = os.environ.get(env_var_for(k))
            if env_val is not None:
                lines.append(f"  {k} = {env_val!r}  [env]")
            elif k in rows and rows[k]:
                lines.append(f"  {k} = {rows[k]!r}  [config]")
            elif k in MODEL_DEFAULTS:
                lines.append(f"  {k} = {MODEL_DEFAULTS[k]!r}  [default]")
        return _themed("get_config",
                        f"{len(lines)} key(s)",
                        "\n".join(lines))

    # ── set_config (allow-listed safe keys) ───────────────────────────────────
    _SETTABLE_KEYS = {
        "chat_model", "embed_model", "code_model", "tag_model", "review_model",
        "ollama_url", "temperature", "top_p", "context_window",
        "code_dirs", "fixer_model", "trek_level", "commie_level", "queer_level",
        # Proactive-doctor knobs — letting the in-opencode LLM (or user
        # via /theme-style slash) tune intervention aggressiveness.
        "doctor_proactive_mode", "doctor_stuck_threshold",
        "doctor_intervene_in", "doctor_auto_apply",
    }

    @server.tool()
    def set_config(key: str, value: str) -> str:
        """Update an allow-listed config key (safe subset only).

        Allow-listed keys: chat_model, embed_model, code_model, tag_model,
        review_model, ollama_url, temperature, top_p, context_window,
        code_dirs, fixer_model, trek_level, commie_level, queer_level,
        plus the doctor_* knobs.

        Refuses keys outside this list — credentials, grants, telemetry, and
        secrets are NEVER writeable from MCP. Every accepted change is
        recorded to the logbook (kind=config) so dbt + the user have an
        audit trail.
        """
        from .logbook import write_event as _log_event
        if key not in _SETTABLE_KEYS:
            _log_event("config", "set_config",
                        args=f"key={key}", outcome="refused",
                        response="not in allow-list")
            return (f"Refused: '{key}' is not in the MCP allow-list.\n"
                    f"Allow-listed keys: {', '.join(sorted(_SETTABLE_KEYS))}")
        from .db import Config
        with get_session(engine) as session:
            row = session.get(Config, key)
            old = row.value if row else "(unset)"
            if row:
                row.value = value
            else:
                session.add(Config(key=key, value=value))
            session.commit()
        _log_event("config", "set_config",
                    args=f"key={key}",
                    response=f"{old!r} → {value!r}", outcome="ok")
        # Literate-config autosync, best-effort + silent if disabled.
        try:
            from . import literate_config as _lc
            _lc.maybe_autosync()
        except Exception:
            pass
        return f"Updated {key}: {old!r} → {value!r}"

    # ── discover_filesystem ───────────────────────────────────────────────────
    @server.tool()
    def discover_filesystem() -> str:
        """Probe the user's filesystem for vaults, repos, dotfiles, Emacs configs.

        Returns a structured report of what actually exists on disk —
        useful for picking code-index roots, MCP grant roots, or just
        understanding the user's environment before answering questions.
        """
        from .discover import (
            discover, suggest_code_dirs, suggest_grant_roots,
            detect_preferred_language,
        )
        found = discover()
        if not found:
            return "No standard locations found. The user has an unusual layout."
        lines = ["FILESYSTEM INVENTORY"]
        for f in found:
            lines.append(f"  {f.path}  [{f.kind}]  {f.description}")
        lines.append("")
        code = suggest_code_dirs(found)
        if code:
            lines.append("Suggested code-index roots:")
            for c in code:
                lines.append(f"  - {c}")
        grants = suggest_grant_roots(found)
        if grants:
            lines.append("Suggested MCP grant roots:")
            for g in grants:
                lines.append(f"  - {g}")
        lang = detect_preferred_language()
        if lang:
            lines.append(f"Detected preferred language: {lang}")
        return "\n".join(lines)

    # ── doctor_health ─────────────────────────────────────────────────────────
    @server.tool()
    def doctor_health() -> str:
        """Concise health report: DB, Ollama, models, free RAM, vault index."""
        from .db import Node, File, Config
        import shutil
        lines = ["ORG-LLM HEALTH"]
        try:
            with get_session(engine) as session:
                n_files    = session.query(File).count()
                n_nodes    = session.query(Node).count()
                n_embedded = session.query(Node).filter(
                    Node.embedding.isnot(None)).count()
                cfg = {r.key: r.value for r in session.query(Config).all()}
            lines.append(f"  DB: ok  ({n_files} files, {n_nodes} nodes, "
                         f"{n_embedded} embedded)")
        except Exception as e:
            lines.append(f"  DB: ERROR — {e}")
            return "\n".join(lines)
        ollama = cfg.get("ollama_url", "http://localhost:11434")
        try:
            import urllib.request
            urllib.request.urlopen(f"{ollama}/api/tags", timeout=2).read()
            lines.append(f"  Ollama: reachable  ({ollama})")
        except Exception as e:
            lines.append(f"  Ollama: UNREACHABLE — {type(e).__name__}")
        lines.append(f"  chat_model:  {cfg.get('chat_model', '(unset)')}")
        lines.append(f"  embed_model: {cfg.get('embed_model', '(unset)')}")
        try:
            from .cloud import local_ram_gb, local_vram_gb
            lines.append(f"  Hardware:   {local_ram_gb():.1f} GB free RAM, "
                         f"{local_vram_gb():.1f} GB VRAM")
        except Exception:
            pass
        du = shutil.disk_usage(Path.home())
        lines.append(f"  Disk free:  {du.free / 2**30:.1f} GB")
        return "\n".join(lines)

    # ── performance_status ────────────────────────────────────────────────────
    @server.tool()
    def performance_status() -> str:
        """Hardware fit summary: free RAM/VRAM vs current model assignments."""
        try:
            from .performance import probe_hardware, recommend
            from .db import Config
            with get_session(engine) as session:
                cfg = {r.key: r.value for r in session.query(Config).all()}
            hw   = probe_hardware()
            recs = recommend(hw, role_assignments=cfg, benchmarks={},
                             pulled=set(), cloud_configured=bool(cfg.get("cloud_provider")))
            head = (f"Free RAM: {hw.ram_free_gb:.1f} GB"
                    + (f"  |  Free VRAM: {hw.vram_free_gb:.1f} GB"
                       if hw.vram_free_gb is not None else ""))
            lines = [head]
            for r in recs:
                marker = {"downgrade": "↓", "upgrade": "↑",
                          "missing": "+", "fit": "="}.get(r.severity, "?")
                lines.append(f"  {marker} {r.role}: {r.current or '(unset)'} "
                             f"→ {r.suggested}  ({r.reason})")
            return "\n".join(lines) if recs else head + "\n  (no role assignments)"
        except Exception as e:
            return f"performance check failed: {e}"

    # ── index_vault ───────────────────────────────────────────────────────────
    @server.tool()
    async def index_vault(ctx: Context | None = None) -> str:
        """Re-scan the org vault and update the index incrementally.

        Emits MCP progress notifications: one per file with the running
        count and the path. Clients that don't subscribe (opencode
        today) get a final string result; clients that do (Claude
        Code) see a live progress widget."""
        import asyncio
        from .indexer import index_directory
        with get_session(engine) as session:
            org_dir = _cfg(session, "org_dir")
            if not org_dir:
                return "org_dir not configured. Run: org-llm config org_dir <path>"
            label = _theme_label("index")
            await _info(ctx, f"{label}: walking {org_dir}")
            # Two-buffer trick — index_directory is sync, but we want async
            # progress events. Stash progress tuples; flush them between
            # batches via asyncio.run_coroutine_threadsafe-style awaits.
            updates: list[tuple[int, int, str]] = []
            def cb(current: int, total: int, path: str) -> None:
                updates.append((current, total, path))
            try:
                # Run the sync indexer in a thread so we can interleave
                # MCP progress flushes from the asyncio event loop.
                loop = asyncio.get_event_loop()
                task = loop.run_in_executor(
                    None,
                    lambda: index_directory(Path(org_dir).expanduser(),
                                              session, progress_cb=cb),
                )
                while not task.done():
                    await asyncio.sleep(0.5)
                    while updates:
                        c, t, p = updates.pop(0)
                        await _report(ctx, c, t,
                                       f"{label} — {c}/{t}: {Path(p).name}")
                files, nodes = await task
                # Drain any trailing updates that arrived after the last sleep.
                while updates:
                    c, t, p = updates.pop(0)
                    await _report(ctx, c, t,
                                   f"{label} — {c}/{t}: {Path(p).name}")
                session.commit()
                await _report(ctx, files, files,
                               f"{label} complete — {files} files, {nodes} nodes")
                return f"Indexed: {files} files, {nodes} nodes."
            except Exception as e:
                return f"Index failed: {e}"

    # ── embed_pending ─────────────────────────────────────────────────────────
    @server.tool()
    async def embed_pending(ctx: Context | None = None) -> str:
        """Generate embeddings for any unembedded nodes.

        Emits MCP progress notifications: one per node, themed via
        TREK_MSGS["embed"] ("Initializing deflector array — 12/100…")."""
        import asyncio
        from .indexer import embed_nodes
        from .db import Node
        with get_session(engine) as session:
            url    = _cfg(session, "ollama_url") or "http://localhost:11434"
            model  = _cfg(session, "embed_model") or "nomic-embed-text"
            n_pending = session.query(Node).filter(
                Node.embedding.is_(None)).count()
            if n_pending == 0:
                return "Nothing to embed — all nodes already embedded."
            label = _theme_label("embed")
            await _info(ctx, f"{label}: {n_pending} pending node(s) "
                              f"with model {model}")

            updates: list[tuple[int, int, str]] = []
            def cb(current: int, total: int, title: str) -> None:
                updates.append((current, total, title))
            try:
                loop = asyncio.get_event_loop()
                task = loop.run_in_executor(
                    None,
                    lambda: embed_nodes(session, model=model, base_url=url,
                                         force=False, progress_cb=cb),
                )
                while not task.done():
                    await asyncio.sleep(0.5)
                    while updates:
                        c, t, title = updates.pop(0)
                        await _report(ctx, c, t,
                                       f"{label} — {c}/{t}: {title[:60]}")
                count = await task
                while updates:
                    c, t, title = updates.pop(0)
                    await _report(ctx, c, t,
                                   f"{label} — {c}/{t}: {title[:60]}")
                await _report(ctx, count, count,
                               f"{label} complete — {count} new embeddings")
                return f"Embedded {count} new nodes (model: {model})."
            except Exception as e:
                return f"Embed failed: {e}. Is Ollama up?"

    # ── code_search ───────────────────────────────────────────────────────────
    @server.tool()
    def code_search(query: str, lang: str = "", limit: int = 10) -> str:
        """Search the code-index corpus only (excludes notes).

        Filters to nodes tagged 'code'. If `lang` is given (e.g. 'python',
        'elisp', 'rust'), further restricts to that language.
        """
        from .db import Node
        from .search import vector_search
        from .llm import embed as _embed
        with get_session(engine) as session:
            url    = _cfg(session, "ollama_url") or "http://localhost:11434"
            model  = _cfg(session, "embed_model") or "nomic-embed-text"
            try:
                qvec = _embed(query, model=model, base_url=url, task="query")
                hits = vector_search(session, qvec, limit=limit * 4)
            except Exception as e:
                return f"Search failed: {e}"
            results = []
            for h in hits:
                # Tags are stored space-separated, e.g. "code code:python"
                tags = (h.tags or "").split()
                if "code" not in tags:
                    continue
                if lang and f"code:{lang}" not in tags:
                    continue
                results.append(h)
                if len(results) >= limit:
                    break
        if not results:
            return f"No code matches for '{query}'" + (f" in {lang}" if lang else "")
        lines = []
        for h in results:
            tag_lang = next((t.split(":", 1)[1] for t in (h.tags or "").split()
                             if t.startswith("code:")), "?")
            lines.append(f"- {h.title}  [{tag_lang}]")
        return "\n".join(lines)

    # ── recent_files ──────────────────────────────────────────────────────────
    @server.tool()
    def recent_files(days: int = 7) -> str:
        """List org files modified in the last N days (file-level, not node-level)."""
        from datetime import datetime, timedelta
        from .db import File
        with get_session(engine) as session:
            since = (datetime.now() - timedelta(days=days)).timestamp()
            files = (
                session.query(File)
                .filter(File.mtime >= since)
                .order_by(File.mtime.desc())
                .limit(40).all()
            )
        if not files:
            return f"No files modified in the last {days} days."
        return "\n".join(
            f"- {Path(f.path).name}  "
            f"({datetime.fromtimestamp(f.mtime).date().isoformat() if f.mtime else '?'})"
            for f in files
        )

    # ── list_models ───────────────────────────────────────────────────────────
    @server.tool()
    def list_models() -> str:
        """List Ollama models pulled locally with their role assignments."""
        try:
            import urllib.request, json as _json
            from .db import Config
            with get_session(engine) as session:
                url = _cfg(session, "ollama_url") or "http://localhost:11434"
                cfg = {r.key: r.value for r in session.query(Config).all()}
            resp = urllib.request.urlopen(f"{url}/api/tags", timeout=3).read()
            tags = _json.loads(resp).get("models", [])
            role_for = {}
            for k in ("chat_model", "embed_model", "code_model", "tag_model",
                      "review_model", "fixer_model"):
                v = cfg.get(k)
                if v:
                    role_for.setdefault(v, []).append(k)
            lines = []
            for m in tags:
                name  = m.get("name", "?")
                size  = m.get("size", 0)
                roles = role_for.get(name, [])
                size_gb = size / 2**30 if size else 0
                roles_str = f"  ← {', '.join(roles)}" if roles else ""
                lines.append(f"- {name}  ({size_gb:.1f} GB){roles_str}")
            return "\n".join(lines) if lines else "No models pulled."
        except Exception as e:
            return f"list_models failed: {e}"

    # ── tutor tools ───────────────────────────────────────────────────────────
    @server.tool()
    def list_tutor_steps() -> str:
        """List all available org-llm tutor steps and their topics."""
        from .cli import _TUTOR_STEPS
        return "\n".join(
            f"  {i+1:2d}. {name}" for i, (name, _) in enumerate(_TUTOR_STEPS)
        )

    @server.tool()
    def get_tutor_step(step: str) -> str:
        """Get the full tutor content for a given step name (e.g. 'embed', 'skills', 'launch')."""
        from .cli import _TUTOR_STEPS
        # Strip Rich markup for plain-text MCP output
        import re
        match = next(((n, b) for n, b in _TUTOR_STEPS if n == step), None)
        if not match:
            names = [n for n, _ in _TUTOR_STEPS]
            return f"Unknown step '{step}'. Available: {', '.join(names)}"
        name, body = match
        # Remove Rich markup tags like [bold], [lcars1], [dim], etc.
        plain = re.sub(r"\[/?[^\]]+\]", "", body)
        return f"=== org-llm tutor: {name} ===\n\n{plain}"

    # ── dbt tools — let the in-opencode LLM run + inspect the analytics layer ──
    #
    # Maps to the org-llm dbt subcommand group. Each tool is a thin shim
    # over `org-llm dbt <verb>` (see cli.py → dbt_app) so the in-opencode
    # LLM can build, test, and report on dbt without the user typing a
    # single dbt command. `dbt_run` / `dbt_build` are async + emit MCP
    # progress events; the read-only ones return string snapshots.

    def _shell_org_llm_dbt(*args: str, timeout: int = 600) -> str:
        """Run `org-llm dbt …` as a subprocess and return combined output."""
        import subprocess
        try:
            proc = subprocess.run(
                ["org-llm", "dbt", *args],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return "org-llm binary not on PATH inside the MCP server env."
        except subprocess.TimeoutExpired:
            return f"`org-llm dbt {' '.join(args)}` timed out after {timeout}s."
        out = (proc.stdout or "")
        if proc.stderr:
            out += "\n[stderr]\n" + proc.stderr
        if len(out) > 8000:
            out = out[:8000] + "\n…(truncated)"
        if proc.returncode != 0:
            out += f"\n[exit {proc.returncode}]"
        return out or f"(no output, exit {proc.returncode})"

    @server.tool()
    async def dbt_status(ctx: Context | None = None) -> str:
        """dbt analytics layer: project paths, DB reachability, model row counts.

        Read-only — safe to call any time. Use this as the first probe
        when the user asks anything analytics-related."""
        await _info(ctx, "dbt status: probing project + row counts")
        return _shell_org_llm_dbt("status", timeout=30)

    @server.tool()
    async def dbt_doctor(ctx: Context | None = None) -> str:
        """dbt health check: binary, project, DB, raw tables, compile clean.

        Use this when something is wrong with the analytics layer or
        before suggesting a non-trivial dbt operation."""
        await _info(ctx, "dbt doctor: running checks")
        return _shell_org_llm_dbt("doctor", timeout=60)

    @server.tool()
    async def dbt_models() -> str:
        """List all dbt models with materialization (table / view / etc.)."""
        return _shell_org_llm_dbt("models", timeout=30)

    # Phase 24.2 — recovery hook site: dbt_run shells out for
    # what can be a multi-minute build. Transient subprocess
    # failures (FileNotFoundError on the org-llm binary, sudden
    # PATH change, OSError from a contended SQLite write) are
    # exactly the v0.1 retry cohort.
    @server.tool()
    @recover_on_failure
    async def dbt_run(select: str = "",
                       full_refresh: bool = False,
                       ctx: Context | None = None) -> str:
        """Build all (or matching) dbt models — staging views + mart tables.

        `select` filters by model name or folder ('staging', 'recent_nodes').
        `full_refresh=True` forces re-creation of incremental models.
        Emits MCP progress events while dbt runs (one tick per ~2s).
        """
        import asyncio, time as _t
        label = _theme_label("default") + " · dbt run"
        if select:
            label += f" -s {select}"
        await _info(ctx, label)
        args = ["run"]
        if select:        args += ["-s", select]
        if full_refresh:  args.append("--full-refresh")

        # Same executor + heartbeat pattern as org_llm_run so progress
        # actually flows during a multi-second dbt run.
        loop = asyncio.get_event_loop()
        started = _t.monotonic()
        timeout = 600
        fut = loop.run_in_executor(
            None, lambda: _shell_org_llm_dbt(*args, timeout=timeout))
        while not fut.done():
            await asyncio.sleep(2.0)
            elapsed = _t.monotonic() - started
            await _report(ctx, elapsed, float(timeout),
                           f"{label} — {elapsed:.0f}s elapsed")
        return await fut

    @server.tool()
    async def dbt_test(select: str = "",
                        ctx: Context | None = None) -> str:
        """Run dbt data tests (schema/data assertions on the materialized layer)."""
        await _info(ctx, "dbt test: running assertions")
        args = ["test"]
        if select: args += ["-s", select]
        return _shell_org_llm_dbt(*args, timeout=300)

    # Phase 24.2 — recovery hook site: dbt_build is dbt_run +
    # dbt_test compounded; same subprocess-shell failure surface,
    # same v0.1 retry cohort.
    @server.tool()
    @recover_on_failure
    async def dbt_build(select: str = "",
                         ctx: Context | None = None) -> str:
        """dbt run + dbt test in dependency order — the canonical "do it all".

        Use this after the user adds new notes (post `index_vault`) to
        keep the analytics views fresh."""
        import asyncio, time as _t
        label = "dbt build" + (f" -s {select}" if select else "")
        await _info(ctx, label)
        args = ["build"]
        if select: args += ["-s", select]
        loop = asyncio.get_event_loop()
        started = _t.monotonic()
        timeout = 600
        fut = loop.run_in_executor(
            None, lambda: _shell_org_llm_dbt(*args, timeout=timeout))
        while not fut.done():
            await asyncio.sleep(2.0)
            elapsed = _t.monotonic() - started
            await _report(ctx, elapsed, float(timeout),
                           f"{label} — {elapsed:.0f}s elapsed")
        return await fut

    @server.tool()
    async def dbt_compile() -> str:
        """Compile dbt SQL without executing — surfaces ref typos and schema drift."""
        return _shell_org_llm_dbt("compile", timeout=60)

    @server.tool()
    async def dbt_design(intent: str = "", apply: bool = False,
                          ctx: Context | None = None) -> str:
        """LLM-driven dbt model designer.

        - With empty intent: returns 3 model proposals grounded in
          the user's actual vault.
        - With intent: generates SQL, validates with `dbt compile`,
          writes the model file under user dbt dir. Pass apply=True
          to also materialize via `dbt build`.

        Useful when the user asks "what dbt models could I add?" or
        "build me a model that does X" — wraps the same flow as the
        CLI `org-llm dbt design [intent] [--apply]`."""
        await _info(ctx, "dbt design: consulting LLM…")
        args = ["design"]
        if intent: args.append(intent)
        if apply:  args.append("--apply")
        return _shell_org_llm_dbt(*args, timeout=180)

    @server.tool()
    async def dbt_walkthrough(model: str = "",
                                ctx: Context | None = None) -> str:
        """Walk through THIS user's dbt models with LLM commentary.

        For each model: SQL + 4-6 sentence explanation grounded in the
        user's vault. Pass `model` to focus on one; blank walks all in
        dependency order (staging → marts)."""
        await _info(ctx, "dbt walkthrough: explaining each model…")
        args = ["walkthrough"]
        if model: args.append(model)
        return _shell_org_llm_dbt(*args, timeout=300)

    @server.tool()
    async def dbt_lessons(level: str = "intro", topic: str = "",
                            ctx: Context | None = None) -> str:
        """LLM-instructed dbt lessons (intro / intermediate / advanced).

        Each lesson is grounded in the user's actual dbt models — the
        examples reference their real models rather than generic
        boilerplate. Pass topic to teach a specific concept; blank
        lists the curriculum for that level."""
        await _info(ctx, f"dbt lessons: level={level} topic={topic or '(curriculum)'}")
        args = ["lessons", "--level", level]
        if topic: args.append(topic)
        return _shell_org_llm_dbt(*args, timeout=180)

    # ── Discovery: list_slash_commands ────────────────────────────────────────
    @server.tool()
    def list_slash_commands() -> str:
        """List slash commands registered in this opencode workspace,
        grouped by intent prefix. Reads .opencode/command/*.md from the
        cwd. Use to answer 'what can I do?' without the user having to
        scroll the slash menu."""
        cmd_dir = Path(".opencode/command")
        if not cmd_dir.is_dir():
            # Fall back to bundled defaults via the cli helper.
            from .cli import _opencode_slash_commands
            cmds = _opencode_slash_commands("all")
            names = sorted(cmds.keys())
        else:
            names = sorted(p.stem for p in cmd_dir.glob("*.md"))
        if not names:
            return _themed("list_slash_commands", "no commands found")
        # Group by prefix (everything before the first '-' is the family).
        groups: dict[str, list[str]] = {}
        for n in names:
            family = n.split("-", 1)[0] if "-" in n else "general"
            groups.setdefault(family, []).append(n)
        body_lines = []
        for family in sorted(groups):
            cmds_in_group = sorted(groups[family])
            body_lines.append(
                f"  {family}:  " + "  ".join(f"/{c}" for c in cmds_in_group))
        return _themed("list_slash_commands",
                        f"{len(names)} command(s) across "
                        f"{len(groups)} group(s)",
                        "\n".join(body_lines))

    # ── Proactive doctor — for use when the in-opencode LLM is stuck ──────────
    @server.tool()
    async def proactive_doctor(ctx: Context | None = None) -> str:
        """Diagnose why the workspace might be slow / stuck and suggest a fix.

        Call this when:
          - You've made 3+ tool calls without converging on an answer.
          - The user complains about slowness or hung responses.
          - Your last reply was a vague hedge ("I'm not sure", "I can't tell")
            and you're not sure why the search came up empty.
          - Anything feels wrong and you want a second opinion.

        Probes:
          1. chat_model fit vs available RAM — is the local model swap-thrashing?
          2. Ollama reachability — is the daemon up?
          3. Cloud provider config — can we route around the local issue?
          4. Vault state — does a search-empty result actually reflect
             a missing index?
          5. Recent tok/s baseline — is the active model running slower
             than known alternatives on this hardware?

        Returns: a themed diagnosis + EXACT next-step command for the user.
        Doesn't auto-apply anything by itself — present the suggestion to
        the user. If they explicitly approve a remediation, you can call
        `proactive_doctor_apply(action=..., reason=..., user_approved=True)`
        for one of the vetted actions. NEVER call apply without user
        approval — that's a hard guardrail."""
        await _info(ctx, "proactive_doctor: probing chat_model + Ollama + cloud + perf")
        # Honour the umbrella suppression so testing / scripted runs
        # don't get auto-healing side-effects. Returns a one-line
        # explanation rather than running the probes.
        import os as _os
        if (_os.environ.get("ORG_LLM_PROACTIVE_DOCTOR", "")
                .strip().lower() == "off"):
            return ("proactive_doctor: SUPPRESSED by "
                    "ORG_LLM_PROACTIVE_DOCTOR=off / "
                    "--suppress-proactive-doctor. No probes run. Re-enable "
                    "by unsetting the env var or invoking without the flag.")
        import subprocess
        try:
            proc = subprocess.run(
                ["org-llm", "doctor", "--power-boost"],
                capture_output=True, text=True, timeout=30,
            )
            boost_out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            boost_out = f"(power-boost probe failed: {e})"
        try:
            proc = subprocess.run(
                ["org-llm", "doctor", "--no-diagnose"],
                capture_output=True, text=True, timeout=30,
            )
            doc_out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            doc_out = f"(doctor probe failed: {e})"

        # Performance baseline — is the active chat_model running slower
        # than a known alternative? Cheap query against existing History.
        # Also pull any unread perf alerts written by the inline lag
        # detector — MCP clients (opencode, Claude Code, …) don't see stderr, so the ring
        # buffer is how those events surface here.
        perf_lines: list[str] = []
        try:
            from . import perf as _perf
            with get_session(engine) as session:
                cur_chat = _cfg(session, "chat_model")
            cur_tok_s = _perf.recent_tok_s(cur_chat) if cur_chat else None
            alt = _perf.fastest_known("chat", current_model=cur_chat or "")
            if cur_tok_s and alt and alt.tok_s > cur_tok_s * 1.3:
                perf_lines.append(
                    f"PERF: {cur_chat} runs {cur_tok_s:.1f} tok/s here "
                    f"vs {alt.model} at {alt.tok_s:.1f} tok/s "
                    f"({alt.tok_s/cur_tok_s:.1f}× faster). Consider "
                    f"action=tune_models.")
            elif alt and not cur_tok_s:
                perf_lines.append(
                    f"PERF: no recent baseline for {cur_chat or '(unset)'}; "
                    f"{alt.model} has the best track record at "
                    f"{alt.tok_s:.1f} tok/s. Action: benchmark_local.")
            try:
                regwarn = _perf.regression_warning(cur_chat) if cur_chat else None
                if regwarn:
                    perf_lines.append(f"REGRESSION: {regwarn}")
            except Exception:
                pass
            # Surface unread lag-detector alerts (max 5 most recent).
            alerts = _perf.recent_perf_alerts(limit=5)
            if alerts:
                perf_lines.append(
                    f"LAG ALERTS ({len(alerts)} recent — MCP clients "
                    f"don't render stderr, so these were silent until now):")
                for a in alerts:
                    perf_lines.append(
                        f"  · {a['model']} took {a['elapsed_s']:.0f}s; "
                        f"{a['suggested_model']} runs faster at "
                        f"{a['suggested_tok_s']:.1f} tok/s")
                # Clear once surfaced — same alert shouldn't keep paging
                # the LLM after it's been actioned.
                _perf.clear_perf_alerts()
        except Exception as e:
            perf_lines.append(f"(perf probe failed: {e})")

        # ── Live vital systems via life_support — fresh probe + log + recent
        # activity-correlations from sensor_log. The MCP-side surface
        # mirrors what `org-llm doctor` shows in CLI.
        vitals_block = ""
        try:
            from . import life_support as _ls
            fresh = _ls.probe_all()
            _ls.record_readings(fresh)
            vitals_block = "\n\n--- live vital systems ---\n"
            for r in fresh:
                vitals_block += (f"  {r.name:<14} {r.label:<24} "
                                  f"[{r.status}]  {r.message}\n")
            recent = _ls.recent_readings(since_secs=3600, limit=400)
            seen: set[str] = set()
            corr_lines: list[str] = []
            for v in recent:
                if v.get("status") not in ("alert", "critical"):
                    continue
                ctx = (v.get("context") or "").strip()
                if not ctx or ctx in seen:
                    continue
                seen.add(ctx)
                corr_lines.append(f"  · {v['probe']} {v['status']} during: {ctx}")
                if len(corr_lines) >= 5:
                    break
            if corr_lines:
                vitals_block += ("\nactivity correlations (alert/critical "
                                  "readings + what the user was doing):\n"
                                  + "\n".join(corr_lines))
        except Exception as e:
            vitals_block = f"\n(life-support probe failed: {e})"

        actions_block = (
            "\n--- vetted apply actions ---\n"
            "If the user explicitly approves, you may call "
            "`proactive_doctor_apply(action=..., reason=..., "
            "user_approved=True)` with one of:\n"
            "  • `tune_models`         — `org-llm models --upgrade --apply`\n"
            "  • `power_boost`         — `org-llm doctor --power-boost --apply`\n"
            "  • `pull_smallest_chat`  — pull llama3.2:1b as a fallback\n"
            "  • `restart_ollama`      — kill + restart the local daemon\n"
            "  • `regen_themes`        — refresh the theme-studio cache\n"
            "  • `init_db`             — `org-llm init` (only if DB missing)\n"
            "Never invent actions; never invoke apply without explicit\n"
            "user approval surfaced in the chat first."
        )
        body = (f"{boost_out.strip()}\n\n--- doctor snapshot ---\n"
                f"{doc_out.strip()}")
        if perf_lines:
            body += "\n\n--- performance baseline ---\n" + "\n".join(perf_lines)
        body += vitals_block
        body += actions_block
        return _themed("proactive_doctor",
                        "model + ollama + cloud + vault + perf probe", body)

    # ── recent_perf_alerts: lightweight lag-event poller ─────────────────────
    @server.tool()
    async def recent_perf_alerts(limit: int = 10,
                                    clear: bool = False,
                                    ctx: Context | None = None) -> str:
        """Read recent lag-detector events from the perf ring buffer.

        The inline lag detector in `llm.chat()` writes one event each
        time a chat call ran meaningfully slower than its rolling
        baseline AND a faster pulled alternative exists. MCP clients
        (opencode, Claude Code, …) don't render stderr, so these events are otherwise
        invisible inside the workspace harness.

        Cheap (one JSON read). With `clear=True`, drains the buffer
        so the same events don't page the LLM repeatedly after
        they've been surfaced.
        """
        from . import perf as _perf
        rows = _perf.recent_perf_alerts(limit=limit)
        if not rows:
            return "no recent perf alerts"
        lines = [f"{len(rows)} recent perf alert(s):"]
        for r in rows:
            from datetime import datetime as _dt
            ts = _dt.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
            cur = (f"{r['current_tok_s']:.1f}"
                    if r.get("current_tok_s") is not None else "?")
            lines.append(
                f"  [{ts}] {r['model']} ran {r['elapsed_s']:.0f}s "
                f"({cur} tok/s); {r['suggested_model']} would run "
                f"{r['suggested_tok_s']:.1f} tok/s")
        if clear:
            n = _perf.clear_perf_alerts()
            lines.append(f"  (cleared {n} alert(s))")
        else:
            lines.append("  (call again with clear=True to mark as read)")
        return "\n".join(lines)

    # ── life_support: vital systems probe + log ──────────────────────────────
    @server.tool()
    async def life_support_status(record: bool = True,
                                    ctx: Context | None = None) -> str:
        """Probe the host system's vital signs RIGHT NOW: battery, CPU,
        memory, disk, thermals, network, Ollama daemon, auto-embedder.

        Returns one line per probe with reading + status + a Trek-themed
        message. Self-hosted means the laptop is the substrate; this is
        how the in-opencode LLM can observe its own running conditions.

        With record=True (default) the readings are also written to
        sensor_log so the timeseries grows. Pass record=False for a
        read-only inspection.
        """
        from . import life_support as _ls
        readings = _ls.probe_all()
        if record:
            _ls.record_readings(readings)
        out = [f"life-support  · {_ls.overall_status(readings)}"]
        for r in readings:
            out.append(f"  {r.name:<14} {r.label:<24} "
                        f"[{r.status}]  {r.message}")
        return "\n".join(out)

    @server.tool()
    async def life_support_history(probe: str = "", window_minutes: int = 60,
                                     limit: int = 50,
                                     ctx: Context | None = None) -> str:
        """Pull rows from the sensor_log timeseries.

        For ALL probes: pass `probe=""` (empty string) or omit the
        argument. Do NOT pass `*`, `all`, `any`, or any other
        wildcard — the column is matched literally and a wildcard
        will return zero rows.

        For a SPECIFIC probe: pass one of `battery`, `cpu`, `memory`,
        `disk`, `thermal`, `network`, `ollama`, `auto_embedder`.

        `window_minutes` defaults to one hour. Each row carries the
        user-activity context recorded at probe time, so the LLM can
        correlate resource spikes with the verbs that caused them.
        Use this when the user asks "have I been thrashing the CPU
        lately?" or "was the battery dropping during my last ask?" —
        the answers are in this table.
        """
        from . import life_support as _ls
        # Defensive: small models keep passing `*` / `all` as a
        # wildcard despite the docstring. Normalize those to empty
        # so the user's question gets answered instead of returning
        # a confusing 'no data' string.
        probe_norm = (probe or "").strip().lower()
        if probe_norm in ("", "*", "all", "any", "none", "null"):
            probe_norm = ""
        rows = _ls.recent_readings(
            probe=probe_norm or None,
            since_secs=max(60, window_minutes * 60),
            limit=max(1, min(500, limit)),
        )
        if not rows:
            return ("no sensor_log data in window — run "
                    "`org-llm life-support --interval 30` to seed it")
        from datetime import datetime as _dt
        out = [f"sensor_log  · {len(rows)} row(s) over last "
                f"{window_minutes}min"]
        for r in rows:
            ts = _dt.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
            ctx_s = (r.get("context") or "").strip()
            ctx_disp = f"  [{ctx_s}]" if ctx_s else ""
            out.append(f"  [{ts}] {r['probe']:<12} "
                        f"{r['label']:<22} [{r['status']}]{ctx_disp}")
        return "\n".join(out)

    @server.tool()
    async def life_support_advice(window_minutes: int = 60,
                                    ctx: Context | None = None) -> str:
        """Ask the chat model for concrete optimisation suggestions
        based on the recent sensor_log window.

        Subject to the same small-sample-size deterministic floor as
        the CLI verb — needs ≥12 samples per probe in the window.
        Below that, returns a message asking the user to seed more
        data. Above it, returns 1-3 bulleted suggestions anchored in
        real probe values + activity correlations.

        The LLM output goes through the rescue.sanitize_llm_advice
        pass so any banned shell pattern (pip install, curl | sh,
        rm -rf, etc.) is flagged with a [FLAGGED:] prefix.
        """
        from . import life_support as _ls
        return (_ls.llm_optimization_advice(
                    window_secs=max(60, window_minutes * 60))
                or "no optimisations to suggest right now — "
                   "everything looks stable")

    @server.tool()
    async def emh_consult(question: str,
                            window_hours: int = 24,
                            diagnose: bool = False,
                            ctx: Context | None = None) -> str:
        """Route a health-of-the-machine question to the Emergency
        Medical Hologram. Voyager EMH persona; ALWAYS opens with
        "Please state the nature of the medical emergency." Answers
        from sensor_log telemetry as ground truth, NOT from the
        org-roam vault.

        Two modes:

        - *Question mode* (default): pass `question` and the EMH
          answers using the timeseries. Good for "have my CPU loads
          been weird lately?", "why is the battery dropping fast?",
          "was thermal in alert when I ran ask --reason yesterday?"

        - *Diagnostic mode* (`diagnose=True`): EMH walks EVERY
          probe systematically, gives a finding per probe, ends
          with a prioritized remediation list. Use when the user
          says "what's wrong with my laptop?" or "do a full check".

        `window_hours` controls how far back the EMH looks (default
        24, max 168 = a week). The EMH explicitly handles HISTORICAL
        questions — point it at the right window and ask.

        Use this for LAPTOP questions. For vault questions use
        search_notes / ask_notes.
        """
        import subprocess
        cmd = ["org-llm", "ask", "--emh",
                "--window-hours", str(max(1, min(168, window_hours)))]
        if diagnose:
            cmd.append("--diagnose")
        cmd.append(question)
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                    text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return ("EMH consult timed out (120s). The chat model is "
                    "probably overloaded; try again or run "
                    "`life_support_advice` for a deterministic fallback.")
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or "EMH offline (no output)"

    # Token store + helpers shared with proactive_doctor_apply pattern,
    # but for self_rewrite the (action, reason) tuple is (module, intent).
    _self_rewrite_tokens: dict[str, dict] = {}
    _SELF_REWRITE_TTL_S = 300

    def _gen_sr_token() -> str:
        import secrets
        return secrets.token_hex(8).upper()

    def _take_sr_token(token: str, module: str, intent: str
                          ) -> tuple[bool, str]:
        import time as _t
        rec = _self_rewrite_tokens.pop(token, None)
        if rec is None:
            return False, "token unknown or already consumed"
        if rec["expires"] < _t.time():
            return False, "token expired (5-minute TTL)"
        if rec["module"] != module:
            return False, (f"token was issued for module "
                            f"{rec['module']!r}, not {module!r}")
        if rec["intent"] != intent:
            return False, (f"token was issued with intent "
                            f"{rec['intent']!r}, not {intent!r}")
        return True, ""

    # ── self_rewrite: LLM-driven patch of org-llm's own source ──────────────
    @server.tool()
    async def self_rewrite(
            module: str,
            intent: str,
            approval_token: str = "",
            ctx: Context | None = None) -> str:
        """LLM-rewrite ONE org-llm source file to fix a bug or address a
        well-defined intent. Snapshots before any change; verifies by
        re-running tests; auto-rolls-back if the patch introduces a
        regression.

        STRICT GUARDRAILS (token-based; the LLM cannot fake approval):

        FIRST CALL: pass module + intent WITHOUT approval_token. Tool
        returns a PENDING APPROVAL message with a token. Show it to
        the user verbatim. Do NOT proceed without their explicit
        token-bearing reply.

        SECOND CALL: pass module + intent + approval_token=<token>.
        Token must match the (module, intent) tuple that issued it
        and must be within the 5-minute TTL.

        `module` must resolve to a file inside the org_llm/ package.
        No arbitrary filesystem writes — paths outside raise.

        Steps (deterministic, in this order):
          1. Resolve `module` to org_llm/<name>.py.
          2. Take a snapshot via `self_mod.create_snapshot`.
          3. Ask the configured code_model for a JSON patch via
             `self_mod.llm_revise(target, intent)`.
          4. Apply via `self_mod.apply_plan`. If apply refuses (bad
             diff shape), bail with snapshot intact.
          5. Run `pytest` on the targeted-tests subset (theme_studio
             + workspace_mcp + cli) under a 5-minute timeout.
          6. If tests pass, return success + the snapshot ID for
             reference. If tests FAIL, automatically roll back via
             `self_mod.rollback(snap)` and return the rollback summary."""
        import os as _os, time as _t
        if (_os.environ.get("ORG_LLM_PROACTIVE_DOCTOR", "")
                .strip().lower() == "off"):
            return ("REFUSED: self_rewrite is disabled by "
                    "ORG_LLM_PROACTIVE_DOCTOR=off — the user opted out "
                    "of auto-healing for this session.")

        # No token → issue one + demand explicit user consent
        if not approval_token:
            tok = _gen_sr_token()
            _self_rewrite_tokens[tok] = {
                "module":  module,
                "intent":  intent,
                "expires": _t.time() + _SELF_REWRITE_TTL_S,
            }
            return (
                f"PENDING APPROVAL: self_rewrite\n"
                f"  module={module!r}\n  intent={intent!r}\n\n"
                f"Token: {tok}  (expires in 5 minutes)\n\n"
                f"Self-rewrite patches org-llm's OWN source code. "
                f"This is high-stakes. Relay this message to the user "
                f"verbatim and ONLY proceed when they reply with the "
                f"literal token:\n"
                f'  "I want to LLM-rewrite `{module}` for: '
                f'{intent}\\n\\nReply `approve {tok}` to proceed. A '
                f'snapshot will be taken; tests will run; an automatic '
                f'rollback fires if anything regresses."\n\n'
                f"After the user types `approve {tok}`, call this tool "
                f"again with approval_token={tok!r}."
            )

        ok, why = _take_sr_token(approval_token, module, intent)
        if not ok:
            return f"REFUSED: approval_token invalid — {why}"

        # ── 1. resolve target ───────────────────────────────────────────────
        from pathlib import Path as _P
        pkg_root = _P(__file__).resolve().parent
        # Accept "cli", "cli.py", or absolute paths inside the package.
        m = module.strip()
        if not m:
            return "REFUSED: empty `module`."
        if not m.endswith(".py"):
            m += ".py"
        if "/" in m:
            target = _P(m).resolve()
        else:
            target = (pkg_root / m).resolve()
        try:
            target.relative_to(pkg_root)
        except ValueError:
            return (f"REFUSED: {target} is outside the org_llm package. "
                    f"self_rewrite only patches files under "
                    f"{pkg_root}.")
        if not target.exists():
            return f"REFUSED: {target} does not exist."

        # ── 2. snapshot ─────────────────────────────────────────────────────
        try:
            from . import self_mod as _sm
        except Exception as e:
            return f"FAILED: cannot import self_mod: {e}"
        try:
            snap = _sm.create_snapshot(
                label=f"pre-self_rewrite-{target.stem}")
        except Exception as e:
            return f"FAILED: snapshot creation refused: {e}"
        await _info(ctx,
                    f"self_rewrite: snapshot {snap.id} taken before "
                    f"patching {target.name}")

        # ── 3. propose patch ────────────────────────────────────────────────
        try:
            with get_session(engine) as session:
                url   = _ollama_url(session)
                model = (_cfg(session, "code_model")
                          or _cfg(session, "chat_model")
                          or "llama3.2")
            plan = _sm.llm_revise(target, intent,
                                    model=model, base_url=url)
        except Exception as e:
            return (f"FAILED: LLM didn't propose a patch: {e}\n"
                    f"Snapshot {snap.id} kept; nothing was changed.")
        if not plan or not plan.get("ops"):
            return (f"NO-OP: LLM returned no operations. Intent may be "
                    f"too vague or the file already satisfies it. "
                    f"Snapshot: {snap.id}.")

        # ── 4. apply ────────────────────────────────────────────────────────
        ok, msg = _sm.apply_plan(target, plan)
        if not ok:
            return (f"REFUSED: apply_plan rejected the diff: {msg}\n"
                    f"Snapshot {snap.id} kept; nothing was changed.")
        await _info(ctx, f"self_rewrite: patch applied to {target.name}")

        # ── 5. verify with tests ────────────────────────────────────────────
        import subprocess
        try:
            proc = subprocess.run(
                ["uv", "run", "pytest",
                 "tests/test_theme_studio.py",
                 "tests/test_workspace_mcp.py",
                 "tests/test_cli.py",
                 "-q", "--no-header", "-x"],
                cwd=str(pkg_root.parent),
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            # Out of an abundance of caution, roll back. A 5-minute
            # test run that doesn't finish is itself a regression.
            try:
                _sm.rollback(snap, also_db=False)
            except Exception:
                pass
            return (f"FAILED: verify tests timed out (>300s); rolled back "
                    f"to snapshot {snap.id}.")
        except Exception as e:
            return (f"FAILED: could not spawn test runner: {e}. Snapshot "
                    f"{snap.id} kept; patch is still applied. Manual "
                    f"verification needed.")

        # ── 6. rollback on regression, otherwise summarise success ──────────
        if proc.returncode != 0:
            tail = (proc.stdout or "") + (proc.stderr or "")
            tail = tail[-2000:]
            try:
                summary = _sm.rollback(snap, also_db=False)
                n_restored = (summary.get("restored_files", "?")
                                if isinstance(summary, dict) else "?")
            except Exception as e:
                return (f"FAILED: tests regressed AND rollback raised "
                        f"({e}). Manual recovery: bash "
                        f"~/.local/share/org-llm/snapshots/"
                        f"{snap.id}/rollback.sh\n\n--- test tail ---\n"
                        f"{tail}")
            return (f"REVERTED: tests regressed after the patch; rolled "
                    f"back to snapshot {snap.id} ({n_restored} files "
                    f"restored). Patch summary: "
                    f"{plan.get('summary', '(none)')!r}\n\n"
                    f"--- test tail ---\n{tail}")
        return (f"DONE: self_rewrite succeeded on {target.name}. "
                f"Snapshot {snap.id} retained for review.\n"
                f"  summary:   {plan.get('summary', '(none)')!r}\n"
                f"  risk:      {plan.get('risk', '(none)')!r}\n"
                f"  test_hint: {plan.get('test_hint', '(none)')!r}\n"
                f"Tell the user: tests pass, but a human review of the "
                f"diff is recommended. Roll back any time with "
                f"`org-llm self rollback {snap.id}`.")

    # ── Token-based approval store (defeats LLM faking user_approved) ───────
    # Earlier the apply tool just trusted a `user_approved=True` flag
    # set by the LLM. A real-world test showed gpt-oss-20b setting that
    # flag UNILATERALLY when asked to "test the guard" — defeating the
    # whole purpose of the gate. The LLM cannot be the source of truth
    # for user intent; an out-of-band token the LLM CAN'T have invented
    # is the only honest enforcement.
    #
    # Flow:
    #   1. LLM calls proactive_doctor_apply(action, reason) without any
    #      token. Tool generates a fresh token, stores
    #      {token: (action, reason, expiry)} in this dict, and returns
    #      a PENDING APPROVAL message instructing the LLM to relay the
    #      token to the user verbatim.
    #   2. The user reads the token + the proposed action, and replies
    #      something like "approve TOKEN" in chat.
    #   3. LLM calls again with approval_token=TOKEN.
    #   4. Tool validates: token exists, not expired, action+reason
    #      match what was first proposed. Pops the entry and runs.
    #
    # In-memory only (per MCP-server process) with a 5-minute TTL —
    # restarting the MCP server invalidates pending approvals, which
    # is the correct safety property.
    _approval_tokens: dict[str, dict] = {}
    _APPROVAL_TTL_S = 300

    def _gen_approval_token() -> str:
        import secrets
        return secrets.token_hex(8).upper()   # 16 hex chars

    def _take_approval(token: str, action: str, reason: str
                          ) -> tuple[bool, str]:
        """Validate + consume an approval token. Returns (ok, why)."""
        import time as _t
        rec = _approval_tokens.pop(token, None)
        if rec is None:
            return False, "token unknown or already consumed"
        if rec["expires"] < _t.time():
            return False, "token expired (5-minute TTL)"
        if rec["action"] != action:
            return False, (f"token was issued for action "
                            f"{rec['action']!r}, not {action!r}")
        if rec["reason"] != reason:
            return False, (f"token was issued with reason "
                            f"{rec['reason']!r}, not {reason!r}")
        return True, ""

    # ── proactive_doctor_apply: vetted-action remediation ───────────────────
    @server.tool()
    async def proactive_doctor_apply(
            action: str,
            reason: str = "",
            approval_token: str = "",
            ctx: Context | None = None) -> str:
        """Apply ONE vetted remediation action to fix a diagnosed issue.

        STRICT GUARDRAILS (token-based; the LLM cannot fake approval):

        FIRST CALL: pass action + reason WITHOUT approval_token. The
        tool returns a PENDING APPROVAL message containing a token
        and instructions. Show the FULL message to the user verbatim;
        do not abbreviate, do not auto-fill the token, do not call
        again unless the user has read the proposal AND replied with
        the literal token.

        SECOND CALL (after explicit user consent): pass action + reason
        + approval_token=<token-from-step-1>. The token is validated
        + consumed. Tokens have a 5-minute TTL and are bound to the
        action + reason from the first call — you cannot reuse a
        token for a different remediation.

        Vetted actions:
          - `tune_models`        — runs `org-llm models --upgrade --apply`
                                    (real benchmark; picks fastest fitting)
          - `power_boost`        — runs `org-llm doctor --power-boost --apply`
                                    (downsizes chat_model when it doesn't fit)
          - `pull_smallest_chat` — pulls `llama3.2:1b` as an emergency fallback
          - `restart_ollama`     — kills + restarts the local Ollama daemon
          - `regen_themes`       — runs `org-llm theme-studio regenerate`
          - `init_db`            — runs `org-llm init` (only if DB missing)

        Every call is recorded to the Captain's Log (kind=doctor)
        with the action, reason, and outcome. The user can audit.
        """
        import os as _os, time as _t
        if (_os.environ.get("ORG_LLM_PROACTIVE_DOCTOR", "")
                .strip().lower() == "off"):
            return ("REFUSED: proactive_doctor_apply is disabled by "
                    "ORG_LLM_PROACTIVE_DOCTOR=off. The user opted out "
                    "of auto-healing for this session.")

        # No token supplied → issue one and demand explicit user consent
        # before the second call. The LLM can't bypass this; it has no
        # way to invent a valid token.
        if not approval_token:
            tok = _gen_approval_token()
            _approval_tokens[tok] = {
                "action":  action,
                "reason":  reason,
                "expires": _t.time() + _APPROVAL_TTL_S,
            }
            return (
                f"PENDING APPROVAL: action={action!r} reason={reason!r}\n\n"
                f"Token: {tok}  (expires in 5 minutes)\n\n"
                f"Relay this message to the user verbatim:\n"
                f'  "I want to run `{action}` ({reason}). '
                f"Reply with `approve {tok}` to proceed.\"\n\n"
                f"Then, AFTER the user types `approve {tok}` (or "
                f"otherwise gives explicit consent that includes the "
                f"literal token), call this tool again with "
                f"approval_token={tok!r}. Do NOT call without an "
                f"explicit user-typed token in their last message."
            )

        # Token supplied → validate
        ok, why = _take_approval(approval_token, action, reason)
        if not ok:
            return f"REFUSED: approval_token invalid — {why}"

        VETTED: dict[str, list[str]] = {
            "tune_models":         ["org-llm", "models", "--upgrade", "--apply"],
            "power_boost":         ["org-llm", "doctor", "--power-boost", "--apply"],
            "pull_smallest_chat":  ["org-llm", "models", "--pull", "llama3.2:1b"],
            "restart_ollama":      [],   # special-cased below (composite)
            "regen_themes":        ["org-llm", "theme-studio", "regenerate"],
            "init_db":             ["org-llm", "init"],
        }
        if action not in VETTED:
            await _info(ctx, f"proactive_doctor_apply REFUSED unknown action: {action!r}")
            return (f"REFUSED: unknown action {action!r}. Vetted set: "
                    f"{', '.join(sorted(VETTED))}.")

        await _info(ctx, f"proactive_doctor_apply: action={action} reason={reason!r} approved=YES")
        import subprocess

        # Slow actions (tune_models runs `models --upgrade --apply`
        # which takes 1-2 min on CPU; regen_themes takes 10+ min)
        # exceed opencode's MCP request timeout (~60s). Fork them
        # background-detached and return immediately with a status
        # file path the user can tail. Fast actions (restart_ollama,
        # init_db, pull_smallest_chat which is one-shot HTTP) stay
        # synchronous so the LLM can summarise the outcome inline.
        SLOW = {"tune_models", "regen_themes"}

        # Log BEFORE running so the audit trail captures intent even if
        # the subprocess hangs.
        try:
            from .logbook import track_event
            with track_event("doctor", "proactive_doctor_apply",
                              args=f'action={action!r} reason={reason!r}'):
                if action == "restart_ollama":
                    # No clean shell-injection vector — pkill + ollama serve.
                    try:
                        subprocess.run(["pkill", "-x", "ollama"],
                                        timeout=5, check=False)
                    except Exception:
                        pass
                    try:
                        subprocess.Popen(
                            ["ollama", "serve"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        return (f"DONE: restart_ollama. "
                                f"reason={reason!r}. Daemon re-launched in background.")
                    except Exception as e:
                        return f"FAILED: restart_ollama: {e}"

                cmd = VETTED[action]
                if action in SLOW:
                    # Fork detached, write stdout+stderr to a per-job
                    # file the user can tail. Return a job ID + the
                    # path so the LLM can tell the user how to follow.
                    from pathlib import Path as _P
                    import time as _t, secrets as _sec, os as _os2
                    job_id = (f"{action}-"
                                f"{int(_t.time())}-{_sec.token_hex(3)}")
                    log_dir = _P(_os2.environ.get("XDG_DATA_HOME")
                                  or _os2.path.expanduser("~/.local/share"))
                    log_dir = log_dir / "org-llm" / "apply-jobs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{job_id}.log"
                    try:
                        with open(log_path, "wb") as fh:
                            subprocess.Popen(
                                cmd,
                                stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True,
                            )
                        return (
                            f"STARTED: {action}. job_id={job_id}\n"
                            f"reason={reason!r}\n"
                            f"This action runs longer than the MCP "
                            f"timeout. Tail the live log with:\n"
                            f"  tail -f {log_path}\n"
                            f"or check completion in a few minutes "
                            f"with `org-llm log --kind doctor`."
                        )
                    except Exception as e:
                        return f"FAILED: {action} could not spawn: {e}"

                # Synchronous path for fast actions
                try:
                    proc = subprocess.run(cmd, capture_output=True,
                                            text=True, timeout=50)
                except subprocess.TimeoutExpired:
                    return (f"FAILED: {action} timed out after 50s. "
                            f"Check `org-llm log --kind doctor` for context.")
                except Exception as e:
                    return f"FAILED: {action}: {e}"
                head = "DONE" if proc.returncode == 0 else "FAILED"
                tail = (proc.stdout or "") + (proc.stderr or "")
                if len(tail) > 4000:
                    tail = tail[:4000] + "\n…(truncated)"
                return (f"{head}: {action}. exit={proc.returncode}. "
                        f"reason={reason!r}.\n\n{tail.strip()}")
        except Exception as e:
            return f"FAILED: proactive_doctor_apply scaffolding: {e}"

    # ── Captain's Log reflection ──────────────────────────────────────────────
    @server.tool()
    async def reflect_on_log(window: int = 30,
                              ctx: Context | None = None) -> str:
        """LLM reflection on the user's recent Captain's Log entries.

        Reads the last `window` events (default 30) from the SQLite
        `history` table, hands them to the chat model, returns
        PATTERNS + SUGGESTIONS + a one-line HEADLINE. Use when the
        user asks 'how am I using this tool', 'is anything off lately',
        or after a long session to surface what happened.
        """
        await _info(ctx, f"reflect_on_log: window={window}")
        import subprocess
        try:
            proc = subprocess.run(
                ["org-llm", "log", "--reflect",
                 "--limit", str(max(5, min(200, window)))],
                capture_output=True, text=True, timeout=120,
            )
            out = (proc.stdout or "")
            if proc.stderr:
                out += "\n[stderr]\n" + proc.stderr
            if len(out) > 8000:
                out = out[:8000] + "\n…(truncated)"
            return out
        except Exception as e:
            return f"reflect_on_log failed: {e}"

    # ── Captain's Log export ──────────────────────────────────────────────────
    @server.tool()
    async def export_log_to_org(dest_path: str,
                                  kind: str = "",
                                  grep: str = "",
                                  limit: int = 100,
                                  ctx: Context | None = None) -> str:
        """Append filtered Captain's Log rows to a user-chosen org file.

        Use when the user says 'save my recent LLM activity to my journal'
        or 'export today's tool calls to ~/org/work.org'. The destination
        gets a per-export parent heading + FILTER property; rows mirror
        the canonical Captain's Log shape (PROPERTIES drawer +
        #+begin_src text body block). Source of truth stays in the DB.
        """
        await _info(ctx,
                    f"export_log_to_org: dest={dest_path!r} kind={kind!r} "
                    f"grep={grep!r} limit={limit}")
        try:
            from . import logbook as _lb
            from .db import History
            from pathlib import Path as _P
            with get_session(engine) as session:
                q = session.query(History).order_by(History.id.desc())
                if kind:
                    q = q.filter(History.kind == kind)
                rows = q.limit(max(1, min(1000, limit * (4 if grep else 1)))
                                ).all()
            if grep:
                gl = grep.lower()
                rows = [r for r in rows
                        if gl in (r.command or "").lower()
                        or gl in (r.query or "").lower()
                        or gl in (r.response or "").lower()][:limit]
            else:
                rows = rows[:limit]
            if not rows:
                return _themed("export_log_to_org",
                               "No rows matched", "")
            filt = []
            if kind: filt.append(f"kind={kind}")
            if grep: filt.append(f"grep={grep}")
            filt.append(f"limit={limit}")
            n = _lb.export_rows_to_org(rows, _P(dest_path).expanduser(),
                                          source_filter=", ".join(filt))
            _lb.write_event("mcp", "export_log_to_org",
                              args=f"dest={dest_path} {' '.join(filt)}",
                              response=f"exported {n} rows", outcome="ok")
            return _themed("export_log_to_org",
                           f"exported {n} rows → {dest_path}",
                           f"filter: {', '.join(filt)}")
        except Exception as e:
            return f"export_log_to_org failed: {e}"

    # ── Config search ─────────────────────────────────────────────────────────
    @server.tool()
    async def search_config(query: str,
                              ctx: Context | None = None) -> str:
        """Fuzzy-search config keys by name AND description AND env-var name.

        Use when the user asks "what's that config key for X" / "how do
        I tune Y" / "is there a setting for Z". Each hit shows current
        value with its source (env / config / default), the env var
        name, and the description.
        """
        await _info(ctx, f"search_config: {query!r}")
        from .literate_config import KEY_DESCRIPTIONS, env_var_for
        from .db import Config, MODEL_DEFAULTS
        with get_session(engine) as session:
            db_rows = {r.key: r.value or "" for r in session.query(Config).all()}
        ql = query.lower()
        hits: list[str] = []
        all_keys = sorted(set(db_rows) | set(MODEL_DEFAULTS) | set(KEY_DESCRIPTIONS))
        for k in all_keys:
            desc = KEY_DESCRIPTIONS.get(k, "")
            env_name = env_var_for(k)
            if (ql in k.lower() or ql in desc.lower()
                    or ql in env_name.lower()):
                env_val = os.environ.get(env_name)
                if env_val is not None:
                    cur, src = env_val, "env"
                elif k in db_rows and db_rows[k]:
                    cur, src = db_rows[k], "config"
                else:
                    cur, src = MODEL_DEFAULTS.get(k, ""), "default"
                hits.append(
                    f"  {k} = {cur!r}  [{src}]\n"
                    f"    env: {env_name}\n"
                    f"    {desc}"
                )
        if not hits:
            return _themed("search_config", f"no matches for {query!r}",
                            "Try a broader pattern or just `get_config` to "
                            "see all keys.")
        return _themed("search_config",
                        f"{len(hits)} match(es) for {query!r}",
                        "\n".join(hits))

    # ── Theme knob: LLM-built ─────────────────────────────────────────────────
    @server.tool()
    async def add_theme_knob(name: str, vibe: str = "",
                              specifics: dict[str, str] | None = None,
                              n_messages: int = 8,
                              ctx: Context | None = None) -> str:
        """Register a new LLM-built theme knob in the user's config.

        Knobs control `make_it_so` completion vocabulary — a "synthwave"
        knob, when ORG_LLM_SYNTHWAVE_LEVEL >= 1, mixes synthwave-flavored
        completion phrases into every successful command's tail message.

        Use this when the user says "give me a knob about X" or
        "make my workspace feel more like Y". The LLM generates ~8
        themed messages; the user can later edit them via the literate
        config file or `org-llm knob remove/add`.

        `vibe` is a free-form description ("1980s neon, late-night
        coding"). `specifics` is a small dict of detail directives the
        user wants in EVERY message — font, icon, color, wording quirk,
        sound, image. Empty dict = LLM picks freely from the vibe.
        """
        await _info(ctx, f"add_theme_knob: {name} (vibe={vibe[:40]!r})")
        import shlex, subprocess
        argv = ["org-llm", "knob", "add", name, "--llm",
                 "--count", str(max(3, min(20, n_messages)))]
        if vibe:
            argv += ["--vibe", vibe]
        for k, v in (specifics or {}).items():
            argv += ["--specifics", f"{k}={v}"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=180)
        except FileNotFoundError:
            return "org-llm binary not on PATH inside the MCP server env."
        except subprocess.TimeoutExpired:
            return "knob generation timed out after 180s."
        out = (proc.stdout or "")
        if proc.stderr:
            out += "\n[stderr]\n" + proc.stderr
        if len(out) > 8000:
            out = out[:8000] + "\n…(truncated)"
        return _themed("add_theme_knob",
                        f"{name} (exit {proc.returncode})", out)

    # ── Live context refresh ──────────────────────────────────────────────────
    @server.tool()
    def refresh_context() -> str:
        """Re-fetch vault stats, recent activity, top tags, and active
        config. Use mid-session when the user mentions running a CLI
        command outside opencode (index, embed, config change) — the
        system prompt was snapshotted at launch and doesn't auto-refresh.

        Returns a compact summary the LLM should integrate into its
        mental model for the rest of the session."""
        from .db import File, Node, Config, merged_tags
        from datetime import datetime, timedelta
        from collections import Counter
        with get_session(engine) as session:
            n_files    = session.query(File).count()
            n_nodes    = session.query(Node).count()
            n_embedded = session.query(Node).filter(
                Node.embedding.isnot(None)).count()
            since = (datetime.now() - timedelta(days=7)).timestamp()
            recent_titles = [n.title for n in session.query(Node)
                              .filter(Node.mtime >= since)
                              .order_by(Node.mtime.desc())
                              .limit(8).all() if n.title]
            tag_counts: Counter = Counter()
            for tags, auto in session.query(Node.tags, Node.auto_tags).all():
                for t in ((tags or "") + " " + (auto or "")).split():
                    if t and len(t) > 2:
                        tag_counts[t.lower()] += 1
            top_tags = tag_counts.most_common(8)
            cfg_chat = _cfg(session, "chat_model")
            cfg_provider = _cfg(session, "cloud_provider") or "ollama (local)"
        body = (
            f"Vault now: {n_files} files / {n_nodes} nodes / "
            f"{n_embedded} embedded\n"
            f"Recent (7d): {', '.join(recent_titles[:6]) or '(none)'}\n"
            f"Top tags: {', '.join(f'{t}({n})' for t, n in top_tags)}\n"
            f"Chat model: {cfg_chat}  |  Provider: {cfg_provider}"
        )
        return _themed("refresh_context",
                        f"snapshot at {datetime.now().strftime('%H:%M:%S')}",
                        body)

    # ── walk + teach (Phase 13) ───────────────────────────────────────────
    @server.tool()
    def walk_pick_targets(track: str = "notes",
                            window_days: int = 7,
                            k: int = 5) -> str:
        """Suggest 1-N notes worth walking through with the user — the
        in-opencode LLM should call this to drive the `walk` workflow
        from inside the workspace conversation. Use when the user
        says things like 'help me review my recent notes', 'walk me
        through my vault', 'what should I think about today?'.

        Returns one node per line with id, title, and selection
        reason. The LLM picks one (or a few), then asks the user to
        explain it. After the user responds, call walk_extract_facts
        to mine atomic claims, then walk_save_facts to persist.

        Tracks: 'notes' (Phase 13.1 — implemented), 'dailies' /
        'projects' (planned)."""
        from . import walk as _walk
        with get_session(engine) as session:
            try:
                nodes = _walk.select_walk_nodes(
                    session, track=track,
                    window_days=max(1, window_days),
                    k=max(1, min(20, k)),
                )
            except ValueError as e:
                return f"unsupported track: {e}"
        if not nodes:
            return ("no walk-worthy nodes — try a wider window_days, "
                    "or ask the user to capture a few notes first")
        lines = [f"Selected {len(nodes)} note(s) for the walk:"]
        for i, n in enumerate(nodes, 1):
            lines.append(
                f"  {i}. id={n.node_id or '(none)'}  "
                f"title={n.title!r}  reason={n.reason}"
            )
            body_preview = n.body[:200].replace('\n', ' ')
            lines.append(f"     body: {body_preview}…"
                          if len(n.body) > 200 else
                          f"     body: {body_preview}")
        return "\n".join(lines)

    @server.tool()
    def walk_extract_facts(node_id: str, user_response: str,
                              note_title: str = "",
                              note_body: str = "") -> str:
        """Given a user's natural-language response about a specific
        note, extract atomic factual claims they MADE (not things
        they paraphrased from the note). Call this AFTER the user
        has explained what the note means in their current life.

        Returns the proposed FACTS + a SLUG (topic name). Show them
        to the user, get confirmation, then call walk_save_facts.

        node_id: the node's :ID: from walk_pick_targets output.
        note_title / note_body: helpful context (LLM picks them up
        from walk_pick_targets and threads them here)."""
        from . import walk as _walk
        # Build a minimal WalkNode from what the LLM passes — we're
        # not re-querying the DB because the LLM already has the
        # context from walk_pick_targets.
        node = _walk.WalkNode(
            node_id=node_id, title=note_title or "(unknown)",
            body=note_body or "", tags="", file_path="",
            mtime=0.0, score=1.0, reason="mcp",
        )
        with get_session(engine) as session:
            url = _cfg(session, "ollama_url") or "http://localhost:11434"
            mdl = (_cfg(session, "fast_model")
                    or _cfg(session, "chat_model") or "llama3.2:1b")
        facts, slug = _walk.extract_facts_from_response(
            node, user_response, model=mdl, base_url=url)
        if not facts:
            return ("NO_FACTS — user's response had nothing context-worthy "
                    "(restated the note, asked a question, or stayed "
                    "general).")
        out = [f"Extracted {len(facts)} fact(s) (suggested slug: {slug or 'general'}):"]
        for i, f in enumerate(facts, 1):
            out.append(f"  {i}. {f}")
        out.append("")
        out.append("Now confirm with the user, then call walk_save_facts(facts=[...], "
                    f"source='walk:{node_id[:8]}') to persist.")
        return "\n".join(out)

    @server.tool()
    def walk_save_facts(facts: list[str],
                          source: str = "walk:mcp",
                          user_approved: bool = False) -> str:
        """Persist user-approved facts to the LLM-context overlay.
        Each fact gets one line in the active-facts block; the LLM
        sees them in every future RAG call's system prompt.

        Phase 13.1.1: atomic-fact validator. Facts are rejected if
        they look like bundled multi-claim sentences (length > 180,
        contains 'history:' meta-prefix, or 3+ commas with named
        entities). Caller must split bundles into atoms and retry.

        Permissive UX (per 2026-04-29 design call): user_approved=True
        is the LLM's signal that the user has, in conversation,
        consented to saving. The LLM doesn't have to ask 'should I
        save?' if the user's response itself was substantive
        teaching. Mistakes are recoverable via walk_undo_last_save.

        Returns the EXACT saved-line text so the LLM can echo
        reality (not its own beautified summary) back to the user.
        """
        if not user_approved:
            shown = "\n".join(f"  - {f}" for f in facts[:8])
            return ("REFUSED: walk_save_facts requires user_approved=True. "
                    "When the user has clearly consented (substantive "
                    "teaching response or explicit yes), call again "
                    "with user_approved=True. Until then, surface these "
                    "to the user:\n\n" + shown)
        if not facts:
            return "NO_FACTS to save."

        # Atomic-fact validator. Reject obvious bundles so the LLM
        # is forced to split them. Phase 13.1 testing surfaced
        # examples like "Work history: data engineer at startup
        # Arkatechture, then manager of BI Platforms at Unum, now
        # senior data engineer at Idexx (2026)." — three claims
        # bundled into one line. Should be three atoms.
        rejected: list[tuple[str, str]] = []
        accepted: list[str] = []
        for f in facts:
            f = (f or "").strip()
            if not f:
                continue
            reasons: list[str] = []
            if len(f) > 180:
                reasons.append(f"too long ({len(f)} chars; cap 180)")
            low = f.lower()
            for prefix in ("work history:", "employment history:",
                            "history:", "summary:", "overview:"):
                if low.startswith(prefix):
                    reasons.append(f"meta-bundle prefix {prefix!r}")
                    break
            n_commas = f.count(",")
            n_thens  = low.count(" then ") + low.count(", then ")
            if n_commas >= 3 and n_thens >= 1:
                reasons.append(f"chain-bundle ({n_commas} commas + "
                                f"{n_thens} 'then')")
            if reasons:
                rejected.append((f, "; ".join(reasons)))
            else:
                accepted.append(f)

        if rejected and not accepted:
            lines = ["REJECTED — these look bundled, split into atoms:"]
            for f, why in rejected:
                lines.append(f"  - {f!r}: {why}")
            lines.append("")
            lines.append("Each fact should be one atomic claim "
                          "(one role, one project, one preference). "
                          "Re-call walk_save_facts with the split atoms.")
            return "\n".join(lines)

        from . import context as _ctx
        saved_lines: list[str] = []
        for f in accepted:
            try:
                _ctx.add_fact(f, source=source)
                saved_lines.append(f)
            except Exception as e:
                return (f"saved {len(saved_lines)}/{len(accepted)} "
                        f"before error: {type(e).__name__}: {e}")

        # Push to undo stack for walk_undo_last_save
        try:
            _ctx.push_walk_undo(saved_lines)
        except Exception:
            pass

        out = [f"Saved {len(saved_lines)} fact(s) to context. "
                f"EXACT lines that landed (echo these back to the "
                f"user, do not paraphrase):"]
        for line in saved_lines:
            out.append(f"  - {line}")
        if rejected:
            out.append("")
            out.append(f"REJECTED {len(rejected)} bundled fact(s) "
                        f"— split and re-call:")
            for f, why in rejected:
                out.append(f"  - {f[:80]}{'…' if len(f) > 80 else ''}: {why}")
        out.append("")
        out.append("Undo with walk_undo_last_save(confirm=True) if needed.")
        return "\n".join(out)

    @server.tool()
    def walk_save_disambiguation(term: str,
                                    periods: dict,
                                    source: str = "walk:mcp",
                                    user_approved: bool = False) -> str:
        """Save a disambiguation entry — for ambiguous words/phrases
        that mean different things in different periods. Phase 13.1.1
        added this affordance after the in-opencode walk surfaced
        the gap (the user said 'tag for disambiguation' and the LLM
        had no tool to act on it).

        term: the ambiguous word, e.g. 'the team'
        periods: {<period clause>: <meaning>, ...}, e.g.
                  {'before 2026': 'Unum BI Platforms team',
                   'after 2026':  'Idexx team'}

        Same user_approved=True permissive contract as
        walk_save_facts. Lands in the * Disambiguations section of
        llm-context.org and is pulled into the LLM's system prompt
        alongside the active-facts block.
        """
        if not user_approved:
            return ("REFUSED: walk_save_disambiguation requires "
                    "user_approved=True. Surface this to the user "
                    f"first:\n  term: {term!r}\n  periods: {periods!r}")
        if not term or not term.strip():
            return "REFUSED: empty term."
        if not periods or not isinstance(periods, dict):
            return "REFUSED: periods must be a non-empty dict {clause: meaning}."

        from . import context as _ctx
        try:
            _ctx.add_disambiguation(term, dict(periods),
                                       source=source)
        except Exception as e:
            return f"save failed: {type(e).__name__}: {e}"

        lines = [f"Saved disambiguation for {term!r}. "
                  f"Future RAG calls see:"]
        for period, meaning in periods.items():
            lines.append(f"  - {period} = {meaning}")
        return "\n".join(lines)

    @server.tool()
    def walk_remove_fact(line: str, user_approved: bool = False) -> str:
        """Surgically remove ONE specific fact line from the
        active-facts block. Use when the user names a fact (or
        you've quoted one back to them) and they want it gone.

        `line` is the exact fact text WITHOUT the leading '- '.
        Match is case-sensitive — quote the line as it appears in
        the file. If you're not sure, call get_context first to see
        the exact text.

        Different from walk_undo_last_save — that's a LIFO pop of
        the most recent SAVE. This is targeted removal of any
        single fact, regardless of when it was saved.

        Use case: a bundled fact ('Work history: A then B then C')
        got saved earlier in the session. The user has since split
        it into atoms, and now wants the original bundle removed.
        walk_undo_last_save can't reach it because newer saves are
        on top of the stack. walk_remove_fact targets it directly.

        Requires user_approved=True (same permissive contract — the
        in-workspace LLM can pass approved=True when the user has
        clearly said which fact to drop).
        """
        if not user_approved:
            return ("REFUSED: walk_remove_fact requires user_approved=True. "
                    f"Confirm the user wants to remove this fact:\n  "
                    f"{line!r}")
        if not line or not line.strip():
            return "REFUSED: empty line."
        from . import context as _ctx
        ok = _ctx.remove_fact(line.strip(),
                                source="walk-remove:mcp")
        if ok:
            return (f"Removed: {line.strip()!r}\n"
                    f"Audit-trail entry written to history block.")
        return (f"NOT_FOUND: no fact in active-facts matches "
                f"{line.strip()!r}. The match is case-sensitive — call "
                f"get_context to see the exact text, then retry.")

    @server.tool()
    def walk_undo_last_save(confirm: bool = False) -> str:
        """Pop the most recent walk_save_facts batch and remove
        those exact lines from the active-facts block. Use when the
        user says 'undo that' / 'remove those' / 'I changed my
        mind'. Refuses without confirm=True (small safety to avoid
        accidental triggering on conversational mention of 'undo').

        Reads from ~/.local/share/org-llm/walk-undo-stack.json which
        walk_save_facts populates after each successful batch."""
        if not confirm:
            return ("REFUSED: walk_undo_last_save requires confirm=True. "
                    "Confirm the user actually wants to undo the most "
                    "recent save before retrying.")
        from . import context as _ctx
        removed = _ctx.pop_walk_undo()
        if not removed:
            return ("Nothing to undo — no recent walk_save_facts batch "
                    "in the undo stack.")
        out = [f"Undone — removed {len(removed)} fact(s) from "
                f"active-facts:"]
        for line in removed:
            out.append(f"  - {line}")
        return "\n".join(out)

    @server.tool()
    def insight_card_feedback(
        card_kind:        str,
        card_title:       str,
        reaction:         str,
        reason:           str = "",
        narration_model:  str = "deterministic",
        suggested_cmd:    str = "",
    ) -> str:
        """Record user feedback on a Phase 12 insight card.

        `reaction`: 'good' (helpful), 'bad' (useless / wrong), or
        'clicked' (the user acted on the card's suggestion). Empty
        string is also accepted for "shown but no signal yet".

        Use when the user reacts to a card: 'that one was useful',
        'the stale-candidates one was wrong', 'tell me more about
        the orphan one' (counts as 'clicked'). The in-workspace LLM
        should call this naturally as part of card-discussion turns;
        no explicit user_approved gate (it's append-only telemetry,
        not destructive).

        Phase 12.5 substrate. `doctor --diagnose-cards` reads from
        this table to surface card-quality patterns over time.
        """
        valid_reactions = {"", "good", "bad", "clicked"}
        if reaction not in valid_reactions:
            return (f"REFUSED: reaction must be one of "
                    f"{sorted(valid_reactions)}, got {reaction!r}.")
        if not card_kind or not card_kind.strip():
            return "REFUSED: card_kind required."
        from .db import InsightEngagement
        import time as _t
        with get_session(engine) as session:
            session.add(InsightEngagement(
                shown_at=int(_t.time()),
                card_kind=card_kind.strip(),
                card_title=card_title or "",
                card_body="",                # body not always available at feedback time
                evidence_json="",
                reaction=reaction,
                reason=reason.strip() or None,
                suggested_cmd=suggested_cmd or None,
                narration_model=narration_model or "deterministic",
            ))
            session.commit()
        return (f"Recorded {reaction!r} on card "
                f"({card_kind}: {card_title[:60]!r}). "
                f"Aggregated patterns surface via "
                f"`org-llm doctor --diagnose-cards`.")

    @server.tool()
    def walk_review_pending(max_facts: int = 5) -> str:
        """List previously-saved context facts that are due for
        re-review (oldest first). Use when the user says 'are my
        notes still current?' / 'review my context' / 'what facts
        am I assuming?'. After listing, ask the user about each
        one — they confirm, update, or supersede. Use
        walk_save_facts(user_approved=True) to write any updates.
        """
        from . import walk as _walk
        cards = _walk.select_review_facts(max_facts=max(1, min(20, max_facts)))
        if not cards:
            return ("no saved context facts to review yet — user has "
                    "not run `walk` or `context add`.")
        out = [f"Reviewing {len(cards)} context fact(s), oldest first:"]
        for i, c in enumerate(cards, 1):
            out.append(f"  {i}. \"{c.line}\"  "
                        f"[added {c.history_ts or '?'}, "
                        f"source: {c.source}]")
        out.append("")
        out.append("Ask the user about each: 'still true?' If yes → "
                    "no-op. If updated → call walk_save_facts with the "
                    "new wording. If superseded → save the new truth, "
                    "noting which old line it replaces.")
        return "\n".join(out)

    # ── org_tools wrappers (Phase 22.6 — general agent tooling) ───────────────

    @server.tool()
    def org_grep(pattern: str, path_glob: str = "**/*.org",
                  max_results: int = 50,
                  ignore_case: bool = True) -> str:
        """Fast text search across the vault. Returns up to
        `max_results` hits as `path:line: text`. Uses ripgrep when
        available, python re fallback otherwise. `pattern` is a
        regex. Use this for grep-style 'where do I mention X?' —
        cheaper and more direct than search_notes for literal text."""
        from . import org_tools as _ot
        hits = _ot.org_grep(pattern, path_glob=path_glob,
                              max_results=max_results,
                              ignore_case=ignore_case)
        if not hits:
            return _themed("org_grep",
                            f"no hits for {pattern!r}")
        body = "\n".join(f"{h['file']}:{h['line']}: {h['text']}"
                          for h in hits)
        return _themed("org_grep",
                        f"{len(hits)} hit(s) for {pattern!r}", body)

    @server.tool()
    def org_count_matches(pattern: str, path_glob: str = "**/*.org",
                            ignore_case: bool = True) -> str:
        """Count regex matches across the vault. Returns total hits,
        files-with-hits, and the top-10 files by hit count.
        Use for questions like 'how many times have I checked off
        making the bed?' — pattern=r'\\[X\\].*make bed' yields a
        number without per-file inspection."""
        from . import org_tools as _ot
        r = _ot.org_count_matches(pattern, path_glob=path_glob,
                                    ignore_case=ignore_case)
        if r.get("error"):
            return _themed("org_count_matches",
                            f"bad regex: {r['error']}")
        body_lines = [f"total_hits      = {r['total_hits']}",
                       f"files_with_hits = {r['files_with_hits']}"]
        if r["top_files"]:
            body_lines.append("top_files:")
            for path, n in r["top_files"]:
                body_lines.append(f"  {n:>4}  {path}")
        return _themed("org_count_matches",
                        f"counted {pattern!r}", "\n".join(body_lines))

    @server.tool()
    def org_file_meta(path: str) -> str:
        """Return file-level metadata for an org file: title, ID,
        file-tags, inline tags, heading counts per level, todo /
        done counts, checkbox counts, roam-link count, mtime, size.
        Path is resolved under org_dir; refuses paths outside."""
        from . import org_tools as _ot
        m = _ot.org_file_meta(path)
        if not m:
            return _themed("org_file_meta",
                            f"refused or missing: {path}")
        body = "\n".join(f"{k:<14} {v}" for k, v in m.items())
        return _themed("org_file_meta", m.get("title", path), body)

    @server.tool()
    def org_outline(path: str, max_depth: int = 99) -> str:
        """Return the heading tree of `path` as a flat indented
        outline with line numbers. Use to pick a sub-tree to read
        without reading the whole file."""
        from . import org_tools as _ot
        nodes = _ot.org_outline(path, max_depth=max_depth)
        if not nodes:
            return _themed("org_outline",
                            f"no headings or refused: {path}")
        lines = []
        for n in nodes:
            indent = "  " * (n["level"] - 1)
            tags = (" :" + ":".join(n["tags"]) + ":") if n["tags"] else ""
            lines.append(f"{n['line']:>5}  {indent}* {n['text']}{tags}")
        return _themed("org_outline",
                        f"{len(nodes)} heading(s) in {path}",
                        "\n".join(lines))

    @server.tool()
    def org_tag_index(limit: int = 50) -> str:
        """Return the vault's tag taxonomy as `(tag, count)` rows
        sorted by frequency. Use BEFORE inventing new tags — pick
        from the user's existing taxonomy. Aggregates :inline:
        tags + #+filetags."""
        from . import org_tools as _ot
        rows = _ot.org_tag_index(limit=limit)
        if not rows:
            return _themed("org_tag_index", "no tags found")
        body = "\n".join(f"  {n:>4}  {tag}" for tag, n in rows)
        return _themed("org_tag_index",
                        f"top {len(rows)} tag(s)", body)

    @server.tool()
    def org_tag_suggest(text: str, top_n: int = 5) -> str:
        """Suggest up to `top_n` tags for arbitrary `text`, drawn
        ONLY from tags that already exist in the vault. Use right
        before capture so tags fit the user's taxonomy."""
        from . import org_tools as _ot
        tags = _ot.org_tag_suggest(text, top_n=top_n)
        if not tags:
            return _themed("org_tag_suggest",
                            "no existing tags fit this text")
        return _themed("org_tag_suggest",
                        f"{len(tags)} suggestion(s)",
                        " ".join(f":{t}:" for t in tags))

    @server.tool()
    def org_agenda(window_days: int = 7) -> str:
        """Build a window agenda from the vault: today, upcoming
        (next `window_days`), overdue, and stale_todo (>30d, no
        date). Pure scan, no LLM. Use when the user asks 'what's
        on deck?' / 'overdue?' / 'agenda'."""
        from . import org_tools as _ot
        a = _ot.org_agenda(window_days=window_days)
        sections = []
        for bucket in ("today", "upcoming", "overdue", "stale_todo"):
            items = a.get(bucket) or []
            if not items:
                continue
            sections.append(f"{bucket.upper()} ({len(items)}):")
            for it in items[:25]:
                date = it.get("scheduled") or it.get("deadline") or "—"
                sections.append(f"  [{it['state']}] {date}  "
                                  f"{it['text']}  ({it['file']}:{it['line']})")
        if not sections:
            return _themed("org_agenda",
                            f"clean — no items in {window_days}d window")
        return _themed("org_agenda",
                        f"agenda window={window_days}d",
                        "\n".join(sections))

    # ── Phase 22.6.1: roam graph + property + refile tools ────────────────────

    @server.tool()
    def org_link_graph(node_id: str, hops: int = 2,
                        max_nodes: int = 100) -> str:
        """Walk roam `[[id:…]]` links from `node_id` up to `hops`
        steps. Returns nodes (id, title, file, depth) + edges.
        Use to answer 'what is this node connected to?' without
        re-reading every file."""
        from . import org_tools as _ot
        g = _ot.org_link_graph(node_id, hops=hops, max_nodes=max_nodes)
        if g.get("error"):
            return _themed("org_link_graph",
                            f"{node_id}: {g['error']}")
        nodes  = g.get("nodes", [])
        edges  = g.get("edges", [])
        head   = (f"{len(nodes)} node(s), {len(edges)} edge(s) "
                   f"within {hops} hop(s) of {node_id}")
        if g.get("truncated"):
            head += " (truncated at max_nodes)"
        body = []
        for n in nodes:
            indent = "  " * n.get("depth", 0)
            body.append(f"{indent}- [{n.get('depth', 0)}] "
                          f"{n.get('title', '')}  "
                          f"({n.get('id', '')[:8]} · {n.get('file', '')})")
        return _themed("org_link_graph", head, "\n".join(body))

    @server.tool()
    def org_backlinks(target: str) -> str:
        """List incoming roam links pointing at `target` (an ID or
        a file path). Each row: from_file:line — anchor text. Use
        when answering 'what links HERE?'"""
        from . import org_tools as _ot
        rows = _ot.org_backlinks(target)
        if not rows:
            return _themed("org_backlinks",
                            f"no backlinks found for {target}")
        body = "\n".join(
            f"  {r['from_file']}:{r['line']}  "
            f"[{r.get('from_title', '')}]  "
            f"→ {r.get('anchor_text', '') or '(unanchored)'}"
            for r in rows
        )
        return _themed("org_backlinks",
                        f"{len(rows)} backlink(s) to {target}", body)

    @server.tool()
    def org_orphans(max_results: int = 50) -> str:
        """Files with NO incoming/outgoing roam links AND no tags.
        Triage candidates for prune / refile / tag. Each row:
        title — file (bytes, mtime, headings)."""
        from . import org_tools as _ot
        from datetime import datetime as _dt
        rows = _ot.org_orphans(max_results=max_results)
        if not rows:
            return _themed("org_orphans",
                            "no orphans — vault is well-linked / well-tagged")
        body_lines = []
        for r in rows:
            mt = _dt.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d")
            body_lines.append(
                f"  {r['title'][:40]:<40}  "
                f"{r['bytes']:>6}b  {mt}  "
                f"{r['headings_total']}h  ({r['file']})"
            )
        return _themed("org_orphans",
                        f"{len(rows)} orphan(s)",
                        "\n".join(body_lines))

    @server.tool()
    def org_property_search(prop: str, value: str = "",
                              path_glob: str = "**/*.org",
                              max_results: int = 50) -> str:
        """Find headings whose :PROPERTIES: drawer contains `prop`
        (case-insensitive). When `value` is non-empty also requires
        the value to match (substring, case-insensitive). Returns
        rows of file:line — title  :prop: value."""
        from . import org_tools as _ot
        rows = _ot.org_property_search(prop, value=(value or None),
                                          path_glob=path_glob,
                                          max_results=max_results)
        if not rows:
            return _themed("org_property_search",
                            f"no headings with :{prop}:"
                            + (f" = {value!r}" if value else ""))
        body = "\n".join(
            f"  {r['file']}:{r['line']}  {r['title'][:50]}  "
            f":{r['prop']}: {r['value']}"
            for r in rows
        )
        return _themed("org_property_search",
                        f"{len(rows)} hit(s) for :{prop}:", body)

    @server.tool()
    def org_id_find(query: str, top_n: int = 10) -> str:
        """Fuzzy-find node IDs by partial title match. Cheaper than
        search_notes when you just want the ID for a known title.
        Use BEFORE org_link_graph / org_backlinks when you have a
        title but not an ID."""
        from . import org_tools as _ot
        rows = _ot.org_id_find(query, top_n=top_n)
        if not rows:
            return _themed("org_id_find",
                            f"no titles matched {query!r}")
        body = "\n".join(
            f"  [{r['score']}] {r['kind']:<7}  {r['id']}  "
            f"{r['title']}  ({r['file']})"
            for r in rows
        )
        return _themed("org_id_find",
                        f"{len(rows)} match(es) for {query!r}", body)

    @server.tool()
    def battery_status() -> str:
        """Return rich battery state: percent, charging/discharging,
        cycle count, current vs design capacity (% health), time
        to empty / full, instantaneous draw, and a panic flag
        (≤5% AND discharging). Use to reason about whether to
        offer a heavy operation now or defer."""
        from . import life_support as _ls
        bd = _ls.battery_details()
        if not bd.get("present"):
            return _themed("battery_status",
                            "no battery (desktop / VM)")
        rows = [
            f"  status:           {bd.get('status', '?')} "
            f"({'plugged' if bd.get('plugged') else 'on battery'})",
            f"  percent:          {bd.get('percent', '?')}%",
        ]
        if bd.get("time_to_empty_min") is not None:
            rows.append(f"  time_to_empty:    "
                          f"{bd['time_to_empty_min']}m")
        if bd.get("time_to_full_min") is not None:
            rows.append(f"  time_to_full:     "
                          f"{bd['time_to_full_min']}m")
        if bd.get("power_now_w") is not None:
            rows.append(f"  power_now:        "
                          f"{bd['power_now_w']}W")
        if bd.get("cycle_count") is not None:
            rows.append(f"  cycle_count:      "
                          f"{bd['cycle_count']} (ageing proxy)")
        if bd.get("energy_full_pct") is not None:
            rows.append(f"  capacity_health:  "
                          f"{bd['energy_full_pct']}% of design  "
                          f"[{bd.get('health', '?')}]")
        rows.append(f"  power_profile:    {_ls.power_profile()}")
        if bd.get("panic"):
            rows.append("  ⚠ PANIC mode — ≤5% and discharging. "
                          "Save work; abort heavy ops.")
        return _themed("battery_status",
                        "battery + power state",
                        "\n".join(rows))

    @server.tool()
    def weather_forecast(days: int = 7, force: bool = False) -> str:
        """Return the next `days` days of weather for the user's
        configured location. Uses open-meteo (FOSS, no key).
        Cached at ~/.cache/org-llm/weather.json. Configure with
        `org-llm config location_lat <N>` and
        `org-llm config location_lon <N>`."""
        from . import weather as _w
        f = _w.get_forecast(days=days, force=force)
        if f.get("error"):
            return _themed("weather_forecast", "weather unavailable",
                            f.get("error"))
        body = []
        if f.get("stale"):
            body.append("[stale — served from cache; fetch failed]")
        for d in f.get("daily") or []:
            body.append(
                f"  {d.get('date')}  {d.get('short','?'):<14s}  "
                f"{d.get('t_min','?')}-{d.get('t_max','?')}°C  "
                f"precip {d.get('precip_prob',0)}%  "
                f"wind {d.get('wind_max','?'):.0f}km/h"
                if isinstance(d.get('wind_max'), (int, float))
                else
                f"  {d.get('date')}  {d.get('short','?'):<14s}  "
                f"{d.get('t_min','?')}-{d.get('t_max','?')}°C  "
                f"precip {d.get('precip_prob',0)}%"
            )
        return _themed("weather_forecast",
                        f"forecast for ({f.get('lat')}, {f.get('lon')})",
                        "\n".join(body) or "(no daily data)")

    @server.tool()
    def weather_for_agenda(days: int = 7) -> str:
        """Cross-reference the next `days` of weather against the
        user's org-agenda. Flags outdoor-flavoured agenda items
        (hike, bbq, garden, mow, walk, etc) whose forecast
        crosses concerning thresholds (≥50% precip, sustained
        wind >35km/h, gusts >50, freezing or heat extremes, high
        UV). Output: forecast summary + flagged items + agenda
        counts. Use BEFORE making any "should I reschedule X"
        recommendations."""
        from . import weather as _w
        b = _w.weather_for_agenda(days=days)
        if b.get("error"):
            return _themed("weather_for_agenda",
                            "weather unavailable", b.get("error"))
        out = []
        if b.get("summary"):
            out.append(f"summary: {b['summary']}")
        flagged = b.get("outdoor_items") or []
        if flagged:
            out.append(f"outdoor_items_with_concerns ({len(flagged)}):")
            for it in flagged[:15]:
                concerns = ", ".join(it.get("concerns") or []) or "fine"
                out.append(f"  {it.get('date')}  "
                            f"[{it.get('state','?')}] "
                            f"{(it.get('item','') or '')[:60]}  "
                            f"→ {concerns}")
        else:
            out.append("outdoor_items: none flagged this window.")
        return _themed("weather_for_agenda",
                        "weather × agenda",
                        "\n".join(out))

    @server.tool()
    def weather_tag_suggest(window_days: int = 14,
                              max_results: int = 50) -> str:
        """Scan agenda items for headings whose text suggests an
        outdoor / weather-sensitive activity but lacks an explicit
        weather-constraint tag (=:cant-rain:= / =:cant-snow:= /
        =:cant-wind:= / =:cant-hot:= / =:cant-cold:= /
        =:needs-sun:= / =:weather-sensitive:=). Surface for the
        user — never auto-apply.

        Part of the cross-agent 'scan + suggest' pattern: every
        agent has a domain-specific hygiene scan that proposes
        structural improvements without writing them. The agent
        confirms with the user before applying."""
        from . import weather as _w
        rows = _w.weather_tag_suggest(window_days=window_days,
                                         max_results=max_results)
        if not rows:
            return _themed("weather_tag_suggest",
                            "all weather-sensitive items already tagged")
        body = []
        for r in rows:
            body.append(
                f"  {r.get('file')}:{r.get('line')}  "
                f"{(r.get('heading','') or '')[:50]}  "
                f"→ suggest "
                + " ".join(f":{t}:" for t in r.get("suggested_tags") or [])
                + f"  ({r.get('reason','')})"
            )
        return _themed("weather_tag_suggest",
                        f"{len(rows)} suggestion(s)",
                        "\n".join(body))

    @server.tool()
    def vault_profile(force: bool = False) -> str:
        """Phase 21 — return a compact human-readable digest of
        the user's vault from all registered inferrers. Use this
        as a startup primer ("who is this user, how do they
        organise their vault?") — one call gets enough context to
        answer most "what does Daniel typically do" questions
        without re-scanning files. Equivalent to calling
        vault_facts() with the synthesised summary view."""
        from . import vault_facts as _vf
        return _themed("vault_profile",
                        "user vault profile",
                        _vf.vault_profile_digest(force=force))

    @server.tool()
    def vault_facts(name: str = "", force: bool = False) -> str:
        """Phase 21 — read DB-backed deterministic facts about the
        user's vault. Pass `name` to fetch one fact (e.g.
        =vault_stats=, =tag_taxonomy=, =routine_chores=); pass empty
        string to get a roll-up of all registered inferrers. Set
        `force=True` to bypass the mtime / TTL cache and recompute.

        These are *facts*, not summaries — agents should treat them
        as authoritative for the questions they answer (e.g. when
        the user asks 'what's my morning routine?', the
        =routine_chores= fact is the answer; do not re-derive)."""
        from . import vault_facts as _vf
        import json as _json
        if name:
            v = _vf.get_fact(name, force=force)
            if v is None:
                avail = ", ".join(r["name"] for r in _vf.list_inferrers())
                return _themed("vault_facts",
                                f"unknown fact {name!r}",
                                f"available: {avail}")
            return _themed("vault_facts",
                            f"fact: {name}",
                            _json.dumps(v, indent=2, default=str)[:2000])
        # Roll-up — list registered facts + their values.
        all_facts = _vf.get_all_facts(force=force)
        body = []
        for n, v in all_facts.items():
            body.append(f"=== {n} ===")
            body.append(_json.dumps(v, indent=2, default=str)[:1500])
            body.append("")
        return _themed("vault_facts",
                        f"{len(all_facts)} fact(s) cached",
                        "\n".join(body))

    @server.tool()
    def org_clock_summary(since_days: int = 7,
                            per_tag: bool = False) -> str:
        """Aggregate org CLOCK time across the vault. Returns total
        minutes + top-25 headings by minutes (and per-tag breakdown
        when per_tag=true). Use to answer 'where did my week go?'"""
        from . import org_tools as _ot
        r = _ot.org_clock_summary(since_days=since_days, per_tag=per_tag)
        total = r.get("total_minutes", 0)
        head_lines = []
        for row in r.get("by_heading", [])[:25]:
            mins = row.get("minutes", 0)
            head_lines.append(f"  {mins//60:>2}h {mins%60:02d}m  "
                                f"{row.get('heading','')[:50]}  "
                                f"({row.get('file','')}:{row.get('line','')})")
        body = "\n".join(head_lines) if head_lines else "(no clocked time)"
        if per_tag and r.get("by_tag"):
            body += "\n\nby tag:\n"
            for tag, mins in r["by_tag"].items():
                body += f"  {mins//60:>2}h {mins%60:02d}m  #{tag}\n"
        return _themed("org_clock_summary",
                        f"{total//60}h {total%60:02d}m clocked "
                        f"in last {since_days}d", body)

    @server.tool()
    def org_drill_review_due(max_results: int = 50) -> str:
        """Find org-drill / org-fc cards whose next review is overdue.
        Sorted most-overdue first. Each row: overdue days + heading
        title + file:line."""
        from . import org_tools as _ot
        rows = _ot.org_drill_review_due(max_results=max_results)
        if not rows:
            return _themed("org_drill_review_due",
                            "no overdue cards — review queue empty")
        body = "\n".join(
            f"  {r['overdue_days']:>4}d  "
            f"{r['title'][:50]}  ({r['file']}:{r['line']})"
            for r in rows
        )
        return _themed("org_drill_review_due",
                        f"{len(rows)} card(s) overdue", body)

    @server.tool()
    def org_attach_list(node_id_or_path: str) -> str:
        """List attachments belonging to a node. Resolves the
        node's attach directory via the standard org-attach layout
        (.attach/<id[:2]>/<id[2:]>). Each row: name, size, mtime."""
        from . import org_tools as _ot
        from datetime import datetime as _dt
        rows = _ot.org_attach_list(node_id_or_path)
        if not rows:
            return _themed("org_attach_list",
                            f"no attachments for {node_id_or_path}")
        body_lines = []
        for r in rows:
            mt = _dt.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d")
            body_lines.append(f"  {r['size']:>8}b  {mt}  {r['name']}")
        return _themed("org_attach_list",
                        f"{len(rows)} attachment(s)",
                        "\n".join(body_lines))

    @server.tool()
    def org_template_apply(name: str, initial: str = "",
                              link: str = "") -> str:
        """Render a saved capture template (registered in
        db.Config('capture_templates')) by name. Substitutes
        %t/%T/%u/%U/%i/%a; leaves %? and %^{prompt} for the caller."""
        from . import org_tools as _ot
        body = _ot.org_template_apply(name, initial=initial, link=link)
        if not body:
            return _themed("org_template_apply",
                            f"no template named {name!r}")
        return _themed("org_template_apply",
                        f"rendered template {name!r}", body)

    @server.tool()
    def doom_packages() -> str:
        """List packages declared in ~/.doom.d/packages.el (or
        ~/.config/doom/packages.el). Each row: name + disabled flag
        + source line. Use BEFORE suggesting a workflow that depends
        on a specific package — verify the user actually has it."""
        from . import org_tools as _ot
        rows = _ot.doom_packages()
        if not rows:
            return _themed("doom_packages",
                            "no packages.el found or no (package! …) entries")
        body = "\n".join(
            f"  {'✗' if r['disabled'] else '✓'}  {r['name']:<30}  "
            f"{r['source_line'][:80]}"
            for r in rows
        )
        return _themed("doom_packages",
                        f"{len(rows)} package(s) declared", body)

    @server.tool()
    def doom_keybinds() -> str:
        """Parse the user's Doom config.el / bindings.el for
        leader-key bindings. Each row: key + desc + command + scope.
        Use BEFORE telling the user to press a specific keybind —
        verify it exists in their config."""
        from . import org_tools as _ot
        rows = _ot.doom_keybinds()
        if not rows:
            return _themed("doom_keybinds",
                            "no bindings parsed — file missing or no (map! …)")
        body = "\n".join(
            f"  [{r['scope'][:6]:<6}] {r['key']:<20}  "
            f"→ {r['command'][:50]}  "
            f"{('— ' + r['desc']) if r['desc'] else ''}"
            for r in rows[:60]
        )
        suffix = (f" (showing first 60 of {len(rows)})"
                   if len(rows) > 60 else "")
        return _themed("doom_keybinds",
                        f"{len(rows)} binding(s){suffix}", body)

    @server.tool()
    def org_refile_candidates(heading_text: str,
                                heading_tags: str = "",
                                top_n: int = 5) -> str:
        """Propose refile destinations for `heading_text`.
        `heading_tags` is a colon- or space-separated tag list
        (e.g. ':work:postgres:' or 'work postgres'). Scores files
        by tag overlap (×3) + title/filename token overlap (×1)."""
        from . import org_tools as _ot
        import re as _re_mod
        tags = [t for t in _re_mod.split(r"[\s:]+", heading_tags or "") if t]
        rows = _ot.org_refile_candidates(heading_text,
                                            heading_tags=tags,
                                            top_n=top_n)
        if not rows:
            return _themed("org_refile_candidates",
                            f"no refile candidates for {heading_text!r}")
        body_lines = []
        for r in rows:
            body_lines.append(f"  [{r['score']}] {r['title'][:50]}  "
                                f"({r['file']})")
            for reason in r.get("reasons", []):
                body_lines.append(f"        — {reason}")
        return _themed("org_refile_candidates",
                        f"{len(rows)} candidate(s)",
                        "\n".join(body_lines))

    return server


def main():
    server = create_mcp_server()
    server.run()
# mcp_server.py:1 ends here
