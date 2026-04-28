# [[file:../../../org/20260425230731-org_llm.org::*mcp_server.py][mcp_server.py:1]]
from __future__ import annotations
from pathlib import Path
import os

# Imported at module top so FastMCP can resolve `Context | None` annotations
# on tool functions via inspect.get_annotations(eval_str=True). Inner-scope
# imports inside create_mcp_server() leave the symbol unresolvable from the
# tool's __globals__.
from mcp.server.fastmcp import Context


def _make_engine():
    from .db import DB_PATH, make_engine
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key: str) -> str:
    from .db import Config
    row = session.get(Config, key)
    return row.value if row else ""


def _themed(tool: str, summary: str, body: str = "") -> str:
    """Wrap a tool's return string in the LCARS chat vocabulary so opencode
    output keeps the same visual rhythm as the CLI.

    Format:
        ◀ <tool> — <summary>
        ────────────────────────────────────────────────
        <body>

    Body is optional — short tool returns (e.g. capture_note returning a
    node ID) get just the header. Free-text returns from search/ask/etc.
    get the full sandwich. Clients without rich rendering still see plain
    text; clients with monospace blocks render the separator cleanly.
    """
    head = f"◀ {tool} — {summary}"
    if not body:
        return head
    sep = "─" * 60
    return f"{head}\n{sep}\n{body.rstrip()}"


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
            "the user's notes."
        ),
    )

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
                    qvec    = embed(query, model=model, base_url=url)
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
                qvec    = embed(question, model=embed_model, base_url=url)
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
        system = (
            "You are an assistant with access to a personal org-mode knowledge base. "
            "Answer using only the provided notes. Be concise. Cite note titles."
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
                qvec = _embed(query, model=model, base_url=url)
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

        Returns: a themed diagnosis + EXACT next-step command for the user.
        Doesn't auto-apply anything — the LLM presents the suggestion and
        the user decides."""
        await _info(ctx, "proactive_doctor: probing chat_model + Ollama + cloud")
        # Run the new --power-boost mode (read-only) for the model-fit signal.
        import subprocess
        try:
            proc = subprocess.run(
                ["org-llm", "doctor", "--power-boost"],
                capture_output=True, text=True, timeout=30,
            )
            boost_out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            boost_out = f"(power-boost probe failed: {e})"
        # Run a fast doctor check too — the existing --diagnose default
        # surfaces concrete issues when something's broken.
        try:
            proc = subprocess.run(
                ["org-llm", "doctor", "--no-diagnose"],
                capture_output=True, text=True, timeout=30,
            )
            doc_out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            doc_out = f"(doctor probe failed: {e})"
        # Combine — prefer the power-boost panel as the headline since
        # it directly addresses the most common cause of slowness.
        body = f"{boost_out.strip()}\n\n--- doctor snapshot ---\n{doc_out.strip()}"
        return _themed("proactive_doctor",
                        "model + ollama + cloud + vault probe", body)

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

    return server


def main():
    server = create_mcp_server()
    server.run()
# mcp_server.py:1 ends here
