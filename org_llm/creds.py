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

_PASS_FALLBACK_PATHS = [
    Path("~/.guix-profile/bin/pass").expanduser(),
    Path("~/.guix-home/profile/bin/pass").expanduser(),
    Path("/usr/bin/pass"),
    Path("/usr/local/bin/pass"),
    Path("/opt/homebrew/bin/pass"),
]


def _pass_bin() -> str | None:
    """Find the pass binary anywhere — PATH, then known profile/system locations."""
    found = shutil.which("pass")
    if found:
        return found
    for p in _PASS_FALLBACK_PATHS:
        if p.exists():
            return str(p)
    return None


def is_installed() -> bool:
    return _pass_bin() is not None


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
            "  Or:     org-llm install-tools --skip-ollama --skip-models --skip-fonts \\\n"
            "                                --skip-opencode --skip-gh --skip-claude\n"
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


def list_gpg_keys() -> list[tuple[str, str]]:
    """Return [(key_id, uid), …] for available secret keys, or []."""
    gbin = _gpg_bin()
    if not gbin:
        return []
    try:
        r = subprocess.run(
            [gbin, "--list-secret-keys", "--with-colons"],
            capture_output=True, text=True, timeout=10,
            env=_augmented_env(),
        )
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    cur_id: str | None = None
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if not parts:
            continue
        if parts[0] == "sec":
            cur_id = parts[4]
        elif parts[0] == "uid" and cur_id:
            out.append((cur_id, parts[9]))
            cur_id = None
    return out


def bootstrap_gpg_key(name: str, email: str, passphrase: str = "") -> str | None:
    """Generate a GPG key non-interactively. Returns the new key id, or None."""
    gbin = _gpg_bin()
    if not gbin:
        return None
    batch = (
        ("%no-protection\n" if not passphrase else f"Passphrase: {passphrase}\n") +
        "Key-Type: RSA\n"
        "Key-Length: 4096\n"
        f"Name-Real: {name}\n"
        f"Name-Email: {email}\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    try:
        r = subprocess.run(
            [gbin, "--batch", "--gen-key"],
            input=batch, capture_output=True, text=True, timeout=300,
            env=_augmented_env(),
        )
        if r.returncode != 0:
            return None
    except Exception:
        return None
    keys = list_gpg_keys()
    for kid, uid in keys:
        if email in uid:
            return kid
    return keys[-1][0] if keys else None


def init_store(key_id: str) -> bool:
    """Run `pass init <key_id>` to bootstrap the password store."""
    pbin = _pass_bin()
    if not pbin:
        return False
    try:
        r = subprocess.run([pbin, "init", key_id],
                           capture_output=True, text=True, timeout=30,
                           env=_augmented_env())
        return r.returncode == 0
    except Exception:
        return False


# ── Secret I/O ────────────────────────────────────────────────────────────────

class _PassError(RuntimeError):
    pass


_GPG_FALLBACK_PATHS = [
    Path("~/.guix-profile/bin/gpg").expanduser(),
    Path("~/.guix-home/profile/bin/gpg").expanduser(),
    Path("/usr/bin/gpg"),
    Path("/usr/local/bin/gpg"),
    Path("/opt/homebrew/bin/gpg"),
]


def _gpg_bin() -> str | None:
    found = shutil.which("gpg")
    if found:
        return found
    for p in _GPG_FALLBACK_PATHS:
        if p.exists():
            return str(p)
    return None


def _augmented_env() -> dict:
    """Return os.environ + a PATH that includes Guix profile locations.

    pass shells out to gpg internally, so we must guarantee gpg is on PATH
    even when the parent process inherited a stripped-down PATH (e.g. uv run).
    """
    env = os.environ.copy()
    extra = [
        str(Path("~/.guix-profile/bin").expanduser()),
        str(Path("~/.guix-home/profile/bin").expanduser()),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    current = env.get("PATH", "")
    parts = current.split(":") if current else []
    for p in extra:
        if p not in parts and Path(p).is_dir():
            parts.append(p)
    env["PATH"] = ":".join(parts)
    return env


def _run_pass(args: list[str], stdin: str | None = None,
              timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke `pass` with the given args. Raises if binary is missing."""
    pbin = _pass_bin()
    if not pbin:
        raise _PassError("pass not installed")
    return subprocess.run(
        [pbin, *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
        env=_augmented_env(),
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
