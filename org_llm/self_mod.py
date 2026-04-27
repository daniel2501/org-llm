"""Self-modification + rollback for org-llm.

The user can ask the app to read or revise its own Python source and
config DB, with safe rollback via an artifact bundle (tarball plus a
standalone shell script). Snapshots live under
`~/.local/share/org-llm/snapshots/<timestamp>/`.

Each snapshot contains:

  - `org_llm/`         — full copy of the package source
  - `org-llm.db.snapshot` — copy of the SQLite DB
  - `manifest.json`    — timestamp, label, git hash, python version, file list
  - `rollback.sh`      — standalone shell script that restores both above
  - `<ts>.tar.gz`      — bundled artifact (siblings of the directory)

Rollback is intentionally a separate shell script, not a Python call,
so it runs OUTSIDE the running app and can recover even when the
in-process code is broken.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime    import datetime
from pathlib     import Path


def snapshot_root() -> Path:
    """Resolve where snapshots live, honouring env overrides at call time."""
    p = os.environ.get("ORG_LLM_SNAPSHOT_DIR")
    if p:
        return Path(p).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "org-llm" / "snapshots"


@dataclass
class Snapshot:
    id:        str          # timestamp slug
    path:      Path         # the directory
    tarball:   Path         # the .tar.gz next to it
    manifest:  dict
    label:     str = ""


# ── Locate the running package source directory ──────────────────────────

def package_dir() -> Path:
    """Return the on-disk directory containing the running org_llm package."""
    import org_llm
    return Path(org_llm.__file__).parent


def db_path() -> Path:
    """Return the org-llm SQLite path (env override → default)."""
    from .db import DB_PATH
    return Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))


def _git_hash(pkg: Path) -> str:
    """Return the git HEAD short hash of the repo containing the package, or ''."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(pkg.parent), capture_output=True, text=True,
            timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


# ── Snapshot creation ─────────────────────────────────────────────────────

_ROLLBACK_SCRIPT_TEMPLATE = """\
#!/usr/bin/env bash
# org-llm rollback script
# Generated: {created_at}
# Snapshot: {snapshot_id}
# Label:    {label}
#
# Restores the org-llm package source AND the SQLite DB to the state
# captured in this snapshot. Designed to be runnable EVEN WHEN the
# in-process app is broken — pure bash, no Python required.

set -euo pipefail

SNAP_DIR="{snapshot_path}"
PKG_DEST="{pkg_dest}"
DB_DEST="{db_dest}"

if [ ! -d "$SNAP_DIR" ]; then
  echo "Error: snapshot directory not found: $SNAP_DIR" >&2
  exit 1
fi

echo "→ Rolling back org-llm to snapshot {snapshot_id}"
echo "  package: $SNAP_DIR/org_llm  →  $PKG_DEST"
echo "  db:      $SNAP_DIR/org-llm.db.snapshot  →  $DB_DEST"

read -r -p "Continue? [y/N] " ans
case "$ans" in
  [yY]|[yY][eE][sS]) ;;
  *) echo "Aborted."; exit 0 ;;
esac

# Backup current state before overwrite — defence against rollback regret.
BACKUP="$(mktemp -d)"
echo "  pre-rollback backup → $BACKUP"
cp -a "$PKG_DEST" "$BACKUP/org_llm" 2>/dev/null || true
cp -a "$DB_DEST"  "$BACKUP/org-llm.db" 2>/dev/null || true

# Restore package source
rm -rf "$PKG_DEST"
cp -a "$SNAP_DIR/org_llm" "$PKG_DEST"

# Restore DB
mkdir -p "$(dirname "$DB_DEST")"
cp -a "$SNAP_DIR/org-llm.db.snapshot" "$DB_DEST"

echo "✓ Rolled back. Pre-rollback state preserved at $BACKUP"
echo "  Verify: org-llm doctor"
"""


def create_snapshot(label: str = "") -> Snapshot:
    """Bundle the running package source + config DB into a snapshot.

    Returns a Snapshot with paths populated. Tarball siblings the
    directory so the user can copy a single file off-machine.
    """
    snapshot_root().mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_dir = snapshot_root() / ts
    snap_dir.mkdir(parents=True, exist_ok=True)

    pkg = package_dir()
    db  = db_path()
    git = _git_hash(pkg)

    # Copy package source
    code_target = snap_dir / "org_llm"
    shutil.copytree(
        pkg, code_target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )

    # Copy DB (skip cleanly when missing — may be a fresh checkout)
    if db.exists():
        shutil.copy2(db, snap_dir / "org-llm.db.snapshot")

    files = sorted(str(p.relative_to(code_target))
                    for p in code_target.rglob("*") if p.is_file())

    manifest = {
        "created_at":  datetime.now().isoformat(),
        "snapshot_id": ts,
        "label":       label,
        "git_hash":    git,
        "python":      sys.version.split()[0],
        "platform":    sys.platform,
        "package_dir": str(pkg),
        "db_path":     str(db),
        "files":       files,
        "n_files":     len(files),
        "db_present":  db.exists(),
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Rollback script
    rollback_path = snap_dir / "rollback.sh"
    rollback_path.write_text(_ROLLBACK_SCRIPT_TEMPLATE.format(
        created_at=manifest["created_at"],
        snapshot_id=ts,
        label=label or "(unlabeled)",
        snapshot_path=snap_dir,
        pkg_dest=pkg,
        db_dest=db,
    ))
    rollback_path.chmod(0o755)

    # Tarball — sibling of the directory for easy off-machine copy
    tar_path = snap_dir.with_suffix(".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(snap_dir, arcname=ts)

    return Snapshot(id=ts, path=snap_dir, tarball=tar_path,
                     manifest=manifest, label=label)


# ── Listing ────────────────────────────────────────────────────────────────

# ── Org-file activity log ─────────────────────────────────────────────────
#
# Every self-mod action (snapshot, rollback, llm-revise, edit) is appended
# to an org file under the user's vault. Lets the user `org-llm ask
# "what have I changed about org-llm lately?"` AND read it as plain text.

def snapshot_log_path() -> Path:
    """Where the org-mode self-mod log lives (env override → vault default)."""
    p = os.environ.get("ORG_LLM_SELFMOD_LOG")
    if p:
        return Path(p).expanduser()
    org_dir = os.environ.get("ORG_LLM_ORG_DIR") or "~/org"
    return Path(org_dir).expanduser() / "org-llm-self-mod.org"


def _ensure_log_header(p: Path) -> None:
    """Create the log file with a basic org header if missing."""
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "#+title: org-llm self-modifications log\n"
        "#+filetags: :org-llm:self-mod:audit:\n\n"
        "Auto-appended record of every snapshot, rollback, llm-revise,\n"
        "and edit performed via `org-llm self ...`. Each entry's rollback\n"
        "shell script is captured in a =:tangle= block so the user can\n"
        "tangle out a stand-alone recovery script per entry.\n\n"
    )


def log_snapshot(snap: Snapshot) -> None:
    """Append a snapshot record to the org log."""
    p = snapshot_log_path()
    _ensure_log_header(p)
    rollback_text = (snap.path / "rollback.sh").read_text(errors="replace") \
                     if (snap.path / "rollback.sh").exists() else ""
    rollback_target = (Path("/tmp") /
                        f"org-llm-rollback-{snap.id}.sh")
    entry = (
        f"\n* {snap.manifest['created_at']} — snapshot {snap.id}"
        f"{' — ' + snap.label if snap.label else ''}\n"
        f":PROPERTIES:\n"
        f":SELFMOD_KIND:   snapshot\n"
        f":SNAPSHOT_ID:    {snap.id}\n"
        f":LABEL:          {snap.label or '-'}\n"
        f":GIT_HASH:       {snap.manifest.get('git_hash') or '-'}\n"
        f":PYTHON:         {snap.manifest.get('python', '?')}\n"
        f":N_FILES:        {snap.manifest.get('n_files', '?')}\n"
        f":DB_PRESENT:     {snap.manifest.get('db_present', False)}\n"
        f":SNAP_DIR:       {snap.path}\n"
        f":TARBALL:        {snap.tarball}\n"
        f":END:\n\n"
        f"Captured the package source ({snap.manifest.get('n_files', '?')} "
        f"files) and the SQLite DB. Rollback script below tangles to a\n"
        f"standalone bash file under =/tmp= for emergency recovery.\n\n"
        f"#+name: rollback-{snap.id}\n"
        f"#+begin_src bash :tangle {rollback_target} :tangle-mode (identity #o755)\n"
        f"{rollback_text}"
        f"#+end_src\n"
    )
    with p.open("a") as fh:
        fh.write(entry)


def log_action(kind: str, summary: str, details: dict | None = None) -> None:
    """Append a non-snapshot self-mod entry (rollback, llm-revise, edit).

    Keeps the kind taxonomy consistent so `ask` queries against the
    log can filter by =:SELFMOD_KIND:= property.
    """
    p = snapshot_log_path()
    _ensure_log_header(p)
    ts = datetime.now().isoformat()
    detail_lines = ""
    if details:
        for k, v in details.items():
            v_str = str(v)[:240].replace("\n", " ⏎ ")
            detail_lines += f":{k.upper()}:    {v_str}\n"
    entry = (
        f"\n* {ts} — {kind} — {summary[:70]}\n"
        f":PROPERTIES:\n"
        f":SELFMOD_KIND:   {kind}\n"
        f":TIMESTAMP:      {ts}\n"
        f"{detail_lines}"
        f":END:\n\n"
        f"{summary}\n"
    )
    with p.open("a") as fh:
        fh.write(entry)


def list_snapshots() -> list[Snapshot]:
    """Return all snapshots, newest first."""
    if not snapshot_root().exists():
        return []
    out: list[Snapshot] = []
    for d in sorted(snapshot_root().iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            continue
        tar = d.with_suffix(".tar.gz")
        out.append(Snapshot(
            id=manifest.get("snapshot_id", d.name),
            path=d,
            tarball=tar,
            manifest=manifest,
            label=manifest.get("label", ""),
        ))
    return out


def find_snapshot(snapshot_id: str = "") -> Snapshot | None:
    """Find a snapshot by id (or label match), or return the newest one
    when `snapshot_id` is empty."""
    snaps = list_snapshots()
    if not snaps:
        return None
    if not snapshot_id:
        return snaps[0]
    for s in snaps:
        if s.id == snapshot_id or s.label == snapshot_id:
            return s
    # Prefix match as a final convenience
    for s in snaps:
        if s.id.startswith(snapshot_id):
            return s
    return None


# ── Rollback (in-process, in addition to the standalone script) ───────────

def rollback(snapshot: Snapshot, *, also_db: bool = True) -> dict:
    """Restore package source (and optionally DB) from a snapshot.

    Returns a dict summarising what was done. The shell script in the
    snapshot does the same thing — this Python entrypoint exists so
    `org-llm self rollback` can run it without dropping to a shell.
    """
    pkg_dest = Path(snapshot.manifest.get("package_dir") or package_dir())
    db_dest  = Path(snapshot.manifest.get("db_path")     or db_path())
    src_pkg  = snapshot.path / "org_llm"
    src_db   = snapshot.path / "org-llm.db.snapshot"

    summary: dict = {"package": False, "db": False, "backup": ""}

    # Pre-rollback backup so the user can un-rollback.
    backup_dir = Path("/tmp") / f"org-llm-pre-rollback-{snapshot.id}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if pkg_dest.exists():
        shutil.copytree(pkg_dest, backup_dir / "org_llm",
                         dirs_exist_ok=True)
    if db_dest.exists():
        shutil.copy2(db_dest, backup_dir / "org-llm.db")
    summary["backup"] = str(backup_dir)

    if src_pkg.exists():
        if pkg_dest.exists():
            shutil.rmtree(pkg_dest)
        shutil.copytree(src_pkg, pkg_dest)
        summary["package"] = True

    if also_db and src_db.exists():
        db_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, db_dest)
        summary["db"] = True

    return summary


# ── LLM-driven code revision (Phase 2 stub, working) ──────────────────────

_REVISE_SYSTEM = """\
You revise a single Python module from the `org-llm` codebase to
satisfy the user's intent. Output STRICT JSON with this shape — no
prose, no markdown, no fences:

  {"summary": "one-sentence description of the change",
   "ops":     [{"type": "replace", "old": "exact-text", "new": "exact-text"}],
   "risk":    "low" | "medium" | "high",
   "test_hint": "how to verify the change worked"}

Rules:
  - Each op's "old" must be a UNIQUE substring in the file (or the
    edit is rejected). Include enough surrounding context to disambig.
  - "new" replaces "old" verbatim; preserve indentation exactly.
  - Refuse changes that would: weaken security boundaries (deny-list
    bypass, traversal), exfiltrate credentials, or run arbitrary
    user input as code. Return {"ops": [], "summary": "refused", ...}
    with the reason in `summary`.
  - Prefer minimal diffs. Three small ops > one giant ops.
"""


def llm_revise(module_path: Path, intent: str, *,
                model: str, base_url: str) -> dict | None:
    """Ask the LLM for a JSON-described patch to a module.

    Returns the parsed plan (with `summary` / `ops` / `risk` /
    `test_hint`) or None on failure. The CLI applies it under user
    confirmation; this function never writes to disk itself.
    """
    if not module_path.exists():
        return None
    try:
        source = module_path.read_text(errors="replace")
    except Exception:
        return None
    if len(source) > 60000:
        return None  # too big for our context budget; user should narrow
    user_prompt = (
        f"File: {module_path.name}  (length: {len(source)} chars)\n"
        f"User intent: {intent}\n\n"
        f"--- BEGIN FILE ---\n{source}\n--- END FILE ---\n\n"
        "Return the JSON patch."
    )
    try:
        from .context import _llm_json_call
    except Exception:
        return None
    plan = _llm_json_call(user_prompt, _REVISE_SYSTEM,
                            model=model, base_url=base_url,
                            label="Revising module", timeout=120.0)
    if not isinstance(plan, dict):
        return None
    return plan


def apply_plan(module_path: Path, plan: dict) -> tuple[bool, str]:
    """Apply an llm_revise plan to a module file. Returns (success, message)."""
    text = module_path.read_text()
    ops = plan.get("ops") or []
    if not isinstance(ops, list) or not ops:
        return (False, "No ops in plan")
    new_text = text
    for op in ops:
        if not isinstance(op, dict):
            return (False, "Malformed op")
        if op.get("type") != "replace":
            return (False, f"Unsupported op type: {op.get('type')!r}")
        old = op.get("old", "")
        new = op.get("new", "")
        if not old or new is None:
            return (False, "Op missing old/new")
        # Must be unique to avoid silent multi-replace surprises.
        if new_text.count(old) == 0:
            return (False, f"`old` block not found in file: {old[:80]}…")
        if new_text.count(old) > 1:
            return (False, f"`old` block matches >1 location: {old[:80]}…")
        new_text = new_text.replace(old, new, 1)
    module_path.write_text(new_text)
    return (True, f"Applied {len(ops)} op(s)")
