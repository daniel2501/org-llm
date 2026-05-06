"""Path resolver — turns path-shaped tokens in a prompt into
absolute candidates.

Closes the canonical motivating bug for Phase 24: user says
"@atoz in org-llm, wiki/superset.org, fix it up". The agent
sees `wiki/superset.org` and resolves it against MCP's cwd
(=~/=), failing. This resolver extracts the token, globs its
basename across granted roots + the user's repo dirs, and
hands the absolute path to the agent before it ever calls
`read_file`.

Design: cheap, defensive, capped. We'd rather miss a candidate
than block the user's turn.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import ResolvedFact


# Tokens that look like file paths. We deliberately accept both
# slashed and bare-extension forms — agents quote both.
#
#   wiki/superset.org   → slashed
#   superset.org        → bare with known extension
#   docs/wiki/x.org     → slashed multi-segment
#
# We do NOT match plain words even if they happen to be filenames
# without extensions; too noisy.
_PATH_EXTS = (
    "org", "py", "md", "txt", "json", "toml", "yaml", "yml",
    "sh", "fish", "el", "ts", "tsx", "js", "html", "css", "sql",
)
_EXT_GROUP = "|".join(_PATH_EXTS)
# Word chars + slashes + dots + dashes; ends in a recognised
# extension. Permissive on the prefix, strict on the suffix.
_PATH_TOKEN_RE = re.compile(
    rf"(?<![\w/.-])"             # boundary
    rf"([\w./-]*?[\w-]+\.(?:{_EXT_GROUP}))"
    rf"(?![\w/.-])"
)

# Per-token cap: we never emit more than this many candidates
# for one token, so a globby basename can't blow up the prompt.
_MAX_CANDIDATES_PER_TOKEN = 5

# Per-call cap: total tokens processed per (prompt, context).
# Prevents quadratic blow-up if someone pastes a large context.
_MAX_TOKENS = 8


def resolve_paths(prompt: str, context: str = "") -> list[ResolvedFact]:
    """Extract path-shaped tokens; return absolute candidates."""
    text = f"{prompt}\n{context}".strip()
    if not text:
        return []
    tokens = _extract_tokens(text)
    if not tokens:
        return []
    roots = _search_roots()
    if not roots:
        return []
    out: list[ResolvedFact] = []
    seen: set[tuple[str, str]] = set()
    for tok in tokens:
        # Two-pass search: cheap probes first; only fall through
        # to rglob if no exact-tail match exists. The vast
        # majority of well-formed prompts ("wiki/superset.org",
        # "docs/wiki/x.org") get answered by pass 1 in microseconds.
        candidates = _probe_token(tok, roots)
        if not candidates:
            candidates = _glob_basename(tok, roots)
        for cand in candidates:
            key = (tok, str(cand))
            if key in seen:
                continue
            seen.add(key)
            out.append(ResolvedFact(
                label=f"path {tok!r}",
                value=str(cand),
                evidence=_evidence_for(tok, cand),
                source="path",
            ))
    return out


def _extract_tokens(text: str) -> list[str]:
    raw = _PATH_TOKEN_RE.findall(text)
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        # Strip surrounding punctuation we sometimes catch.
        t = t.strip(".,;:!?\"'`()[]{}")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= _MAX_TOKENS:
            break
    return out


def _probe_token(token: str, roots: list[Path]) -> list[Path]:
    """Pass 1: cheap exact-relative probes.

    For each root, check `<root>/<token>` and a few common
    nested-directory conventions (`docs/`, `org_llm/`, `src/`).
    All `.is_file()` checks — no glob, no recursion. Microseconds
    per root even on big trees.
    """
    found: list[Path] = []
    nested_prefixes: tuple[str, ...] = ("", "docs", "org_llm", "src")
    for root in roots:
        for prefix in nested_prefixes:
            candidate = (root / prefix / token) if prefix else (root / token)
            try:
                if candidate.is_file():
                    rp = candidate.resolve()
                    if rp not in found:
                        found.append(rp)
                        if len(found) >= _MAX_CANDIDATES_PER_TOKEN:
                            return found
            except OSError:
                continue
    return found


def _glob_basename(token: str, roots: list[Path]) -> list[Path]:
    """Pass 2: basename glob across all roots.

    Only run when probe found nothing. Slower (rglob walks the
    tree) but bounded by the per-token candidate cap and by the
    overall resolve_all wall-clock budget.
    """
    found: list[Path] = []
    base = os.path.basename(token)
    if not base:
        return []
    for root in roots:
        try:
            for p in root.rglob(base):
                try:
                    if p.is_file():
                        rp = p.resolve()
                        if rp not in found:
                            found.append(rp)
                            if len(found) >= _MAX_CANDIDATES_PER_TOKEN:
                                return found
                except OSError:
                    continue
        except OSError:
            continue
    return found


def _evidence_for(token: str, candidate: Path) -> str:
    if "/" in token and str(candidate).endswith(token):
        return "tail match under root"
    return "basename match across roots"


_ROOTS_CACHE: tuple[Path, ...] | None = None
_ROOTS_CACHE_KEY: tuple[str, str, str] | None = None


def _search_roots() -> list[Path]:
    """The set of directories where we glob. Order matters
    (highest-confidence first), since we cap per-token results.

    Today's set:
      1. The MCP server's cwd (where the user is most likely
         to mean a relative path)
      2. The user's `~/repos/<known-repos>/` directories
      3. The MCP allow-list (anything explicitly granted)
      4. The org vault `~/org/`

    Cached against (cwd, $HOME, $ORG_LLM_ORG_DIR) — repos rarely
    appear/disappear mid-session and the access allowlist is
    stable across delegate calls. Cache invalidates if the user
    `cd`s into a different repo or relaunches with a new env.

    Phase 25's DB-authoritative inversion will collapse this
    into a DB lookup; today it's just `os.scandir` + the
    existing access allowlist.
    """
    global _ROOTS_CACHE, _ROOTS_CACHE_KEY
    cwd = os.getcwd()
    home = os.environ.get("HOME", "")
    org_dir = os.environ.get("ORG_LLM_ORG_DIR", "")
    cache_key = (cwd, home, org_dir)
    if _ROOTS_CACHE is not None and _ROOTS_CACHE_KEY == cache_key:
        return list(_ROOTS_CACHE)

    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path | None) -> None:
        if p is None:
            return
        try:
            rp = p.expanduser().resolve()
        except OSError:
            return
        if rp in seen or not rp.exists():
            return
        seen.add(rp)
        roots.append(rp)

    # 1. cwd (MCP server's launch dir)
    try:
        _add(Path(cwd))
    except OSError:
        pass

    # 2. ~/repos/<*> with .git
    repos_root = Path.home() / "repos"
    if repos_root.is_dir():
        try:
            for entry in repos_root.iterdir():
                if entry.is_dir() and (entry / ".git").exists():
                    _add(entry)
        except OSError:
            pass

    # 3. Allow-list (covers any user-granted location). DB read
    #    is the slow part of this function — caching the whole
    #    root set sidesteps it on subsequent calls.
    try:
        from ..access import allowlist
        for p in allowlist():
            _add(p)
    except Exception:
        pass

    # 4. Org vault
    if org_dir:
        _add(Path(org_dir))
    else:
        _add(Path.home() / "org")

    _ROOTS_CACHE = tuple(roots)
    _ROOTS_CACHE_KEY = cache_key
    return roots


def _reset_cache_for_tests() -> None:
    """Tests poke this between fixtures so cwd/HOME changes are
    honoured. Not called from runtime."""
    global _ROOTS_CACHE, _ROOTS_CACHE_KEY
    _ROOTS_CACHE = None
    _ROOTS_CACHE_KEY = None
# end
