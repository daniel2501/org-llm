"""Tests for `scripts/release.sh` — DEC-019 — Hybrid SemVer + acpt suffix.

The release-helper is pure POSIX bash; we shell out to it from a
freshly-init'd git repo per test so we never touch the user's real
tags / working tree.

Coverage (≥6):
  1. `next-acpt` computes from latest prod tag (and bootstrap case).
  2. `cut-acpt` validates VERSION shape + branch + tag-not-exists.
  3. `pep440` translates v0.1.1-acpt1 → 0.1.1a1 (and prod passthrough).
  4. `current` output for prod / acpt / dev branches.
  5. Dirty-tree behaviour without `--yes`.
  6. `--dry-run` mode emits "[dry-run]" and creates no tag.
  7. (Bonus) syntax check + cut-acpt happy path with --yes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.sh"


# ──────────────────────────────────────────────────────────────────────
# Repo fixture: fresh git repo per test, with release.sh pre-copied in.
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def repo(tmp_path):
    """Create a fresh git repo in tmp_path with one initial commit on
    branch `trunk`. Returns the repo Path. release.sh is copied in so
    tests don't depend on the developer's working tree."""
    r = tmp_path / "repo"
    r.mkdir()

    def run(*args, check=True):
        return subprocess.run(
            args, cwd=r, check=check, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    run("git", "init", "-q", "-b", "trunk")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (r / "README").write_text("hi\n")
    run("git", "add", "README")
    run("git", "commit", "-q", "-m", "initial")

    # Copy release.sh into the test repo's scripts/ so PATH-relative
    # operation works.
    (r / "scripts").mkdir()
    dst = r / "scripts" / "release.sh"
    shutil.copy(SCRIPT, dst)
    dst.chmod(0o755)
    return r


def _run(repo: Path, *args, check: bool = False) -> subprocess.CompletedProcess:
    """Invoke release.sh inside repo with the given args."""
    return subprocess.run(
        ["bash", str(repo / "scripts" / "release.sh"), *args],
        cwd=repo, check=check, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _git(repo: Path, *args, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


# ──────────────────────────────────────────────────────────────────────
# 0. Syntax check (cheap canary).
# ──────────────────────────────────────────────────────────────────────
def test_release_sh_syntax_clean():
    r = subprocess.run(["bash", "-n", str(SCRIPT)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ──────────────────────────────────────────────────────────────────────
# 1. next-acpt
# ──────────────────────────────────────────────────────────────────────
class TestNextAcpt:
    def test_bootstrap_when_no_prod_tag(self, repo):
        """No prod tag yet → next-acpt is v0.1.0-acpt1."""
        r = _run(repo, "next-acpt")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "v0.1.0-acpt1"

    def test_bumps_patch_after_prod(self, repo):
        """Latest prod = v0.1.0 → next-acpt = v0.1.1-acpt1."""
        _git(repo, "tag", "v0.1.0")
        r = _run(repo, "next-acpt")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "v0.1.1-acpt1"

    def test_increments_n_when_acpt_exists(self, repo):
        """With v0.1.0 prod + v0.1.1-acpt1 / -acpt2 → next is acpt3."""
        _git(repo, "tag", "v0.1.0")
        _git(repo, "tag", "v0.1.1-acpt1")
        _git(repo, "tag", "v0.1.1-acpt2")
        r = _run(repo, "next-acpt")
        assert r.returncode == 0
        assert r.stdout.strip() == "v0.1.1-acpt3"


# ──────────────────────────────────────────────────────────────────────
# 2. cut-acpt validation + happy path
# ──────────────────────────────────────────────────────────────────────
class TestCutAcpt:
    def test_rejects_acpt_suffix_in_version_arg(self, repo):
        _git(repo, "checkout", "-q", "-b", "acpt")
        r = _run(repo, "cut-acpt", "v0.1.1-acpt1")
        assert r.returncode == 3, r.stderr  # E_VERSION_FORMAT
        assert "no suffix" in r.stderr or "must look like" in r.stderr

    def test_rejects_wrong_branch(self, repo):
        # On trunk, cut-acpt must error.
        r = _run(repo, "cut-acpt", "v0.1.1")
        assert r.returncode == 4, r.stderr  # E_BRANCH_MISMATCH
        assert "acpt" in r.stderr

    def test_rejects_version_not_greater_than_prod(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "checkout", "-q", "-b", "acpt")
        r = _run(repo, "cut-acpt", "v0.1.0", "--yes")
        assert r.returncode == 9, r.stderr  # E_VALIDATION
        assert "not greater" in r.stderr

    def test_happy_path_with_yes_creates_tag(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "checkout", "-q", "-b", "acpt")
        r = _run(repo, "cut-acpt", "v0.1.1", "--yes")
        assert r.returncode == 0, r.stderr
        # Tag should exist; should NOT have been pushed (no --push).
        tags = _git(repo, "tag", "--list").stdout.split()
        assert "v0.1.1-acpt1" in tags
        assert "(not pushed" in r.stdout


# ──────────────────────────────────────────────────────────────────────
# 3. pep440 translation
# ──────────────────────────────────────────────────────────────────────
class TestPep440:
    def test_acpt_to_alpha(self, repo):
        r = _run(repo, "pep440", "v0.1.1-acpt1")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "0.1.1a1"

    def test_higher_n(self, repo):
        r = _run(repo, "pep440", "v0.2.0-acpt7")
        assert r.returncode == 0
        assert r.stdout.strip() == "0.2.0a7"

    def test_prod_passthrough(self, repo):
        r = _run(repo, "pep440", "v0.1.0")
        assert r.returncode == 0
        assert r.stdout.strip() == "0.1.0"

    def test_bad_format_fails(self, repo):
        r = _run(repo, "pep440", "1.2.3")  # missing v prefix
        assert r.returncode == 3, r.stderr  # E_VERSION_FORMAT


# ──────────────────────────────────────────────────────────────────────
# 4. current — different branches
# ──────────────────────────────────────────────────────────────────────
class TestCurrent:
    def test_main_emits_latest_prod(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "checkout", "-q", "-b", "main")
        r = _run(repo, "current")
        assert r.returncode == 0
        assert r.stdout.strip() == "v0.1.0"

    def test_acpt_emits_latest_acpt(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "tag", "v0.1.1-acpt1")
        _git(repo, "checkout", "-q", "-b", "acpt")
        r = _run(repo, "current")
        assert r.returncode == 0
        assert r.stdout.strip() == "v0.1.1-acpt1"

    def test_trunk_emits_dev_local_version(self, repo):
        _git(repo, "tag", "v0.1.0")
        # already on trunk per fixture
        sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        r = _run(repo, "current")
        assert r.returncode == 0
        assert r.stdout.strip() == f"v0.1.0+dev{sha}"


# ──────────────────────────────────────────────────────────────────────
# 5. Dirty tree without --yes blocks tagging.
# ──────────────────────────────────────────────────────────────────────
class TestDirtyTree:
    def test_dirty_tree_without_yes_errors(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "checkout", "-q", "-b", "acpt")
        # Make tree dirty
        (repo / "dirty.txt").write_text("uncommitted\n")
        # Without --yes, must die with E_DIRTY_TREE.
        r = _run(repo, "cut-acpt", "v0.1.1")
        assert r.returncode == 5, r.stderr  # E_DIRTY_TREE
        assert "dirty" in r.stderr.lower()


# ──────────────────────────────────────────────────────────────────────
# 6. --dry-run mode skips tagging.
# ──────────────────────────────────────────────────────────────────────
class TestDryRun:
    def test_dry_run_does_not_create_tag(self, repo):
        _git(repo, "tag", "v0.1.0")
        _git(repo, "checkout", "-q", "-b", "acpt")
        r = _run(repo, "cut-acpt", "v0.1.1", "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "[dry-run]" in r.stdout
        # Tag NOT created
        tags = _git(repo, "tag", "--list").stdout.split()
        assert "v0.1.1-acpt1" not in tags
