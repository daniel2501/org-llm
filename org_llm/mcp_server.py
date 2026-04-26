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
            return (
                f"**{node.title}**\n"
                f"File:     {node.file.path}\n"
                f"Tags:     {node.tags or 'none'}\n"
                f"ID:       {node.node_id or 'none'}\n"
                f"Modified: {modified}\n\n"
                f"{node.body}"
            )

    # ── list_nodes_by_tag ─────────────────────────────────────────────────────
    @server.tool()
    def list_nodes_by_tag(tag: str, limit: int = 30) -> str:
        """List all notes that contain a given tag."""
        from .db import File, Node
        with get_session(engine) as session:
            rows = (
                session.query(Node.title, Node.tags, File.path)
                .join(File, File.id == Node.file_id)
                .filter(Node.tags.ilike(f"%{tag}%"))
                .limit(limit).all()
            )
        if not rows:
            return f"No nodes tagged '{tag}'."
        return "\n".join(
            f"- {title}  [{tags}]  ({Path(path).name})"
            for title, tags, path in rows
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
            n_tagged   = session.query(Node).filter(
                Node.tags.isnot(None), Node.tags != ""
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
        """Run org-babel-tangle on an org file via emacsclient (Emacs must be running)."""
        import subprocess
        try:
            result = subprocess.run(
                ["emacsclient", "--eval", f'(org-babel-tangle-file "{file_path}")'],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return f"Tangled: {file_path}\n{result.stdout.strip()}"
            return f"Tangle failed: {result.stderr.strip()}"
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
    def get_config() -> str:
        """Return current org-llm configuration (model assignments, org_dir, etc.)."""
        from .db import Config
        with get_session(engine) as session:
            rows = session.query(Config).all()
        return "\n".join(f"  {r.key} = {r.value}" for r in rows)

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
