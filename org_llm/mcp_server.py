# [[file:../../../org/20260425230731-org_llm.org::*mcp_server.py][mcp_server.py:1]]
from __future__ import annotations
from pathlib import Path
import os


def _make_engine():
    from .db import DB_PATH, make_engine
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key: str) -> str:
    from .db import Config
    row = session.get(Config, key)
    return row.value if row else ""


def create_mcp_server():
    from mcp.server.fastmcp import FastMCP
    from .db import get_session

    engine = _make_engine()

    server = FastMCP(
        "org-llm",
        instructions=(
            "You are connected to the user's org-roam knowledge base via org-llm.\n"
            "Use these tools to search notes, answer questions, capture ideas,\n"
            "run org-babel skill workflows, and explore the knowledge graph.\n"
            "Always search before answering questions about the user's notes."
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
            return "No results found."
        return "\n\n---\n\n".join(
            f"**{r.title}**\nFile: {Path(r.file_path).name}\n"
            f"Tags: {r.tags or 'none'}\n\n{r.body[:600]}"
            for r in results
        )

    # ── ask_notes ─────────────────────────────────────────────────────────────
    @server.tool()
    def ask_notes(question: str, top_k: int = 6) -> str:
        """Answer a question using RAG over org notes. Grounds answer in the vault."""
        from .llm import embed, chat
        from .search import vector_search
        with get_session(engine) as session:
            url         = _cfg(session, "ollama_url") or "http://localhost:11434"
            embed_model = _cfg(session, "embed_model") or "nomic-embed-text"
            chat_model  = _cfg(session, "chat_model")  or "llama3.2"
            try:
                qvec    = embed(question, model=embed_model, base_url=url)
                results = vector_search(session, qvec, limit=top_k)
            except Exception as e:
                return f"Search error: {e}"
            if not results:
                return "No relevant notes found."
            ctx = "\n\n---\n\n".join(f"# {r.title}\n{r.body[:800]}" for r in results)
        system = (
            "You are an assistant with access to a personal org-mode knowledge base. "
            "Answer using only the provided notes. Be concise. Cite note titles."
        )
        try:
            return chat(
                f"Notes:\n\n{ctx}\n\n---\n\nQuestion: {question}",
                model=chat_model, base_url=url, system=system,
            )
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
        return f"Captured '{title}' → {org_file}\nID: {node_id}\nRun org-llm index to add to search."

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
                return f"No node found matching '{title}'. Try search_notes() to explore."
            modified = (
                datetime.fromtimestamp(node.mtime).isoformat(timespec="seconds")
                if node.mtime else "?"
            )
            from .db import merged_tags as _merged
            return (
                f"**{node.title}**\n"
                f"File:     {node.file.path}\n"
                f"Tags:     {_merged(node) or 'none'}\n"
                f"ID:       {node.node_id or 'none'}\n"
                f"Modified: {modified}\n\n"
                f"{node.body}"
            )

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
            return f"No nodes tagged '{tag}'."
        return "\n".join(
            f"- {n.title}  [{_merged(n)}]  ({Path(p).name})"
            for n, p in rows
        )

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
            return f"No nodes modified in the last {days} days."
        return "\n".join(
            f"- {n.title}  ({datetime.fromtimestamp(n.mtime).date().isoformat() if n.mtime else '?'})"
            for n in nodes
        )

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
        return (
            f"Vault: {org_dir}\n"
            f"  {n_files} files  |  {n_nodes} nodes\n"
            f"  {n_embedded}/{n_nodes} embedded ({pct_e}%)\n"
            f"  {n_tagged}/{n_nodes} tagged ({pct_t}%)"
        )

    # ── list_skills ───────────────────────────────────────────────────────────
    @server.tool()
    def list_skills() -> str:
        """List all registered org-llm skill workflows (org-babel :skill: blocks)."""
        from .skills import Skill
        with get_session(engine) as session:
            skills = session.query(Skill).all()
        if not skills:
            return "No skills registered. Add :skill: blocks to org files and run org-llm skill-index."
        return "\n".join(
            f"- {s.name}  (lang: {s.lang}, model: {s.model_key})"
            for s in skills
        )

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
    def org_llm_run(command_string: str, timeout: int = 60) -> str:
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
        stdout/stderr from the run, capped at 8000 chars. Use this
        when the user gives a vague intent and you want the CLI's
        auto-fix layer to figure out the exact argv.
        """
        import shlex, subprocess
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
        try:
            proc = subprocess.run(
                ["org-llm", *argv],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return "org-llm binary not on PATH inside the MCP server env."
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s."
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
        """Return current org-llm configuration (model assignments, org_dir, etc.)."""
        from .db import Config
        with get_session(engine) as session:
            rows = session.query(Config).all()
        return "\n".join(f"  {r.key} = {r.value}" for r in rows)

    # ── set_config (allow-listed safe keys) ───────────────────────────────────
    _SETTABLE_KEYS = {
        "chat_model", "embed_model", "code_model", "tag_model", "review_model",
        "ollama_url", "temperature", "top_p", "context_window",
        "code_dirs", "fixer_model", "trek_level", "commie_level", "queer_level",
    }

    @server.tool()
    def set_config(key: str, value: str) -> str:
        """Update an allow-listed config key (safe subset only).

        Allow-listed keys: chat_model, embed_model, code_model, tag_model,
        review_model, ollama_url, temperature, top_p, context_window,
        code_dirs, fixer_model, trek_level, commie_level, queer_level.

        Refuses keys outside this list — credentials, grants, telemetry, and
        secrets are NEVER writeable from MCP.
        """
        if key not in _SETTABLE_KEYS:
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
    def index_vault() -> str:
        """Re-scan the org vault and update the index incrementally."""
        from .indexer import index_directory
        with get_session(engine) as session:
            org_dir = _cfg(session, "org_dir")
            if not org_dir:
                return "org_dir not configured. Run: org-llm config org_dir <path>"
            try:
                files, nodes = index_directory(Path(org_dir).expanduser(), session)
                session.commit()
                return f"Indexed: {files} files, {nodes} nodes."
            except Exception as e:
                return f"Index failed: {e}"

    # ── embed_pending ─────────────────────────────────────────────────────────
    @server.tool()
    def embed_pending() -> str:
        """Generate embeddings for any unembedded nodes."""
        from .indexer import embed_nodes
        from .db import Node
        with get_session(engine) as session:
            url    = _cfg(session, "ollama_url") or "http://localhost:11434"
            model  = _cfg(session, "embed_model") or "nomic-embed-text"
            n_pending = session.query(Node).filter(
                Node.embedding.is_(None)).count()
            if n_pending == 0:
                return "Nothing to embed — all nodes already embedded."
            try:
                count = embed_nodes(session, model=model, base_url=url,
                                    force=False)
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

    return server


def main():
    server = create_mcp_server()
    server.run()
# mcp_server.py:1 ends here
