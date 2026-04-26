# [[file:../../../org/20260425230731-org_llm.org::*creds.py][creds.py:1]]
"""Credential management — wraps the `pass` password store (passwordstore.org).

The shape of the store under ~/.password-store/:

    org-llm/
      cloud/
        runpod/api-key
        vast/api-key
        lambda/api-key
        ...
      anthropic/api-key            # for `org-llm claude`
      huggingface/token            # future: gated model downloads

Public API:
  - is_available()        — `pass` binary present and store initialized
  - is_installed()        — binary present (store may not yet be initialized)
  - is_initialized()      — `~/.password-store/.gpg-id` exists
  - install_help()        — multi-line user-facing setup instructions
  - install(bin_dir)      — best-effort install via guix / apt / pacman
  - read_secret(slug)     — `pass show <slug>` → str | None
  - write_secret(slug, v) — `pass insert -m <slug>` → bool
  - delete_secret(slug)   — `pass rm -f <slug>` → bool
  - list_secrets(prefix)  — list slugs under a prefix (e.g. "org-llm/cloud")
  - cloud_slug(provider)  — canonical slug for a cloud provider's API key
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


PASS_STORE = Path(os.environ.get("PASSWORD_STORE_DIR") or "~/.password-store").expanduser()


# ── Slugs ─────────────────────────────────────────────────────────────────────

def cloud_slug(provider_slug: str) -> str:
    """Canonical pass slug for a cloud provider API key."""
    return f"org-llm/cloud/{provider_slug}/api-key"


def anthropic_slug() -> str:
    return "org-llm/anthropic/api-key"


# ── Status checks ─────────────────────────────────────────────────────────────

def is_installed() -> bool:
    return shutil.which("pass") is not None


def is_initialized() -> bool:
    """True when the store has a .gpg-id file (i.e. `pass init <key>` was run)."""
    return (PASS_STORE / ".gpg-id").exists()


def is_available() -> bool:
    """True when secrets can be read/written right now (binary + initialized)."""
    return is_installed() and is_initialized()


def install_help() -> str:
    """Multi-line user-facing instructions for getting `pass` ready."""
    if not is_installed():
        return (
            "`pass` (the standard Unix password manager) is not installed.\n"
            "  Guix:   guix install password-store gnupg\n"
            "  Debian: sudo apt install pass\n"
            "  Arch:   sudo pacman -S pass\n"
            "  macOS:  brew install pass\n"
            "  Or:     org-llm install --skip-ollama --skip-models --skip-fonts \\\n"
            "                          --skip-opencode --skip-gh --skip-claude\n"
        )
    if not is_initialized():
        return (
            "`pass` is installed but the store is not initialized.\n"
            "  1. Create or pick a GPG key:    gpg --list-secret-keys\n"
            "                                   gpg --full-generate-key   (if none)\n"
            "  2. Copy the long key id (the hex string after `sec  rsa…/`)\n"
            "  3. Initialize the store:        pass init <KEY-ID>\n"
            "  4. Re-run the org-llm command — secrets will be stored under\n"
            f"     {PASS_STORE}/org-llm/...\n"
        )
    return f"`pass` is ready. Store at {PASS_STORE}."


# ── Install (best effort) ─────────────────────────────────────────────────────

def install() -> bool:
    """Try to install `pass` via a known package manager. Returns True if pass is now installed."""
    if is_installed():
        return True

    candidates: list[list[str]] = []
    if shutil.which("guix"):
        candidates.append(["guix", "install", "password-store", "gnupg"])
    if shutil.which("apt-get"):
        candidates.append(["sudo", "apt-get", "install", "-y", "pass"])
    if shutil.which("pacman"):
        candidates.append(["sudo", "pacman", "-S", "--noconfirm", "pass"])
    if shutil.which("brew"):
        candidates.append(["brew", "install", "pass"])
    if shutil.which("dnf"):
        candidates.append(["sudo", "dnf", "install", "-y", "pass"])

    for cmd in candidates:
        try:
            r = subprocess.run(cmd, timeout=180)
            if r.returncode == 0 and is_installed():
                return True
        except Exception:
            continue
    return is_installed()


# ── Secret I/O ────────────────────────────────────────────────────────────────

class _PassError(RuntimeError):
    pass


def _run_pass(args: list[str], stdin: str | None = None,
              timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke `pass` with the given args. Raises if binary is missing."""
    if not is_installed():
        raise _PassError("pass not installed")
    return subprocess.run(
        ["pass", *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def read_secret(slug: str) -> str | None:
    """Return the stored secret, or None if missing / unreadable."""
    if not is_available():
        return None
    try:
        r = _run_pass(["show", slug])
    except Exception:
        return None
    if r.returncode != 0:
        return None
    # `pass show` outputs the password followed by a newline; multi-line entries
    # have additional metadata after the first line.
    return r.stdout.splitlines()[0] if r.stdout else None


def write_secret(slug: str, value: str) -> bool:
    """Store a secret. Returns True on success.

    Uses `pass insert --multiline --force` so a single-line value can be passed
    on stdin without shell quoting. Existing entries are overwritten.
    """
    if not is_available():
        return False
    try:
        r = _run_pass(["insert", "--multiline", "--force", slug],
                      stdin=value.rstrip("\n") + "\n")
    except Exception:
        return False
    return r.returncode == 0


def delete_secret(slug: str) -> bool:
    if not is_available():
        return False
    try:
        r = _run_pass(["rm", "--force", slug])
    except Exception:
        return False
    return r.returncode == 0


def list_secrets(prefix: str = "org-llm") -> list[str]:
    """Return slugs under a prefix (e.g. 'org-llm/cloud')."""
    if not is_available():
        return []
    base = PASS_STORE / prefix
    if not base.exists():
        return []
    out = []
    for p in base.rglob("*.gpg"):
        rel = p.relative_to(PASS_STORE).with_suffix("")
        out.append(str(rel))
    return sorted(out)


# ── Status snapshot ──────────────────────────────────────────────────────────

class CredsStatus(NamedTuple):
    installed:   bool
    initialized: bool
    store_path:  Path
    gpg_id:      str
    secrets:     list[str]


def status() -> CredsStatus:
    gpg_id = ""
    if is_initialized():
        try:
            gpg_id = (PASS_STORE / ".gpg-id").read_text().strip()
        except Exception:
            pass
    return CredsStatus(
        installed   = is_installed(),
        initialized = is_initialized(),
        store_path  = PASS_STORE,
        gpg_id      = gpg_id,
        secrets     = list_secrets("org-llm") if is_available() else [],
    )
# creds.py:1 ends here
