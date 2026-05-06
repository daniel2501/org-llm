# [[file:../../../org/20260425230731-org_llm.org::*access.py][access.py:1]]
"""Permission-gated file + browser access for the MCP server.

The LLM running inside an MCP client (opencode, Claude Code, …) can request to read files or
drive a browser, but only paths the user has explicitly authorized via
`org-llm grant <path>` are reachable. Without grants, every file-access
tool returns a refusal that names the missing grant — so the LLM can
relay it to the user, who can authorize and retry.

Why an allow-list and not a per-call prompt?
  • The MCP server runs over stdio with no TTY of its own; it can't
    interactively prompt the human at request time.
  • An explicit, persistent allow-list is auditable
    (`org-llm grants` lists everything the LLM currently sees).
  • Grants live in the SQLite Config row `mcp_file_allowlist`,
    comma-separated, so they ride along with the rest of the project.

Browser access is a separate flag (`mcp_browser_enabled`) so a user can
allow file reads without opening URLs from the LLM, or vice-versa.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


# ── Allow-list helpers ────────────────────────────────────────────────────────

def _read_config_value(key: str) -> str:
    """Read a single Config row without bringing in the rest of the CLI."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return ""
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, key)
            return (row.value if row else "") or ""
    except Exception:
        return ""


def _write_config_value(key: str, value: str) -> bool:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, key)
            if row:
                row.value = value
            else:
                s.add(Config(key=key, value=value))
            s.commit()
        return True
    except Exception:
        return False


def allowlist() -> list[Path]:
    """Return the current set of allow-listed prefix paths (resolved, expanded)."""
    raw = _read_config_value("mcp_file_allowlist")
    out: list[Path] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            out.append(Path(entry).expanduser().resolve())
        except Exception:
            continue
    return out


def grant(path: str) -> bool:
    """Add a path prefix to the allow-list. Idempotent."""
    p = Path(path).expanduser().resolve()
    current = allowlist()
    canonical = str(p)
    if any(str(c) == canonical for c in current):
        return True
    current.append(p)
    return _write_config_value("mcp_file_allowlist",
                               ",".join(str(c) for c in current))


def revoke(path: str) -> bool:
    """Remove a path prefix from the allow-list. Idempotent."""
    p = Path(path).expanduser().resolve()
    current = allowlist()
    new_list = [c for c in current if str(c) != str(p)]
    return _write_config_value("mcp_file_allowlist",
                               ",".join(str(c) for c in new_list))


def is_allowed(path: str) -> tuple[bool, Path]:
    """Return (allowed?, resolved_path) for a candidate file or directory.

    A path is allowed if it equals or is contained under any allow-list
    entry. Symlinks are followed during the resolution step.
    """
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return (False, Path(path))
    for prefix in allowlist():
        try:
            target.relative_to(prefix)
            return (True, target)
        except ValueError:
            continue
    return (False, target)


# ── Auto-grant: trusted roots the LLM can self-grant within ──────────────────
#
# `mcp_auto_grant_roots` lets the user pre-authorize whole subtrees. When the
# LLM calls `request_access(path, reason)`, the path is auto-granted if it
# lies under any auto-grant root AND is not in the sensitive deny-list.

# Always-denied paths regardless of grants. Symlink-resolved before checking,
# so a user-owned symlink at ~/safe → ~/.ssh still gets blocked.
_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".ssh",                # private keys, known_hosts
    ".gnupg",              # gpg keyring
    ".password-store",     # pass — encrypted but exposing slugs is still leak
    ".aws/credentials",
    ".azure",
    ".gcloud",
    ".kube/config",
    ".netrc",
    ".pgpass",
    ".docker/config.json",
    ".npmrc",
    ".pypirc",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssh",
    "/root/",
)


def _is_sensitive(target: Path) -> bool:
    """Block reads from anything matching a sensitive pattern."""
    s = str(target)
    for pat in _SENSITIVE_PATTERNS:
        if pat.startswith("/"):
            if s.startswith(pat) or s == pat.rstrip("/"):
                return True
        else:
            # Match as a path component anywhere (e.g. ~/work/.ssh/x)
            if f"/{pat}/" in (s + "/") or s.endswith(f"/{pat}"):
                return True
    return False


def auto_grant_roots() -> list[Path]:
    raw = _read_config_value("mcp_auto_grant_roots")
    out: list[Path] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            out.append(Path(entry).expanduser().resolve())
        except Exception:
            continue
    # Implicit default: the user's vault. The user has already pointed
    # org-llm at `org_dir` — granting the LLM read-access to its own
    # vault by default removes the "ask the user to run 7 grant
    # commands" failure mode. Sensitive deny-list still applies, so
    # things like ~/org/.gnupg or ~/org/.ssh still get refused.
    if not out:
        org_dir = _read_config_value("org_dir") or "~/org"
        try:
            out.append(Path(org_dir).expanduser().resolve())
        except Exception:
            pass
    return out


def add_auto_root(path: str) -> bool:
    p = Path(path).expanduser().resolve()
    current = auto_grant_roots()
    if any(str(c) == str(p) for c in current):
        return True
    current.append(p)
    return _write_config_value("mcp_auto_grant_roots",
                                ",".join(str(c) for c in current))


def remove_auto_root(path: str) -> bool:
    p = Path(path).expanduser().resolve()
    current = auto_grant_roots()
    new_list = [c for c in current if str(c) != str(p)]
    return _write_config_value("mcp_auto_grant_roots",
                                ",".join(str(c) for c in new_list))


class AutoGrantResult(NamedTuple):
    granted:    bool
    sensitive:  bool      # True iff the request was refused due to deny-list
    target:     Path
    message:    str       # user-visible explanation


def request_self_grant(path: str, reason: str = "") -> AutoGrantResult:
    """Try to auto-grant a path the LLM has requested.

    Allowed iff:
      • path resolves under one of the user's auto-grant roots, AND
      • path is not on the always-sensitive deny-list

    On success, the path is appended to the regular allow-list (so future
    read_file calls succeed without going through this tool again).
    """
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return AutoGrantResult(False, False, Path(path),
                                f"Could not resolve path: {path}")

    if _is_sensitive(target):
        return AutoGrantResult(
            False, True, target,
            f"Refused: {target} is in the always-sensitive deny-list "
            "(SSH keys, GPG, cloud credentials, etc.). Even with an "
            "auto-grant root, these never auto-authorize.",
        )

    roots = auto_grant_roots()
    if not roots:
        return AutoGrantResult(
            False, False, target,
            "No auto-grant roots configured. Either:\n"
            f"  1. Ask the user to run:  org-llm grant {target}\n"
            "  2. Or to run:           org-llm grant-root <prefix>\n"
            "to enable LLM self-grants under a trusted prefix.",
        )

    for root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        # Match — auto-grant by appending to the regular allow-list
        if grant(str(target)):
            return AutoGrantResult(
                True, False, target,
                f"Auto-granted: {target}\n"
                f"(under trusted root {root}; reason: {reason or 'not given'})",
            )
        return AutoGrantResult(False, False, target,
                                "Failed to write allow-list (DB issue?).")

    return AutoGrantResult(
        False, False, target,
        f"Refused: {target} is not under any auto-grant root.\n"
        f"Auto-grant roots: {', '.join(str(r) for r in roots) or '(none)'}\n"
        f"Ask the user to run:  org-llm grant {target}",
    )


# ── File reads (gated) ────────────────────────────────────────────────────────

MAX_READ_BYTES = 64_000


class FileRead(NamedTuple):
    ok:        bool
    content:   str
    error:     str
    truncated: bool


def read_file(path: str) -> FileRead:
    """Read a file if allowed; otherwise return a refusal naming the path.

    The first line of `error` is structured ("Access denied: <path>") so the
    LLM can recognise it and pass the message back to the user verbatim.
    """
    allowed, resolved = is_allowed(path)
    if not allowed:
        return FileRead(False, "", _denial_message(resolved), False)
    if not resolved.exists():
        return FileRead(False, "", f"File does not exist: {resolved}", False)
    if resolved.is_dir():
        return FileRead(False, "", f"Path is a directory: {resolved}", False)
    try:
        size = resolved.stat().st_size
        with open(resolved, "rb") as f:
            raw = f.read(MAX_READ_BYTES)
    except Exception as e:
        return FileRead(False, "", f"Read failed: {e}", False)
    truncated = size > MAX_READ_BYTES
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n... [truncated; full file is {size} bytes]"
    return FileRead(True, text, "", truncated)


def list_directory(path: str, max_entries: int = 200) -> FileRead:
    """List a directory if allowed."""
    allowed, resolved = is_allowed(path)
    if not allowed:
        return FileRead(False, "", _denial_message(resolved), False)
    if not resolved.exists() or not resolved.is_dir():
        return FileRead(False, "", f"Not a directory: {resolved}", False)
    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except Exception as e:
        return FileRead(False, "", f"List failed: {e}", False)
    truncated = len(entries) > max_entries
    lines = []
    for e in entries[:max_entries]:
        kind = "d" if e.is_dir() else "f"
        try:
            size = "" if e.is_dir() else f"{e.stat().st_size:>10}"
        except Exception:
            size = ""
        lines.append(f"{kind} {size}  {e.name}")
    body = "\n".join(lines)
    if truncated:
        body += f"\n\n... [truncated; {len(entries)} total entries]"
    return FileRead(True, body, "", truncated)


def _denial_message(path: Path) -> str:
    grants = allowlist()
    summary = (", ".join(str(g) for g in grants[:5])
               + (" + more" if len(grants) > 5 else "")) if grants else "(none)"
    parent = path.parent if path.is_file() else path
    return (
        f"Access denied: {path}\n"
        f"This path is not in the org-llm MCP allow-list.\n"
        f"Currently granted prefixes: {summary}\n"
        f"BEFORE asking the user to run shell commands: try "
        f"`request_access({parent!r}, reason='…')` — granting the "
        f"parent directory covers all files inside it in one call.\n"
        f"If self-grant is refused, suggest ONE consolidated "
        f"command to the user:  `org-llm grant {parent}`  "
        f"(directory grant covers every descendant)."
    )


# ── Browser access (qutebrowser preferred, webbrowser fallback) ──────────────

def browser_enabled() -> bool:
    return _read_config_value("mcp_browser_enabled").lower() in ("1", "true", "yes")


def set_browser_enabled(on: bool) -> bool:
    return _write_config_value("mcp_browser_enabled", "1" if on else "0")


def _qute_bin() -> str | None:
    found = shutil.which("qutebrowser")
    return found


def open_url(url: str) -> tuple[bool, str]:
    """Open a URL in qutebrowser (if running/installed) else default browser.

    Returns (ok, message). Refuses if mcp_browser_enabled is false.
    """
    if not browser_enabled():
        return (False, ("Browser access is disabled. Ask the user to run:\n"
                        "  org-llm grant-browser    # enable browser tools"))
    # Only http(s)/file URLs — refuse javascript: / data: / etc.
    if not (url.startswith("http://") or url.startswith("https://")
            or url.startswith("file://")):
        return (False, f"Refusing non-http(s) URL: {url[:80]}")
    qute = _qute_bin()
    if qute:
        try:
            r = subprocess.run([qute, ":open", "-t", url],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return (True, f"Opened in qutebrowser: {url}")
        except Exception:
            pass
    # Fall back to the system default
    try:
        import webbrowser
        webbrowser.open(url)
        return (True, f"Opened: {url}")
    except Exception as e:
        return (False, f"Browser open failed: {e}")


def qute_command(command: str) -> tuple[bool, str]:
    """Send a colon-command to a running qutebrowser instance via IPC."""
    if not browser_enabled():
        return (False, "Browser access disabled. Run: org-llm grant-browser")
    if not command.startswith(":"):
        command = ":" + command
    qute = _qute_bin()
    if not qute:
        return (False, "qutebrowser not installed")
    try:
        r = subprocess.run([qute, command],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return (True, f"Sent to qutebrowser: {command}")
        return (False, r.stderr.strip()[:200] or f"qutebrowser exited {r.returncode}")
    except Exception as e:
        return (False, f"qute_command failed: {e}")
# access.py:1 ends here
