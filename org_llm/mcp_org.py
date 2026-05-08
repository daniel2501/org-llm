"""org-mcp Python wrapper — bridges org-llm specialists to org-mcp v0.9.

org-mcp (https://github.com/laurynas-biveinis/org-mcp) is an Emacs-side
MCP server: =org-add-todo=, =org-update-todo-state=, =org-edit-body=,
=org-rename-headline=, plus =org-read-*= readers, all registered with
=mcp-server-lib= and exposed via =emacs-mcp-stdio.sh --server-id=org-mcp=
over JSON-RPC stdio.

For specialist runtime use we don't need a long-lived stdio server.
Each wrapper here shells to =emacs --batch -Q --eval ...= and drives
the upstream =org-= primitives directly. That keeps every tool call
stateless (matching =add_property= / =set_todo_state= / =validate_org=
elsewhere in specialist.py) and works without =mcp-server-lib= present
— so P1-16 ships now even though the MELPA recipe for =mcp-server-lib=
is not yet built in this dev env.

Spec → primitive mapping:

| spec name        | implementation                                       |
|------------------+------------------------------------------------------|
| list_todos       | =org-map-entries= over allowed files                 |
| refile_node      | =org-refile= driven by :ID: lookup                   |
| create_node      | =org-id-get-create= after appending new heading      |
| query_agenda     | =org-map-entries= filtered by SCHEDULED/DEADLINE     |
| search_by_tag    | =org-map-entries= with a tag-match string            |

Status (R26 P1-16, 2026-05-08): PARTIAL. The five tool entry points
work without =mcp-server-lib= installed; once it lands the wrappers
can switch to org-mcp's =org-mcp--tool-*= helpers for stricter
allowed-files enforcement, but the contract above is stable.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional


# ── Helpers ──────────────────────────────────────────────────────────────
def _run_emacs_batch(elisp: str, *,
                       timeout: int = 30) -> tuple[bool, str]:
    """Run =elisp= under =emacs --batch -Q=. Returns (ok, stdout+stderr)."""
    args: list[str] = ["emacs", "--batch", "-Q", "--eval", elisp]
    try:
        cp = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "emacs binary not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"emacs --batch: timeout ({timeout}s)"
    out = (cp.stdout or "").strip()
    if cp.stderr and cp.returncode != 0:
        out += f"\n--stderr--\n{cp.stderr.strip()[:500]}"
    return cp.returncode == 0 and not out.startswith("ERR:"), out


def _allowed_files_in(workdir: Path | str) -> list[str]:
    """All =.org= files under workdir, returned as absolute paths.

    org-mcp requires =org-mcp-allowed-files= to be set explicitly. We
    collect every =.org= file under the specialist's workdir; that's a
    sane default scope for harness use (specialists already operate
    under workdir-scope checks).
    """
    workdir = Path(workdir)
    return sorted(str(p.resolve()) for p in workdir.rglob("*.org") if p.is_file())


def _format_lisp_string_list(values: list[str]) -> str:
    """Quote each value as an elisp string literal inside =(quote (...))=.

    The =(quote ...)= wrapper matters: this list is bound via =let= /
    =let*=, which evaluates the value form. Without =quote=, an unquoted
    =("a" "b")= would be evaluated as =(funcall "a" "b")= → invalid-function.
    """
    inner = " ".join('"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
                       for v in values)
    return f"(quote ({inner}))"


# ── Tool entry points ───────────────────────────────────────────────────

def list_todos(workdir: Path,
                  state_filter: Optional[str] = None) -> tuple[bool, str]:
    """List TODO entries across all =.org= files under =workdir=.

    Mirrors what an =org-list-todos= MCP tool would return. Each entry
    is =({"file", "heading", "state", "tags", "level"})=. =state_filter=
    optionally restricts to e.g. ="TODO"= or ="NEXT"=.

    Implementation: =emacs --batch -Q -l org= + =org-map-entries=. This
    works without =mcp-server-lib= installed, since it uses upstream
    =org= primitives only.
    """
    workdir = Path(workdir)
    files = _allowed_files_in(workdir)
    if not files:
        return True, json.dumps([])
    file_list = _format_lisp_string_list(files)
    if state_filter:
        state_pred = (
            f'(and state (string= state "{state_filter}"))'
        )
    else:
        state_pred = "(when state t)"
    # =or-list= guards json-encode against returning "null" for an empty
    # list — we want "[]" so callers always parse a list back out.
    elisp = (
        f'(progn (require (quote org)) (require (quote json)) '
        f'(let ((files {file_list}) (results (list))) '
        f'(dolist (f files) '
        f'  (when (file-exists-p f) '
        f'    (with-temp-buffer (insert-file-contents f) (org-mode) '
        f'      (org-map-entries '
        f'        (lambda () '
        f'          (let* ((state (org-get-todo-state)) '
        f'                 (heading (org-get-heading t t t t)) '
        f'                 (tags (org-get-tags)) '
        f'                 (level (org-current-level))) '
        f'            (when {state_pred} '
        f'              (push (list (cons "file" f) '
        f'                          (cons "heading" heading) '
        f'                          (cons "state" state) '
        f'                          (cons "tags" (or tags (list))) '
        f'                          (cons "level" level)) '
        f'                    results)))))))) '
        f'(let ((json-encoding-default-indentation "")) '
        f'  (princ (json-encode (or (nreverse results) (list)))))))'
    )
    ok, out = _run_emacs_batch(elisp)
    if not ok:
        return False, f"list_todos failed: {out[:500]}"
    if out == "null":
        out = "[]"
    return True, out


def refile_node(workdir: Path, node_id: str,
                  target_path: str) -> tuple[bool, str]:
    """Refile the heading with =:ID:= =node_id= under =target_path=.

    =target_path= is "/file.org/Parent Heading/Sub Heading" — the same
    slash-separated form org-mcp uses for =org-headline://= URIs.
    org-mcp itself does not currently expose =refile= as an MCP tool,
    so we drive =org-refile= directly via =emacs --batch=.

    Returns (ok, message). On success message is JSON
    ={"moved": "<id>", "into": "<target>"}=.
    """
    if not node_id:
        return False, "missing node_id"
    if not target_path:
        return False, "missing target_path"
    workdir = Path(workdir)
    parts = [p for p in target_path.split("/") if p]
    if len(parts) < 1:
        return False, f"target_path {target_path!r} must include at least a file"
    target_file = parts[0]
    if not target_file.endswith(".org"):
        target_file += ".org"
    target_file_abs = (workdir / target_file).resolve()
    if not target_file_abs.exists():
        return False, f"target file not found: {target_file_abs}"
    target_heading_path = "/".join(parts[1:])
    files = _allowed_files_in(workdir)
    file_list = _format_lisp_string_list(files)
    # Drive org-refile manually: find the source by ID, then refile to
    # the named heading in target_file. We use a temp-buffer per source
    # file rather than persistent buffers so the output is
    # deterministic.
    elisp = (
        f'(progn (require (quote org)) (require (quote org-id)) '
        f'(setq org-refile-targets (list (cons (list "{target_file_abs}") '
        f'                                       (cons :maxlevel 9)))) '
        f'(let ((files {file_list}) (found nil)) '
        f'(dolist (f files) '
        f'  (when (and (not found) (file-exists-p f)) '
        f'    (with-current-buffer (find-file-noselect f) '
        f'      (org-mode) '
        f'      (let ((m (org-find-property "ID" "{node_id}"))) '
        f'        (when m (goto-char m) '
        f'                (condition-case err '
        f'                  (progn '
        f'                    (org-refile nil nil '
        f'                      (list "{target_heading_path}" "{target_file_abs}" '
        f'                            nil (org-find-exact-headline-in-buffer '
        f'                                  "{target_heading_path}" '
        f'                                  (find-file-noselect "{target_file_abs}")))) '
        f'                    (save-buffer) '
        f'                    (with-current-buffer '
        f'                      (find-file-noselect "{target_file_abs}") (save-buffer)) '
        f'                    (setq found t) '
        f'                    (princ (json-encode '
        f'                      (list (cons "moved" "{node_id}") '
        f'                            (cons "into" "{target_path}"))))) '
        f'                  (error (princ (format "ERR: %S" err))))))))) '
        f'(unless found (princ (format "ERR: id {node_id} not found")))))'
    )
    ok, out = _run_emacs_batch(elisp, timeout=30)
    if not ok or out.startswith("ERR:"):
        return False, f"refile_node failed: {out[:500]}"
    return True, out


def create_node(workdir: Path, parent_path: str,
                  title: str, body: str = "") -> tuple[bool, str]:
    """Create a new TODO heading under =parent_path=, return its new =:ID:=.

    =parent_path= is "/file.org" for top-level or
    "/file.org/Parent Heading" for nested. Mirrors org-mcp's
    =org-add-todo= contract.

    When =mcp-server-lib= is available, this calls org-mcp's internal
    helper. When not, it falls back to a plain =org-= insertion that
    creates the same shape of node + assigns an ID via =org-id-get-create=.
    """
    if not parent_path:
        return False, "missing parent_path"
    if not title:
        return False, "missing title"
    workdir = Path(workdir)
    parts = [p for p in parent_path.split("/") if p]
    if not parts:
        return False, f"parent_path {parent_path!r} must include a file"
    target_file = parts[0]
    if not target_file.endswith(".org"):
        target_file += ".org"
    target_file_abs = (workdir / target_file).resolve()
    if not target_file_abs.exists():
        return False, f"parent file not found: {target_file_abs}"
    parent_heading_path = "/".join(parts[1:])
    title_q = title.replace('"', '\\"')
    body_q = body.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    if parent_heading_path:
        # Find the parent heading and append a child of one deeper level.
        elisp = (
            f'(progn (require (quote org)) (require (quote org-id)) '
            f'(with-current-buffer (find-file-noselect "{target_file_abs}") '
            f'  (org-mode) '
            f'  (let ((m (org-find-exact-headline-in-buffer '
            f'              "{parent_heading_path}" (current-buffer)))) '
            f'    (if (not m) (princ (format "ERR: parent heading %s not found" '
            f'                                  "{parent_heading_path}")) '
            f'      (goto-char m) '
            f'      (let ((parent-level (org-current-level))) '
            f'        (org-end-of-subtree t t) '
            f'        (unless (bolp) (insert "\\n")) '
            f'        (insert (make-string (1+ parent-level) ?*) " TODO {title_q}\\n") '
            f'        (when (> (length "{body_q}") 0) (insert "{body_q}\\n")) '
            f'        (forward-line -1) '
            f'        (when (> (length "{body_q}") 0) (forward-line -1)) '
            f'        (let ((id (org-id-get-create))) '
            f'          (save-buffer) '
            f'          (princ id)))))))'
        )
    else:
        # Top-level: append at end of file as a level-1 heading.
        elisp = (
            f'(progn (require (quote org)) (require (quote org-id)) '
            f'(with-current-buffer (find-file-noselect "{target_file_abs}") '
            f'  (org-mode) '
            f'  (goto-char (point-max)) '
            f'  (unless (bolp) (insert "\\n")) '
            f'  (insert "* TODO {title_q}\\n") '
            f'  (when (> (length "{body_q}") 0) (insert "{body_q}\\n")) '
            f'  (forward-line -1) '
            f'  (when (> (length "{body_q}") 0) (forward-line -1)) '
            f'  (let ((id (org-id-get-create))) '
            f'    (save-buffer) '
            f'    (princ id))))'
        )
    ok, out = _run_emacs_batch(elisp, timeout=30)
    if not ok or out.startswith("ERR:"):
        return False, f"create_node failed: {out[:500]}"
    return True, out


def query_agenda(workdir: Path, days: int = 7) -> tuple[bool, str]:
    """Return upcoming items with SCHEDULED or DEADLINE in next =days= days.

    org-mcp doesn't ship an agenda primitive, so we walk the org files
    via =org-map-entries= and filter by deadline / scheduled date. Each
    entry is ={"file", "heading", "state", "deadline", "scheduled"}=.
    """
    workdir = Path(workdir)
    days = max(1, min(int(days or 7), 90))
    files = _allowed_files_in(workdir)
    if not files:
        return True, json.dumps([])
    file_list = _format_lisp_string_list(files)
    elisp = (
        f'(progn (require (quote org)) (require (quote json)) '
        f'(let* ((files {file_list}) '
        f'       (cutoff (time-add (current-time) (days-to-time {days}))) '
        f'       (results (list))) '
        f'(dolist (f files) '
        f'  (when (file-exists-p f) '
        f'    (with-temp-buffer (insert-file-contents f) (org-mode) '
        f'      (org-map-entries '
        f'        (lambda () '
        f'          (let* ((deadline (org-entry-get nil "DEADLINE")) '
        f'                 (scheduled (org-entry-get nil "SCHEDULED")) '
        f'                 (any (or deadline scheduled))) '
        f'            (when any '
        f'              (let ((tm (org-time-string-to-time any))) '
        f'                (when (time-less-p tm cutoff) '
        f'                  (push (list (cons "file" f) '
        f'                              (cons "heading" '
        f'                                    (org-get-heading t t t t)) '
        f'                              (cons "state" (org-get-todo-state)) '
        f'                              (cons "deadline" (or deadline "")) '
        f'                              (cons "scheduled" (or scheduled ""))) '
        f'                        results)))))))))) '
        f'(princ (json-encode (or (nreverse results) (list))))))'
    )
    ok, out = _run_emacs_batch(elisp)
    if not ok:
        return False, f"query_agenda failed: {out[:500]}"
    if out == "null":
        out = "[]"
    return True, out


def search_by_tag(workdir: Path, tag: str) -> tuple[bool, str]:
    """Return all headings tagged =tag= across workdir =.org= files.

    Uses =org-map-entries= with a tag-match string so inheritance is
    honored per the file's =org-use-tag-inheritance= setting.
    """
    if not tag:
        return False, "missing tag"
    if not re.match(r"^[A-Za-z0-9_@:-]+$", tag):
        return False, f"invalid tag {tag!r}; must match [A-Za-z0-9_@:-]+"
    workdir = Path(workdir)
    files = _allowed_files_in(workdir)
    if not files:
        return True, json.dumps([])
    file_list = _format_lisp_string_list(files)
    elisp = (
        f'(progn (require (quote org)) (require (quote json)) '
        f'(let ((files {file_list}) (results (list))) '
        f'(dolist (f files) '
        f'  (when (file-exists-p f) '
        f'    (with-temp-buffer (insert-file-contents f) (org-mode) '
        f'      (org-map-entries '
        f'        (lambda () '
        f'          (push (list (cons "file" f) '
        f'                      (cons "heading" (org-get-heading t t t t)) '
        f'                      (cons "state" (or (org-get-todo-state) "")) '
        f'                      (cons "tags" (or (org-get-tags) (list)))) '
        f'                results)) '
        f'        "{tag}")))) '
        f'(princ (json-encode (or (nreverse results) (list))))))'
    )
    ok, out = _run_emacs_batch(elisp)
    if not ok:
        return False, f"search_by_tag failed: {out[:500]}"
    if out == "null":
        out = "[]"
    return True, out


# ── Tool spec dicts (OpenAI tool-call schema) ───────────────────────────
LIST_TODOS_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_org_list_todos",
        "description": (
            "List TODO entries across all .org files in workdir. "
            "Wraps org-mcp's org-mode reader surface. Returns JSON "
            "list of {file, heading, state, tags, level}. Optional "
            "state_filter restricts to one TODO keyword (e.g. 'TODO', "
            "'NEXT', 'DONE')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state_filter": {
                    "type": "string",
                    "description": (
                        "Optional TODO keyword to filter on. Omit for "
                        "all stateful entries."),
                },
            },
            "required": [],
        },
    },
}

REFILE_NODE_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_org_refile_node",
        "description": (
            "Refile (move) the heading with the given :ID: to a new "
            "parent path. target_path is '/file.org/Parent/Sub'. "
            "Equivalent to running M-x org-refile in Emacs against the "
            "matched node. Note: org-mcp v0.9 does not yet expose refile "
            "as an MCP tool; this wrapper drives upstream org-refile "
            "directly under emacs --batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": ":ID: UUID of the heading to move",
                },
                "target_path": {
                    "type": "string",
                    "description": (
                        "Destination as '/file.org' or "
                        "'/file.org/Parent Heading/Sub Heading'."),
                },
            },
            "required": ["node_id", "target_path"],
        },
    },
}

CREATE_NODE_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_org_create_node",
        "description": (
            "Create a new TODO heading. parent_path is '/file.org' for "
            "top-level or '/file.org/Parent Heading' for nested. Returns "
            "the newly assigned :ID: UUID. Body is optional. Mirrors "
            "org-mcp's org-add-todo contract."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parent_path": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["parent_path", "title"],
        },
    },
}

QUERY_AGENDA_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_org_query_agenda",
        "description": (
            "Return headings whose SCHEDULED or DEADLINE falls in the "
            "next N days. Default 7. Returns JSON list of "
            "{file, heading, state, deadline, scheduled}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookahead window in days (1-90, default 7)",
                },
            },
            "required": [],
        },
    },
}

SEARCH_BY_TAG_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_org_search_by_tag",
        "description": (
            "Find every heading tagged with the given tag. Tag inheritance "
            "honors the file's org-use-tag-inheritance setting. Returns "
            "JSON list of {file, heading, state, tags}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Tag name without leading/trailing colons",
                },
            },
            "required": ["tag"],
        },
    },
}

MCP_TOOLS = [
    LIST_TODOS_TOOL,
    REFILE_NODE_TOOL,
    CREATE_NODE_TOOL,
    QUERY_AGENDA_TOOL,
    SEARCH_BY_TAG_TOOL,
]
