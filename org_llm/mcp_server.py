# [[file:../../../org/20260425230731-org_llm.org::*mcp_server.py][mcp_server.py:1]]
from __future__ import annotations
from pathlib import Path
import asyncio
import inspect
import os

# Imported at module top so FastMCP can resolve `Context | None` annotations
# on tool functions via inspect.get_annotations(eval_str=True). Inner-scope
# imports inside create_mcp_server() leave the symbol unresolvable from the
# tool's __globals__.
from mcp.server.fastmcp import Context


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
    # JSON-RPC error and the LLM in opencode/claude/pi sees an opaque
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
            "  - `walk_pick_targets`     — get N candidate notes worth a\n"
            "                              walk (orphans / recent /\n"
            "                              untagged-substantive)\n"
            "  - For each one, present it + ASK the user about it\n"
            "  - `walk_extract_facts`    — mine atomic claims from the\n"
            "                              user's response (no auto-save)\n"
            "  - SHOW extracted facts to user, get explicit approval\n"
            "  - `walk_save_facts(user_approved=True)` — persist to the\n"
            "                              llm-context overlay (refuses\n"
            "                              without user_approved=True)\n"
            "  - `walk_review_pending`   — re-walk old facts; user\n"
            "                              confirms/updates/supersedes\n"
            "Saved facts flow into ask_notes' system prompt automatically\n"
            "— so once you've helped the user teach you something, future\n"
            "questions get the benefit. Periodically (every few sessions)\n"
            "OFFER to run walk_review_pending to keep context current."
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
    def capture_note(title: str, body: str, file: str = "inbox.org") -> str:
        """Capture a new note into the org vault. Returns the node ID for linking."""
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
    @server.tool()
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
    @server.tool()
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

    @server.tool()
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

    @server.tool()
    def open_url(url: str) -> str:
        """Open a URL in qutebrowser (or the user's default browser).

        Disabled by default; the user enables it with `org-llm grant-browser`.
        Only http(s) and file:// URLs are accepted; javascript:/data: refused.
        """
        from .access import open_url as _open
        ok, msg = _open(url)
        return msg

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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
        # detector — opencode/claude don't see stderr, so the ring
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
                    f"LAG ALERTS ({len(alerts)} recent — opencode/claude "
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
        baseline AND a faster pulled alternative exists. opencode /
        claude don't render stderr, so these events are otherwise
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

        Requires user_approved=True — the in-workspace LLM must
        explicitly confirm with the user before calling. Without
        the flag we refuse and surface the proposed facts for
        explicit approval (same pattern as proactive_doctor_apply).
        """
        if not user_approved:
            shown = "\n".join(f"  - {f}" for f in facts[:8])
            return ("REFUSED: walk_save_facts requires user_approved=True.\n"
                    "Show these facts to the user FIRST and get explicit "
                    "confirmation:\n\n" + shown +
                    "\n\nIf they confirm, call this tool again with "
                    "user_approved=True.")
        if not facts:
            return "NO_FACTS to save."
        from . import context as _ctx
        saved = 0
        for f in facts:
            f = (f or "").strip()
            if not f:
                continue
            try:
                _ctx.add_fact(f, source=source)
                saved += 1
            except Exception as e:
                return (f"saved {saved}/{len(facts)} before error: "
                        f"{type(e).__name__}: {e}")
        return (f"Saved {saved} fact(s) to context. They flow into "
                f"the system prompt on every future chat call. "
                f"Run `org-llm context show` to inspect.")

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

    return server


def main():
    server = create_mcp_server()
    server.run()
# mcp_server.py:1 ends here
