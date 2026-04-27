# [[file:../../../org/20260425230731-org_llm.org::*cli.py][cli.py:1]]
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from typer.core import TyperGroup

from .db import DB_PATH, MODEL_DEFAULTS, get_session, init_db, make_engine
from .indexer import index_directory
from .ui import TREK_MSGS, console, hail, impulse, make_it_so, on_screen, red_alert, thinking, warp


# ── Shortest-unique-prefix command resolution ────────────────────────────────
# `org-llm do` → `doctor`, `org-llm rev` → `review-emacs`, etc.
# Ambiguous prefixes (e.g. `s` matching search/skill/skills/skill-new/…) fail
# with an explicit list of candidates instead of "No such command".

class PrefixGroup(TyperGroup):
    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        matches = sorted(n for n in self.list_commands(ctx) if n.startswith(cmd_name))
        if len(matches) == 1:
            return super().get_command(ctx, matches[0])
        if len(matches) > 1:
            ctx.fail(
                f"Ambiguous prefix {cmd_name!r}: matches {matches}. "
                "Add more characters to disambiguate."
            )
        return None


app = typer.Typer(
    help="org-llm: LLM-powered org-roam CLI",
    rich_markup_mode="rich",
    cls=PrefixGroup,
)


# ── Env var taps ──────────────────────────────────────────────────────────────
# Documented in the `env` tutor step. Order: env > config table > built-in default.

def _env_or_cfg(session, env_var: str, key: str, default: str = "") -> str:
    return os.environ.get(env_var) or _cfg(session, key) or default


def _engine():
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    return make_engine(path)


def _cfg(session, key: str) -> str:
    from .db import Config
    row = session.get(Config, key)
    return row.value if row else ""


def _ollama_url(session) -> str:
    return (os.environ.get("ORG_LLM_OLLAMA_URL")
            or _cfg(session, "ollama_url")
            or "http://localhost:11434")


# Substrings that identify an embedding-only model — these don't support /api/chat.
_EMBED_MODEL_SUBSTRINGS = (
    "embed",          # nomic-embed-text, mxbai-embed-large, snowflake-arctic-embed
    "bge-",           # bge-m3, bge-large
    "all-minilm",     # all-minilm-l6-v2
    "paraphrase-",    # paraphrase-multilingual-mpnet
)


def _is_embed_model(name: str) -> bool:
    n = name.lower()
    return any(sub in n for sub in _EMBED_MODEL_SUBSTRINGS)


def _normalize_tag(name: str) -> str:
    """Canonicalize an Ollama model tag for comparison.

    Ollama returns tags like 'llama3.3:latest', 'nomic-embed-text:latest',
    'qwen2.5-coder:7b'. The catalog often has stems like 'llama3.3' (no tag,
    meaning :latest) or specific variants like 'llama3.3:70b'. Naive set
    comparison breaks because ':latest' is implicit. Drop ':latest' and
    return everything lowercased so 'llama3.3:latest' == 'llama3.3'.
    """
    n = (name or "").strip().lower()
    if n.endswith(":latest"):
        n = n[: -len(":latest")]
    return n


def _ollama_has(model: str, base_url: str) -> bool:
    """Return True if Ollama already has EXACTLY this tag pulled.

    Strict match (after :latest normalization). Stem-matching here was a
    bug: the user's `llama3.2` (which Ollama resolves to `llama3.2:latest`)
    would falsely succeed when only `llama3.2:1b` was pulled, then chat()
    would 404 at runtime. Use _is_pulled() for fuzzy "do we have anything
    in this family" questions instead.
    """
    try:
        from .llm import list_models
        pulled = list_models(base_url)
    except Exception:
        return False
    target = _normalize_tag(model)
    return any(_normalize_tag(m) == target for m in pulled)


def _pulled_normalized(base_url: str) -> set[str]:
    """Return the set of normalized pulled model tags."""
    try:
        from .llm import list_models
        return {_normalize_tag(m) for m in list_models(base_url)}
    except Exception:
        return set()


def _is_pulled(model: str, pulled_norm: set[str]) -> bool:
    """Check if a (possibly stem-only) model name is satisfied by any pulled tag."""
    if not model:
        return False
    target = _normalize_tag(model)
    if target in pulled_norm:
        return True
    target_stem = target.split(":")[0]
    return any(p.split(":")[0] == target_stem for p in pulled_norm)


def _ollama_pull(model: str) -> bool:
    """Pull a model via the local ollama binary, streaming progress.

    Returns True on success. Streaming output goes straight to the user's
    terminal so they see Ollama's own progress bars (these are nicer than
    anything we can render via Rich without a streaming HTTP client).
    """
    import shutil, subprocess
    ollama = shutil.which("ollama") or str(Path("~/.local/bin/ollama").expanduser())
    if not Path(ollama).exists():
        red_alert("ollama binary not found — run: org-llm install-tools --skip-models --skip-fonts")
        return False
    hail(f"Pulling [bold]{model}[/bold] via Ollama…")
    return subprocess.run([ollama, "pull", model]).returncode == 0


def _cloud_chat_with_local_fallback(
    prompt: str, *, cloud_model: str, cloud_endpoint: str, cloud_api_key: str,
    local_model: str, local_url: str, system: str = "",
    fallback_on: tuple[str, ...] = ("rate_limit", "auth_error", "server_error"),
) -> str:
    """Try cloud_chat; on classifiable failure, transparently fall back to local.

    Surfaces a single yellow line ("Cloud {provider} hit {outcome}; falling
    back to local {local_model}.") so the user knows they're getting the
    weaker model. Re-raises any exception class it doesn't know how to
    classify so users still see real bugs.
    """
    from .cloud import cloud_chat, _classify_error
    from .llm   import chat as _local_chat
    try:
        return cloud_chat(prompt, model=cloud_model,
                           endpoint_url=cloud_endpoint, api_key=cloud_api_key,
                           system=system)
    except Exception as e:
        outcome, _ = _classify_error(e)
        if outcome not in fallback_on:
            raise
        on_screen(f"[yellow]Cloud failed ({outcome}); falling back to "
                  f"local {local_model}.[/yellow]")
        try:
            return _local_chat(prompt, model=local_model,
                                base_url=local_url, system=system)
        except Exception:
            # If local fallback ALSO fails, re-raise the original cloud
            # error so the user sees the more informative message.
            raise e


def _suggest_model_tag(bad_tag: str, base_url: str) -> str | None:
    """Fuzzy-match a typo'd model tag against pulled-locally + catalog."""
    import difflib as _dl
    candidates: set[str] = set()
    try:
        from .llm import list_models
        for m in list_models(base_url) or []:
            n = m.get("name") if isinstance(m, dict) else getattr(m, "name", None)
            if n:
                candidates.add(n)
                # Also add the stem (without :tag suffix) for closer matching
                candidates.add(n.split(":")[0])
    except Exception:
        pass
    try:
        from .models import CATALOG
        for entry in CATALOG:
            candidates.add(entry.tag)
            candidates.add(entry.tag.split(":")[0])
    except Exception:
        pass
    if not candidates:
        return None
    matches = _dl.get_close_matches(bad_tag, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _ensure_model_pulled(model: str, base_url: str, _try_llm_fix: bool = True) -> bool:
    """Idempotent: make sure `model` is locally available, pulling if needed.

    Recovery chain when the pull fails:
      1. Deterministic fuzzy-match against pulled-locally + catalog.
      2. LLM assisted-fix (the existing SRE pathway).
    """
    if not model:
        return False
    if _ollama_has(model, base_url):
        return True
    if _ollama_pull(model) and _ollama_has(model, base_url):
        return True

    # Layer 1: fuzzy-match before reaching for the LLM.
    guess = _suggest_model_tag(model, base_url)
    if guess and guess != model:
        on_screen(f"[yellow]Model tag {model!r} not found; "
                  f"trying close match {guess!r}.[/yellow]")
        if _ollama_has(guess, base_url) or (
                _ollama_pull(guess) and _ollama_has(guess, base_url)):
            on_screen(f"[dim]Auto-substituted: {model!r} → {guess!r} "
                      f"(use `org-llm config <role>_model {guess}` to persist)[/dim]")
            return True

    if _try_llm_fix and _llm_assisted_fix(
        error=f"Failed to pull or load Ollama model {model!r}",
        attempted_command=f"_ensure_model_pulled({model!r})",
        context="The user may have configured a non-existent model tag.",
    ):
        return _ollama_has(model, base_url) or _ensure_model_pulled(
            model, base_url, _try_llm_fix=False)
    return False


# ── Self-healing helpers ─────────────────────────────────────────────────────
#
# Anywhere a failure has a deterministic, safe remediation we run it ourselves
# instead of asking the user to. Three rules:
#   1. Recovery must be safe (idempotent / no destructive writes).
#   2. The user sees a one-line note that we self-healed (auditability).
#   3. If the recovery itself fails, fall back to a friendly error.

def _auto_init_db_if_needed(silent: bool = False) -> bool:
    """Initialize the SQLite DB if it doesn't exist yet. Idempotent.

    Returns True iff init was actually performed (False if already there).
    Used by every command that needs config rows so 'no such table: config'
    becomes a self-heal instead of a red alert.
    """
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if path.exists():
        # Quick probe: if the path exists but tables don't, init is still safe
        try:
            from sqlalchemy import inspect as _inspect
            engine = make_engine(path)
            tables = set(_inspect(engine).get_table_names())
            if "config" in tables:
                return False
        except Exception:
            pass
    try:
        engine = make_engine(path)
        init_db(engine)
        if not silent:
            hail(f"Auto-initialised database at {path}")
        return True
    except Exception:
        return False


def _auto_start_ollama_if_needed(base_url: str, silent: bool = False) -> bool:
    """Try to start `ollama serve` if it's not reachable. Returns True on success."""
    try:
        from .llm import list_models
        list_models(base_url)
        return True   # already up
    except Exception:
        pass
    import shutil, subprocess
    ollama = shutil.which("ollama") or str(Path("~/.local/bin/ollama").expanduser())
    if not Path(ollama).exists():
        return False
    if not silent:
        hail("Ollama isn't running — auto-starting in the background…")
    try:
        subprocess.Popen([ollama, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        import time
        for _ in range(8):   # up to ~4 s
            time.sleep(0.5)
            try:
                from .llm import list_models
                list_models(base_url)
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


# Allow-list of org-llm subcommands the LLM may auto-invoke as fixes.
# Critically excludes destructive verbs: tag --apply (writes to org files),
# capture (writes), revoke (removes grants), skill (RCE), claude/launch
# (interactive workspaces), install (heavy side effects).
_LLM_FIXABLE_VERBS = (
    "config", "models", "doctor", "embed", "index", "code-index",
    "knob", "grant", "grant-root", "grant-browser",
    "performance", "theme", "discover", "context", "stale",
)


def _llm_assisted_fix(error: str, attempted_command: str,
                       context: str = "", _depth: int = 0) -> bool:
    """Ask the configured cloud LLM for a remediation when deterministic
    auto-fix isn't possible. Executes the suggestion only if it's an
    allow-listed `org-llm` subcommand. Logs loudly so the user sees what
    we did.

    Returns True iff a fix was successfully run (caller should retry the
    original operation). Guarded against runaway recursion: depth ≤ 1.
    """
    if _depth >= 1:
        return False  # don't recursively self-fix the auto-fixer
    try:
        engine = _engine()
        with get_session(engine) as session:
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            # Prefer fixer_model (benchmarked best at remediation) over the
            # generic cloud_model (which the user likely picked for chat
            # quality, not JSON-following).
            cloud_model    = (_cfg(session, "fixer_model")
                              or _cfg(session, "cloud_model")
                              or "openai/gpt-oss-20b:free")
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
    except Exception:
        return False
    if not cloud_endpoint:
        return False  # No cloud configured; can't ask the LLM

    from . import creds as _creds
    api_key = (_creds.read_secret(_creds.cloud_slug(cloud_provider))
               if cloud_provider else None) or db_api_key

    sys_prompt = (
        "You are an SRE assistant for a CLI tool called `org-llm`. The user "
        "just hit an error. Reply with STRICT JSON, no prose, no markdown fences:\n\n"
        '  {"action": "run", "argv": ["doctor", "--fix"], "reason": "…"}\n\n'
        'OR {"action": "skip", "reason": "explain why no auto-fix is safe"}\n\n'
        "Allowed verbs (argv[0] MUST be one of these):\n"
        "  " + ", ".join(_LLM_FIXABLE_VERBS) + "\n\n"
        "Decision rules:\n"
        "  • The attempted command JUST FAILED. NEVER suggest re-running the\n"
        "    same operation; that loops. Pick a DIFFERENT remediation.\n"
        "  • 'pull model manifest: file does not exist' → the model tag is\n"
        "    bogus. Suggest [\"config\", \"<role>_model\", \"llama3.2\"] to\n"
        "    swap to a known-good 2 GB model. Don't try to pull the bogus tag.\n"
        "  • 'system memory ... than is available' → suggest\n"
        "    [\"performance\", \"--apply\"] to auto-pick a fitting model.\n"
        "  • 'no such table: config' → suggest [\"doctor\", \"--fix\"].\n"
        "  • 'connection refused' / 'Connection error' → [\"doctor\", \"--fix\"].\n"
        "  • Empty index / no nodes → [\"index\"] then [\"embed\"]; suggest\n"
        "    just [\"index\"] (embed will run automatically afterward).\n"
        "  • Configured model not in our catalog (qwen99, llama99, etc.) →\n"
        "    swap it via config. Common-good defaults: chat_model=llama3.2,\n"
        "    embed_model=nomic-embed-text, code_model=qwen2.5-coder:7b,\n"
        "    fast_model=phi3.5, reason_model=deepseek-r1:7b.\n"
        "  • If the error involves user creds / API keys / sensitive paths,\n"
        "    return action=skip — those need user input.\n"
        "  • When unsure, action=skip with one sentence of reasoning.\n"
    )
    user_prompt = (
        f"Failed command: {attempted_command}\n\n"
        f"Error message:\n{error[:1500]}\n\n"
        + (f"Additional context:\n{context[:500]}\n" if context else "")
    )

    try:
        from .cloud import cloud_chat
        reply = cloud_chat(user_prompt, model=cloud_model,
                           endpoint_url=cloud_endpoint,
                           api_key=api_key, system=sys_prompt)
    except Exception:
        return False

    # Strip any markdown fences the LLM emitted despite the rules
    cleaned = _strip_code_fences(reply, "json").strip()
    import json as _json
    try:
        plan = _json.loads(cleaned)
    except Exception:
        # The LLM gave prose; surface the suggestion but don't execute
        on_screen(f"[dim]LLM suggested:[/dim] {reply[:300]}")
        return False

    if plan.get("action") != "run":
        on_screen(f"[dim]LLM declined to auto-fix:[/dim] "
                  f"{plan.get('reason', '(no reason given)')}")
        return False

    argv = plan.get("argv") or []
    if not isinstance(argv, list) or not argv:
        return False
    verb = str(argv[0])
    if verb not in _LLM_FIXABLE_VERBS:
        on_screen(f"[yellow]LLM suggested unsafe verb {verb!r}; ignoring.[/yellow]")
        return False

    hail(f"LLM auto-fix: org-llm {' '.join(str(x) for x in argv)}")
    if plan.get("reason"):
        on_screen(f"  [dim]reason: {plan['reason']}[/dim]")

    import subprocess
    invoke = [sys.argv[0]] if sys.argv and os.path.isabs(sys.argv[0]) \
             else ["uv", "run", "org-llm"]
    try:
        result = subprocess.run(
            invoke + [str(x) for x in argv],
            timeout=180,
        )
        return result.returncode == 0
    except Exception:
        return False


def _auto_index_if_empty(silent: bool = False) -> bool:
    """If the index is empty but org_dir has .org files, run `index` automatically."""
    try:
        from .db import File, Node
        engine = _engine()
        with get_session(engine) as session:
            n_nodes = session.query(Node).count()
            org_dir = _org_dir(session)
        if n_nodes > 0:
            return False
        if not org_dir.exists():
            return False
        org_files = list(org_dir.rglob("*.org"))
        if not org_files:
            return False
        if not silent:
            hail(f"Index is empty — auto-indexing {len(org_files)} .org files from {org_dir}…")
        with warp(TREK_MSGS["index"] + f": {org_dir}"):
            with get_session(engine) as session:
                index_directory(org_dir, session)
        return True
    except Exception:
        return False


def _model_fits_locally(model: str) -> tuple[bool, float, float]:
    """Return (fits?, model_vram_estimate_gb, ram_free_gb).

    Estimates the model's VRAM requirement from the catalog (stem-matched).
    `fits` is True iff the estimated requirement is ≤ free RAM with a small
    overhead buffer, OR we don't have a catalog estimate (assume yes; let
    Ollama tell us no later).
    """
    try:
        from .models import _vram_for_tag
        from .performance import probe_hardware
        needed = _vram_for_tag(model)
        hw = probe_hardware()
        free = hw.vram_free_gb if hw.vram_free_gb is not None else hw.ram_free_gb
        if needed <= 0:
            return (True, 0.0, free)        # unknown — let it try
        return (needed <= free + 0.5, needed, free)
    except Exception:
        return (True, 0.0, 0.0)


def _local_chat_or_friendly_error(prompt: str, model: str, base_url: str,
                                    system: str = "", cloud_hint: str = "") -> str:
    """Wrap a local Ollama chat call with the standard pre-flight + OOM
    handling so we never dump a raw traceback on a memory exhaustion.

    Self-healing for the recoverable failure modes:
      • Model not found (404)  → auto-pull the exact tag, retry once
      • Connection refused     → ollama serve isn't running; suggest doctor --fix
      • Memory exhaustion      → can't fix; suggest --cloud / smaller model

    Returns the chat response, or calls red_alert + raises typer.Exit(1).
    """
    fits, needed, free = _model_fits_locally(model)
    if not fits:
        red_alert(f"{model} needs ~{needed:.1f} GB but only {free:.1f} GB is free.")
        on_screen("Three ways to recover:")
        on_screen(f"  • [bold]{cloud_hint or 'org-llm ask --cloud'}[/bold]"
                  "  ← route this call to OpenRouter")
        on_screen("  • [bold]org-llm performance --apply[/bold]"
                  "  ← auto-pick a model that fits")
        on_screen("  • [bold]org-llm config chat_model llama3.2[/bold]"
                  "  ← swap in a 2 GB model")
        raise typer.Exit(1)

    from .llm import chat as local_chat

    def _is_oom(msg: str) -> bool:
        m = msg.lower()
        return ("system memory" in m or "out of memory" in m or "oom" in m
                or ("memory" in m and "available" in m))

    def _is_not_found(msg: str) -> bool:
        m = msg.lower()
        return ("not found" in m or "404" in m
                or "no such model" in m or "manifest" in m and "not" in m)

    def _is_conn(msg: str) -> bool:
        m = msg.lower()
        return "connect" in m or "refused" in m or "actively refused" in m

    try:
        return local_chat(prompt, model=model, base_url=base_url, system=system)
    except Exception as exc:
        msg = str(exc)

        # Recoverable: model isn't pulled. Pull it and retry once.
        if _is_not_found(msg):
            hail(f"{model!r} isn't pulled. Pulling now…")
            if _ollama_pull(model):
                try:
                    return local_chat(prompt, model=model, base_url=base_url, system=system)
                except Exception as exc2:
                    msg = str(exc2)  # fall through to the failure paths below
                    # Re-evaluate: maybe the pull resolved to a model that's too big
                    if _is_oom(msg):
                        # Treat as OOM; let the OOM branch run
                        pass
                    elif _is_not_found(msg):
                        red_alert(f"Pulled {model!r} but Ollama still can't load it: {msg[:200]}")
                        raise typer.Exit(1)
                    else:
                        red_alert(f"Retry after pull failed: {msg[:200]}")
                        raise typer.Exit(1)
            else:
                red_alert(f"Could not pull {model!r}.")
                on_screen(f"  • [bold]{cloud_hint or 'org-llm ask --cloud'}[/bold]"
                          "  ← skip the local pull and use the cloud")
                on_screen(f"  • [bold]ollama pull {model}[/bold]"
                          "  ← try the pull manually for the actual error")
                raise typer.Exit(1)

        # Memory exhaustion (initial call OR retry-after-pull)
        if _is_oom(msg):
            red_alert(f"{model} ran out of memory at runtime: {msg[:120]}")
            on_screen(f"  • [bold]{cloud_hint or 'org-llm ask --cloud'}[/bold]"
                      "  ← retry via OpenRouter")
            on_screen("  • [bold]org-llm performance --benchmark --apply[/bold]"
                      "  ← measure and pick a fitting model")
            raise typer.Exit(1)

        if _is_conn(msg):
            red_alert("Ollama isn't reachable. Auto-fixing…")
            # doctor --fix knows how to start ollama serve. Run it.
            import subprocess
            subprocess.run(
                [sys.argv[0] if sys.argv else "org-llm", "doctor", "--fix"],
                capture_output=True, timeout=30,
            )
            try:
                return local_chat(prompt, model=model, base_url=base_url, system=system)
            except Exception as exc3:
                red_alert(f"Ollama still unreachable after auto-fix: {exc3}")
                on_screen(f"  • [bold]{cloud_hint or 'org-llm ask --cloud'}[/bold]")
                raise typer.Exit(1)

        # Last resort: ask the LLM for a fix
        if _llm_assisted_fix(msg, f"local chat with model {model!r}",
                              context=f"prompt length={len(prompt)} chars"):
            try:
                return local_chat(prompt, model=model, base_url=base_url, system=system)
            except Exception as exc4:
                red_alert(f"LLM-suggested fix ran but chat still failed: {exc4}")
                raise typer.Exit(1)
        red_alert(f"Local chat failed: {msg[:200]}")
        raise typer.Exit(1)


# ── Temporal-phrase auto-detection for `ask` ────────────────────────────────

_TIME_PHRASES = [
    # (regex, days)
    (r"\byesterday\b",                              1),
    (r"\btoday\b",                                  1),
    (r"\bthis week\b",                              7),
    (r"\blast week\b",                              7),
    (r"\bpast week\b",                              7),
    (r"\bthis month\b",                            30),
    (r"\blast month\b",                            30),
    (r"\bpast month\b",                            30),
    (r"\bthis quarter\b",                          90),
    (r"\blast quarter\b",                          90),
    (r"\bthis year\b",                            365),
    (r"\blast year\b",                            365),
    (r"\brecent(ly)?\b",                           14),
    (r"\blately\b",                                14),
]


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


_TAG_STOPWORDS = {
    "a", "an", "the", "my", "any", "some", "this", "that", "tag", "tags",
    "or", "and", "of", "for", "to", "from", "in", "with", "at", "on",
    "all", "every", "specific", "particular", "given", "above",
}


def _parse_tag_hints(query: str) -> list[str]:
    """Extract candidate tag names from a free-form question.

    Recognises patterns like:
      - "my politics tag"   / "the tech tag"   / "politics tag"
      - "tagged politics"   / "tagged with X"
      - ":lit:" / ":queer:" (literal org-mode tag syntax)

    Returns a deduplicated list (in first-seen order). Whether each
    candidate corresponds to a real tag is a separate validation step;
    that's done by intersecting with `existing_tags(session)`.
    """
    if not query:
        return []
    import re
    q = query.lower()
    candidates: list[str] = []

    def _add(t: str) -> None:
        t = t.strip(":-_").strip()
        if t and t not in _TAG_STOPWORDS and t not in candidates:
            candidates.append(t)

    # "my X tag" / "the X tag" / "X tag"
    for m in re.finditer(r"\b(?:my|the|a|an)?\s*([a-z][a-z0-9_-]{1,40})\s+tags?\b", q):
        _add(m.group(1))
    # "tagged X" / "tagged with X"
    for m in re.finditer(r"\btagged(?:\s+with)?\s+([a-z][a-z0-9_-]{1,40})\b", q):
        _add(m.group(1))
    # Literal org-mode tag syntax ":foo:"
    for m in re.finditer(r":([a-z][a-z0-9_-]{1,40}):", q):
        _add(m.group(1))
    return candidates


def _did_you_mean(target: str, candidates, n: int = 3) -> list[str]:
    """Return up to n closest tags to a misspelled target via difflib."""
    import difflib
    return difflib.get_close_matches(target.lower(), list(candidates), n=n, cutoff=0.5)


def _parse_days_window(query: str) -> int | None:
    """Extract a "last N days/weeks/months/years" intent from a free-form question.

    Returns the number of days to look back, or None if no temporal phrase
    is present. Numeric phrases like "last 30 days" / "past 6 months" win
    over keyword phrases like "last week".
    """
    if not query:
        return None
    import re
    q = query.lower()

    # Spelled-out numbers: "last six months" → "last 6 months"
    for word, n in _NUMBER_WORDS.items():
        q = re.sub(rf"\b(last|past|previous)\s+{word}\b", rf"\1 {n}", q)

    # Numeric units: "last 30 days", "past 6 months", "previous 2 years", "in the last 4 weeks"
    units = {"day": 1, "days": 1, "week": 7, "weeks": 7,
             "month": 30, "months": 30, "year": 365, "years": 365}
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,4})\s+(\w+)\b", q)
    if m and m.group(2) in units:
        return int(m.group(1)) * units[m.group(2)]
    m = re.search(r"\b(\d{1,4})\s+(\w+)\s+ago\b", q)
    if m and m.group(2) in units:
        return int(m.group(1)) * units[m.group(2)]

    # Keyword phrases
    for pattern, days in _TIME_PHRASES:
        if re.search(pattern, q):
            return days
    return None


def _org_dir(session) -> Path:
    return Path(os.environ.get("ORG_LLM_ORG_DIR")
                or _cfg(session, "org_dir") or "~/org").expanduser()


def _safe_org_path(org_dir: Path, user_path: str) -> Path:
    """Resolve user-provided relative path inside org_dir; refuse traversal.

    Raises typer.Exit(1) with a red_alert if the resolved path escapes org_dir
    (e.g. `--file ../../../tmp/exfil.org`). Symlinks are followed and the
    final target is checked against the resolved org_dir.
    """
    org_dir_r = org_dir.expanduser().resolve()
    # Use parent.resolve() / name pattern so we don't require the file to exist
    candidate = (org_dir / user_path).expanduser()
    parent = candidate.parent.resolve() if candidate.parent.exists() else candidate.parent
    target = (parent / candidate.name)
    try:
        # Forces a clean comparison even when path doesn't exist yet
        Path(os.path.normpath(str(target))).relative_to(org_dir_r)
    except ValueError:
        red_alert(f"Refusing to write outside org_dir: {target}")
        on_screen(f"  org_dir: {org_dir_r}")
        on_screen(f"  user --file: {user_path!r}")
        raise typer.Exit(1)
    return target


@app.command(rich_help_panel="Onboarding")
def init():
    """Initialize database and write default config."""
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    is_new = not path.exists()
    with warp(TREK_MSGS["init"]):
        engine = make_engine(path)
        init_db(engine)
    hail(f"Database ready at {path}")
    if is_new:
        # Probe org_dir to tailor the next-step suggestion to actual content.
        try:
            engine_now = make_engine(path)
            with get_session(engine_now) as session:
                org_dir = _org_dir(session)
            n_org = sum(1 for _ in org_dir.rglob("*.org")) if org_dir.exists() else 0
        except Exception:
            n_org = 0
        if n_org > 0:
            on_screen(f"Next: [bold]org-llm index[/bold]  — found {n_org} .org files in {org_dir}")
            on_screen("Then: [bold]org-llm embed[/bold]   then  [bold]org-llm ask \"…\"[/bold]")
            on_screen("Personalise the UI: [bold]org-llm personalize --apply[/bold]")
        else:
            on_screen(f"Next: drop some .org files into [bold]{org_dir}[/bold] then run [bold]org-llm index[/bold]")
        on_screen("Tour: [bold]org-llm tutor welcome[/bold]")
    else:
        on_screen("(Existing DB detected — config rows preserved)")
    make_it_so()


@app.command(rich_help_panel="Onboarding")
def setup(
    yes:        Annotated[bool, typer.Option("--yes", "-y",
                help="Skip confirmations; pick reasonable defaults")] = False,
    skip_models: Annotated[bool, typer.Option("--skip-models",
                 help="Don't pull or tune Ollama models")] = False,
    skip_index:  Annotated[bool, typer.Option("--skip-index",
                 help="Don't run index/embed (do it later)")] = False,
    skip_personalize: Annotated[bool, typer.Option("--skip-personalize",
                      help="Don't auto-create theme knobs")] = False,
):
    """First-run setup — chains the steps a new user needs in one command.

    Runs in order, asking for confirmation at each step:
      1. init                    — create the SQLite DB
      2. install-tools           — Ollama + opencode + models (if missing)
      3. discover                — scan filesystem for org / repo / dotfiles
      4. doctor                  — health check; surface concrete gaps
      5. install FOSS tools      — bat / ripgrep / fzf / … (only if missing)
      6. models --tune           — pick a hardware-fitting set
      7. index                   — scan your `.org` files
      8. tag --apply             — LLM auto-tags untagged notes
      9. embed                   — vectorise unembedded nodes (idempotent)
     10. personalize --apply     — auto-create theme knobs from real content
     11. context build           — LLM infers durable facts about you
     12. interview               — LLM asks 3-4 clarifying questions
     13. history build           — narrative of older + archived notes
     14. tutor welcome           — print the welcome step
     15. open the full tour      — copies it into your vault, then opens it
     16. (optional) launch opencode for a first-touch with the LLM TUI

    Pass --yes to run all steps non-interactively with reasonable
    defaults. Each step is idempotent and safe to skip / re-run later
    via the corresponding subcommand.

    Ctrl-C exits cleanly at any prompt — partial progress is preserved
    (the DB is created, any embeddings written stay written, etc).
    """
    import importlib

    def _confirm(prompt: str, default: bool = True) -> bool:
        if yes:
            return True
        try:
            return typer.confirm(prompt, default=default)
        except (KeyboardInterrupt, EOFError):
            console.print()
            on_screen("[yellow]Setup interrupted. "
                      "Re-run [bold]org-llm setup[/bold] to resume "
                      "(every step is idempotent).[/yellow]")
            raise typer.Exit(130)   # 128 + SIGINT

    def _ask(prompt: str, default: str = "") -> str:
        """Ctrl-C-safe replacement for typer.prompt."""
        if yes:
            return default
        try:
            return typer.prompt(prompt, default=default,
                                  show_default=bool(default))
        except (KeyboardInterrupt, EOFError):
            console.print()
            on_screen("[yellow]Setup interrupted. "
                      "Re-run [bold]org-llm setup[/bold] to resume.[/yellow]")
            raise typer.Exit(130)

    console.print()
    console.rule("[lcars1]org-llm setup — first-run walkthrough[/lcars1]")
    console.print()

    # LLM-personalized welcome — reads what's on disk, writes a one-line
    # greeting grounded in the user's actual environment. Silent fallback
    # when LLM is unreachable (no Ollama on a fresh box yet, e.g.).
    try:
        from .discover import discover as _disc
        _found = _disc()
        _inv = "\n".join(f"  - {f.path} [{f.kind}] {f.description}"
                          for f in _found[:6]) or "  (empty)"
        _welcome = _llm_one_liner(
            f"Filesystem inventory:\n{_inv}\n\nWelcome the user and "
            "name 1-2 specific things setup will configure based on "
            "what they have.",
            system=("Write ONE friendly sentence introducing org-llm's "
                    "first-run setup, grounded in the inventory. Output "
                    "only the sentence — no preamble, no quotes, no "
                    "markdown. 12-26 words. Concrete, no hype."),
            fallback="",
        ).strip()
        if _welcome:
            on_screen(f"[lcars3]{_welcome}[/lcars3]")
            console.print()
    except Exception:
        pass

    # 1. init — always safe
    on_screen("[lcars2]Step 1/15[/lcars2] init the database")
    init()
    console.print()

    # 2. install-tools (Ollama + opencode + …) — closing the gap where
    # setup previously assumed Ollama was already installed. Idempotent:
    # the install-tools command itself skips anything already present,
    # but we offer the prompt so a sandboxed user can opt out.
    import shutil as _shutil
    have_ollama   = bool(_shutil.which("ollama"))
    have_opencode = bool(_shutil.which("opencode"))
    if have_ollama and have_opencode:
        on_screen("[dim]Ollama + opencode already installed — skipping step 2.[/dim]")
        console.print()
    else:
        missing_core = []
        if not have_ollama:   missing_core.append("Ollama")
        if not have_opencode: missing_core.append("opencode")
        prompt2 = (f"Install core binaries ({', '.join(missing_core)})? "
                   f"[runs `org-llm install-tools` — pulls models, takes minutes]")
        if _confirm(prompt2, default=True):
            on_screen("[lcars2]Step 2/15[/lcars2] install-tools "
                      "[dim](Ollama + models + opencode — output streams below)[/dim]")
            try:
                import subprocess as _sub
                # Skip fonts / gh / claude / pass by default — those are
                # optional and the user can run install-tools later if
                # they want them.
                with warp("Installing core binaries (Ollama, models, opencode)"):
                    _sub.run(["org-llm", "install-tools",
                              "--skip-fonts", "--skip-gh",
                              "--skip-claude", "--skip-pass"])
            except Exception as e:
                on_screen(f"[dim]install-tools failed: {e}[/dim]")
            console.print()

    # 3. discover
    if _confirm("Probe the filesystem for org/repo/Emacs roots?", default=True):
        on_screen("[lcars2]Step 3/15[/lcars2] discover")
        try:
            discover()
        except Exception as e:
            on_screen(f"[dim]discover failed: {e}[/dim]")
        console.print()

    # 4. doctor (always run — read-only)
    on_screen("[lcars2]Step 4/15[/lcars2] doctor health check")
    try:
        doctor()
    except SystemExit:
        pass    # doctor exits even on success; not fatal here
    except Exception as e:
        on_screen(f"[dim]doctor failed: {e}[/dim]")
    console.print()

    # 4. install FOSS tools — show what's already there + what's missing
    # before deciding, then stream output to the user's terminal during
    # install. CliRunner.invoke() would have swallowed all of it.
    try:
        import shutil as _shutil
        from .models import TOOL_REGISTRY as _TR
        tools = list(_TR)
        installed = [t for t in tools if _shutil.which(t.name)]
        missing   = [t for t in tools if not _shutil.which(t.name)]
        if installed or missing:
            on_screen(f"[dim]FOSS tools — installed:[/dim] "
                      f"{', '.join(t.name for t in installed) or '(none)'}")
            on_screen(f"[dim]FOSS tools — missing:[/dim]   "
                      f"{', '.join(t.name for t in missing) or '(none)'}")
    except Exception:
        missing = None
    install_q = "Install missing FOSS tools"
    if missing is not None and missing:
        # Use actual missing names (first 3) — not a hardcoded list that
        # may overlap with what's already installed.
        examples = ", ".join(t.name for t in missing[:3])
        more     = "" if len(missing) <= 3 else ", …"
        install_q += f" ({len(missing)} missing — {examples}{more})?"
    elif missing is None:
        install_q += " (eza, ripgrep, bat, …)?"
    if missing == [] :
        on_screen("[dim]All FOSS tools already installed — skipping step 4.[/dim]")
        console.print()
    elif _confirm(install_q, default=False):
        on_screen("[lcars2]Step 5/15[/lcars2] install FOSS tools "
                  "[dim](streams output below — may take a few minutes)[/dim]")
        try:
            import subprocess as _sub
            with warp("Installing FOSS tools (output streams below)"):
                _sub.run(["org-llm", "doctor", "--install", "all"])
        except Exception as e:
            on_screen(f"[dim]install failed: {e}[/dim]")
        console.print()

    # 5. models --tune  — data-driven prompt
    if not skip_models:
        try:
            from .cloud import local_ram_gb, local_vram_gb
            ram  = local_ram_gb()
            vram = local_vram_gb()
            hw_str = f"{ram:.1f} GB free RAM" + (f" + {vram:.1f} GB VRAM"
                                                  if vram else "")
        except Exception:
            hw_str = "(hardware probe failed)"
        engine_now = _engine()
        with get_session(engine_now) as session:
            assigned = sum(1 for _, k, _ in _TASK_MODEL_KEYS
                            if (_cfg(session, k) or "").strip())
        models_q = (f"Pick hardware-fitting Ollama models? "
                    f"({assigned}/{len(_TASK_MODEL_KEYS)} role(s) assigned · "
                    f"hardware: {hw_str})")
        if _confirm(models_q, default=True):
            on_screen("[lcars2]Step 6/15[/lcars2] models --tune --apply "
                      "[dim](may pull models — multiple minutes)[/dim]")
            try:
                import subprocess as _sub
                with warp("Tuning models for your hardware"):
                    _sub.run(["org-llm", "models", "--tune", "--apply"])
            except Exception as e:
                on_screen(f"[dim]models --tune failed: {e}[/dim]")
            console.print()

    # 6+7. index → tag — data-driven prompt
    try:
        from .db import File as _F, Node as _N
        engine_now = _engine()
        with get_session(engine_now) as session:
            org_dir_now = _org_dir(session)
            n_org_disk = (sum(1 for _ in org_dir_now.rglob("*.org"))
                          if org_dir_now.exists() else 0)
            db_paths = {f.path for f in session.query(_F).all()}
            n_unindexed = sum(1 for p in (org_dir_now.rglob("*.org")
                                            if org_dir_now.exists() else [])
                              if str(p) not in db_paths)
            n_untagged = (session.query(_N).filter(
                (_N.tags == "") | (_N.tags.is_(None))).count())
        idx_q = (f"Index and auto-tag your org notes now? "
                 f"({n_unindexed} unindexed of {n_org_disk} on disk · "
                 f"{n_untagged} untagged node(s))")
    except Exception:
        idx_q = "Index and auto-tag your org notes now?"
    if not skip_index and _confirm(idx_q, default=True):
        on_screen("[lcars2]Step 7/15[/lcars2] index")
        try:
            index()    # has its own warp spinner
        except SystemExit:
            pass

        # Auto-tag untagged nodes — uses fast_model. Has its own impulse
        # progress bar (one tick per node).
        on_screen("[lcars2]Step 8/15[/lcars2] tag --apply (LLM auto-tags untagged notes)")
        try:
            tag(force=False, limit=200, apply=True, dry_run=False)
        except SystemExit:
            pass
        except Exception as e:
            on_screen(f"[dim]tag failed: {e}[/dim]")
        console.print()

    # 8. embed — UNCONDITIONAL. Idempotent: only embeds nodes that don't
    # have a vector yet. If everything's embedded already, the inner check
    # short-circuits cheap. We're not asking — silent partial-embeddings
    # ("96% embedded; run: org-llm embed") are exactly the friction setup
    # is meant to prevent.
    if not skip_index:
        on_screen("[lcars2]Step 9/15[/lcars2] embed (auto — fills any gaps)")
        try:
            embed()    # has its own impulse progress bar
        except SystemExit:
            pass
        except Exception as e:
            on_screen(f"[dim]embed failed: {e}[/dim]")
        console.print()

    # 9. personalize — data-driven prompt
    try:
        from .db import Node as _N2
        engine_now = _engine()
        with get_session(engine_now) as session:
            n_nodes_now = session.query(_N2).count()
        pz_q = (f"Auto-create theme knobs from your content? "
                f"({n_nodes_now} note(s) for the LLM to draw inspiration from)")
    except Exception:
        pz_q = "Auto-create theme knobs from your content?"
    if not skip_personalize and _confirm(pz_q, default=True):
        on_screen("[lcars2]Step 10/15[/lcars2] personalize --apply "
                  "[dim](LLM synthesises themes — may take 30-90s)[/dim]")
        try:
            personalize(apply=True, no_llm=False, max_themes=5,
                         overwrite=False)
        except SystemExit:
            pass
        except Exception as e:
            on_screen(f"[dim]personalize failed: {e}[/dim]")
        console.print()

    # Auto-write code_dirs from discover output (if not already set).
    try:
        from .discover import discover as _disc, suggest_code_dirs
        from .db       import Config
        engine = _engine()
        with get_session(engine) as session:
            existing = session.get(Config, "code_dirs")
        if not existing or not (existing.value or "").strip() \
                or existing.value == "~/repos":
            roots = suggest_code_dirs(_disc())
            if roots:
                with get_session(engine) as session:
                    row = session.get(Config, "code_dirs")
                    new_val = ",".join(str(r) for r in roots[:4])
                    if row:
                        row.value = new_val
                    else:
                        session.add(Config(key="code_dirs", value=new_val))
                    session.commit()
                on_screen(f"[dim]Auto-set code_dirs → {new_val}[/dim]")
    except Exception:
        pass

    # 10. learn initial facts from the user's content
    if _confirm("Let the LLM read your notes and propose initial "
                 "context facts about you?", default=True):
        on_screen("[lcars2]Step 10/11[/lcars2] infer durable facts from real content")
        try:
            from . import context as _ctx
            engine = _engine()
            with get_session(engine) as session:
                url = _ollama_url(session)
                mdl = (_cfg(session, "chat_model")
                       or _cfg(session, "fast_model")
                       or "llama3.2")
                try:
                    from .llm import list_models as _lm
                    pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                               for m in _lm(url) or []}
                    for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                        if any(p == cand or p.startswith(cand + ":") for p in pulled):
                            mdl = cand; break
                except Exception:
                    pass
                facts = _ctx.infer_initial_facts(session, model=mdl, base_url=url)
            if not facts:
                on_screen("[dim]No confident facts inferred from current content.[/dim]")
            else:
                on_screen("[lcars3]LLM inferred:[/lcars3]")
                for f in facts:
                    on_screen(f"  - {f}")
                if _confirm(f"Add these {len(facts)} fact(s) to context?",
                            default=True):
                    for f in facts:
                        _ctx.add_fact(f, source="setup-inferred")
                    hail(f"Added {len(facts)} fact(s).")
        except Exception as e:
            on_screen(f"[dim]Fact inference failed: {e}[/dim]")
        console.print()
        # Also offer a manual fact line
        if not yes:
            extra = _ask("Optional: add another fact in your own words "
                          "(blank to skip)", default="").strip()
            if extra:
                from . import context as _ctx
                _ctx.add_fact(extra, source="setup")
                hail("Added.")

    # 11.5. Interview the user — LLM reads their notes, picks 3-4
    # ambiguities, asks clarifying questions. Each answer becomes a
    # context fact. Optional but enabled by default — short process.
    if _confirm("Quick interview? The LLM will look for ambiguities in "
                 "your notes (job changes, project status, …) and ask "
                 "you 3-4 clarifying questions",
                 default=True):
        on_screen("[lcars2]Step 12/15[/lcars2] interview "
                  "[dim](LLM drafts questions from your notes)[/dim]")
        try:
            from . import context as _ctx_iv
            engine_now = _engine()
            with get_session(engine_now) as session:
                url = _ollama_url(session)
                mdl = (_cfg(session, "chat_model")
                       or _cfg(session, "fast_model")
                       or "llama3.2")
                try:
                    from .llm import list_models as _lm
                    pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                               for m in _lm(url) or []}
                    for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                        if any(p == cand or p.startswith(cand + ":") for p in pulled):
                            mdl = cand; break
                except Exception:
                    pass
                questions = _ctx_iv.interview_for_facts(
                    session, model=mdl, base_url=url, max_questions=4)
            if not questions:
                on_screen("[dim]LLM didn't find clear ambiguities to ask "
                          "about — your context is already well-defined "
                          "or the vault's still small.[/dim]")
            else:
                on_screen(f"[lcars3]The LLM has {len(questions)} "
                          f"question(s):[/lcars3]")
                console.print()
                added = 0
                for q in questions:
                    on_screen(f"  [dim]({q.get('why', '')})[/dim]")
                    answer = _ask(q["q"], default="").strip()
                    if not answer:
                        on_screen("  [dim]Skipped.[/dim]")
                        continue
                    # Compose a fact "Q :: A" — the LLM in future prompts
                    # will read both halves and infer the user's situation.
                    fact = f"{q['q']} — {answer}"
                    _ctx_iv.add_fact(fact, source="setup-interview")
                    added += 1
                if added:
                    hail(f"Added {added} interview-driven fact(s).")
        except Exception as e:
            on_screen(f"[dim]Interview failed: {e}[/dim]")
        console.print()

    # 11. history narrative — data-driven prompt
    try:
        from .db        import Node as _N3
        from . import context as _ctx
        import time as _time
        cutoff = _time.time() - 180 * 86400
        engine_now = _engine()
        with get_session(engine_now) as session:
            n_stale = session.query(_N3).filter(
                _N3.tags.like("%stale%")).count()
            n_old   = session.query(_N3).filter(
                _N3.mtime < cutoff,
                ~_N3.tags.like("%code%")).count()
        n_archive = len(_ctx._scan_archive_files())
        hist_q = (f"Build the historical-context narrative now? "
                  f"({n_stale} stale-tagged · {n_old} >180-day · "
                  f"{n_archive} archive file(s) — 1-3 minutes)")
    except Exception:
        hist_q = ("Build the historical-context narrative now? "
                   "(LLM scans old + archived notes — 1-3 minutes)")
    if _confirm(hist_q, default=True):
        on_screen("[lcars2]Step 11/12[/lcars2] history build")
        try:
            from . import context as _ctx
            engine = _engine()
            with get_session(engine) as session:
                url = _ollama_url(session)
                mdl = (_cfg(session, "chat_model")
                       or _cfg(session, "fast_model")
                       or "llama3.2")
                try:
                    from .llm import list_models as _lm
                    pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                               for m in _lm(url) or []}
                    for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                        if any(p == cand or p.startswith(cand + ":") for p in pulled):
                            mdl = cand; break
                except Exception:
                    pass
            with get_session(engine) as session:
                narrative = _ctx.build_history(session, model=mdl, base_url=url)
            if narrative:
                hail(f"History narrative built → {_ctx.history_org_path()}")
                on_screen(f"[dim]Tangled to: {_ctx.history_tangle_path()}[/dim]")
            else:
                on_screen("[dim]No qualifying notes — skip for now. "
                          "Run later: org-llm history build[/dim]")
        except Exception as e:
            on_screen(f"[dim]history build failed: {e}[/dim]")
        console.print()

    # 12. welcome
    on_screen("[lcars2]Step 13/15[/lcars2] tutor welcome — your map of the rest")
    try:
        tutor("welcome")
    except SystemExit:
        pass
    except Exception:
        pass
    console.print()

    # 13. open the full tour. Locate it from canonical paths; if it lives
    # in the package's docs/ but not in the user's vault, COPY it to
    # ~/org/org-llm-tour.org so it's visible to `index` and findable next
    # time without filesystem-walk magic.
    if _confirm("Open the full tour now? (a real org-mode tour with "
                 "missions and exercises)", default=True):
        on_screen("[lcars2]Step 14/15[/lcars2] open the tour")
        engine_now = _engine()
        with get_session(engine_now) as session:
            org_dir_now = _org_dir(session)
        vault_tour = org_dir_now / "org-llm-tour.org"
        candidates = [
            vault_tour,
            Path("~/org/org-llm-tour.org").expanduser(),
            Path(__file__).parent.parent / "docs" / "org-llm-tour.org",
            Path(__file__).parent.parent.parent / "org-llm-tour.org",
        ]
        tour_path: Path | None = next(
            (p for p in candidates if p.exists()), None)
        # If the tour exists in the package but NOT the user's vault, copy
        # it so it's part of their notes graph too.
        if tour_path and tour_path != vault_tour and not vault_tour.exists():
            try:
                vault_tour.parent.mkdir(parents=True, exist_ok=True)
                vault_tour.write_text(tour_path.read_text(errors="replace"))
                hail(f"Copied tour into your vault: {vault_tour}")
                tour_path = vault_tour
            except Exception:
                pass    # leave tour_path pointing at the package copy

        if not tour_path:
            on_screen("[dim]No tour file found in standard locations.[/dim]")
            on_screen("[dim]Get the latest:[/dim] [bold]https://github.com/daniel2501/org-llm/blob/trunk/docs/org-llm-tour.org[/bold]")
        else:
            editor = os.environ.get("EDITOR", "")
            if not editor:
                # Try emacsclient (works for Doom users); fall back to xdg-open.
                import shutil as _shutil
                if _shutil.which("emacsclient"):
                    editor = "emacsclient -c"
                elif _shutil.which("xdg-open"):
                    editor = "xdg-open"
            if not editor:
                on_screen(f"[dim]Tour at:[/dim] {tour_path}")
                on_screen("[dim]Open it in any editor when ready.[/dim]")
            else:
                hail(f"Opening tour: {tour_path}")
                import subprocess
                try:
                    parts = editor.split() + [str(tour_path)]
                    subprocess.Popen(parts,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                except Exception as e:
                    on_screen(f"[dim]Couldn't open editor: {e}[/dim]")
                    on_screen(f"[dim]Tour at:[/dim] {tour_path}")
        console.print()

    # 16. (optional) opencode first-touch — explicitly NOT auto-confirmed
    # under --yes since launching opencode takes over the user's terminal.
    # Setup is headless-friendly: this step is opt-in only, and the rest
    # of setup never depends on opencode being launched.
    if not yes and _confirm("Spin up the opencode workspace now? "
                              "(takes over the terminal until you /exit)",
                              default=False):
        on_screen("[lcars2]Step 16/16[/lcars2] launch opencode workspace")
        try:
            launch(workspace="all", model="", no_context=False,
                   no_theme=False, no_commands=False, dry_run=False)
        except SystemExit:
            pass
        except Exception as e:
            on_screen(f"[dim]opencode launch failed: {e}[/dim]")
            on_screen("[dim]Try later: [/dim][bold]org-llm launch[/bold]")
        console.print()

    console.rule("[lcars1]Setup complete[/lcars1]")

    # LLM-generated personalized closing recommendations — feed the model
    # a snapshot of what setup actually accomplished, ask for 3 specific
    # commands the user might enjoy first.
    try:
        from .db        import Node as _N4, Config as _Cfg, File as _F2
        from . import context as _ctx2
        engine_now = _engine()
        with get_session(engine_now) as session:
            n_files    = session.query(_F2).count()
            n_nodes    = session.query(_N4).count()
            n_embedded = session.query(_N4).filter(
                _N4.embedding.isnot(None)).count()
            cfg = {r.key: r.value for r in session.query(_Cfg).all()}
        ctx_chars = len(_ctx2.read_context_for_prompt() or "")
        hist_chars = len(_ctx2.read_history_for_prompt() or "")
        chat_mdl = cfg.get("chat_model", "")
        sys_msg = (
            "Suggest exactly 3 short concrete `org-llm` commands a "
            "first-time user should try, given their setup state. Output "
            "ONE command per line, in this format:\n"
            "  org-llm <subcommand and args> — <≤ 12-word reason>\n"
            "No preamble, no markdown, no quotes around commands. Use "
            "actual user state in the reasons (tag names, project names, "
            "model names if you have them). Avoid suggesting `setup` "
            "again or anything destructive."
        )
        user_msg = (
            f"Setup state:\n"
            f"  files indexed: {n_files}, nodes: {n_nodes}, "
            f"embedded: {n_embedded}/{n_nodes}\n"
            f"  chat_model: {chat_mdl or '(not set)'}\n"
            f"  USER CONTEXT chars: {ctx_chars}\n"
            f"  HISTORICAL CONTEXT chars: {hist_chars}\n\n"
            "Three personalised first-run commands."
        )
        try:
            from .llm import chat as _chat_local
            recs = _chat_local(user_msg, model=chat_mdl or "llama3.2",
                               base_url=cfg.get("ollama_url",
                                                "http://localhost:11434"),
                               system=sys_msg)
        except Exception:
            recs = ""
        recs = (recs or "").strip()
        if recs:
            on_screen("[lcars3]Tailored next steps for you:[/lcars3]")
            for line in recs.splitlines()[:3]:
                line = line.strip(" -•").strip()
                if line:
                    on_screen(f"  • {line}")
        else:
            on_screen("[dim]Next stop: [/dim][bold]org-llm ask 'your first question'[/bold]")
            on_screen("[dim]Or:        [/dim][bold]org-llm launch[/bold] (opencode workspace)")
    except Exception:
        on_screen("[dim]Next stop: [/dim][bold]org-llm ask 'your first question'[/bold]")
        on_screen("[dim]Or:        [/dim][bold]org-llm launch[/bold] (opencode workspace)")
    make_it_so()


@app.command(rich_help_panel="Indexing")
def index(
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-index all files")] = False,
):
    """Scan org files and populate the index.

    --force takes an exclusive lock on the SQLite DB so two parallel runs
    can't interleave deletes and inserts.
    """
    _auto_init_db_if_needed()
    engine = _engine()
    with get_session(engine) as session:
        org_dir = _org_dir(session)
        if not org_dir.exists():
            # Auto-fix: create the org_dir if it doesn't exist yet
            try:
                org_dir.mkdir(parents=True, exist_ok=True)
                hail(f"Auto-created org_dir at {org_dir}")
            except Exception:
                red_alert(f"Could not create org_dir: {org_dir}")
                raise typer.Exit(1)

        if force:
            from .db import File, Node
            from sqlalchemy import text as _sql_text
            try:
                session.execute(_sql_text("BEGIN EXCLUSIVE"))
            except Exception:
                # Another process holds the lock — bail rather than corrupt state
                red_alert("Another process is currently running `index --force`. "
                          "Wait for it to finish, then re-run.")
                raise typer.Exit(1)
            session.query(Node).delete()
            session.query(File).delete()
            session.commit()
            hail("Cleared existing index.")

    with warp(TREK_MSGS["index"] + f": {org_dir}"):
        engine = _engine()
        with get_session(engine) as session:
            files, nodes = index_directory(org_dir, session)

    hail(f"Indexed {files} files, {nodes} nodes.")
    if nodes > 0:
        with get_session(_engine()) as session:
            on_screen(_suggest_note_ask(session, prefix="Try it: "))
    make_it_so()


def _llm_one_liner(prompt: str, *, system: str = "",
                    timeout: float = 8.0,
                    fallback: str = "") -> str:
    """Ask the configured LLM for ONE short line of text.

    Uses fast_model (or chat_model) on local Ollama by default. Always
    returns a string — `fallback` if the call fails, times out, or the
    model returns nothing usable. Never raises.

    The whole point: stop emitting template strings everywhere when we
    have a perfectly good LLM that can write better copy from real
    context. Use this anywhere we'd otherwise hardcode a suggestion.
    """
    if not prompt:
        return fallback
    try:
        engine = _engine()
        with get_session(engine) as session:
            url = _ollama_url(session)
            mdl = (_cfg(session, "fast_model")
                   or _cfg(session, "chat_model")
                   or MODEL_DEFAULTS["chat_model"])
    except Exception:
        return fallback

    sys_default = (
        "You write ONE short line of helpful CLI copy. Output the line "
        "only — no preamble, no quotes, no markdown, no numbering. "
        "Aim for 8-22 words. Concrete, specific, no hype words."
    )
    try:
        from .llm import chat as _chat
        from .ui  import thinking as _thinking
        import threading
        result: dict = {"text": ""}
        def _run():
            try:
                result["text"] = _chat(prompt, model=mdl, base_url=url,
                                       system=system or sys_default) or ""
            except Exception:
                pass
        with _thinking("Composing", model=mdl):
            t = threading.Thread(target=_run, daemon=True)
            t.start(); t.join(timeout=timeout)
        if t.is_alive():
            return fallback
    except Exception:
        return fallback
    text = (result["text"] or "").strip()
    if not text:
        return fallback
    # Take the first non-empty line, strip quotes/list-markers/numbering.
    for line in text.splitlines():
        l = line.strip(" -–—•\"'1234567890.").strip()
        if 6 <= len(l) <= 240:
            return l
    return fallback


def _suggest_note_ask(session, prefix: str = "Try it: ") -> str:
    """Build a context-aware `org-llm ask` suggestion.

    LLM-driven by default: feeds top tags + recent titles to fast_model
    and asks for one fitting follow-up question. Falls back to a string
    template when the LLM is unreachable.
    """
    import random
    from collections import Counter
    from datetime import datetime, timedelta
    from .db import Node

    # Gather evidence
    tag_counts: Counter = Counter()
    for (tags,) in session.query(Node.tags).filter(Node.tags.isnot(None)).all():
        for t in (tags or "").split():
            t = t.strip().lower()
            if t and t != "code" and not t.startswith("code:"):
                tag_counts[t] += 1
    top_tags = [t for t, _ in tag_counts.most_common(15)]

    since = (datetime.now() - timedelta(days=30)).timestamp()
    recents = (
        session.query(Node.title)
        .filter(Node.mtime >= since,
                Node.tags.isnot(None),
                ~Node.tags.like("%code%"))
        .order_by(Node.mtime.desc())
        .limit(15).all()
    )
    recent_titles = [t[0] for t in recents if t[0]]

    if not (top_tags or recent_titles):
        return (f"{prefix}[bold]org-llm ask "
                f"'what do I have in this vault?'[/bold]")

    def _safe(s: str) -> str:
        return s.replace("'", "")

    # LLM-driven path: ask fast_model for one specific question grounded in
    # the user's actual content.
    titles_block = "\n".join(f"  - {_safe(t)[:70]}" for t in recent_titles[:10])
    tags_block   = ", ".join(_safe(t) for t in top_tags[:10])
    sys_msg = (
        "You suggest ONE concrete question a person could ask their "
        "personal note vault. Output the question only — no preamble, "
        "no quotes, no command syntax. 6-18 words. Use the user's actual "
        "tags and titles as anchors. Vary across runs: don't always "
        "default to 'summarise X'. Keep it specific and curious."
    )
    user_msg = (
        f"Top tags: {tags_block or '(none)'}\n"
        f"Recent titles:\n{titles_block or '  (none)'}\n\n"
        "One question they might ask their notes."
    )
    question = _llm_one_liner(user_msg, system=sys_msg, fallback="").strip("?")
    if question:
        # Strip stray quotes, ensure trailing question mark
        question = _safe(question.strip())
        if not question.endswith("?"):
            question += "?"
        cloud_flag = " --cloud" if random.random() < 0.4 else ""
        return f"{prefix}[bold]org-llm ask{cloud_flag} '{question}'[/bold]"

    # Fallback template pool when the LLM is unreachable.
    pool: list[str] = []
    for tag in top_tags:
        t = _safe(tag)
        pool.extend([
            f'what did I write about {t} lately?',
            f'summarise my {t} notes',
            f'find the most-linked note tagged {t}',
        ])
    for title in recent_titles:
        short = title if len(title) <= 60 else title[:57] + "…"
        s = _safe(short)
        pool.extend([
            f'what does {s} say?',
            f'connect {s} to anything else in my vault',
        ])
    if not pool:
        return (f"{prefix}[bold]org-llm ask "
                f"'what do I have in this vault?'[/bold]")
    cloud_flag = " --cloud" if random.random() < 0.4 else ""
    return f"{prefix}[bold]org-llm ask{cloud_flag} '{random.choice(pool)}'[/bold]"


def _suggest_code_ask(session, roots: list[Path]) -> str:
    """Build a context-aware `org-llm ask` suggestion from what was just indexed.

    Samples real nodes under `roots` from the DB, picks one at random, derives
    its language from the `code:<lang>` tag, and renders a template from a
    per-language pool. Output varies across runs by design.
    """
    import random
    from .db import File, Node

    import re as _re
    samples: list[tuple[str, str, str, list[str]]] = []  # (title, path, lang, symbols)
    prefixes = [str(r.resolve()) + "/" for r in roots]
    rows = (
        session.query(Node.title, Node.tags, File.path, Node.body)
        .join(File, Node.file_id == File.id)
        .filter(Node.tags.like("%code%"))
        .order_by(File.mtime.desc())
        .limit(80)
        .all()
    )
    sym_patterns = {
        "python":     _re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", _re.M),
        "elisp":      _re.compile(r"^\(defun\s+([A-Za-z_][A-Za-z0-9_/-]*)", _re.M),
        "rust":       _re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)", _re.M),
        "go":         _re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)", _re.M),
        "typescript": _re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)|^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)", _re.M),
        "javascript": _re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)", _re.M),
        "scheme":     _re.compile(r"^\(define[*]?\s+\(?([A-Za-z_][A-Za-z0-9_!?/<>=+*-]*)", _re.M),
    }
    for title, tags, path, body in rows:
        if not any(path.startswith(p) for p in prefixes):
            continue
        lang = next((t.split(":", 1)[1]
                     for t in (tags or "").split()
                     if t.startswith("code:")), "")
        symbols: list[str] = []
        pattern = sym_patterns.get(lang)
        if pattern and body:
            seen_syms: set[str] = set()
            for m in pattern.finditer(body or ""):
                # First non-empty group across the alternation handles TS/JS
                sym = next((g for g in m.groups() if g), None)
                if sym and not sym.startswith("_") and sym not in seen_syms:
                    seen_syms.add(sym)
                    symbols.append(sym)
                    if len(symbols) >= 8:
                        break
        samples.append((title, path, lang, symbols))
        if len(samples) >= 30:
            break

    # Generic templates always available; specific pools take priority when matched.
    pools: dict[str, list[str]] = {
        "":         [
            "what does {file} do?",
            "summarise the architecture under {dir}",
            "find every TODO/FIXME under {dir}",
            "what's the entry point for {project}?",
            "what looks out of place in {file}?",
        ],
        "python":   [
            "what does {file} do?",
            "which functions in {file} would I document first?",
            "where is the public API defined in {project}?",
            "find every place {project} catches a bare Exception",
            "summarise the data model in {file}",
        ],
        "elisp":    [
            "what does {file} do?",
            "which interactive commands does {project} expose?",
            "find every defcustom in {project}",
            "what hooks does {file} register?",
        ],
        "rust":     [
            "what does {file} do?",
            "find unsafe blocks under {dir}",
            "which traits does {project} define?",
            "what's the public API of {project}?",
        ],
        "typescript": [
            "what does {file} do?",
            "find every TODO in {project}",
            "which exports does {file} provide?",
            "what's the type model in {file}?",
        ],
        "javascript": [
            "what does {file} do?",
            "find every console.log under {dir}",
            "summarise the module graph in {project}",
        ],
        "go":       [
            "what does {file} do?",
            "find every goroutine launch under {dir}",
            "which interfaces does {project} define?",
        ],
        "shell":    [
            "what does {file} do?",
            "find every set -e missing from scripts under {dir}",
        ],
        "scheme":   [
            "what does {file} do?",
            "what does {project} expose at the top level?",
        ],
        "markdown": [
            "summarise {file}",
            "extract every heading from {project}",
        ],
        "org":      [
            "what's the latest section in {file}?",
            "summarise the workflow under {dir}",
        ],
    }

    # Strip single quotes from every interpolated value so we can wrap the
    # final shell command in single quotes safely (any double quotes inside
    # the question are fine that way).
    def _safe(s: str) -> str:
        return (s or "").replace("'", "")

    if not samples:
        # No code samples yet (nothing under roots, or unembedded). Fall back
        # to a non-cloud generic pick referencing the actual root.
        root_label = _safe(str(roots[0]) if roots else "your code")
        question = random.choice([
            f"summarise the architecture under {root_label}",
            f"what looks important under {root_label}?",
            f"find every TODO under {root_label}",
        ])
        return f"Try it: [bold]org-llm ask '{question}'[/bold]"

    title, path, lang, symbols = random.choice(samples)
    file_name = _safe(Path(path).name)
    # Project = first path segment under any of the roots
    project = file_name
    for pref in prefixes:
        if path.startswith(pref):
            tail = path[len(pref):]
            project = _safe(tail.split("/", 1)[0]) or file_name
            break
    parent_dir = _safe(str(Path(path).parent))

    pool = list(pools.get(lang) or pools[""])
    # Symbol-aware extras when we extracted any from the file's body.
    if symbols:
        sym = _safe(random.choice(symbols))
        pool.extend([
            f"how is {sym} used across {{project}}?",
            f"explain {sym} in {{file}}",
            f"find every callsite of {sym}",
        ])

    # LLM-driven: feed the actual sample's identity to fast_model and ask
    # for one specific, concrete question.
    sym_str = (", ".join(symbols[:5]) if symbols else "")
    sys_msg = (
        "You suggest ONE concrete question a developer could ask about a "
        "code file. Output the question only — no preamble, no quotes. "
        "6-16 words. Reference the file/symbol concretely; never say "
        "'this code'."
    )
    user_msg = (
        f"File: {file_name}  (project: {project}, language: {lang or 'unknown'})\n"
        + (f"Public symbols in the file: {sym_str}\n" if sym_str else "")
        + "\nOne question someone might ask about this file."
    )
    llm_q = _llm_one_liner(user_msg, system=sys_msg, fallback="").strip("?")
    if llm_q:
        llm_q = _safe(llm_q.strip())
        if not llm_q.endswith("?"):
            llm_q += "?"
        cloud_flag = " --cloud" if random.random() < 0.5 else ""
        return f"Try it: [bold]org-llm ask{cloud_flag} '{llm_q}'[/bold]"

    # Fallback to the deterministic template pool.
    template = random.choice(pool)
    question = template.format(file=file_name, dir=parent_dir, project=project)
    cloud_flag = " --cloud" if random.random() < 0.5 else ""
    return f"Try it: [bold]org-llm ask{cloud_flag} '{question}'[/bold]"


@app.command(name="code-index", rich_help_panel="Indexing")
def code_index(
    paths: Annotated[list[str], typer.Argument(
        help="Code directories to index. Defaults to the `code_dirs` config row (~/repos).")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-index unchanged files")] = False,
    embed_after: Annotated[bool, typer.Option("--embed/--no-embed", "-e/-E",
                  help="Run `embed` after indexing so the new code is searchable immediately")] = True,
):
    """Index source-code repos so `ask` can answer across notes AND code.

    Walks each directory, picking up files with extensions known to be
    source-code-shaped (.py .el .rs .ts .md .org .yaml …). Skips obvious
    noise (.git, node_modules, .venv, target, dist, …). One row per file,
    body capped at 24 KB, tagged `code code:<lang>` so `ask` can scope.

    After indexing, by default also runs `embed` against the new nodes so
    they're semantically searchable in the same session — pass --no-embed
    to skip and embed later.
    """
    from .code_index import index_code_dirs

    engine = _engine()
    if not paths:
        with get_session(engine) as session:
            raw = _cfg(session, "code_dirs") or "~/repos"
        paths = [p.strip() for p in raw.split(",") if p.strip()]
    roots = [Path(p).expanduser() for p in paths]

    # Partition: skip missing paths with a warning rather than bailing.
    # Only fail if ZERO of the requested paths exist.
    valid   = [r for r in roots if r.exists() and r.is_dir()]
    missing = [r for r in roots if not r.exists()]
    not_dir = [r for r in roots if r.exists() and not r.is_dir()]
    for m in missing:
        on_screen(f"[yellow]Skipping {m}: path does not exist[/yellow]")
    for nd in not_dir:
        on_screen(f"[yellow]Skipping {nd}: not a directory[/yellow]")
    if not valid:
        # Auto-heal: probe the filesystem for code-shaped roots before bailing.
        from .discover import discover, suggest_code_dirs
        on_screen("[yellow]None of the given paths exist — scanning filesystem for code roots…[/yellow]")
        found = discover()
        suggested = suggest_code_dirs(found)
        if not suggested:
            red_alert(f"None of the requested paths exist: {', '.join(str(r) for r in roots)}")
            on_screen("No fallback code roots found either. Try:")
            on_screen("  org-llm discover                            — see what's on disk")
            on_screen("  org-llm config code_dirs ~/path/to/code     — set explicitly")
            raise typer.Exit(1)
        on_screen("[green]Found these instead:[/green]")
        for s in suggested:
            on_screen(f"  · {s}")
        if not typer.confirm("Index these?", default=True):
            on_screen("Aborted. Run [bold]org-llm discover[/bold] to see what's available.")
            raise typer.Exit(1)
        roots = suggested
    else:
        roots = valid

    if force:
        # Wipe rows whose file path lives under any code dir
        from .db import File, Node
        with get_session(engine) as session:
            for r in roots:
                prefix = str(r.resolve()) + "/"
                stale = session.query(File).filter(File.path.like(f"{prefix}%")).all()
                for f in stale:
                    session.query(Node).filter_by(file_id=f.id).delete()
                    session.delete(f)
            session.commit()
        hail("Cleared existing code index.")

    def _per_dir(root, files, nodes):
        on_screen(f"  {root}: {files} files, {nodes} nodes")

    total_files = total_nodes = 0
    with warp(f"Indexing code under {', '.join(str(r) for r in roots)}"):
        with get_session(engine) as session:
            total_files, total_nodes = index_code_dirs(roots, session,
                                                        progress_cb=_per_dir)

    hail(f"Indexed {total_files} code files / {total_nodes} nodes.")

    # Embed the new code so `ask` can find it right away.
    if embed_after and total_nodes > 0:
        from .db import Node
        from .indexer import embed_nodes
        with get_session(engine) as session:
            embed_mdl = _cfg(session, "embed_model") or "nomic-embed-text"
            url       = _ollama_url(session)
            unembedded = session.query(Node).filter(Node.embedding.is_(None)).count()
        if unembedded > 0:
            if not _ensure_model_pulled(embed_mdl, url):
                red_alert(f"Could not pull embed model {embed_mdl!r}.")
                raise typer.Exit(1)
            with get_session(engine) as session:
                with impulse(TREK_MSGS["embed"], total=unembedded) as (prog, task):
                    def tick(): prog.advance(task)
                    count = embed_nodes(session, model=embed_mdl, base_url=url,
                                        force=False, progress_cb=tick)
            hail(f"Embedded {count} new code nodes.")

    with get_session(engine) as session:
        on_screen(_suggest_code_ask(session, roots))
    make_it_so()


@app.command(rich_help_panel="Onboarding")
def discover(
    extra: Annotated[list[str], typer.Argument(
        help="Additional directories to probe alongside the standard set.")] = None,
):
    """Probe the filesystem for things org-llm could use.

    Looks at standard locations (~/org, ~/repos, ~/.config/doom, …) plus
    any extras you pass, and reports vaults, repo roots, dotfiles, and
    Emacs configs that actually exist. Used to bootstrap `code-index`,
    `grant-root`, and as a one-shot "what's here?" report.
    """
    from rich.table import Table as _T
    from .discover import (
        discover as _do_discover, suggest_code_dirs, suggest_grant_roots,
        detect_preferred_language,
    )

    found = _do_discover(extra_dirs=extra)
    if not found:
        on_screen("[yellow]Nothing found at standard locations.[/yellow]")
        on_screen("Pass paths to probe explicitly: "
                  "[bold]org-llm discover ~/some/dir[/bold]")
        return

    tbl = _T(title="[lcars2]Filesystem discoveries[/lcars2]",
             box=None, pad_edge=False)
    tbl.add_column("Path",        style="lcars2", no_wrap=True)
    tbl.add_column("Kind",        style="dim", width=14)
    tbl.add_column("Description", style="white")
    for f in found:
        tbl.add_row(str(f.path), f.kind, f.description)
    console.print()
    console.print(tbl)
    console.print()

    code_dirs   = suggest_code_dirs(found)
    grant_roots = suggest_grant_roots(found)
    pref_lang   = detect_preferred_language()

    if code_dirs:
        on_screen("[lcars3]Suggested code-index roots:[/lcars3]")
        for c in code_dirs:
            on_screen(f"  · {c}")
        on_screen(f"  → [bold]org-llm code-index "
                  f"{' '.join(str(c) for c in code_dirs)}[/bold]")
        console.print()
    if grant_roots:
        on_screen("[lcars3]Suggested MCP auto-grant roots:[/lcars3]")
        for g in grant_roots:
            on_screen(f"  · {g}")
        for g in grant_roots:
            on_screen(f"  → [bold]org-llm grant-root {g}[/bold]")
        console.print()
    if pref_lang:
        on_screen(f"[dim]Detected preferred language: [bold]{pref_lang}[/bold] "
                  "(used by `code` for default language)[/dim]")


@app.command(rich_help_panel="Indexing")
def embed(
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-embed all nodes")] = False,
):
    """Generate embeddings for indexed nodes (requires Ollama; pulls the embed model if missing)."""
    from .indexer import embed_nodes
    from .db import Node

    _auto_init_db_if_needed()
    _auto_index_if_empty()

    engine = _engine()
    with get_session(engine) as session:
        url   = _ollama_url(session)
        model = _cfg(session, "embed_model") or "nomic-embed-text"
        if force:
            total = session.query(Node).count()
        else:
            total = session.query(Node).filter(Node.embedding.is_(None)).count()

    _auto_start_ollama_if_needed(url)
    if not _ensure_model_pulled(model, url):
        red_alert(f"Could not pull embed model {model!r}. Tried: ollama pull {model}")
        raise typer.Exit(1)

    hail(f"Embedding {total} nodes with [bold]{model}[/bold]")

    with get_session(engine) as session:
        with impulse(TREK_MSGS["embed"], total=total) as (prog, task):
            def tick():
                prog.advance(task)
            count = embed_nodes(session, model=model, base_url=url,
                                force=force, progress_cb=tick)

    hail(f"Embedded {count} nodes.")
    if count > 0:
        with get_session(engine) as session:
            on_screen(_suggest_note_ask(session, prefix="Try it: "))
    make_it_so()


@app.command(rich_help_panel="Querying")
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int,  typer.Option("--limit", "-n")] = 10,
    keyword: Annotated[bool, typer.Option("--keyword", "-k",
             help="Keyword search instead of semantic")] = False,
):
    """Search your org notes semantically or by keyword."""
    from .search import keyword_search, vector_search

    if not query.strip():
        red_alert("Empty search query. Pass a non-empty string.")
        raise typer.Exit(1)

    _auto_init_db_if_needed()
    _auto_index_if_empty()

    engine = _engine()
    try:
        with get_session(engine) as session:
            url   = _ollama_url(session)
            model = _cfg(session, "embed_model") or "nomic-embed-text"
            if not keyword:
                _auto_start_ollama_if_needed(url)

            if keyword:
                results = keyword_search(session, query, limit=limit)
            else:
                with warp(TREK_MSGS["search"] + f": {query!r}"):
                    from .llm import embed
                    qvec = embed(query, model=model, base_url=url)
                    results = vector_search(session, qvec, limit=limit)
    except Exception as exc:
        msg = str(exc)
        if "Connection" in msg or "refused" in msg.lower():
            red_alert("Ollama not reachable. Run: org-llm doctor --fix")
        else:
            red_alert(f"Search failed: {exc}")
        raise typer.Exit(1)

    if not results:
        on_screen("No results found.")
        return

    table = Table(box=None, pad_edge=False, show_header=True)
    table.add_column("Score", style="lcars1", width=6, no_wrap=True)
    table.add_column("Title", style="lcars2")
    table.add_column("Tags",  style="dim", width=20)
    table.add_column("File",  style="dim")

    for r in results:
        score = f"{r.score:.3f}" if not keyword else "—"
        fname = Path(r.file_path).name
        table.add_row(score, r.title, r.tags or "—", fname)

    console.print(table)


@app.command(rich_help_panel="Querying")
def ask(
    query:   Annotated[str,  typer.Argument(help="Your question")],
    top_k:   Annotated[int,  typer.Option("--top-k", "-k")] = 6,
    model:   Annotated[str,  typer.Option("--model", "-m",
             help="Override chat model")] = "",
    context: Annotated[bool, typer.Option("--context", "-c",
             help="Show retrieved context nodes with similarity scores")] = False,
    reason:  Annotated[bool, typer.Option("--reason", "-r",
             help="Use reason_model (deepseek-r1) instead of chat_model")] = False,
    cloud_:  Annotated[bool, typer.Option("--cloud", "-C",
             help="Route the chat through the configured cloud backend instead of local Ollama")] = False,
    days:    Annotated[int,  typer.Option("--days", "-D",
             help="Restrict retrieval to nodes modified in the last N days (0 = no filter; auto-detected from query phrases like 'last week')")] = 0,
):
    """Ask a question answered from your org notes (RAG).

    By default the chat model runs on local Ollama. Pass --cloud to route
    the chat call through the configured cloud provider (set up via
    `org-llm cloud --quick-start <provider>`). Embeddings still come from
    local Ollama unless you also override embed_model.

    Temporal queries: phrases like "last week", "last month", "yesterday",
    "last N days" auto-set --days. Override explicitly with --days N (or
    --days 0 to disable the filter).
    """
    from .llm import chat as local_chat, embed
    from .search import (
        existing_tags, nodes_with_tag,
        recent_in_path, recent_nodes, vector_search,
    )

    # Auto-fix #1: missing DB
    _auto_init_db_if_needed()

    engine = _engine()
    try:
        with get_session(engine) as session:
            url        = _ollama_url(session)
            embed_mdl  = _cfg(session, "embed_model") or "nomic-embed-text"
            chat_mdl   = model or (
                _cfg(session, "reason_model") if reason
                else _cfg(session, "chat_model")
            ) or MODEL_DEFAULTS["chat_model"]
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            cloud_model    = _cfg(session, "cloud_model")
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
    except Exception as exc:
        red_alert(f"Config read failed: {exc}")
        raise typer.Exit(1)

    # Auto-fix #2: ollama not running (only matters for embed; cloud bypasses it)
    if not cloud_:
        _auto_start_ollama_if_needed(url)
    else:
        # Even with --cloud, embeddings still go through local Ollama
        _auto_start_ollama_if_needed(url, silent=True)

    # Auto-fix #3: empty index — run `index` if org_dir has files
    _auto_index_if_empty()

    if cloud_:
        if not cloud_endpoint:
            on_screen("[yellow]--cloud requested but no cloud_endpoint_url configured. "
                      "Falling back to local Ollama.[/yellow]")
            on_screen("[dim]To enable cloud: org-llm cloud --quick-start openrouter[/dim]")
            cloud_ = False
        else:
            from . import creds as creds_mod
            api_key = (creds_mod.read_secret(creds_mod.cloud_slug(cloud_provider))
                       if cloud_provider else None) or db_api_key
            chat_mdl = model or cloud_model or chat_mdl
    if not cloud_:
        # Local path: ensure the chat model is pulled before asking.
        if not _ensure_model_pulled(chat_mdl, url):
            # The auto-fix may have swapped chat_model in the config —
            # re-read so we don't keep retrying a bogus tag.
            with get_session(engine) as session:
                fresh_mdl = (model or
                             (_cfg(session, "reason_model") if reason
                              else _cfg(session, "chat_model"))
                             or MODEL_DEFAULTS["chat_model"])
            if fresh_mdl != chat_mdl:
                hail(f"Config swapped chat_model → {fresh_mdl}; retrying.")
                chat_mdl = fresh_mdl
                if not _ensure_model_pulled(chat_mdl, url, _try_llm_fix=False):
                    red_alert(f"Could not pull {chat_mdl!r} either.")
                    on_screen("Try --cloud, or:  org-llm performance --apply")
                    raise typer.Exit(1)
            else:
                red_alert(f"Could not pull local model {chat_mdl!r}.")
                on_screen("Either run with --cloud, or: org-llm doctor --fix")
                raise typer.Exit(1)

    # Embeddings always come from local Ollama — make sure the embed model is here too
    if not _ensure_model_pulled(embed_mdl, url):
        red_alert(f"Could not pull embedding model {embed_mdl!r}.")
        raise typer.Exit(1)

    # Time-window resolution: explicit --days wins; otherwise auto-detect.
    days_window = days if days > 0 else _parse_days_window(query)
    since_mtime = None
    if days_window:
        from datetime import datetime, timedelta
        since_mtime = (datetime.now() - timedelta(days=days_window)).timestamp()

    # Project-name detection: when the query names a real repo under one of
    # the user's code roots, augment retrieval with that repo's README so the
    # LLM has architecture context even if no notes match the project.
    project_files: list[Path] = []
    try:
        from .discover import discover as _disc
        for found in _disc():
            if found.kind != "repos-root":
                continue
            try:
                children = [p for p in found.path.iterdir()
                            if p.is_dir() and not p.name.startswith(".")]
            except OSError:
                continue
            for child in children:
                if child.name.lower() in query.lower():
                    for readme_name in ("README.md", "README.org", "README"):
                        p = child / readme_name
                        if p.exists():
                            project_files.append(p)
                            break
        # De-dup, cap at 3 to avoid prompt bloat
        seen_proj_files: set[Path] = set()
        project_files = [p for p in project_files
                          if not (p in seen_proj_files or seen_proj_files.add(p))][:3]
    except Exception:
        project_files = []

    # Detect path-anchored query intents (daily / journal / diary). When the
    # query references one of these subfolders, we augment retrieval with the
    # most-recent files from that path REGARDLESS of the mtime window — so
    # "summarize my daily notes" surfaces the actual journal even if the user
    # hasn't written one this week.
    path_hints: list[str] = []
    q_lower = query.lower()
    for keyword, folder in [("daily note", "daily"), ("daily", "daily"),
                              ("journal", "journal"), ("diary", "diary")]:
        if keyword in q_lower and folder not in path_hints:
            path_hints.append(folder)

    # Tag-anchored intents: "my politics tag", "tagged X", ":foo:".
    # Validated against actual tags later (inside the session block) so we
    # can also surface "did you mean…" hints when the user names a tag that
    # doesn't exist.
    tag_candidates = _parse_tag_hints(query)
    valid_tags: list[str] = []
    invalid_tag_suggestions: dict[str, list[str]] = {}

    with warp(TREK_MSGS["ask"] + " — retrieving context"):
        try:
            with get_session(engine) as session:
                qvec    = embed(query, model=embed_mdl, base_url=url)
                try:
                    results = list(vector_search(session, qvec, limit=top_k,
                                                  since_mtime=since_mtime,
                                                  query_text=query))
                except Exception as ve:
                    # Embed-dimension mismatch: stored vectors don't match the
                    # current embed model. Auto re-embed everything once.
                    msg = str(ve).lower()
                    if "dimension" in msg and ("mismatch" in msg or "mistmatch" in msg):
                        from .db       import Node
                        from .indexer  import embed_nodes
                        n_total = session.query(Node).count()
                        on_screen(f"[yellow]Embedding dimension mismatch — re-embedding "
                                  f"all {n_total} nodes with {embed_mdl}…[/yellow]")
                        # Force re-embed: clear all vectors first
                        session.query(Node).update({Node.embedding: None})
                        session.commit()
                        if _ensure_model_pulled(embed_mdl, url):
                            with impulse(TREK_MSGS["embed"], total=n_total) as (prog, task):
                                def _t(): prog.advance(task)
                                embed_nodes(session, model=embed_mdl, base_url=url,
                                            force=True, progress_cb=_t)
                            qvec = embed(query, model=embed_mdl, base_url=url)
                            results = list(vector_search(session, qvec, limit=top_k,
                                                          since_mtime=since_mtime,
                                                          query_text=query))
                        else:
                            raise
                    else:
                        raise
                seen = {(r.node_id, r.title, r.file_path) for r in results}

                # When a time window is active, augment with mtime-DESC nodes
                # so date-titled files don't get filtered out by weak semantic
                # similarity to phrases like "daily notes".
                if since_mtime is not None:
                    recent = recent_nodes(session, limit=top_k, since_mtime=since_mtime)
                    for r in recent:
                        key = (r.node_id, r.title, r.file_path)
                        if key not in seen:
                            results.append(r); seen.add(key)

                # Path-anchored augmentation (daily/journal/diary). When the
                # user explicitly references one of these folders we PREPEND
                # them rather than append, so they lead in the LLM's prompt.
                # Pull more files when the time window is large — "last six
                # months" wants ~24 distinct daily entries, not 6.
                path_limit = max(top_k, (days_window or 7) // 7)
                path_aug: list = []
                for folder in path_hints:
                    path_aug += recent_in_path(session, folder, limit=path_limit)
                fresh_path = []
                for r in path_aug:
                    key = (r.node_id, r.title, r.file_path)
                    if key not in seen:
                        fresh_path.append(r); seen.add(key)
                if fresh_path:
                    results = fresh_path + results

                # Tag-anchored augmentation. Validate candidates against the
                # actual tag inventory; for valid tags pull notes; for
                # invalid ones build "did you mean" suggestions.
                if tag_candidates:
                    all_tags = existing_tags(session)
                    tag_aug: list = []
                    for cand in tag_candidates:
                        if cand in all_tags:
                            valid_tags.append(cand)
                            tag_aug += nodes_with_tag(session, cand,
                                                      limit=max(top_k, 8))
                        else:
                            invalid_tag_suggestions[cand] = _did_you_mean(
                                cand, all_tags, n=3,
                            )
                    fresh_tag = []
                    for r in tag_aug:
                        key = (r.node_id, r.title, r.file_path)
                        if key not in seen:
                            fresh_tag.append(r); seen.add(key)
                    # Tag-anchored matches lead — that's the user's explicit
                    # filter, even more specific than path hints.
                    if fresh_tag:
                        results = fresh_tag + results
        except Exception as e:
            red_alert(f"Embed/search failed: {e}")
            on_screen("If Ollama isn't running locally, run: ollama serve")
            raise typer.Exit(1)

    # If a time filter wiped out all matches, retry without it and warn.
    filter_relaxed = False
    if not results and since_mtime is not None:
        filter_relaxed = True
        with get_session(engine) as session:
            results = vector_search(session, qvec, limit=top_k, since_mtime=None,
                                     query_text=query)

    if not results:
        # Try one round of auto-fix: maybe nothing has been embedded yet.
        from .db import Node
        with get_session(engine) as session:
            unembedded = session.query(Node).filter(Node.embedding.is_(None)).count()
        if unembedded > 0:
            hail(f"Index has {unembedded} unembedded nodes — running embed automatically…")
            from .indexer import embed_nodes
            if _ensure_model_pulled(embed_mdl, url):
                with get_session(engine) as session:
                    with impulse(TREK_MSGS["embed"], total=unembedded) as (prog, task):
                        def _tick(): prog.advance(task)
                        embed_nodes(session, model=embed_mdl, base_url=url,
                                    force=False, progress_cb=_tick)
                # Retry the search with the freshly-embedded vault
                with warp(TREK_MSGS["ask"] + " — retrying search after auto-embed"):
                    with get_session(engine) as session:
                        qvec = embed(query, model=embed_mdl, base_url=url)
                        results = list(vector_search(session, qvec, limit=top_k,
                                                      since_mtime=since_mtime,
                                                      query_text=query))
        if not results:
            red_alert("No indexed nodes found. Tried auto-index + auto-embed; "
                      "your vault may be empty.")
            raise typer.Exit(1)

    # Empty-but-not-vault-empty path: vault HAS content, but no semantic match.
    # Surface 2-3 alternative queries the user might mean, drawn from their
    # actual top tags + recent titles. Best-effort — never block the answer.
    if not results:
        try:
            with get_session(engine) as session:
                from .db import Node
                from collections import Counter as _C
                tag_counts: _C = _C()
                for (tags,) in session.query(Node.tags).filter(
                        Node.tags.isnot(None)).all():
                    for tk in (tags or "").split():
                        tk = tk.strip().lower()
                        if tk and tk != "code" and not tk.startswith("code:"):
                            tag_counts[tk] += 1
                top_tags = [t for t, _ in tag_counts.most_common(5)]
                recent_titles = [
                    t[0] for t in session.query(Node.title)
                    .order_by(Node.mtime.desc()).limit(5).all() if t[0]
                ]
            on_screen("[yellow]No matches in the index. "
                      "Maybe try one of these instead:[/yellow]")
            for tag in top_tags[:3]:
                on_screen(f"  [bold]org-llm ask \"what did I write about {tag}?\"[/bold]")
            for title in recent_titles[:2]:
                short = title if len(title) <= 50 else title[:47] + "…"
                on_screen(f"  [bold]org-llm ask \"what does '{short}' say?\"[/bold]")
        except Exception:
            pass
        raise typer.Exit(0)

    # Always show a one-line retrieval summary so the user can sanity-check
    # whether retrieval was on-topic before the LLM responds.
    titles = ", ".join(r.title[:40] for r in results[:3])
    path_note = (f" + {'/'.join(path_hints)} folder" if path_hints else "")
    tag_note  = (f" + tag:{','.join(valid_tags)}" if valid_tags else "")
    if days_window:
        if filter_relaxed:
            on_screen(f"[yellow]No notes in the last {days_window} days; "
                      f"falling back to all-time top {len(results)}{path_note}{tag_note}: {titles}…[/yellow]")
        else:
            on_screen(f"Retrieved {len(results)} note(s) from the last "
                      f"{days_window} days{path_note}{tag_note}: [dim]{titles}…[/dim]")
    else:
        on_screen(f"Retrieved {len(results)} note(s){path_note}{tag_note}: [dim]{titles}…[/dim]")

    # If the user named tags that don't exist, say so up front — they shouldn't
    # have to wait for the LLM to refuse and then guess what's wrong.
    for missing, suggestions in invalid_tag_suggestions.items():
        if suggestions:
            on_screen(f"[yellow]Note: tag [bold]'{missing}'[/bold] not found. "
                      f"Closest existing: {', '.join(suggestions)}[/yellow]")
        else:
            on_screen(f"[yellow]Note: tag [bold]'{missing}'[/bold] not found and "
                      "no close matches.[/yellow]")

    if context:
        for r in results:
            on_screen(f"  [{r.score:.3f}] {r.title} ({Path(r.file_path).name})")
    console.print()

    ctx_text = "\n\n---\n\n".join(
        f"# {r.title}\n{r.body[:800]}" for r in results
    )
    # Project READMEs (when query named a real repo) come ahead of note
    # context — the LLM should anchor to project-level shape first.
    if project_files:
        readme_chunks = []
        for p in project_files:
            try:
                txt = p.read_text(errors="replace")[:1600]
                readme_chunks.append(f"# Project: {p.parent.name} ({p.name})\n{txt}")
            except Exception:
                continue
        if readme_chunks:
            ctx_text = "\n\n---\n\n".join(readme_chunks) + "\n\n---\n\n" + ctx_text
            on_screen(f"[dim]Augmented with {len(readme_chunks)} project README(s): "
                      f"{', '.join(p.parent.name for p in project_files)}[/dim]")

    system = (
        "You are answering using ONLY the org-roam notes the user has "
        "retrieved and pasted below. The notes ARE the user's data — they "
        "are giving them to you directly in this prompt. You DO have access "
        "to them. NEVER reply with 'I don't have access', 'please share', "
        "'the actual files are not present', or any variant. If a note is "
        "named like a date (e.g. `2026-04-12`) and is in a `daily/` folder, "
        "treat it as a daily journal entry — its sub-headings ARE the day's "
        "content. If the user asked about a time window but the retrieved "
        "notes are from outside it, summarise what you HAVE and explicitly "
        "name the dates you found. Be concise. Cite note titles in backticks. "
        "When asked for bullets, output bullets, not paragraphs of caveats."
    )
    # Prepend the user's current-truth context so the LLM knows which
    # facts override stale info in older notes. Also include the LLM-
    # generated history narrative under HISTORICAL CONTEXT — stale notes
    # are still valuable as background, just not as current truth.
    try:
        from . import context as _ctx
        context_block = _ctx.render_context_block()
        history_block = _ctx.render_history_block()
        if context_block:
            system = system + context_block + (
                "\nIf any retrieved note contradicts USER CONTEXT, prefer "
                "USER CONTEXT and call out the contradiction. Notes tagged "
                ":stale: should be cited only when the user asks for "
                "history."
            )
        if history_block:
            system = system + history_block + (
                "\nUse HISTORICAL CONTEXT to add temporal background, but "
                "anchor present-tense answers in USER CONTEXT and the "
                "retrieved notes."
            )
    except Exception:
        pass
    window_note = ""
    if days_window and not filter_relaxed:
        window_note = (f"\n\nThe user asked about the last {days_window} days. "
                       f"Some retrieved notes are from that window; some are "
                       f"recent files from a {'/'.join(path_hints) or 'matched'} folder "
                       "regardless of mtime so you have content to work with."
                       if path_hints else
                       f"\n\nThese notes are filtered to mtime within the last {days_window} days.")
    if filter_relaxed:
        window_note = (f"\n\nNote: the user asked about the last {days_window} days, "
                       f"but no notes match that window — these {len(results)} are "
                       "the closest matches available. Use them; mention the dates "
                       "they're actually from.")
    tag_note = ""
    if invalid_tag_suggestions:
        bits = []
        for missing, suggestions in invalid_tag_suggestions.items():
            if suggestions:
                bits.append(f"'{missing}' (no such tag; closest existing: {', '.join(suggestions)})")
            else:
                bits.append(f"'{missing}' (no such tag, no close match)")
        tag_note = (f"\n\nThe user referenced these tags that DO NOT exist in their vault: "
                    f"{'; '.join(bits)}. Tell them which tags they actually have based "
                    "on the retrieved notes' :tags: field, and answer using those.")
    if valid_tags:
        tag_note += f"\n\nNotes explicitly tagged {', '.join(valid_tags)} are PREPENDED in the list below — use them."
    prompt = (f"Question: {query}\n\n"
              f"Retrieved notes ({len(results)}):{window_note}{tag_note}\n\n"
              f"{ctx_text}")

    if cloud_:
        # Resolve a sensible local fallback in case the cloud is rate-limited
        # or refuses the key. Use chat_model from config OR the same model
        # the user requested if it happens to also be pulled locally.
        with get_session(engine) as session:
            local_fallback = (_cfg(session, "chat_model")
                              or MODEL_DEFAULTS["chat_model"])
        with thinking("Asking cloud", model=chat_mdl):
            try:
                answer = _cloud_chat_with_local_fallback(
                    prompt, cloud_model=chat_mdl,
                    cloud_endpoint=cloud_endpoint, cloud_api_key=api_key,
                    local_model=local_fallback, local_url=url, system=system,
                )
            except Exception as e:
                red_alert(f"Cloud chat failed: {e}")
                raise typer.Exit(1)
        label = f"{cloud_provider}:{chat_mdl}"
    else:
        with thinking("Asking", model=chat_mdl):
            answer = _local_chat_or_friendly_error(
                prompt, model=chat_mdl, base_url=url, system=system,
                cloud_hint=f"org-llm ask --cloud {query!r}",
            )
        label = chat_mdl

    console.print()
    console.rule(f"[lcars2]{label}[/lcars2]")
    console.print(answer)
    console.rule()


_TASK_MODEL_KEYS = [
    ("embed",    "embed_model",    "Semantic search embeddings"),
    ("chat",     "chat_model",     "ask / general Q&A"),
    ("code",     "code_model",     "Code generation"),
    ("reason",   "reason_model",   "Planning & complex reasoning"),
    ("fast",     "fast_model",     "Tagging & classification"),
    ("instruct", "instruct_model", "Capture & instruction following"),
    ("text",     "text_model",     "Summarization & text analysis"),
]


@app.command(rich_help_panel="Models & Cloud")
def models(
    discover: Annotated[bool, typer.Option("--discover", "-d",
              help="Show FOSS catalog filtered by hardware")] = False,
    tune:     Annotated[bool, typer.Option("--tune",     "-t",
              help="Analyze current config and recommend upgrades (read-only)")] = False,
    apply:    Annotated[bool, typer.Option("--apply", "-A",
              help="With --tune: actually apply recommendations (default: read-only)")] = False,
    pull:     Annotated[str,  typer.Option("--pull",     "-p",
              help="Pull a model via Ollama")] = "",
    assign:   Annotated[bool, typer.Option("--assign",   "-a",
              help="Interactively assign models to roles")] = False,
):
    """Show, discover, tune, and manage FOSS LLM assignments."""
    from rich.panel import Panel
    from .models import (
        CATALOG, ROLE_KEYS, fitting_hardware, recommendations,
    )
    from .cloud import local_vram_gb, local_ram_gb

    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)
        current = {role: _cfg(session, key) for role, key, _ in _TASK_MODEL_KEYS}

    pulled = _pulled_normalized(url)

    vram_gb = local_vram_gb()
    ram_gb  = local_ram_gb()

    # ── pull a model ─────────────────────────────────────────────────────────
    if pull:
        import subprocess, shutil
        ollama_exe = shutil.which("ollama") or str(Path("~/.local/bin/ollama").expanduser())
        hail(f"Pulling {pull}…")
        result = subprocess.run([ollama_exe, "pull", pull])
        if result.returncode == 0:
            hail(f"Pulled: {pull}")
            make_it_so()
        else:
            red_alert(f"Pull failed for {pull}")
        return

    # ── discover FOSS catalog ─────────────────────────────────────────────────
    if discover:
        console.rule("[lcars1]FOSS LLM Catalog[/lcars1]")
        hw_info = (f"{vram_gb:.0f}GB VRAM" if vram_gb else f"{ram_gb:.0f}GB RAM (CPU)")
        hail(f"Hardware: {hw_info}  |  showing models that fit + all others")
        console.print()

        fits = {m.tag for m in fitting_hardware(vram_gb, ram_gb)}
        # Drop License + Description columns on narrow terminals so the
        # critical Model / Params / VRAM / Roles columns render cleanly.
        narrow = console.width < 110
        tbl = Table(box=None, pad_edge=False, show_header=True)
        tbl.add_column("Fits",   width=4,  no_wrap=True)
        tbl.add_column("Pulled", width=6,  no_wrap=True)
        tbl.add_column("Model",  style="lcars2", no_wrap=True, max_width=22, overflow="ellipsis")
        tbl.add_column("Params", width=6, style="dim",   no_wrap=True)
        tbl.add_column("VRAM",   width=6, style="lcars3",no_wrap=True)
        if not narrow:
            tbl.add_column("License", style="dim",   no_wrap=True, max_width=18, overflow="ellipsis")
        tbl.add_column("Roles",  style="lcars1", no_wrap=True, max_width=22, overflow="ellipsis")
        if not narrow:
            tbl.add_column("Description", style="dim", overflow="fold", min_width=20)

        prev_role_group = ""
        for m in CATALOG:
            role_group = m.roles[0]
            if role_group != prev_role_group:
                blanks = (8 if not narrow else 6)
                tbl.add_row(*([""] * blanks), style="dim")
                prev_role_group = role_group
            fit_sym  = "[bold green]✓[/]" if m.tag in fits else "[dim]→cloud[/]"
            pull_sym = "[bold cyan]✓[/]"  if _is_pulled(m.tag, pulled) else ""
            row = [fit_sym, pull_sym, m.tag, m.params, f"{m.vram_gb:.1f}G"]
            if not narrow:
                row.append(m.license)
            row.append(" ".join(m.roles))
            if not narrow:
                row.append(m.description)
            tbl.add_row(*row)
        console.print(tbl)
        if narrow:
            on_screen("[dim]Narrow terminal — License + Description columns hidden. "
                      "Widen >= 110 cols to see them.[/dim]")
        console.print()
        on_screen(f"Pull any model: [bold]org-llm models --pull <tag>[/bold]")
        on_screen(f"Tune assignments: [bold]org-llm models --tune[/bold]")
        return

    # ── tune recommendations ──────────────────────────────────────────────────
    if tune:
        console.rule("[lcars1]LLM Tuning Advisor[/lcars1]")
        hw_info = (f"{vram_gb:.0f}GB VRAM" if vram_gb else f"{ram_gb:.0f}GB RAM (CPU)")
        hail(f"Hardware: {hw_info}")

        recs = recommendations(current, pulled, vram_gb, ram_gb)

        if not recs:
            console.print("\n[bold green]✓  All model assignments are optimal for your hardware.[/bold green]\n")
            make_it_so()
            return

        console.print()
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("Role",      style="lcars1",  no_wrap=True)
        tbl.add_column("Current",   style="dim",     no_wrap=True)
        tbl.add_column("→",         width=2)
        tbl.add_column("Suggested", style="lcars2",  no_wrap=True)
        tbl.add_column("License",   style="dim",     no_wrap=True)
        tbl.add_column("VRAM",      style="lcars3",  width=6)
        tbl.add_column("Why",       style="dim")

        for rec in recs:
            arrow = "[bold yellow]↑[/]" if rec["upgrade"] else "[bold green]+[/]"
            tbl.add_row(
                rec["role"], rec["current"], arrow,
                rec["suggested"], rec["license"], f"{rec['vram']:.1f}G", rec["reason"],
            )
        console.print(tbl)
        console.print()

        if not apply:
            on_screen("Read-only — re-run with [bold]--apply[/bold] to write these changes.")
            return
        if typer.confirm("Apply all recommendations?", default=False):
            from .db import Config as Cfg
            role_to_key = {role: key for role, key, _ in _TASK_MODEL_KEYS}
            with get_session(engine) as session:
                for rec in recs:
                    key = role_to_key.get(rec["role"])
                    if not key:
                        continue
                    row = session.get(Cfg, key)
                    if row:
                        row.value = rec["suggested"]
                    else:
                        session.add(Cfg(key=key, value=rec["suggested"]))
                session.commit()
            hail("Config updated. Pull new models with: org-llm models --pull <tag>")
            make_it_so()
        return

    # ── interactive assign ────────────────────────────────────────────────────
    if assign:
        console.rule("[lcars1]Model Assignment Wizard[/lcars1]")
        fits = [m.tag for m in fitting_hardware(vram_gb, ram_gb)]
        from .db import Config as Cfg
        with get_session(engine) as session:
            for role, key, purpose in _TASK_MODEL_KEYS:
                cur = _cfg(session, key) or "—"
                options = [m for m in fits
                           if role in next((c.roles for c in CATALOG if c.tag == m), ())]
                if not options:
                    options = fits[:10]
                console.print(f"\n[lcars1]{role}[/lcars1] ({purpose})  current: [lcars2]{cur}[/lcars2]")
                for i, opt in enumerate(options[:8], 1):
                    pulled_mark = " [cyan]✓ pulled[/cyan]" if _is_pulled(opt, pulled) else ""
                    console.print(f"  {i}. {opt}{pulled_mark}")
                choice = typer.prompt(
                    f"Enter number or model tag (blank = keep {cur})", default=""
                )
                if not choice:
                    continue
                new_val = (
                    options[int(choice) - 1]
                    if choice.isdigit() and 1 <= int(choice) <= len(options)
                    else choice
                )
                row = session.get(Cfg, key)
                if row:
                    row.value = new_val
                else:
                    session.add(Cfg(key=key, value=new_val))
            session.commit()
        hail("Model assignments saved.")
        make_it_so()
        return

    # ── default: show current assignments + pulled models ─────────────────────
    console.rule("[lcars1]Model Assignments[/lcars1]")
    assign_tbl = Table(box=None, pad_edge=False)
    assign_tbl.add_column("Role",    style="lcars1")
    assign_tbl.add_column("Model",   style="lcars2")
    assign_tbl.add_column("Purpose", style="dim")
    assign_tbl.add_column("Status",  width=12)
    for role, key, purpose in _TASK_MODEL_KEYS:
        m = current.get(role) or "—"
        status = (
            "[green]✓ pulled[/green]" if _is_pulled(m, pulled)
            else ("[dim]—[/dim]" if m == "—" else "[yellow]not pulled[/yellow]")
        )
        assign_tbl.add_row(role, m, purpose, status)
    console.print(assign_tbl)

    console.print()
    if pulled:
        pull_tbl = Table(title="Pulled in Ollama", box=None, pad_edge=False)
        pull_tbl.add_column("Model", style="lcars3")
        for name in sorted(pulled):
            pull_tbl.add_row(name)
        console.print(pull_tbl)
    else:
        console.print("[dim]Ollama not reachable or no models pulled.[/dim]")

    console.print()
    # LLM-driven recommendation: feed the model state to fast_model and ask
    # for one concrete next step. Falls back to deterministic template if
    # the LLM is unavailable.
    unassigned = [role for role, _, _ in _TASK_MODEL_KEYS
                  if not (current.get(role) or "").strip() or current.get(role) == "—"]
    not_pulled = [(role, current[role]) for role, _, _ in _TASK_MODEL_KEYS
                  if current.get(role) and current[role] != "—"
                  and not _is_pulled(current[role], pulled)]
    state_lines = []
    for role, _, purpose in _TASK_MODEL_KEYS:
        m = current.get(role) or "—"
        status = ("✓ pulled" if _is_pulled(m, pulled) else
                  "—" if m == "—" else "not pulled")
        state_lines.append(f"  {role}={m}  [{status}]  ({purpose})")
    state = "\n".join(state_lines)
    sys_msg = (
        "You recommend ONE concrete next command for an org-llm user "
        "given their FOSS model assignment state. Output the command "
        "wrapped in [bold]…[/bold] markup with one short reason "
        "before it. Format: 'Next: <reason> — [bold]org-llm <cmd>[/bold]'. "
        "≤ 22 words. Pick from: models --tune --apply, models --pull "
        "<tag>, performance --apply, performance --benchmark, install. "
        "Choose the most pressing single gap."
    )
    user_msg = (
        f"Model role assignments:\n{state}\n\n"
        f"Pulled in Ollama: {sorted(pulled) if pulled else '(none)'}\n\n"
        "What's the single most useful next command?"
    )
    llm_rec = _llm_one_liner(user_msg, system=sys_msg, fallback="")
    if llm_rec:
        on_screen(llm_rec)
    elif unassigned:
        role = unassigned[0]
        on_screen(f"[dim]Next:[/dim] {role} is unassigned — "
                  f"[bold]org-llm models --tune --apply[/bold] picks one for your hardware")
    elif not_pulled:
        role, model = not_pulled[0]
        on_screen(f"[dim]Next:[/dim] {role}={model} is configured but not pulled — "
                  f"[bold]org-llm models --pull {model}[/bold]")
    elif not pulled:
        on_screen("[dim]Next:[/dim] no Ollama models pulled — "
                  "[bold]org-llm install-tools --skip-fonts --skip-opencode --skip-gh --skip-claude[/bold]")
    else:
        on_screen("[dim]All roles assigned and pulled.[/dim] "
                  "[bold]org-llm performance --benchmark[/bold] "
                  "measures real tok/s if you want tuning data.")


_CONFIG_VALIDATORS = {
    "ollama_url":         "url",
    "cloud_endpoint_url": "url",
    "embed_dim":          "int",
    "theme":              ("dark", "light"),
    "db_version":         "int",
    # Theme intensity dials — accept 0..3 only
    "trek_level":         ("0", "1", "2", "3"),
    "commie_level":       ("0", "1", "2", "3"),
    "queer_level":        ("0", "1", "2", "3"),
}


def _validate_config(key: str, value: str) -> str | None:
    """Return None if value is acceptable for key, else a human-readable error."""
    rule = _CONFIG_VALIDATORS.get(key)
    if rule is None:
        return None  # unknown keys are accepted (custom user keys are fine)
    if rule == "url":
        if not (value.startswith("http://") or value.startswith("https://")):
            return f"{key!r} must be an http(s) URL (got {value!r})"
    elif rule == "int":
        if not value.lstrip("-").isdigit():
            return f"{key!r} must be an integer (got {value!r})"
    elif isinstance(rule, tuple):
        if value not in rule:
            return f"{key!r} must be one of {rule} (got {value!r})"
    return None


@app.command(rich_help_panel="Models & Cloud")
def performance(
    apply:     Annotated[bool, typer.Option("--apply", "-a",
               help="Write recommended role assignments to config (default: read-only report)")] = False,
    benchmark: Annotated[bool, typer.Option("--benchmark", "-b",
               help="Run per-model timing tests (slow — ~1-3 min depending on pulled models)")] = False,
    quick:     Annotated[bool, typer.Option("--quick", "-q",
               help="Hardware probe only; skip Ollama probe and model recommendations")] = False,
):
    """Tune org-llm to your real hardware.

    Probes free RAM/VRAM (not total — you have other apps open), pings Ollama,
    optionally benchmarks each pulled chat/embed model for tokens-per-second,
    then recommends role assignments that:
      • fit your *available* memory
      • prefer already-pulled models (no download required)
      • flag oversize current assignments as DOWNGRADE NEEDED
      • route to the configured cloud backend when nothing fits locally

    With --apply, recommendations are written to the config DB.
    """
    from rich.panel import Panel
    from rich.table import Table as _T
    from . import performance as perf

    console.rule(f"[lcars1]Performance probe  ·  stardate {__import__('org_llm.ui', fromlist=['stardate']).stardate()}[/lcars1]")
    console.print()

    # ── Hardware ──────────────────────────────────────────────────────────────
    with warp("Probing hardware"):
        hw = perf.probe_hardware()

    hw_tbl = _T(box=None, pad_edge=False, show_header=False)
    hw_tbl.add_column("Key",   style="lcars1", width=18, no_wrap=True)
    hw_tbl.add_column("Value", style="lcars2", overflow="fold")
    # Trim the obvious noise from CPU model strings so the panel stays tight
    cpu_clean = (hw.cpu_model.replace("(R)", "").replace("(TM)", "")
                            .replace("11th Gen ", "11th-Gen ").strip())
    hw_tbl.add_row("CPU",        f"{cpu_clean}  ({hw.cpu_count} cores)")
    hw_tbl.add_row("RAM total",  f"{hw.ram_total_gb:.1f} GB")
    hw_tbl.add_row("RAM free",   f"[bold]{hw.ram_free_gb:.1f} GB[/bold]"
                                  + ("  [yellow](used as budget)[/yellow]"
                                     if hw.vram_total_gb is None else ""))
    if hw.vram_total_gb is not None:
        hw_tbl.add_row("VRAM total", f"{hw.vram_total_gb:.1f} GB")
        hw_tbl.add_row("VRAM free",  f"[bold]{hw.vram_free_gb:.1f} GB[/bold]  [yellow](used as budget)[/yellow]")
    else:
        hw_tbl.add_row("GPU",        "[dim]none detected (CPU inference only)[/dim]")
    hw_tbl.add_row("Disk free",  f"{hw.disk_free_gb:.0f} GB (in $HOME)")
    console.print(Panel(hw_tbl, title="[lcars1]Hardware[/lcars1]", border_style="lcars2"))

    if quick:
        return

    # ── Ollama state + cloud check ────────────────────────────────────────────
    engine = _engine()
    try:
        with get_session(engine) as session:
            url      = _ollama_url(session)
            current  = {role: _cfg(session, key) for role, key, _ in _TASK_MODEL_KEYS}
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
    except Exception as exc:
        if "no such table" in str(exc):
            red_alert("Database not initialised. Run: [bold]org-llm init[/bold] first.")
        else:
            red_alert(f"Config read failed: {exc}")
        raise typer.Exit(1)

    pulled_norm = _pulled_normalized(url)
    if not pulled_norm:
        red_alert(f"Ollama not reachable at {url}. Start it (or run: org-llm doctor --fix).")
        on_screen("Cloud-only recommendations still possible if you have --cloud configured.")
        if not cloud_endpoint:
            raise typer.Exit(1)

    console.print()
    pulled_tbl = _T(box=None, pad_edge=False)
    pulled_tbl.add_column("Pulled tag", style="lcars3")
    if pulled_norm:
        for p in sorted(pulled_norm):
            pulled_tbl.add_row(p)
        console.print(Panel(pulled_tbl,
                            title=f"[lcars1]Local models  ({len(pulled_norm)} pulled)[/lcars1]",
                            border_style="lcars3"))

    # ── Optional benchmarks ───────────────────────────────────────────────────
    benchmarks: dict[str, perf.BenchmarkResult] = {}
    if benchmark and pulled_norm:
        on_screen("Benchmarking each pulled chat-capable model (1 short prompt each)…")
        for tag in sorted(pulled_norm):
            full_tag = tag + (":latest" if ":" not in tag else "")
            if _is_embed_model(full_tag):
                bench = perf.benchmark_embed_model(full_tag, url)
            else:
                bench = perf.benchmark_chat_model(full_tag, url)
            benchmarks[tag] = bench

        bench_tbl = _T(box=None, pad_edge=False)
        bench_tbl.add_column("Model",        style="lcars2")
        bench_tbl.add_column("Role",         style="dim", width=7)
        bench_tbl.add_column("Latency",      style="lcars3", justify="right", width=10)
        bench_tbl.add_column("Tokens/s",     style="lcars1", justify="right", width=10)
        bench_tbl.add_column("Notes",        style="dim")
        for tag, b in benchmarks.items():
            if b.error:
                bench_tbl.add_row(tag, b.role, "—", "—", f"[red]{b.error}[/red]")
            else:
                bench_tbl.add_row(tag, b.role,
                                   f"{b.latency_ms:.0f} ms",
                                   f"{b.tokens_per_sec:.1f}", "")
        console.print()
        console.print(Panel(bench_tbl, title="[lcars1]Measured throughput[/lcars1]",
                            border_style="lcars2"))

    # ── Recommendations ───────────────────────────────────────────────────────
    recs = perf.recommend(
        hw, current, benchmarks, pulled_norm,
        cloud_configured=bool(cloud_endpoint),
    )
    rec_tbl = _T(box=None, pad_edge=False)
    rec_tbl.add_column("Role",      style="lcars1", width=10,   no_wrap=True)
    rec_tbl.add_column("Current",   style="dim",                no_wrap=True, max_width=20, overflow="ellipsis")
    rec_tbl.add_column("→",         width=2)
    rec_tbl.add_column("Suggested", style="lcars2",             no_wrap=True, max_width=24, overflow="ellipsis")
    rec_tbl.add_column("Why",       style="dim",                overflow="fold", min_width=24)
    rec_tbl.add_column("tok/s",     style="lcars3", justify="right", width=7)

    severity_arrow = {
        "downgrade": "[bold red]↓[/]",
        "upgrade":   "[bold yellow]↑[/]",
        "missing":   "[bold cyan]+[/]",
        "fit":       "[bold green]=[/]",
    }

    has_changes = False
    for r in recs:
        arrow = severity_arrow.get(r.severity, "·")
        tps   = f"{r.measured_tps:.1f}" if r.measured_tps is not None else "—"
        rec_tbl.add_row(r.role, r.current, arrow, r.suggested, r.reason, tps)
        if r.severity in ("upgrade", "downgrade", "missing"):
            has_changes = True

    console.print()
    console.print(Panel(rec_tbl, title="[lcars1]Recommendations[/lcars1]",
                        border_style="lcars2"))
    console.print()

    if not has_changes:
        on_screen("[bold green]✓  Your role assignments already fit this hardware.[/bold green]")
        make_it_so()
        return

    if not apply:
        on_screen("Read-only report. Re-run with [bold]--apply[/bold] to write these changes.")
        on_screen("Or override individual roles: [bold]org-llm config <role>_model <tag>[/bold]")
        return

    # ── Apply ────────────────────────────────────────────────────────────────
    from .db import Config as Cfg
    role_to_key = {role: key for role, key, _ in _TASK_MODEL_KEYS}
    with get_session(engine) as session:
        for r in recs:
            if r.severity not in ("upgrade", "downgrade", "missing"):
                continue
            key = role_to_key.get(r.role)
            if not key:
                continue
            row = session.get(Cfg, key)
            if row:
                row.value = r.suggested
            else:
                session.add(Cfg(key=key, value=r.suggested))
        session.commit()
    hail("Config updated.")
    on_screen("Pull any newly-suggested models with: [bold]org-llm doctor --fix[/bold]")
    make_it_so()


@app.command(rich_help_panel="Maintenance")
def config(
    key:   Annotated[str, typer.Argument(help="Config key")] = "",
    value: Annotated[str, typer.Argument(help="Value to set")] = "",
):
    """Get or set a config value. No args = show all."""
    import difflib as _dl
    from .db import Config as Cfg
    engine = _engine()
    with get_session(engine) as session:
        if not key:
            table = Table(box=None, pad_edge=False)
            table.add_column("Key",   style="lcars1")
            table.add_column("Value", style="lcars2")
            for row in session.query(Cfg).order_by(Cfg.key):
                table.add_row(row.key, row.value)
            console.print(table)
        elif not value:
            row = session.get(Cfg, key)
            if row:
                console.print(row.value)
            else:
                # Unknown / unset — fuzzy-match against actual rows + defaults.
                known = {r.key for r in session.query(Cfg).all()} | set(MODEL_DEFAULTS.keys())
                guess = _dl.get_close_matches(key, sorted(known), n=3, cutoff=0.55)
                console.print("[error]not set[/error]")
                if guess:
                    on_screen(f"[dim]Did you mean: {', '.join(guess)}?[/dim]")
        else:
            err = _validate_config(key, value)
            if err:
                red_alert(err)
                # Suggest a likely correct key on validation failure too.
                known = {r.key for r in session.query(Cfg).all()} | set(MODEL_DEFAULTS.keys())
                guess = _dl.get_close_matches(key, sorted(known), n=3, cutoff=0.55)
                if guess:
                    on_screen(f"[dim]Did you mean: {', '.join(guess)}?[/dim]")
                raise typer.Exit(1)
            row = session.get(Cfg, key)
            if row:
                row.value = value
            else:
                # Brand-new key — warn if it doesn't fuzzy-match any known key.
                # We allow unknown keys (extension knobs use them) but the user
                # should know if they're typing something nobody else reads.
                known = {r.key for r in session.query(Cfg).all()} | set(MODEL_DEFAULTS.keys())
                if key not in known:
                    guess = _dl.get_close_matches(key, sorted(known), n=2, cutoff=0.7)
                    if guess:
                        on_screen(f"[yellow]New key {key!r} — "
                                  f"did you mean {' / '.join(guess)}?[/yellow]")
                session.add(Cfg(key=key, value=value))
            session.commit()
            hail(f"{key} = {value}")
            make_it_so()


@app.command(name="db", rich_help_panel="Maintenance")
def db_info(
    schema: Annotated[bool, typer.Option("--schema", "-s", help="Show CREATE TABLE statements")] = False,
    dict_:  Annotated[bool, typer.Option("--dict",   "-d", help="Print full data dictionary")] = False,
    query:  Annotated[str,  typer.Option("--query", "-q", help="Run a raw SQL SELECT")] = "",
):
    """Inspect the SQLite database: row counts, schema, data dictionary, or raw SQL."""
    from rich.panel  import Panel
    from rich.syntax import Syntax
    from sqlalchemy  import inspect, text

    engine = _engine()

    # ── tutor data dictionary ─────────────────────────────────────────────────
    if dict_:
        from .ui import trans_stripe
        console.rule("[lcars1]org-llm Data Dictionary[/lcars1]")
        console.print()
        console.print(trans_stripe(52))
        # Re-use the db tutor step body
        match = next(((n, b) for n, b in _TUTOR_STEPS if n == "db"), None)
        if match:
            from rich.panel import Panel as P
            console.print(P(match[1], title="[lcars1]db[/lcars1]", border_style="lcars2",
                            padding=(1, 2)))
        return

    # ── raw SQL query ─────────────────────────────────────────────────────────
    if query:
        head = query.lstrip().upper()
        if not head:
            red_alert("Empty query.")
            raise typer.Exit(1)
        # Read-only allow-list: SELECT, WITH (CTEs), EXPLAIN, PRAGMA TABLE_*
        allowed = ("SELECT", "WITH ", "EXPLAIN ", "PRAGMA TABLE_INFO", "PRAGMA TABLE_LIST")
        if not any(head.startswith(prefix) for prefix in allowed):
            red_alert("Only read-only queries are allowed (SELECT / WITH / EXPLAIN / PRAGMA TABLE_INFO).")
            raise typer.Exit(1)
        with engine.connect() as conn:
            try:
                rows = list(conn.execute(text(query)))
                if not rows:
                    on_screen("(no rows)")
                    return
                tbl = Table(box=None, pad_edge=False)
                for col in rows[0]._fields:
                    tbl.add_column(col, style="lcars2")
                for row in rows[:200]:
                    tbl.add_row(*[str(v) for v in row])
                console.print(tbl)
                if len(rows) > 200:
                    on_screen(f"(showing 200 of {len(rows)} rows)")
            except Exception as exc:
                red_alert(f"Query failed: {exc}")
                raise typer.Exit(1)
        return

    # ── CREATE TABLE schema ───────────────────────────────────────────────────
    if schema:
        console.rule("[lcars1]Schema[/lcars1]")
        with engine.connect() as conn:
            for row in conn.execute(text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND sql IS NOT NULL ORDER BY name"
            )):
                console.print(f"\n[lcars1]{row.name}[/lcars1]")
                console.print(Syntax(row.sql, "sql", theme="monokai"))
        return

    # ── default: row counts + sample ─────────────────────────────────────────
    db_path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    console.rule("[lcars1]Database Overview[/lcars1]")
    hail(f"File: {db_path}")
    console.print()

    tables_meta = [
        ("files",   "One row per indexed .org file"),
        ("nodes",   "One row per org-mode heading (+ optional embedding)"),
        ("history", "LLM interaction log"),
        ("config",  "Key/value settings"),
    ]
    stats_tbl = Table(box=None, pad_edge=False)
    stats_tbl.add_column("Table",       style="lcars1")
    stats_tbl.add_column("Rows",        style="lcars2", justify="right")
    stats_tbl.add_column("Description", style="dim")

    with engine.connect() as conn:
        for tname, desc in tables_meta:
            try:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar()
            except Exception:
                count = "?"
            stats_tbl.add_row(tname, str(count), desc)
        # also show dbt views if present
        views = [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ))]
        if views:
            stats_tbl.add_row("", "", "")
            for v in views:
                try:
                    count = conn.execute(text(f"SELECT COUNT(*) FROM {v}")).scalar()
                except Exception:
                    count = "?"
                stats_tbl.add_row(f"[dim]{v}[/dim]", str(count), "[dim]dbt view[/dim]")

    console.print(stats_tbl)
    console.print()
    on_screen("[dim]Tip:[/dim]  --schema  │  --dict  │  --query 'SELECT ...'")
    on_screen("         org-llm tutor db  — full data dictionary")


def _opencode_bin() -> Path | None:
    """Return path to opencode binary if it exists anywhere on PATH or known locations."""
    import shutil
    found = shutil.which("opencode")
    if found:
        return Path(found)
    for candidate in [
        Path("~/.opencode/bin/opencode").expanduser(),
        Path("~/.local/bin/opencode").expanduser(),
    ]:
        if candidate.exists():
            return candidate
    return None


def _install_opencode_bin(bin_dir: Path) -> Path | None:
    """Download and install opencode using its official installer. Returns path or None."""
    import subprocess
    import os
    # The opencode installer always installs to ~/.opencode/bin regardless of env vars
    default_install = Path("~/.opencode/bin/opencode").expanduser()
    install_sh = bin_dir / "_opencode_install.sh"
    try:
        dl = subprocess.run(
            ["curl", "-fsSL", "-o", str(install_sh), "https://opencode.ai/install"],
            capture_output=True, text=True, timeout=30,
        )
        if dl.returncode != 0:
            red_alert(f"opencode download failed: {dl.stderr.strip()[:120]}")
            return None
        install_sh.chmod(0o755)
        run = subprocess.run(
            ["sh", str(install_sh)],
            capture_output=True, text=True,
            env={**os.environ},
        )
        install_sh.unlink(missing_ok=True)
        result = _opencode_bin()
        if result:
            hail(f"opencode installed at {result}")
            return result
        red_alert(f"opencode install failed: {run.stderr.strip()[:200]}")
        return None
    except Exception as exc:
        install_sh.unlink(missing_ok=True)
        red_alert(f"opencode install error: {exc}")
        return None


def _gh_bin() -> str | None:
    """Return path to gh CLI, or None if not available."""
    import shutil
    return shutil.which("gh") or (
        str(Path("~/.local/bin/gh").expanduser())
        if Path("~/.local/bin/gh").expanduser().exists() else None
    )


def _claude_bin() -> Path | None:
    """Return path to claude CLI if installed anywhere."""
    import shutil
    found = shutil.which("claude")
    if found:
        return Path(found)
    for candidate in [
        Path("~/.claude/bin/claude").expanduser(),
        Path("~/.local/bin/claude").expanduser(),
        Path("~/.npm-global/bin/claude").expanduser(),
        Path("~/.local/share/npm/bin/claude").expanduser(),
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.exists():
            return candidate
    return None


def _install_claude_bin() -> Path | None:
    """Install claude CLI via npm. Returns path or None."""
    import subprocess, shutil
    npm = shutil.which("npm")
    if not npm:
        red_alert("npm not found — install Node.js to get Claude Code  (nodejs.org)")
        return None
    hail("Installing @anthropic-ai/claude-code via npm…")
    result = subprocess.run(
        [npm, "install", "-g", "@anthropic-ai/claude-code"],
        timeout=120,
    )
    if result.returncode != 0:
        red_alert("claude install failed — check npm output above")
        return None
    return _claude_bin()


def _install_gh(bin_dir: Path) -> Path | None:
    """Download and install gh CLI to bin_dir. Returns path or None on failure."""
    import platform
    import urllib.request
    import json

    arch = platform.machine().lower()
    arch_slug = "amd64" if arch in ("x86_64", "amd64") else "arm64"
    try:
        with urllib.request.urlopen(
            "https://api.github.com/repos/cli/cli/releases/latest", timeout=10
        ) as r:
            release = json.loads(r.read())
        version = release["tag_name"].lstrip("v")
        url = (
            f"https://github.com/cli/cli/releases/download/v{version}/"
            f"gh_{version}_linux_{arch_slug}.tar.gz"
        )
        tmp = Path("/tmp/gh.tar.gz")
        with warp("Downloading gh CLI"):
            urllib.request.urlretrieve(url, tmp)
        import subprocess, tarfile
        with tarfile.open(tmp) as tf:
            for member in tf.getmembers():
                if member.name.endswith("/bin/gh"):
                    member.name = "gh"
                    tf.extract(member, path=bin_dir)
                    break
        gh = bin_dir / "gh"
        gh.chmod(0o755)
        tmp.unlink(missing_ok=True)
        hail(f"gh CLI v{version} installed at {gh}")
        return gh
    except Exception as exc:
        red_alert(f"gh install failed: {exc}")
        return None


@app.command(name="install-tools", rich_help_panel="Onboarding")
def install_tools(
    skip_ollama:   Annotated[bool, typer.Option("--skip-ollama",   "-O")] = False,
    skip_models:   Annotated[bool, typer.Option("--skip-models",   "-M")] = False,
    skip_fonts:    Annotated[bool, typer.Option("--skip-fonts",    "-F")] = False,
    skip_opencode: Annotated[bool, typer.Option("--skip-opencode", "-P")] = False,
    skip_gh:       Annotated[bool, typer.Option("--skip-gh",       "-G")] = False,
    skip_claude:   Annotated[bool, typer.Option("--skip-claude",   "-K")] = False,
    skip_pass:     Annotated[bool, typer.Option("--skip-pass",     "-A")] = False,
):
    """Install Ollama, models, Nerd Fonts, opencode, gh CLI, Claude Code, and pass."""
    import platform
    import shutil
    import subprocess
    import urllib.request
    from .ui import solidarity, trans_stripe

    solidarity()
    console.print()

    bin_dir   = Path("~/.local/bin").expanduser()
    font_dir  = Path("~/.local/share/fonts/NerdFonts").expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    font_dir.mkdir(parents=True, exist_ok=True)

    ollama_bin = bin_dir / "ollama"

    # ── Ollama ────────────────────────────────────────────────────────────────
    if not skip_ollama:
        if shutil.which("ollama") or ollama_bin.exists():
            hail("Ollama already installed — skipping binary download.")
        else:
            arch = platform.machine().lower()
            arch_slug = "amd64" if arch in ("x86_64", "amd64") else "arm64"
            url = (
                f"https://github.com/ollama/ollama/releases/latest/download/"
                f"ollama-linux-{arch_slug}"
            )
            hail(f"Downloading Ollama ({arch_slug}) → {ollama_bin}")
            with warp("Beaming Ollama aboard"):
                urllib.request.urlretrieve(url, ollama_bin)
            ollama_bin.chmod(0o755)
            hail(f"Ollama installed at {ollama_bin}")

        # Start ollama serve in background if not already running
        try:
            import ollama as _ol
            _ol.Client(host="http://localhost:11434").list()
            hail("Ollama is already running.")
        except Exception:
            hail("Starting ollama serve in background…")
            subprocess.Popen(
                [str(ollama_bin if ollama_bin.exists() else shutil.which("ollama")),
                 "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import time; time.sleep(2)

    # ── Models ────────────────────────────────────────────────────────────────
    if not skip_models:
        engine = _engine()
        with get_session(engine) as session:
            url = _ollama_url(session)
            model_keys = [
                "embed_model", "chat_model", "code_model",
                "reason_model", "fast_model", "instruct_model", "text_model",
            ]
            models_to_pull = list({_cfg(session, k) for k in model_keys
                                   if _cfg(session, k)})

        hail(f"Pulling {len(models_to_pull)} models via Ollama…")
        ollama_exe = shutil.which("ollama") or str(ollama_bin)
        with impulse("Pulling models", total=len(models_to_pull)) as (prog, task):
            for model in models_to_pull:
                on_screen(f"  ollama pull {model}")
                result = subprocess.run(
                    [ollama_exe, "pull", model],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    red_alert(f"Failed to pull {model}: {result.stderr.strip()}")
                prog.advance(task)

    # ── Nerd Fonts ────────────────────────────────────────────────────────────
    if not skip_fonts:
        font_name = "NotoMono"
        font_zip  = font_dir / f"{font_name}.tar.xz"
        font_url  = (
            f"https://github.com/ryanoasis/nerd-fonts/releases/latest/download/"
            f"{font_name}.tar.xz"
        )
        existing = list(font_dir.glob("*.ttf")) + list(font_dir.glob("*.otf"))
        if existing:
            hail(f"Nerd Fonts already present in {font_dir} — skipping.")
        else:
            hail(f"Downloading {font_name} Nerd Font…")
            with warp("Transporting font data"):
                urllib.request.urlretrieve(font_url, font_zip)
            subprocess.run(
                ["tar", "-xf", str(font_zip), "-C", str(font_dir)],
                check=True,
            )
            font_zip.unlink(missing_ok=True)
            subprocess.run(["fc-cache", "-fv"], capture_output=True)
            hail(f"NotoMono Nerd Font installed in {font_dir}")

    # ── opencode ──────────────────────────────────────────────────────────────
    if not skip_opencode:
        existing = _opencode_bin()
        if existing:
            hail(f"opencode already installed — skipping. ({existing})")
        else:
            hail("Installing opencode (AI coding agent)…")
            _install_opencode_bin(bin_dir)

    # ── gh CLI ────────────────────────────────────────────────────────────────
    if not skip_gh:
        if _gh_bin():
            hail(f"gh CLI already installed — skipping. ({_gh_bin()})")
        else:
            hail("Installing gh CLI (GitHub CLI)…")
            _install_gh(bin_dir)

    # ── Claude Code CLI ───────────────────────────────────────────────────────
    if not skip_claude:
        existing = _claude_bin()
        if existing:
            hail(f"Claude Code already installed — skipping. ({existing})")
        else:
            hail("Installing Claude Code CLI…")
            _install_claude_bin()

    # ── pass (credential manager) ─────────────────────────────────────────────
    if not skip_pass:
        from . import creds as creds_mod
        if creds_mod.is_installed():
            hail(f"pass already installed at {shutil.which('pass')}")
        else:
            hail("Installing pass (Unix password manager)…")
            if creds_mod.install():
                hail("pass installed successfully")
            else:
                console.print()
                console.print(creds_mod.install_help())
        if creds_mod.is_installed() and not creds_mod.is_initialized():
            console.print()
            console.print(creds_mod.install_help())

    console.print()
    console.print(trans_stripe(52))
    make_it_so()


@app.command(rich_help_panel="Maintenance")
def report(
    section: Annotated[str, typer.Argument(
        help="Section: overview | tags | recent | orphans | daily | all"
    )] = "all",
    days: Annotated[int, typer.Option("--days", "-d", help="Days back for recent")] = 14,
):
    """Render rich text reports on your org-roam library."""
    from .report import (
        report_daily, report_orphans, report_overview,
        report_recent, report_top_tags, NF, REPORT_HEADER,
    )
    engine = _engine()
    with get_session(engine) as session:
        console.rule(REPORT_HEADER)
        match section:
            case "overview": report_overview(session)
            case "tags":     report_top_tags(session)
            case "recent":   report_recent(session, days=days)
            case "orphans":  report_orphans(session)
            case "daily":    report_daily(session)
            case "all":
                report_overview(session)
                report_top_tags(session)
                console.print()
                report_recent(session, days=days)
                console.print()
                report_daily(session)
                console.print()
                report_orphans(session)
            case _:
                red_alert(f"Unknown section: {section!r}. "
                          "Use: overview | tags | recent | orphans | daily | all")
                raise typer.Exit(1)
        console.rule("[dim]end of report[/dim]")


# ── doctor --walkthrough: LLM-driven self-test ──────────────────────────────

# A curated list of read-only commands the walkthrough may run. Each entry:
#   (label, argv, expected_substrings, why)
# The LLM judges actual stdout/stderr against expected_substrings + 'why' to
# decide pass/fail/concern.
_WALKTHROUGH_PROBES: list[tuple[str, list[str], list[str], str]] = [
    ("Banner / pride / trans stripe",
     ["tutor", "welcome"],
     ["welcome", "Local-first", "tutor"],
     "Confirm the welcome step renders cleanly with intentional banners."),
    ("Health dashboard",
     ["doctor"],
     ["System", "Database", "Ollama"],
     "Confirm doctor's panels render without crashes and reflect real state."),
    ("Cloud heartbeat",
     ["cloud", "--status"],
     ["Cloud Status", "Provider"],
     "Cloud configured? endpoint reachable? key source visible?"),
    ("Provider catalog",
     ["cloud", "--providers"],
     ["runpod", "vast", "openrouter"],
     "Provider table readable; no 4-char-strip wrapping."),
    ("Cost comparison",
     ["cloud", "--cost"],
     ["$/hr", "tok"],
     "Cost matrix renders cleanly; numbers plausible."),
    ("Model catalog (filtered to local hardware)",
     ["models", "--discover"],
     ["embed", "chat"],
     "Catalog rows; pulled markers correct; columns align on this width."),
    ("Performance probe",
     ["performance", "--quick"],
     ["Hardware", "RAM"],
     "Hardware panel readable; CPU model not chopped."),
    ("Theme dial state",
     ["theme", "show"],
     ["Stored theme"],
     "Stored vs active theme; no stale 'Active right now' line."),
    ("User-defined knobs",
     ["knob", "list"],
     ["trek", "commie", "queer"],
     "Knob table shows built-ins + user knobs with active level."),
    ("MCP grants overview",
     ["grants"],
     ["grants"],
     "Grants panel; deny-list mentioned; current state honest."),
    ("DB row counts",
     ["db", "-q", "SELECT count(*) AS files FROM files"],
     ["files"],
     "Read-only DB query returns a count without traceback."),
    ("Tag leaderboard sanity",
     ["report", "tags"],
     ["Top Tags"],
     "Tag report renders; no SQL errors; counts plausible."),
    ("Help discoverability",
     ["--help"],
     ["init", "ask", "doctor", "performance"],
     "Top-level help lists every command, no surprises."),
]


def _doctor_walkthrough(report_to: str = "") -> None:
    """LLM-driven self-test: run a curated set of read-only commands, ask the
    cloud LLM to judge each output, surface issues + suggestions.

    Read-only by design — this never writes to your DB or ~/org. The cloud
    is required because the local chat model often can't fit in available
    RAM, and we'd rather walkthrough always work than fail half the time.
    """
    import json
    import os
    import subprocess
    import sys
    from datetime import datetime
    from rich.panel import Panel as _P

    _auto_init_db_if_needed()
    engine = _engine()
    try:
        with get_session(engine) as session:
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            cloud_model    = _cfg(session, "cloud_model") or "openai/gpt-oss-20b:free"
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
    except Exception as exc:
        red_alert(f"Could not read config: {exc}")
        raise typer.Exit(1)

    if not cloud_endpoint:
        red_alert("--walkthrough needs a configured cloud backend (LLM judge).")
        on_screen("Run: [bold]org-llm cloud --quick-start openrouter[/bold]")
        raise typer.Exit(1)

    from . import creds as creds_mod
    api_key = (creds_mod.read_secret(creds_mod.cloud_slug(cloud_provider))
               if cloud_provider else None) or db_api_key

    console.print()
    console.rule(f"[lcars1]Doctor walkthrough  ·  {cloud_provider}:{cloud_model}[/lcars1]")
    console.print()
    on_screen(f"Running {len(_WALKTHROUGH_PROBES)} read-only probes; "
              "judging output via the cloud LLM.")
    console.print()

    org_llm_bin = sys.argv[0] if sys.argv else "org-llm"
    # Fall back to `uv run org-llm` when invoked under uv tool / pipx so the
    # subprocess uses the same code we're currently running.
    invoke = [org_llm_bin] if os.path.isabs(org_llm_bin) else ["uv", "run", "org-llm"]

    probe_results = []
    with impulse("Walkthrough", total=len(_WALKTHROUGH_PROBES)) as (prog, task):
        for label, args, expected, why in _WALKTHROUGH_PROBES:
            try:
                p = subprocess.run(
                    invoke + list(args),
                    capture_output=True, text=True, timeout=45,
                    cwd=os.path.dirname(os.path.dirname(__file__)) or None,
                )
                stdout = p.stdout or ""
                stderr = p.stderr or ""
                rc = p.returncode
            except Exception as exc:
                stdout = ""; stderr = str(exc); rc = -1

            # Local mechanical assessment
            missing = [s for s in expected if s.lower() not in stdout.lower()]
            mech = "pass" if (rc == 0 and not missing) else "fail"

            probe_results.append({
                "label":    label,
                "command":  "org-llm " + " ".join(args),
                "why":      why,
                "rc":       rc,
                "stdout":   stdout[:6000],
                "stderr":   stderr[:1500],
                "missing":  missing,
                "mech":     mech,
            })
            prog.advance(task)

    # Summarise mechanical results first (cheap, deterministic)
    from rich.table import Table as _T
    summary = _T(box=None, pad_edge=False)
    summary.add_column("Probe",   style="lcars2", no_wrap=True, max_width=40, overflow="ellipsis")
    summary.add_column("Command", style="dim",    no_wrap=True, max_width=32, overflow="ellipsis")
    summary.add_column("Exit",    width=4,        justify="right")
    summary.add_column("Mech",    width=6,        justify="center")
    summary.add_column("Note",    style="dim")
    for r in probe_results:
        sym = "[green]✓[/]" if r["mech"] == "pass" else "[red]✗[/]"
        note = ("missing: " + ", ".join(r["missing"])) if r["missing"] else ""
        if r["rc"] != 0 and not note:
            note = f"exit={r['rc']}"
        summary.add_row(r["label"], r["command"], str(r["rc"]), sym, note)
    console.print()
    console.print(_P(summary, title="[lcars1]Mechanical results[/lcars1]",
                      border_style="lcars2"))
    console.print()

    # Now ask the LLM for a qualitative read on the same outputs
    on_screen(f"Asking [bold]{cloud_model}[/bold] for an assessment…")
    sys_prompt = (
        "You are evaluating a CLI tool called org-llm. The user just ran "
        "a self-test that executes read-only commands and captures stdout. "
        "For each probe, respond with one of three verdicts:\n"
        "  ✓ PASS  — output looks clean, on-topic, no concerns\n"
        "  ⚠ NIT   — works but has a UX rough edge worth fixing\n"
        "  ✗ ISSUE — broken, misleading, or missing expected content\n\n"
        "Be specific. When you flag an issue, name the file or command that "
        "would fix it. Cap your reply at ~600 words. End with a 'TOP 3 "
        "RECOMMENDATIONS' section ranked by impact-to-effort."
    )
    payload = "\n\n---\n\n".join(
        f"## Probe: {r['label']}\n"
        f"Command: {r['command']}\n"
        f"Why we ran it: {r['why']}\n"
        f"Exit code: {r['rc']}\n"
        f"Mechanical: {'PASS' if r['mech']=='pass' else 'FAIL'}"
        + (f" (missing substrings: {r['missing']})" if r['missing'] else "")
        + f"\n\nstdout (first 4 KB):\n{r['stdout'][:4000]}"
        + (f"\n\nstderr:\n{r['stderr']}" if r['stderr'] else "")
        for r in probe_results
    )
    from .cloud import cloud_chat
    try:
        with thinking("Walkthrough assessment", model=cloud_model):
            assessment = cloud_chat(payload, model=cloud_model,
                                    endpoint_url=cloud_endpoint,
                                    api_key=api_key, system=sys_prompt)
    except Exception as exc:
        red_alert(f"Cloud assessment failed: {exc}")
        assessment = "(LLM assessment unavailable; mechanical results above are the report.)"

    console.print()
    console.print(_P(assessment, title=f"[lcars1]LLM assessment ({cloud_model})[/lcars1]",
                      border_style="lcars2", padding=(1, 2)))
    console.print()

    # ── Second LLM call: get structured executable fixes ─────────────────────
    fix_sys = (
        "Based on the probe results below, output STRICT JSON: a list of fixes "
        "for any issues you found. Return [] if everything looked fine. "
        "Each fix is {\"argv\": [\"<verb>\", ...], \"reason\": \"...\"}. "
        "argv[0] MUST be one of: " + ", ".join(_LLM_FIXABLE_VERBS) + ". "
        "Do NOT include fixes for problems that need user input "
        "(missing API keys, --config-dir typos, OOM). Only include "
        "fixes you're confident are safe and idempotent. NO MARKDOWN, "
        "NO PROSE, just the JSON array."
    )
    fix_user = "Probe results (mechanical):\n" + "\n".join(
        f"- {r['label']}: {'PASS' if r['mech']=='pass' else 'FAIL ('+', '.join(r['missing'] or ['nonzero exit'])+')'}"
        for r in probe_results
    )
    applied_fixes = []
    skipped_fixes = []
    try:
        with warp("Asking LLM for executable fixes"):
            fix_reply = cloud_chat(fix_user, model=cloud_model,
                                    endpoint_url=cloud_endpoint,
                                    api_key=api_key, system=fix_sys)
        cleaned = _strip_code_fences(fix_reply, "json").strip()
        import json as _json
        plan = _json.loads(cleaned)
        if not isinstance(plan, list):
            plan = []
    except Exception:
        plan = []

    if plan:
        console.print()
        console.rule("[lcars1]Auto-applying LLM-suggested fixes[/lcars1]")
        import subprocess as _sp
        invoke = [sys.argv[0]] if sys.argv and os.path.isabs(sys.argv[0]) \
                 else ["uv", "run", "org-llm"]
        for fix in plan:
            argv = fix.get("argv") if isinstance(fix, dict) else None
            reason = fix.get("reason", "") if isinstance(fix, dict) else ""
            if not (isinstance(argv, list) and argv):
                continue
            verb = str(argv[0])
            if verb not in _LLM_FIXABLE_VERBS:
                skipped_fixes.append((argv, f"unsafe verb {verb!r}"))
                continue
            cmd_str = "org-llm " + " ".join(str(x) for x in argv)
            on_screen(f"[lcars2]→[/lcars2] {cmd_str}")
            if reason:
                on_screen(f"  [dim]{reason}[/dim]")
            try:
                r = _sp.run(invoke + [str(x) for x in argv],
                             capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    applied_fixes.append((argv, reason))
                    on_screen(f"  [green]✓[/green] applied")
                else:
                    skipped_fixes.append((argv, f"exit={r.returncode}"))
                    on_screen(f"  [red]✗ failed (exit={r.returncode})[/red]")
            except Exception as exc:
                skipped_fixes.append((argv, f"exception: {exc}"))
                on_screen(f"  [red]✗ {exc}[/red]")
        console.print()
        on_screen(f"Applied {len(applied_fixes)}; skipped {len(skipped_fixes)}.")
    else:
        on_screen("[dim]LLM proposed no executable fixes.[/dim]")
    console.print()

    # Optional org-mode report
    if report_to:
        path = Path(report_to).expanduser()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"\n* org-llm doctor walkthrough — {ts}",
            f":PROPERTIES:",
            f":CREATED: [{ts}]",
            f":CMD:     org-llm doctor --walkthrough",
            f":MODEL:   {cloud_provider}:{cloud_model}",
            f":END:",
            "",
            "** Mechanical results",
            "",
            "| Probe | Command | Exit | Mech | Notes |",
            "|---|---|---|---|---|",
        ]
        for r in probe_results:
            note = ("missing: " + ", ".join(r["missing"])) if r["missing"] else ""
            mech = "PASS" if r["mech"] == "pass" else "FAIL"
            lines.append(f"| {r['label']} | ={r['command']}= | {r['rc']} | {mech} | {note} |")
        lines += ["", "** LLM assessment", "", "#+begin_quote", assessment, "#+end_quote", ""]
        if applied_fixes or skipped_fixes:
            lines += ["", "** Auto-applied fixes", ""]
            for argv, reason in applied_fixes:
                cmd = "org-llm " + " ".join(str(x) for x in argv)
                lines.append(f"- ✓ ={cmd}=  — {reason}")
            for argv, reason in skipped_fixes:
                cmd = "org-llm " + " ".join(str(x) for x in argv)
                lines.append(f"- ✗ ={cmd}=  — skipped: {reason}")
            lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("\n".join(lines))
        hail(f"Appended walkthrough report → {path}")

    failures = [r for r in probe_results if r["mech"] != "pass"]
    if failures:
        red_alert(f"{len(failures)} probe(s) failed mechanically. See above.")
        raise typer.Exit(1)
    make_it_so()


def _doctor_benchmark_fixers(report_to: str = "", apply_fixer: bool = False) -> None:
    """Score candidate cloud LLMs on canonical org-llm fix scenarios.

    For each candidate model, send a fixed set of error-recovery prompts
    and grade the JSON response. Reports per-model accuracy + per-scenario
    pass/fail. With --apply-fixer, persists the top-scoring model as the
    `fixer_model` config row, which the auto-fix layer prefers when set.
    """
    from rich.panel import Panel as _P
    from rich.table import Table as _T
    from . import fixer_bench as _bench
    from . import creds as _creds

    engine = _engine()
    try:
        with get_session(engine) as session:
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
            cur_fixer      = _cfg(session, "fixer_model")
    except Exception as exc:
        red_alert(f"Could not read config: {exc}")
        raise typer.Exit(1)
    if not cloud_endpoint:
        red_alert("--benchmark-fixers needs a configured cloud backend.")
        on_screen("Run: [bold]org-llm cloud --quick-start openrouter[/bold]")
        raise typer.Exit(1)
    api_key = (_creds.read_secret(_creds.cloud_slug(cloud_provider))
               if cloud_provider else None) or db_api_key

    candidates = list(_bench.DEFAULT_CANDIDATES)
    console.print()
    console.rule("[lcars1]LLM-fixer benchmark[/lcars1]")
    on_screen(f"Probing {len(candidates)} candidate models against "
              f"{len(_bench.SCENARIOS)} canonical scenarios.")
    on_screen(f"Endpoint: {cloud_endpoint}   ({len(candidates)} × "
              f"{len(_bench.SCENARIOS)} = {len(candidates)*len(_bench.SCENARIOS)} calls)")
    console.print()

    results: list[_bench.ModelResult] = []
    with impulse("Benchmarking", total=len(candidates)) as (prog, task):
        for model in candidates:
            r = _bench.benchmark_model(model, cloud_endpoint, api_key)
            results.append(r)
            prog.advance(task)

    # Leaderboard
    results.sort(key=lambda m: (m.passed, -m.error_count), reverse=True)
    tbl = _T(box=None, pad_edge=False)
    tbl.add_column("Rank",     style="lcars1", width=4,  justify="right")
    tbl.add_column("Model",    style="lcars2", no_wrap=True, max_width=40, overflow="ellipsis")
    tbl.add_column("Passed",   style="lcars3", justify="right", width=8)
    tbl.add_column("Accuracy", style="lcars3", justify="right", width=10)
    tbl.add_column("Errors",   style="dim",   justify="right", width=7)
    tbl.add_column("Failed scenarios", style="dim")
    for i, r in enumerate(results, 1):
        failed = ", ".join(s.scenario for s in r.scenarios if not s.overall_pass)
        tbl.add_row(str(i), r.model, f"{r.passed}/{r.total}",
                    f"{r.accuracy*100:.0f}%", str(r.error_count),
                    failed[:60] + ("…" if len(failed) > 60 else ""))
    console.print(_P(tbl, title="[lcars1]LLM-fixer leaderboard[/lcars1]",
                      border_style="lcars2"))
    console.print()

    winner = results[0] if results else None
    if winner:
        on_screen(f"[bold green]Winner: {winner.model}[/bold green]  "
                  f"({winner.accuracy*100:.0f}% accuracy)")
        if cur_fixer:
            on_screen(f"Currently configured fixer_model: [dim]{cur_fixer}[/dim]")
        else:
            on_screen(f"Currently configured fixer_model: [dim](unset — auto-fix uses cloud_model)[/dim]")

    # Persist winner if requested
    if apply_fixer and winner:
        from .db import Config as _Cfg
        with get_session(engine) as session:
            row = session.get(_Cfg, "fixer_model")
            if row: row.value = winner.model
            else:   session.add(_Cfg(key="fixer_model", value=winner.model))
            session.commit()
        hail(f"Persisted fixer_model → {winner.model}")
    elif winner and not apply_fixer:
        on_screen(f"Adopt the winner: [bold]org-llm doctor -BA[/bold]  "
                  "(or set manually: [bold]org-llm config fixer_model "
                  f"{winner.model}[/bold])")

    # Optional org-mode report
    if report_to:
        from datetime import datetime as _dt
        path = Path(report_to).expanduser()
        ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"\n* org-llm fixer-model benchmark — {ts}",
            f":PROPERTIES:",
            f":CREATED: [{ts}]",
            f":CMD:     org-llm doctor --benchmark-fixers",
            f":WINNER:  {winner.model if winner else '(none)'}",
            f":SCENARIOS: {len(_bench.SCENARIOS)}",
            f":CANDIDATES: {len(candidates)}",
            f":END:",
            "",
            "** Leaderboard",
            "",
            "| Rank | Model | Passed | Accuracy | Errors |",
            "|---|---|---|---|---|",
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"| {i} | ={r.model}= | {r.passed}/{r.total} "
                          f"| {r.accuracy*100:.0f}% | {r.error_count} |")
        lines += ["", "** Per-scenario detail", ""]
        # Per-scenario × per-model matrix
        scenario_names = [s.name for s in _bench.SCENARIOS]
        header = "| Scenario | " + " | ".join(r.model.split("/")[-1].split(":")[0] for r in results) + " |"
        lines.append(header)
        lines.append("|---" * (len(results) + 1) + "|")
        for sn in scenario_names:
            row_cells = [f"={sn}="]
            for r in results:
                hit = next((s for s in r.scenarios if s.scenario == sn), None)
                row_cells.append("✓" if hit and hit.overall_pass else "✗")
            lines.append("| " + " | ".join(row_cells) + " |")
        lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("\n".join(lines))
        hail(f"Appended fixer-bench report → {path}")

    make_it_so()


@app.command(rich_help_panel="Maintenance")
def doctor(
    diagnose: Annotated[bool, typer.Option("--diagnose", "-d",
              help="Use LLM to explain failures and suggest fixes")] = False,
    fix:      Annotated[bool, typer.Option("--fix",          "-f",
              help="Auto-apply safe fixes (init DB, start Ollama)")] = False,
    install_tool: Annotated[str, typer.Option("--install",   "-i",
              help="Install + theme a FOSS CLI tool by name (e.g. bat, eza). Use 'all' to install everything in the registry.")] = "",
    list_tools:   Annotated[bool, typer.Option("--list-tools","-l",
              help="List all installable FOSS tools")] = False,
    walkthrough:  Annotated[bool, typer.Option("--walkthrough", "-w",
              help="LLM-driven self-test: generate a tour, run each command, assess output, suggest fixes")] = False,
    report_to:    Annotated[str,  typer.Option("--report-to",   "-r",
              help="Append a structured report of this run to PATH (an .org file)")] = "",
    benchmark_fixers: Annotated[bool, typer.Option("--benchmark-fixers", "-B",
              help="Score multiple cloud LLMs on canonical fix scenarios; persist the winner as fixer_model")] = False,
    apply_fixer:  Annotated[bool, typer.Option("--apply-fixer", "-A",
              help="With --benchmark-fixers, write the top-scoring model to config:fixer_model")] = False,
):
    """Deep health check, LLM tuning advisor, and FOSS tool installer.

    --walkthrough turns doctor into a self-tester: the cloud LLM picks a
    set of read-only commands to run, watches the output, judges each
    against expectations, and ends with a punch-list of issues + fixes.

    --report-to writes a structured org-mode log of the run to a file
    (compatible with the dev log format in your vault).
    """
    if walkthrough:
        return _doctor_walkthrough(report_to=report_to)
    if benchmark_fixers:
        return _doctor_benchmark_fixers(report_to=report_to, apply_fixer=apply_fixer)
    import os
    import shutil
    import subprocess
    import sys
    import time
    from datetime import datetime
    from rich.panel import Panel
    from rich.rule  import Rule
    from rich.text  import Text
    from rich.table import Table
    from .db import Node, File
    from .ui import trans_stripe, PRIDE_BANNER, NERD_FONTS
    from .models import TOOL_REGISTRY, get_tool, apply_theme

    # ── FOSS tool list ────────────────────────────────────────────────────────
    if list_tools:
        console.rule("[lcars1]Installable FOSS Tools[/lcars1]")
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("Name",        style="lcars2",  no_wrap=True)
        tbl.add_column("Category",    style="lcars1",  width=10)
        tbl.add_column("License",     style="dim",     no_wrap=True)
        tbl.add_column("Installed",   width=10)
        tbl.add_column("Themed",      width=8)
        tbl.add_column("Description")
        bin_dir = Path("~/.local/bin").expanduser()
        for t in TOOL_REGISTRY:
            try:
                installed = bool(shutil.which(t.check_cmd.split()[0]) or
                                 (bin_dir / t.check_cmd.split()[0]).exists())
            except Exception:
                installed = False
            tbl.add_row(
                t.name,
                t.category,
                t.license,
                "[green]✓[/green]" if installed else "[dim]—[/dim]",
                "[cyan]✓[/cyan]"  if t.theme_fn  else "[dim]—[/dim]",
                t.description,
            )
        console.print(tbl)
        console.print()
        on_screen("Install one:  [bold]org-llm doctor --install <name>[/bold]")
        on_screen("Install all:  [bold]org-llm doctor --install all[/bold]")
        return

    # ── FOSS tool installer ───────────────────────────────────────────────────
    if install_tool:
        bin_dir_str = str(Path("~/.local/bin").expanduser())

        def _install_one(tool, *, quiet: bool = False) -> tuple[str, str]:
            """Install one FOSS tool. Returns (status, detail).

            status ∈ {'already', 'installed', 'failed', 'no-fn'}.
            """
            already = shutil.which(tool.check_cmd.split()[0])
            if already:
                if not quiet:
                    hail(f"{tool.name} already installed at {already}")
                return ("already", str(already))
            if not quiet:
                hail(f"Installing {tool.name} ({tool.license}) — {tool.description}")
            import importlib
            models_mod = importlib.import_module("org_llm.models")
            fn = getattr(models_mod, tool.install_fn, None)
            if fn is None:
                if not quiet:
                    red_alert(f"No install function for {tool.name}")
                return ("no-fn", "")
            try:
                ok_install = fn(bin_dir_str)
            except Exception as exc:
                if not quiet:
                    red_alert(f"Install raised for {tool.name}: {exc}")
                return ("failed", str(exc)[:100])
            if not ok_install:
                if not quiet:
                    red_alert(f"Install failed for {tool.name} — check internet / GitHub release availability")
                return ("failed", "")
            if not quiet:
                hail(f"{tool.name} installed successfully")
            return ("installed", "")

        def _theme_one(tool) -> tuple[bool, str]:
            if not tool.theme_fn:
                return (False, "no theme template")
            success, path = apply_theme(tool.name)
            return (success, path)

        # ── Bulk install: --install all ──────────────────────────────────────
        if install_tool.lower() == "all":
            console.rule(f"[lcars1]Installing all {len(TOOL_REGISTRY)} FOSS tools[/lcars1]")
            console.print()
            results: dict[str, list[str]] = {
                "installed": [], "already": [], "failed": [], "no-fn": [],
            }
            themed: list[str] = []
            theme_skipped: list[str] = []
            with impulse(f"FOSS tool install", total=len(TOOL_REGISTRY)) as (prog, task):
                for tool in TOOL_REGISTRY:
                    status, _ = _install_one(tool, quiet=False)
                    results[status].append(tool.name)
                    if status in ("installed", "already") and tool.theme_fn:
                        ok_theme, path = _theme_one(tool)
                        if ok_theme:
                            themed.append(f"{tool.name} → {path}")
                        else:
                            theme_skipped.append(f"{tool.name} ({path or 'exists'})")
                    prog.advance(task)

            console.print()
            console.rule("[lcars1]Summary[/lcars1]")
            tbl = Table(box=None, pad_edge=False, show_header=False)
            tbl.add_column("Result", style="lcars1", width=22)
            tbl.add_column("Count",  style="lcars2", justify="right", width=5)
            tbl.add_column("Tools",  style="dim")
            tbl.add_row("[green]✓ newly installed[/green]",
                        str(len(results["installed"])),
                        ", ".join(results["installed"]) or "—")
            tbl.add_row("[cyan]· already installed[/cyan]",
                        str(len(results["already"])),
                        ", ".join(results["already"]) or "—")
            tbl.add_row("[yellow]⚠ no install fn[/yellow]",
                        str(len(results["no-fn"])),
                        ", ".join(results["no-fn"]) or "—")
            tbl.add_row("[red]✗ failed[/red]",
                        str(len(results["failed"])),
                        ", ".join(results["failed"]) or "—")
            tbl.add_row("[bold]themes applied[/bold]",
                        str(len(themed)), "")
            console.print(tbl)
            if themed:
                console.print()
                on_screen("Theme files written:")
                for line in themed:
                    console.print(f"  [dim]·[/dim] {line}")
            console.print()
            if results["failed"]:
                on_screen("[yellow]Retry the failures with: org-llm doctor --install <name>[/yellow]")
            make_it_so()
            return

        # ── Single-tool install ──────────────────────────────────────────────
        tool = get_tool(install_tool)
        if not tool:
            names = [t.name for t in TOOL_REGISTRY]
            red_alert(f"Unknown tool: {install_tool!r}")
            on_screen(f"Available: {', '.join(names)}")
            on_screen("All tools:  org-llm doctor --install all")
            on_screen("List:       org-llm doctor --list-tools")
            raise typer.Exit(1)

        status, _ = _install_one(tool)
        if status == "failed" or status == "no-fn":
            raise typer.Exit(1)

        ok_theme, path = _theme_one(tool)
        if tool.theme_fn:
            if ok_theme:
                hail(f"Theme applied → {path}")
                console.print(f"[dim]  Review and source/reload as needed.[/dim]")
            else:
                hail(f"Theme config already exists at {path} — not overwritten")
        else:
            hail(f"No theme template for {tool.name}")

        console.print()
        make_it_so()
        return

    PASS = "[bold green]✓[/bold green]"
    FAIL = "[bold red]✗[/bold red]"
    WARN = "[bold yellow]⚠[/bold yellow]"
    INFO = "[dim]·[/dim]"

    issues:   list[str] = []   # failures for LLM diagnosis
    warnings: list[str] = []   # non-fatal
    checks:   list[tuple[str, str, str]] = []  # (status, label, detail)

    def ok(label: str, detail: str = "") -> None:
        checks.append((PASS, label, detail))

    def fail(label: str, detail: str = "", fix_hint: str = "") -> None:
        checks.append((FAIL, label, detail))
        msg = f"FAIL: {label}"
        if detail:
            msg += f" — {detail}"
        if fix_hint:
            msg += f". Fix: {fix_hint}"
        issues.append(msg)

    def warn(label: str, detail: str = "") -> None:
        checks.append((WARN, label, detail))
        warnings.append(f"WARN: {label} — {detail}")

    def info(label: str, detail: str = "") -> None:
        checks.append((INFO, label, detail))

    def section(title: str) -> None:
        checks.append(("", f"[lcars1]{title}[/lcars1]", ""))

    # ── System ─────────────────────────────────────────────────────────────────
    section("System")
    py = sys.version.split()[0]
    ok("Python", py) if tuple(int(x) for x in py.split(".")[:2]) >= (3, 11) else \
        fail("Python ≥ 3.11 required", py)

    uv_bin = shutil.which("uv")
    ok("uv", uv_bin or "") if uv_bin else warn("uv not on PATH", "install: pip install uv")

    ollama_bin = shutil.which("ollama") or str(Path("~/.local/bin/ollama").expanduser())
    ok("ollama binary", ollama_bin) if Path(ollama_bin).exists() else \
        fail("Ollama not installed", "~/.local/bin/ollama missing",
             "org-llm install-tools --skip-models --skip-fonts")

    oc_path = _opencode_bin()
    if oc_path:
        ok("opencode", str(oc_path))
    else:
        warn("opencode not installed", "run: org-llm install-tools --skip-ollama --skip-models --skip-fonts")

    # Disk space for DB and org_dir
    try:
        import shutil as _sh
        db_dir = DB_PATH.parent
        db_dir.mkdir(parents=True, exist_ok=True)
        usage = _sh.disk_usage(db_dir)
        free_gb = usage.free / 1_073_741_824
        db_size = DB_PATH.stat().st_size / 1_048_576 if DB_PATH.exists() else 0
        if free_gb < 1:
            fail("Disk space", f"{free_gb:.1f} GB free — very low",
                 "free up disk space")
        elif free_gb < 5:
            warn("Disk space", f"{free_gb:.1f} GB free; DB is {db_size:.0f} MB")
        else:
            ok("Disk space", f"{free_gb:.0f} GB free; DB is {db_size:.0f} MB")
    except Exception as e:
        warn("Disk space check failed", str(e))

    # ── Database ───────────────────────────────────────────────────────────────
    section("Database")
    engine = _engine()
    db_ok = False
    try:
        init_db(engine)
        db_ok = True
        ok("DB reachable", str(DB_PATH))
        if fix:
            hail("DB initialised (--fix)")
    except Exception as e:
        fail("DB not reachable", str(e), "org-llm init")
        if fix:
            hail("Attempting org-llm init …")
            try:
                init_db(make_engine(DB_PATH))
                ok("DB init (auto-fixed)", str(DB_PATH))
                db_ok = True
            except Exception as e2:
                fail("DB init failed", str(e2))

    if db_ok:
        # sqlite-vec
        try:
            import sqlite_vec  # noqa: F401
            ok("sqlite-vec extension")
        except Exception as e:
            fail("sqlite-vec not loadable", str(e),
                 "uv add sqlite-vec && uv run org-llm init")

        # PRAGMA integrity_check
        try:
            from sqlalchemy import text as _text
            with get_session(engine) as _s:
                result = _s.execute(_text("PRAGMA integrity_check")).scalar()
            if result == "ok":
                ok("DB integrity", "PRAGMA integrity_check = ok")
            else:
                fail("DB integrity", result or "unknown",
                     "backup DB, then: org-llm init --force")
        except Exception as e:
            warn("DB integrity check failed", str(e))

        # Index state
        with get_session(engine) as session:
            file_count  = session.query(File).count()
            node_count  = session.query(Node).count()
            embed_count = session.query(Node).filter(Node.embedding.isnot(None)).count()

            # Last index time
            last_file = session.query(File).order_by(File.indexed_at.desc()).first()
            last_ts = last_file.indexed_at if last_file else None

            # Stale files (in DB but not on disk)
            all_paths = [f.path for f in session.query(File).all()]
            stale = [p for p in all_paths if not Path(p).exists()]

        if node_count > 0:
            ok("Index populated", f"{file_count} files / {node_count} nodes")
        else:
            fail("Index empty", "no nodes found", "org-llm index")

        if last_ts:
            info("Last indexed", last_ts)

        if stale:
            warn("Stale DB records",
                 f"{len(stale)} file(s) in DB no longer exist on disk — "
                 "run: org-llm index --force")

        if node_count > 0:
            pct = int(embed_count / node_count * 100)
            pending = node_count - embed_count
            if pct == 100:
                ok("Embeddings", f"{embed_count}/{node_count} (100%)")
            else:
                # Partial / low embeddings: with --fix, auto-run embed for
                # any pending nodes (idempotent + cheap). The previous
                # behaviour just nagged the user to run a separate command.
                level = "partial" if pct >= 80 else "low"
                summary = f"{embed_count}/{node_count} ({pct}%)"
                if fix and pending > 0:
                    on_screen(f"[yellow]Embeddings {level} ({summary}) — "
                              f"auto-running embed for {pending} node(s)…[/yellow]")
                    try:
                        embed()
                        ok("Embeddings", f"{node_count}/{node_count} "
                                          f"(100% after auto-fix)")
                    except SystemExit:
                        warn(f"Embeddings {level}", f"{summary} — auto-fix bailed")
                    except Exception as e:
                        warn(f"Embeddings {level}", f"{summary} — auto-fix failed: {e}")
                else:
                    if level == "partial":
                        warn("Embeddings partial",
                             f"{summary} — run: org-llm embed  "
                             "(or: org-llm doctor --fix)")
                    else:
                        fail("Embeddings low", summary,
                             "org-llm embed  (or: org-llm doctor --fix)")

    # ── Org Files ──────────────────────────────────────────────────────────────
    section("Org Files")
    with get_session(engine) as session:
        org_dir = _org_dir(session)
    if org_dir.exists():
        org_files = list(org_dir.rglob("*.org"))
        ok("org_dir", str(org_dir))
        if org_files:
            ok("org files found", f"{len(org_files)} .org files")
            if db_ok:
                with get_session(engine) as session:
                    db_paths = {f.path for f in session.query(File).all()}
                unindexed = [p for p in org_files if str(p) not in db_paths]
                if unindexed:
                    warn("Unindexed files",
                         f"{len(unindexed)} .org file(s) not yet in DB — run: org-llm index")
                else:
                    ok("All org files indexed")
        else:
            fail("No org files", f"no .org files in {org_dir}", f"add .org files to {org_dir}")
    else:
        fail("org_dir missing", str(org_dir),
             f"mkdir -p {org_dir}  or  org-llm config org_dir /path/to/your/org")

    # ── User context (current truth) ───────────────────────────────────────────
    section("User Context")
    try:
        from . import context as _ctx
        ctx_org    = _ctx.context_org_path()
        ctx_tangle = _ctx.context_tangle_path()
        if not ctx_org.exists():
            info("No context file",
                 f"create one with: org-llm context add 'I work at <X> now'")
        else:
            tangle_body = _ctx.read_context_for_prompt(max_chars=20000)
            n_facts = sum(1 for ln in tangle_body.splitlines()
                            if ln.strip().startswith("-"))
            ok("Context file", f"{ctx_org} ({n_facts} fact line(s))")
            # Tangle staleness
            if ctx_tangle.exists() and ctx_org.stat().st_mtime > ctx_tangle.stat().st_mtime + 5:
                warn("Tangle stale",
                     "context.org is newer than tangle output; "
                     "run: org-llm context tangle")
            elif ctx_tangle.exists():
                ok("Tangle current", str(ctx_tangle))
            # Unswept stale candidates
            if db_ok:
                with get_session(engine) as session:
                    n_pending = _ctx.count_unreviewed_stale_candidates(session)
                if n_pending > 0:
                    warn("Unreviewed stale candidates",
                         f"{n_pending} note(s) reference context keywords; "
                         "run: org-llm stale --apply")
                else:
                    ok("No unreviewed stale notes")
    except Exception as e:
        warn("Context check failed", str(e)[:80])

    # ── Ollama ─────────────────────────────────────────────────────────────────
    section("Ollama")
    with get_session(engine) as session:
        url = _ollama_url(session)
    pulled_models: list[str] = []
    ollama_live = False
    try:
        from .llm import list_models
        pulled_models = list_models(url)
        ollama_live = True
        ok("Ollama API", url)
    except Exception as e:
        fail("Ollama not reachable", f"{url} — {e}",
             "ollama serve &  (or: org-llm install-tools --skip-models --skip-fonts)")
        if fix and Path(ollama_bin).exists():
            hail("Starting ollama serve (--fix) …")
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            try:
                pulled_models = list_models(url)
                ollama_live = True
                ok("Ollama API (auto-started)", url)
            except Exception:
                fail("Ollama still unreachable after start attempt", url)

    if ollama_live:
        info("Pulled models", ", ".join(pulled_models) or "none")
        model_keys = [
            "embed_model", "chat_model", "code_model",
            "reason_model", "fast_model", "instruct_model", "text_model",
        ]
        with get_session(engine) as session:
            missing_models = []
            for key in model_keys:
                model = _cfg(session, key)
                if any(model in m for m in pulled_models):
                    ok(f"  {key}", model)
                else:
                    fail(f"  {key} not pulled", model,
                         f"ollama pull {model}")
                    missing_models.append(model)

        # --fix: auto-pull every missing configured model
        if fix and missing_models:
            console.print()
            hail(f"--fix: pulling {len(missing_models)} missing model(s)…")
            for m in missing_models:
                if _ollama_pull(m):
                    ok(f"  pulled {m}", "")
                else:
                    fail(f"  failed to pull {m}", "")
            try:
                pulled_models = list_models(url)
            except Exception:
                pass

        # Ping embed model to verify it actually responds
        with get_session(engine) as session:
            embed_mdl = _cfg(session, "embed_model") or "nomic-embed-text"
        if any(embed_mdl in m for m in pulled_models):
            try:
                from .llm import embed as _embed
                _embed("ping", model=embed_mdl, base_url=url)
                ok("  embed model ping", f"{embed_mdl} responded")
            except Exception as e:
                fail("  embed model unresponsive", str(e)[:80],
                     f"ollama pull {embed_mdl}")

    # ── Cloud GPU backend ─────────────────────────────────────────────────────
    section("Cloud GPU")
    with get_session(engine) as session:
        cloud_provider = _cfg(session, "cloud_provider")
        cloud_endpoint = _cfg(session, "cloud_endpoint_url")
        cloud_api_key  = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
        cloud_model_   = _cfg(session, "cloud_model") or _cfg(session, "chat_model") or MODEL_DEFAULTS["chat_model"]
    from .cloud import get_provider as _get_provider
    provider_label = (_get_provider(cloud_provider).name
                      if cloud_provider and _get_provider(cloud_provider)
                      else (cloud_provider or "—"))
    if cloud_endpoint:
        info("Provider", provider_label)
        try:
            from .cloud import check_connection
            cs = check_connection(cloud_endpoint, cloud_api_key, cloud_model_)
            if cs.reachable and cs.auth_ok:
                ok("Cloud endpoint", f"{cloud_endpoint}  ({cs.latency_ms:.0f}ms)")
            elif cs.reachable:
                warn("Cloud auth failed", "check cloud_api_key config")
            else:
                fail("Cloud endpoint unreachable", cloud_endpoint,
                     f"check pod is running: org-llm cloud --console {cloud_provider or ''}".rstrip())
        except Exception as e:
            warn("Cloud check failed", str(e)[:60])
    else:
        info("Cloud GPU", "not configured — org-llm cloud --providers to compare options")

    with get_session(engine) as session:
        all_models = [_cfg(session, k) for _, k, _ in _TASK_MODEL_KEYS if _cfg(session, k)]
    try:
        from .cloud import assess_local_capability, local_vram_gb
        vram = local_vram_gb()
        results = assess_local_capability(list(set(all_models)))
        cloud_needed = [r["model"] for r in results if not r["can_local"]]
        if cloud_needed:
            if cloud_endpoint:
                ok("Compute coverage", f"{len(cloud_needed)} model(s) offloaded to cloud")
            else:
                warn("Compute gap",
                     f"{len(cloud_needed)} model(s) need cloud: {', '.join(cloud_needed)}")
        else:
            gpu_str = f"{vram:.0f}GB VRAM" if vram else "CPU/RAM"
            ok("Local compute sufficient", f"{gpu_str} handles all models")
    except Exception as e:
        warn("Compute assessment failed", str(e)[:60])

    # ── gh CLI ─────────────────────────────────────────────────────────────────
    section("gh CLI")
    gh = _gh_bin()
    if gh:
        ok("gh installed", gh)
        try:
            result = subprocess.run(
                [gh, "auth", "status"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Extract logged-in account from output
                for line in (result.stdout + result.stderr).splitlines():
                    if "Logged in to" in line or "account" in line.lower():
                        info("gh auth", line.strip())
                        break
                else:
                    ok("gh auth", "authenticated")
            else:
                warn("gh not authenticated",
                     "run: gh auth login  (or: org-llm install-tools --skip-ollama ...)")
        except Exception as e:
            warn("gh auth check failed", str(e))
    else:
        warn("gh CLI not installed",
             "run: org-llm install-tools --skip-ollama --skip-models --skip-fonts --skip-opencode")

    # ── Credentials (pass) ─────────────────────────────────────────────────────
    section("Credentials (pass)")
    from . import creds as _creds
    if _creds.is_installed():
        ok("pass installed", shutil.which("pass") or "")
        if _creds.is_initialized():
            ok("pass store initialized", str(_creds.PASS_STORE))
            secrets = _creds.list_secrets("org-llm")
            if secrets:
                info("stored secrets", f"{len(secrets)} (org-llm/*)")
                for s in secrets:
                    info(f"  ↪ {s}", "")
            else:
                info("stored secrets", "none yet — org-llm cloud --signup <slug>")
        else:
            warn("pass store not initialized",
                 "run: pass init <gpg-key-id>  (see: org-llm tutor creds)")
    else:
        warn("pass not installed",
             "run: org-llm install-tools --skip-ollama --skip-models --skip-fonts "
             "--skip-opencode --skip-gh --skip-claude")

    # ── Claude Code ────────────────────────────────────────────────────────────
    section("Claude Code")
    claude = _claude_bin()
    if claude:
        ok("claude installed", str(claude))
        api_key = (os.environ.get("ANTHROPIC_API_KEY", "")
                   or (_creds.read_secret(_creds.anthropic_slug()) or ""))
        if api_key:
            src = "env" if os.environ.get("ANTHROPIC_API_KEY") else "pass"
            ok("ANTHROPIC_API_KEY", f"set ({len(api_key)} chars, source: {src})")
        else:
            warn("ANTHROPIC_API_KEY not set",
                 f"set in shell, or: pass insert {_creds.anthropic_slug()}")
    else:
        warn("Claude Code not installed",
             "run: org-llm install-tools --skip-ollama --skip-models --skip-fonts --skip-opencode --skip-gh")

    # ── Fonts / UI ─────────────────────────────────────────────────────────────
    section("Fonts & UI")
    font_dirs = [
        Path("~/.local/share/fonts/NerdFonts").expanduser(),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    nf_files: list = []
    for fd in font_dirs:
        if fd.exists():
            nf_files += list(fd.rglob("*Nerd*")) + list(fd.rglob("*NFM*"))
    if nf_files:
        ok("Nerd Font files", f"{len(nf_files)} found")
    else:
        fail("Nerd Font not installed",
             "icons will show as □",
             "org-llm install-tools --skip-ollama --skip-models")
    if NERD_FONTS:
        ok("Nerd Font detection", "icons enabled")
    else:
        warn("Nerd Font detection off",
             "icons disabled — set ORG_LLM_NERD_FONTS=1 to force-enable")

    # ── FOSS tool suggestions ──────────────────────────────────────────────────
    section("FOSS Tools")
    not_installed = []
    for t in TOOL_REGISTRY:
        cmd = t.check_cmd.split()[0]
        if shutil.which(cmd):
            ok(t.name, t.description)
        else:
            not_installed.append(t.name)
    if not_installed:
        warn("Suggested FOSS tools",
             f"{len(not_installed)} not installed: "
             f"{', '.join(not_installed[:6])}{'…' if len(not_installed) > 6 else ''}")
        info("install any",
             "org-llm doctor --install <name>  │  --install all  │  --list-tools")

    # ── LLM tuning hint ───────────────────────────────────────────────────────
    from .models import recommendations as model_recs
    from .cloud import local_vram_gb, local_ram_gb
    _vram = local_vram_gb()
    _ram  = local_ram_gb()
    with get_session(engine) as _s:
        _cur = {role: _cfg(_s, key) for role, key, _ in _TASK_MODEL_KEYS}
    try:
        from .llm import list_models as _lm
        with get_session(engine) as _s:
            _pulled_set = set(_lm(_ollama_url(_s)))
    except Exception:
        _pulled_set = set()
    _recs = model_recs(_cur, _pulled_set, _vram, _ram)
    if _recs:
        warn("LLM tuning available",
             f"{len(_recs)} role(s) have better FOSS options for your hardware")
        info("run tuner", "org-llm models --tune")

    # ── Render table ──────────────────────────────────────────────────────────
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("St", width=3)
    table.add_column("Check", style="lcars2")
    table.add_column("Detail", style="dim")
    for status, label, detail in checks:
        table.add_row(status, label, detail)

    console.print()
    console.print(trans_stripe(52))
    console.print(PRIDE_BANNER)
    console.print(Panel(table, title="[lcars1]org-llm doctor[/lcars1]",
                        border_style="lcars1"))

    fail_count = len(issues)
    warn_count = len(warnings)
    summary = (
        f"[bold green]{fail_count == 0 and 'All checks passed' or ''}[/bold green]"
        f"[bold red]{fail_count} failure(s)[/bold red]  " if fail_count else ""
    ) + (f"[bold yellow]{warn_count} warning(s)[/bold yellow]" if warn_count else "")
    if summary:
        console.print(f"  {summary.strip()}")

    console.print(trans_stripe(52))
    console.print()

    # ── LLM Diagnosis ─────────────────────────────────────────────────────────
    if (issues or diagnose) and ollama_live:
        with get_session(engine) as session:
            url = _ollama_url(session)
            preferred = [
                _cfg(session, "chat_model"),
                _cfg(session, "fast_model"),
                _cfg(session, "text_model"),
                _cfg(session, "instruct_model"),
                _cfg(session, "reason_model"),
            ]
        # Embedding models can't do chat; never let them fall through.
        chat_capable = [p for p in pulled_models if not _is_embed_model(p)]
        diag_model = next(
            (m for m in preferred if m and any(m in p for p in chat_capable)),
            chat_capable[0] if chat_capable else None,
        )
        if not diag_model:
            red_alert("No chat-capable model pulled — cannot run LLM diagnosis.")
            on_screen("Auto-fix everything missing:  [bold]org-llm doctor --fix[/bold]")
            on_screen("Or pull manually:              [bold]org-llm models --pull llama3.2[/bold]")
            return

        state_summary = "\n".join(
            [f"- {i}" for i in issues] +
            [f"- {w}" for w in warnings]
        ) or "All checks passed — user requested diagnosis anyway."

        system = (
            "You are an expert assistant for org-llm, a Python CLI tool that indexes org-roam "
            "notes and provides LLM-powered search and Q&A using Ollama. "
            "You are given a health-check report. Respond with:\n"
            "1. A brief plain-English explanation of each failure/warning\n"
            "2. Ordered fix steps with exact commands\n"
            "3. Any follow-up checks the user should run after fixing\n"
            "Be concise, specific, and helpful. Use plain text (no markdown)."
        )
        prompt = (
            f"org-llm health check results:\n\n{state_summary}\n\n"
            f"System: Python {sys.version.split()[0]}, Ollama at {url}\n"
            f"DB: {DB_PATH}\n"
            f"Org dir: {org_dir}\n"
        )

        from .llm import chat
        try:
            with thinking("Diagnosing", model=diag_model):
                diagnosis = chat(prompt, model=diag_model, base_url=url, system=system)
            console.print(Panel(
                diagnosis,
                title=f"[lcars1]LLM Diagnosis ({diag_model})[/lcars1]",
                border_style="lcars2",
                padding=(1, 2),
            ))
        except Exception as e:
            red_alert(f"LLM diagnosis failed ({diag_model}): {e}")
            on_screen("Pull a chat-capable model:  [bold]ollama pull llama3.2[/bold]")
        console.print()
    elif issues and not ollama_live:
        red_alert(
            f"{len(issues)} issue(s) found but Ollama is not running — "
            "start it with 'ollama serve' then run 'org-llm doctor --diagnose'"
        )

    # ── Optional: append an org-mode report to a file ────────────────────────
    if report_to:
        from datetime import datetime as _dt
        path = Path(report_to).expanduser()
        ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"\n* org-llm doctor — {ts}",
            f":PROPERTIES:",
            f":CREATED:  [{ts}]",
            f":FAIL:     {len(issues)}",
            f":WARN:     {len(warnings)}",
            f":END:",
            "",
            "** Health checks",
            "",
            "| Status | Check | Detail |",
            "|---|---|---|",
        ]
        # Strip Rich markup for the org table
        import re as _re
        def _strip(s: str) -> str:
            return _re.sub(r"\[/?[^\]]+\]", "", s).replace("|", "/")
        for status, label, detail in checks:
            lines.append(f"| {_strip(status) or '·'} | {_strip(label)} | {_strip(detail)} |")
        lines += [
            "",
            f"** Failures ({len(issues)})", "",
            *(f"- {i}" for i in issues),
            "",
            f"** Warnings ({len(warnings)})", "",
            *(f"- {w}" for w in warnings),
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("\n".join(lines))
        hail(f"Appended doctor report → {path}")

    # ── Finding-aware closing suggestion ─────────────────────────────────────
    # LLM-summarises the worst finding into one actionable line; falls back
    # to raw issue text when the LLM is unreachable.
    if issues or warnings:
        all_findings = [("FAIL", i) for i in issues] + \
                        [("WARN", w) for w in warnings]
        bullet_block = "\n".join(f"  [{sev}] {msg}" for sev, msg in all_findings[:8])
        sys_msg = (
            "Summarise the worst finding from a CLI health check into ONE "
            "line: a one-clause description of the problem, then a concrete "
            "command. Format: 'Worst: <plain-language problem> — "
            "[bold]org-llm <fix command>[/bold]'. ≤ 28 words."
        )
        user_msg = (
            f"Findings (FAIL=blocking, WARN=advisory):\n{bullet_block}\n\n"
            "Pick the worst, summarise + give one fix command."
        )
        llm_summary = _llm_one_liner(user_msg, system=sys_msg, fallback="")
        if llm_summary:
            on_screen(llm_summary)
        elif issues:
            on_screen(f"[dim]Worst finding:[/dim] {issues[0]}")
            on_screen("[dim]Auto-fix:[/dim]      [bold]org-llm doctor --fix[/bold]")
        else:
            on_screen(f"[dim]Top warning:[/dim] {warnings[0]}")
    else:
        try:
            from .discover import discover, suggest_grant_roots
            from .db import Node
            with get_session(_engine()) as session:
                n_nodes = session.query(Node).count()
            roots = suggest_grant_roots()
            if n_nodes == 0:
                on_screen("[dim]Healthy.[/dim] Vault is empty — "
                          "[bold]org-llm index[/bold] when ready.")
            elif roots:
                on_screen("[dim]Healthy.[/dim] Try: "
                          "[bold]org-llm discover[/bold] for filesystem suggestions.")
            else:
                on_screen("[dim]Healthy.[/dim] Try: "
                          "[bold]org-llm performance --benchmark[/bold] for tuning data.")
        except Exception:
            pass


_TUTOR_STEPS = [
    (
        "welcome",
        "[bold lcars1]Welcome aboard, officer.[/bold lcars1]\n\n"
        "[lcars1]org-llm[/lcars1] is your personal LLM-powered second brain, "
        "built entirely on your org-roam notes.\n\n"
        "  ✦ Local-first — Ollama serves models locally; cloud is opt-in via [bold]--cloud[/bold].\n"
        "  ✦ SQLite stores the index and config — one file, zero infra.\n"
        "  ✦ sqlite-vec provides vector search inside that same file.\n"
        "  ✦ Skills let you define LLM workflows as org-babel blocks.\n"
        "  ✦ dbt transforms raw indexed data into analytics-ready views.\n"
        "  ✦ Doom Emacs integration gives you SPC l bindings for everything.\n\n"
        "Navigate with: [bold]org-llm tutor <step>[/bold]\n"
        "All steps:     [bold]org-llm tutor --all[/bold]\n"
        "Steps: welcome → init → index → embed → code-index → discover → search → ask\n"
        "       → capture → tag → code → config → skills → report → doctor →\n"
        "       doctor-walkthrough → install → db → dbt → opencode → source →\n"
        "       performance → grants → knob → personalize → theme → env →\n"
        "       review-emacs → creds → cloud → launch → emacs → claude → done",
    ),
    (
        "init",
        "[lcars2]org-llm init[/lcars2] — bootstrap your installation\n\n"
        "Creates the SQLite database at [bold]~/.local/share/org-llm/org-llm.db[/bold]\n"
        "and writes default config values into it. Safe to re-run (idempotent).\n\n"
        "[lcars1]What gets created:[/lcars1]\n"
        "  tables: files, nodes, history, config, skills\n"
        "  config defaults: org_dir=~/org, ollama_url, all model assignments\n\n"
        "[lcars1]Try it:[/lcars1]  [bold]org-llm init[/bold]\n\n"
        "[lcars1]Override DB path:[/lcars1]\n"
        "  [bold]ORG_LLM_DB=/tmp/test.db org-llm init[/bold]\n"
        "  (useful for testing without touching your real DB)\n\n"
        "[dim]Source: org_llm/db.py → init_db()  |  org-llm source db[/dim]",
    ),
    (
        "index",
        "[lcars2]org-llm index[/lcars2] — parse org files into the database\n\n"
        "Walks every .org file under org_dir, extracts:\n"
        "  • file-level #+TITLE and body\n"
        "  • each heading: title, body text, :ID: property, tags\n"
        "  • file mtime (used to skip unchanged files on re-runs)\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm index[/bold]          — incremental (skip unchanged files)\n"
        "  [bold]org-llm index --force[/bold]  — wipe and re-index everything\n\n"
        "[lcars1]What goes in the DB:[/lcars1]\n"
        "  files table  → one row per .org file (path, mtime, node_count)\n"
        "  nodes table  → one row per heading/file (title, body, tags, mtime)\n\n"
        "[lcars1]Check result:[/lcars1]  [bold]org-llm report overview[/bold]\n\n"
        "[dim]Source: org_llm/indexer.py  |  org-llm source indexer[/dim]",
    ),
    (
        "embed",
        "[lcars2]org-llm embed[/lcars2] — generate vector embeddings\n\n"
        "For every node in the DB, calls the embed_model (default: nomic-embed-text)\n"
        "and stores a 768-dimensional float32 vector in the nodes.embedding column.\n\n"
        "[lcars1]Why embeddings?[/lcars1]\n"
        "  An embedding turns text into a point in high-dimensional space.\n"
        "  Semantically similar text lands near each other. This is what powers\n"
        "  'search' and 'ask' — instead of matching words, we match meaning.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm embed[/bold]          — embed only new/unembedded nodes\n"
        "  [bold]org-llm embed --force[/bold]  — re-embed everything\n\n"
        "[lcars1]Check progress:[/lcars1]  [bold]org-llm doctor[/bold]\n\n"
        "[dim]Large vaults (10k+ nodes) take 10-30 min on first run.[/dim]\n"
        "[dim]Source: org_llm/indexer.py → embed_nodes()  |  org-llm source indexer[/dim]",
    ),
    (
        "search",
        "[lcars2]org-llm search <query>[/lcars2] — find relevant notes\n\n"
        "[lcars1]Semantic search (default):[/lcars1]\n"
        "  Embeds your query → finds nearest nodes by cosine distance.\n"
        "  Works even if no words overlap — it matches meaning.\n\n"
        "[lcars1]Keyword search:[/lcars1]\n"
        "  SQL LIKE match on title, body, and tags. Fast, exact.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm search 'Emacs configuration'[/bold]          — semantic\n"
        "  [bold]org-llm search --keyword python[/bold]               — keyword\n"
        "  [bold]org-llm search 'machine learning' --limit 20[/bold]  — top 20\n\n"
        "[lcars1]Output columns:[/lcars1]  Score | Title | Tags | File\n"
        "  Score = cosine distance (lower = more similar, 0.0 = identical)\n\n"
        "[dim]Requires embeddings. Run 'org-llm embed' first for semantic search.[/dim]\n"
        "[dim]Source: org_llm/search.py  |  org-llm source search[/dim]",
    ),
    (
        "ask",
        "[lcars2]org-llm ask <question>[/lcars2] — RAG over your org notes (and code)\n\n"
        "[lcars1]How it works (RAG = Retrieval-Augmented Generation):[/lcars1]\n"
        "  1. Your question is embedded into a vector\n"
        "  2. The top-K nearest nodes are retrieved from the DB\n"
        "  3. Smart augmentation kicks in:\n"
        "     • Temporal phrases (\"last week\", \"past 3 months\") → mtime filter\n"
        "     • Path keywords (\"daily\", \"journal\", \"diary\") → folder boost\n"
        "     • Tag refs (\"my politics tag\", \":queer:\") → tag-anchored retrieval\n"
        "  4. A retrieval line shows what was found before the LLM answers\n"
        "  5. The chat_model answers using your notes as context\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm ask 'What did I write about Emacs?'[/bold]\n"
        "  [bold]org-llm ask --cloud 'X'[/bold]                 ← route via OpenRouter\n"
        "  [bold]org-llm ask --top-k 10 'X'[/bold]              ← wider retrieval\n"
        "  [bold]org-llm ask --reason 'Plan my week'[/bold]     ← uses reason_model\n"
        "  [bold]org-llm ask --context 'X'[/bold]               ← show retrieved nodes\n"
        "  [bold]org-llm ask --days 30 'X'[/bold]               ← restrict to last 30 days\n\n"
        "[lcars1]Auto-detected temporal phrases:[/lcars1]\n"
        "  yesterday | today | this/last week | this/last month |\n"
        "  this/last quarter | this/last year | last 6 months | past 30 days |\n"
        "  recent(ly) | lately | spelled-out numbers (\"last six months\")\n\n"
        "[lcars1]Tag-aware retrieval:[/lcars1]\n"
        "  When you mention a tag (\"my X tag\", \"tagged X\", \":X:\"), org-llm checks\n"
        "  whether it actually exists. If yes, those notes lead the prompt. If no,\n"
        "  you see \"tag X not found. Closest existing: ...\" up front.\n\n"
        "[lcars1]Cross-corpus (notes + code):[/lcars1]\n"
        "  After [bold]org-llm code-index[/bold], your repos are in the same DB —\n"
        "  ask questions like \"how does cli.py wire MCP?\" and get real answers.\n\n"
        "[dim]Source: org_llm/cli.py → ask()  |  org-llm source cli[/dim]",
    ),
    (
        "capture",
        "[lcars2]org-llm capture[/lcars2] — add a new note to your vault\n\n"
        "Prompts for a title and body, optionally polishes the content with an LLM\n"
        "(instruct_model), then appends a properly-formatted org heading with a\n"
        "UUID :ID: property to a target file.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm capture[/bold]                           — interactive prompts\n"
        "  [bold]org-llm capture --title 'Meeting notes' --body 'Discussed X'[/bold]\n"
        "  [bold]org-llm capture --no-polish[/bold]               — skip LLM formatting\n"
        "  [bold]org-llm capture --file 'projects/work.org'[/bold] — custom target file\n\n"
        "[lcars1]Output format (appended to inbox.org):[/lcars1]\n"
        "  * Your Title\n"
        "  :PROPERTIES:\n"
        "  :ID: <uuid4>\n"
        "  :CREATED: [20260426120000]\n"
        "  :END:\n\n"
        "  <polished org-mode content>\n\n"
        "[dim]Run 'org-llm index' after capturing to add the new node to the DB.[/dim]\n"
        "[dim]Source: org_llm/cli.py → capture()  |  org-llm source cli[/dim]",
    ),
    (
        "tag",
        "[lcars2]org-llm tag[/lcars2] — auto-tag nodes with the fast_model\n\n"
        "Finds untagged nodes (or all nodes with --force), sends each node's\n"
        "title + body to the fast_model, and stores the suggested tags in the DB.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm tag[/bold]              — tag up to 50 untagged nodes\n"
        "  [bold]org-llm tag --limit 200[/bold]  — process more at once\n"
        "  [bold]org-llm tag --force[/bold]      — re-tag even already-tagged nodes\n\n"
        "[lcars1]How it works:[/lcars1]\n"
        "  The model is asked to output ONLY space-separated lowercase tags.\n"
        "  Tags are stored in nodes.tags in the DB.\n"
        "  Use 'org-llm tag --apply' (planned) to write them back to .org files.\n\n"
        "[lcars1]Check results:[/lcars1]  [bold]org-llm report tags[/bold]\n\n"
        "[dim]Model used: fast_model (phi4 by default) — fast, low memory.[/dim]\n"
        "[dim]Source: org_llm/cli.py → tag()  |  org-llm source cli[/dim]",
    ),
    (
        "code",
        "[lcars2]org-llm code <task>[/lcars2] — generate code from org context\n\n"
        "Describes a coding task, optionally retrieves relevant org notes as context,\n"
        "then generates code using the code_model. Output is syntax-highlighted.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm code 'parse an org file and list all headings'[/bold]\n"
        "  [bold]org-llm code 'backup my org dir' --lang sh[/bold]\n"
        "  [bold]org-llm code 'roam node query' --lang elisp[/bold]\n"
        "  [bold]org-llm code 'X' --output my_script.py[/bold]   ← write to file\n"
        "  [bold]org-llm code 'X' --no-context[/bold]            ← no org retrieval\n\n"
        "[lcars1]Supported languages:[/lcars1]  python | sh | elisp | sql | rust\n\n"
        "[lcars1]How context works:[/lcars1]\n"
        "  Your task description is embedded → top-4 nearest nodes are fetched\n"
        "  and prepended as context for the code_model.\n\n"
        "[dim]Model: code_model (qwen2.5-coder by default).[/dim]\n"
        "[dim]Source: org_llm/cli.py → code()  |  org-llm source cli[/dim]",
    ),
    (
        "config",
        "[lcars2]org-llm config[/lcars2] — view and change settings\n\n"
        "All config is stored in the SQLite DB. No config files to edit.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm config[/bold]                         — show all keys\n"
        "  [bold]org-llm config chat_model[/bold]              — show one value\n"
        "  [bold]org-llm config chat_model llama3.2[/bold]     — set a value\n\n"
        "[lcars1]All config keys:[/lcars1]\n"
        "  org_dir         path to scan for .org files  (default: ~/org)\n"
        "  ollama_url      Ollama API endpoint           (default: localhost:11434)\n"
        "  embed_model     embedding model               (nomic-embed-text)\n"
        "  chat_model      ask / general Q&A             (llama3.3)\n"
        "  code_model      code generation               (qwen2.5-coder)\n"
        "  reason_model    planning, complex reasoning   (deepseek-r1)\n"
        "  fast_model      tagging, classification       (phi4)\n"
        "  instruct_model  capture, instruction follow   (mistral-nemo)\n"
        "  text_model      summarisation, analysis       (gemma3)\n"
        "  embed_dim       embedding dimensions          (768)\n\n"
        "[dim]Source: org_llm/db.py → MODEL_DEFAULTS  |  org-llm source db[/dim]",
    ),
    (
        "skills",
        "[lcars2]Skills[/lcars2] — define reusable LLM workflows in org-mode\n\n"
        "Skills are org-babel source blocks tagged [bold]:skill:[/bold].\n"
        "You write them in your org notes, and org-llm discovers and runs them.\n\n"
        "[lcars1]Anatomy of a skill:[/lcars1]\n\n"
        "  * Summarise a note                        :skill:\n"
        "  :PROPERTIES:\n"
        "  :SKILL_NAME:  summarise\n"
        "  :SKILL_LANG:  python       ← python or sh\n"
        "  :SKILL_MODEL: text_model   ← which model config key to use\n"
        "  :END:\n\n"
        "  #+begin_src python\n"
        "  # {{input}} → replaced with your CLI argument at runtime\n"
        "  # llm.chat(prompt) → calls the configured model, returns a string\n"
        "  # cfg → dict of all config values (keys from 'org-llm config')\n"
        "  prompt = f'Summarise in 2 sentences:\\n\\n{{input}}'\n"
        "  print(llm.chat(prompt))\n"
        "  #+end_src\n\n"
        "[lcars1]Scaffold → register → run:[/lcars1]\n"
        "  [bold]org-llm skill-new my_skill[/bold]              — create template in skills.org\n"
        "  [bold]org-llm skill-new my_skill --lang sh[/bold]    — shell skill template\n"
        "  [bold]org-llm skill-index[/bold]                     — scan files, register in DB\n"
        "  [bold]org-llm skills[/bold]                          — list all registered skills\n"
        "  [bold]org-llm skill my_skill 'input text'[/bold]     — run with literal input\n"
        "  [bold]org-llm skill my_skill --node 'Emacs'[/bold]   — use a node body as input\n\n"
        "[lcars1]Shell skill example:[/lcars1]\n\n"
        "  :SKILL_LANG: sh\n"
        "  #+begin_src sh\n"
        "  echo 'Input was: {{input}}'\n"
        "  #+end_src\n\n"
        "[dim]Skills live in your org vault — version-control them with your notes.[/dim]\n"
        "[dim]Source: org_llm/skills.py  |  org-llm source skills[/dim]",
    ),
    (
        "report",
        "[lcars2]org-llm report[/lcars2] — rich text analytics on your vault\n\n"
        "[lcars1]Sections:[/lcars1]\n"
        "  [bold]overview[/bold]  files indexed, node count, embedding %, tag sets\n"
        "  [bold]tags[/bold]      tag frequency leaderboard with bar chart\n"
        "  [bold]recent[/bold]    nodes modified in the last N days (--days 14)\n"
        "  [bold]orphans[/bold]   nodes with org-roam IDs but no outgoing links\n"
        "  [bold]daily[/bold]     recent daily notes with preview\n"
        "  [bold]all[/bold]       all of the above in sequence\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm report[/bold]                 — all sections\n"
        "  [bold]org-llm report overview[/bold]        — just the stat panels\n"
        "  [bold]org-llm report recent --days 30[/bold] — 30-day window\n\n"
        "[lcars1]dbt integration:[/lcars1]\n"
        "  The raw tables are also transformed by dbt into mart views:\n"
        "  nodes_by_tag, recent_nodes, orphan_nodes, daily_notes.\n"
        "  Run [bold]dbt run[/bold] from ~/repos/org-llm/dbt/ after indexing.\n\n"
        "[dim]Source: org_llm/report.py  |  org-llm source report[/dim]",
    ),
    (
        "doctor",
        "[lcars2]org-llm doctor[/lcars2] — health check\n\n"
        "Runs a battery of checks and displays ✓ / ✗ for each:\n\n"
        "  [bold]Database reachable[/bold]   — SQLite file exists and is readable\n"
        "  [bold]sqlite-vec loaded[/bold]    — vector extension imported OK\n"
        "  [bold]org_dir exists[/bold]       — your org directory is found\n"
        "  [bold]org files found[/bold]      — .org files present in org_dir\n"
        "  [bold]Index populated[/bold]      — files + nodes in DB\n"
        "  [bold]Embeddings present[/bold]   — % of nodes with embeddings\n"
        "  [bold]Ollama reachable[/bold]     — API responding at ollama_url\n"
        "  [bold]model: <key>[/bold]         — each configured model is pulled\n"
        "  [bold]Nerd Font installed[/bold]  — fonts in ~/.local/share/fonts/\n\n"
        "[lcars1]Command:[/lcars1]  [bold]org-llm doctor[/bold]\n\n"
        "Run this first when something seems wrong. Every ✗ has a fix.\n\n"
        "[dim]Source: org_llm/cli.py → doctor()  |  org-llm source cli[/dim]",
    ),
    (
        "install",
        "[lcars2]org-llm install-tools[/lcars2] — one-shot bootstrap\n\n"
        "Downloads and installs everything you need to ~/.local (no sudo):\n\n"
        "  [bold]Ollama[/bold]      binary → ~/.local/bin/ollama, starts 'ollama serve'\n"
        "  [bold]Models[/bold]      pulls all configured models via 'ollama pull'\n"
        "  [bold]Nerd Font[/bold]   NotoMono from ryanoasis/nerd-fonts → ~/.local/share/fonts/\n"
        "  [bold]opencode[/bold]    AI coding agent from opencode.ai → ~/.local/bin/\n"
        "  [bold]gh CLI[/bold]      GitHub CLI from cli/cli releases → ~/.local/bin/\n\n"
        "[lcars1]Flags:[/lcars1]\n"
        "  --skip-ollama     skip Ollama binary download\n"
        "  --skip-models     skip model pulls\n"
        "  --skip-fonts      skip font download\n"
        "  --skip-opencode   skip opencode installation\n"
        "  --skip-gh         skip gh CLI installation\n\n"
        "[lcars1]Command:[/lcars1]  [bold]org-llm install-tools[/bold]\n\n"
        "[dim]After install, run: org-llm init → index → embed → doctor[/dim]\n"
        "[dim]Source: org_llm/cli.py → install()  |  org-llm source cli[/dim]",
    ),
    (
        "db",
        "[lcars2]org-llm db[/lcars2] — the SQLite database powering everything\n\n"
        "All data lives in a [bold]single SQLite file[/bold]:\n"
        "  [lcars1]~/.local/share/org-llm/org-llm.db[/lcars1]\n\n"
        "[lcars1]Extension:[/lcars1] sqlite-vec — adds vector similarity search inside SQLite.\n"
        "No separate vector database. One file, zero infrastructure.\n\n"
        "━━  TABLES  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "[bold lcars1]files[/bold lcars1]  — one row per indexed .org file\n"
        "  id          INTEGER PK   — auto-increment\n"
        "  path        TEXT         — absolute filesystem path\n"
        "  indexed_at  TEXT         — ISO-8601 timestamp of last index\n"
        "  node_count  INTEGER      — number of nodes parsed from this file\n"
        "  mtime       REAL         — file modification time (Unix float)\n\n"
        "[bold lcars2]nodes[/bold lcars2]  — one row per org-mode heading\n"
        "  id          INTEGER PK\n"
        "  file_id     INTEGER FK   → files.id (CASCADE DELETE)\n"
        "  node_id     TEXT UNIQUE  — org-id property (UUID) or generated\n"
        "  title       TEXT         — heading text\n"
        "  body        TEXT         — full text content under heading\n"
        "  tags        TEXT         — space-separated org tags\n"
        "  mtime       REAL         — inherited from parent file mtime\n"
        "  embedding   BLOB         — 768-dim float32 vector (nomic-embed-text)\n\n"
        "  Indexes: idx_nodes_file, idx_nodes_title, idx_nodes_tags\n\n"
        "[bold lcars3]history[/bold lcars3]  — LLM interaction log\n"
        "  id          INTEGER PK\n"
        "  timestamp   TEXT         — ISO-8601\n"
        "  command     TEXT         — CLI command (ask / code / tag / capture)\n"
        "  query       TEXT         — user input\n"
        "  response    TEXT         — LLM response\n\n"
        "[bold]config[/bold]  — key/value settings store\n"
        "  key         TEXT PK      — setting name\n"
        "  value       TEXT         — setting value\n\n"
        "  Notable keys: org_dir, ollama_url, embed_model, chat_model,\n"
        "  code_model, reason_model, fast_model, instruct_model, text_model,\n"
        "  embed_dim, cloud_provider, cloud_endpoint_url, cloud_api_key, cloud_model\n\n"
        "━━  DBT VIEWS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "dbt builds analytics views on top of the same SQLite file:\n\n"
        "  staging/stg_nodes    — cleaned nodes: relative paths, formatted dates\n"
        "  staging/stg_files    — files with days_since_modified\n"
        "  marts/nodes_by_tag   — tag → node count (powers report tags)\n"
        "  marts/recent_nodes   — modified in last 30 days\n"
        "  marts/orphan_nodes   — nodes with no incoming links\n"
        "  marts/daily_notes    — files under /daily/ path\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm db[/bold]           — show table row counts + sample rows\n"
        "  [bold]org-llm db --schema[/bold]  — show CREATE TABLE statements\n"
        "  [bold]org-llm db --dict[/bold]    — full data dictionary (this content)\n"
        "  [bold]org-llm db --query[/bold]   — run a raw SQL query\n"
        "  [bold]org-llm source db[/bold]    — see SQLAlchemy models source\n\n"
        "[dim]File: ~/.local/share/org-llm/org-llm.db  |  ORM: org_llm/db.py[/dim]",
    ),
    (
        "dbt",
        "[lcars2]dbt layer[/lcars2] — SQL transformations on your org index\n\n"
        "org-llm uses dbt (data build tool) to transform the raw indexed tables\n"
        "into analytics-ready views and tables in the same SQLite database.\n\n"
        "[lcars1]Architecture:[/lcars1]\n"
        "  Python indexer writes raw data into: files, nodes, history, config\n"
        "  dbt reads those raw tables and produces:\n\n"
        "  [bold]staging/[/bold]\n"
        "    stg_nodes    — clean nodes: relative paths, formatted dates, has_embedding flag\n"
        "    stg_files    — files with days_since_modified, formatted indexed_at\n\n"
        "  [bold]marts/[/bold]\n"
        "    nodes_by_tag   — tag → node count + titles (powers report tags)\n"
        "    recent_nodes   — nodes modified in last 30 days\n"
        "    orphan_nodes   — nodes with IDs but no incoming links\n"
        "    daily_notes    — files under /daily/ path\n\n"
        "[lcars1]How to run dbt:[/lcars1]\n"
        "  [bold]cd ~/repos/org-llm/dbt[/bold]\n"
        "  [bold]dbt run[/bold]            — build all models\n"
        "  [bold]dbt run --select staging[/bold]  — only staging models\n"
        "  [bold]dbt test[/bold]           — run data tests\n\n"
        "[lcars1]Configuration:[/lcars1]\n"
        "  dbt/profiles.yml points at ORG_LLM_DB (default: ~/.local/share/org-llm/org-llm.db)\n"
        "  Override with: ORG_LLM_DB=/path/to/other.db dbt run\n\n"
        "[dim]dbt is installed as a dependency — 'uv run dbt run' also works.[/dim]",
    ),
    (
        "opencode",
        "[lcars2]org-llm launch[/lcars2] — opencode workspace with full vault context\n\n"
        "opencode (opencode.ai) is an interactive terminal AI coding agent.\n"
        "[bold]org-llm launch[/bold] transforms it into a fully-configured second brain workspace:\n\n"
        "[lcars1]What launch does:[/lcars1]\n"
        "  1. Writes [bold]{org_dir}/.opencode.json[/bold] with:\n"
        "       • Ollama provider (uses your configured chat_model)\n"
        "       • A rich system prompt injecting vault stats, recent nodes, skills\n"
        "       • MCP server: org-llm mcp (stdio) — all org-llm tools available natively\n"
        "  2. Prints a themed launch panel (LCARS)\n"
        "  3. exec-replaces itself with opencode in org_dir\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]\n"
        "  [lcars2]SPC l o[/lcars2]  → opens a vterm buffer running [bold]org-llm launch[/bold]\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]              — full vault context + opencode\n"
        "  [bold]org-llm launch --no-context[/bold] — minimal system prompt\n"
        "  [bold]org-llm launch --dry-run[/bold]    — preview .opencode.json without launching\n"
        "  [bold]org-llm launch --model phi4[/bold] — override model\n\n"
        "[lcars1]MCP tools available inside opencode:[/lcars1]\n"
        "  search_notes   ask_notes    capture_note   get_node\n"
        "  list_skills    run_skill    tangle_file    get_vault_stats\n"
        "  list_recent_nodes  list_nodes_by_tag  get_config\n"
        "  get_tutor_step  list_tutor_steps\n\n"
        "[lcars1]Use cases inside opencode:[/lcars1]\n"
        "  • 'What did I write about Python last month?'  → search_notes\n"
        "  • 'Summarise my project notes'                → ask_notes\n"
        "  • 'Save this idea to inbox.org'               → capture_note\n"
        "  • 'Run my summarise skill on this text'       → run_skill\n"
        "  • 'Tangle my config.org'                      → tangle_file\n"
        "  • 'Show me how embeddings work'               → get_tutor_step\n\n"
        "[dim]org-llm mcp   starts the MCP server standalone (for other clients too).[/dim]",
    ),
    (
        "source",
        "[lcars2]org-llm source <module>[/lcars2] — inspect org-llm's own code\n\n"
        "Shows the source of any org-llm module with syntax highlighting.\n"
        "With --explain, the LLM explains what the code does.\n\n"
        "[lcars1]Available modules:[/lcars1]\n"
        "  cli        — all CLI commands (Typer app)\n"
        "  db         — SQLAlchemy models + init_db\n"
        "  indexer    — org file parser + embed_nodes\n"
        "  llm        — Ollama client wrapper\n"
        "  search     — vector_search + keyword_search\n"
        "  skills     — skill discovery + execution engine\n"
        "  cli_skills — skill CLI commands\n"
        "  report     — report_overview, report_tags, etc.\n"
        "  ui         — Rich theming + progress bars\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm source db[/bold]               — show db.py with line numbers\n"
        "  [bold]org-llm source indexer --explain[/bold] — show + LLM explanation\n"
        "  [bold]org-llm source llm[/bold]               — the Ollama wrapper (it's tiny!)\n\n"
        "[dim]This command uses Python's inspect module — always shows live code.[/dim]",
    ),
    (
        "emacs",
        "[lcars2]Doom Emacs integration[/lcars2]\n\n"
        "Load from config.el:  [bold](load! \"~/repos/org-llm/doom/org-llm\")[/bold]\n\n"
        "[lcars1]Keybindings (all under SPC l):[/lcars1]\n\n"
        "  [lcars2]SPC l o[/lcars2]   org-llm-launch       — open opencode workspace in full vterm\n"
        "  [lcars2]SPC l a[/lcars2]   org-llm-ask          — ask a question, answer in side window\n"
        "  [lcars2]SPC l A[/lcars2]   ask (reason model)   — use deepseek-r1 for hard questions\n"
        "  [lcars2]SPC l s[/lcars2]   org-llm-search       — semantic search, results in side window\n"
        "  [lcars2]SPC l S[/lcars2]   keyword search       — fast SQL-based search\n"
        "  [lcars2]SPC l c[/lcars2]   org-llm-capture      — capture note (prompts title + body)\n"
        "  [lcars2]SPC l r[/lcars2]   org-llm-report       — open report in vterm\n"
        "  [lcars2]SPC l i[/lcars2]   org-llm-index        — re-index org files\n"
        "  [lcars2]SPC l e[/lcars2]   org-llm-embed        — generate embeddings\n"
        "  [lcars2]SPC l m[/lcars2]   org-llm-models       — list models in side window\n"
        "  [lcars2]SPC l d[/lcars2]   org-llm-doctor       — health check in vterm\n"
        "  [lcars2]SPC l .[/lcars2]   org-llm-ask-dwim     — ask about region or sentence at point\n\n"
        "[lcars1]How results appear:[/lcars1]\n"
        "  SPC l o  → full window vterm (opencode takes over — q to quit)\n"
        "  ask / search / models → side window (right, 45% width), ANSI-coloured\n"
        "  index / embed / report / doctor → vterm side window (bottom, 35% height)\n\n"
        "[dim]Source: doom/org-llm.el  |  org-llm source cli[/dim]",
    ),
    (
        "code-index",
        "[lcars2]org-llm code-index[/lcars2] — index source repos for cross-corpus retrieval\n\n"
        "Walks any directory tree (default: =~/repos= via the [bold]code_dirs[/bold]\n"
        "config row) and indexes source-shaped files (.py .el .rs .ts .md .org .yaml\n"
        ".toml etc) into the same SQLite DB as your notes. After this, [bold]ask[/bold]\n"
        "can answer across notes AND code in one shot.\n\n"
        "[lcars1]What gets indexed:[/lcars1]\n"
        "  • One File + one Node per file (whole-file body, capped at 24 KB)\n"
        "  • Tagged [lcars3]code code:<lang>[/lcars3] so retrieval can scope\n"
        "  • Skips: .git, node_modules, .venv, target, dist, build, hidden dirs\n"
        "  • Per-file isolation: a binary or symlink loop rolls back only that row\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm code-index[/bold]                           — index ~/repos\n"
        "  [bold]org-llm code-index ~/proj1 ~/proj2[/bold]           — explicit paths\n"
        "  [bold]org-llm code-index --force[/bold]                   — wipe and reindex\n"
        "  [bold]org-llm code-index --no-embed[/bold]                — skip auto-embed\n\n"
        "[lcars1]Set the default search root:[/lcars1]\n"
        "  [bold]org-llm config code_dirs ~/repos,~/dotfiles[/bold]\n\n"
        "[lcars1]Try it:[/lcars1]\n"
        "  [bold]org-llm ask --cloud 'how does cli.py wire MCP?'[/bold]\n"
        "  [bold]org-llm ask --cloud 'where do we filter sensitive paths?'[/bold]\n\n"
        "[dim]Source: org_llm/code_index.py  |  org-llm source code_index[/dim]",
    ),
    (
        "discover",
        "[lcars2]org-llm discover[/lcars2] — probe the filesystem for what org-llm can use\n\n"
        "Walks a small set of standard locations (=~/org=, =~/repos=, =~/code=,\n"
        "=~/projects=, =~/.config/doom=, =~/.emacs.d=, =~/.password-store=, …)\n"
        "and reports what actually exists, with file counts and the most-frequent\n"
        "code language per root.\n\n"
        "[lcars1]What you get back:[/lcars1]\n"
        "  • A table of vault / repos-root / dotfiles / doom-config / pass-store rows\n"
        "  • Suggested [bold]code-index[/bold] roots based on which dirs hold real code\n"
        "  • Suggested [bold]grant-root[/bold] candidates for MCP self-grant access\n"
        "  • Detected preferred language (used by [bold]code[/bold] for default lang)\n\n"
        "[lcars1]Auto-heal: where this kicks in implicitly:[/lcars1]\n"
        "  • [bold]code-index <bad-paths>[/bold] — if NONE of the given paths exist,\n"
        "    [bold]discover[/bold] runs automatically and offers found code roots\n"
        "    instead of red-alerting.\n"
        "  • [bold]grants[/bold] — empty-state shows discovered grant-root candidates\n"
        "    with ready-to-paste commands.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm discover[/bold]                  — standard probe\n"
        "  [bold]org-llm discover ~/extra/dir[/bold]      — also probe an extra dir\n\n"
        "[dim]Source: org_llm/discover.py  |  org-llm source discover[/dim]",
    ),
    (
        "performance",
        "[lcars2]org-llm performance[/lcars2] — tune model assignments to your hardware\n\n"
        "Probes free RAM/VRAM (not total — you have other apps open!), CPU info,\n"
        "and disk free. Optionally benchmarks each pulled chat/embed model for\n"
        "real tokens-per-second. Then recommends role assignments that fit your\n"
        "actual budget, with explicit DOWNGRADE for oversized current models.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm performance[/bold]              — read-only report\n"
        "  [bold]org-llm performance --quick[/bold]      — hardware probe only (skip Ollama)\n"
        "  [bold]org-llm performance --benchmark[/bold]  — measure tokens/s per model (1-3 min)\n"
        "  [bold]org-llm performance --apply[/bold]      — write recommended assignments to config\n\n"
        "[lcars1]Severity icons in the recommendation table:[/lcars1]\n"
        "  [bold red]↓[/]  [red]downgrade[/]  — current model needs more RAM than you have\n"
        "  [bold yellow]↑[/]  [yellow]upgrade[/]    — a higher-quality fitting model exists\n"
        "  [bold cyan]+[/]  [cyan]missing[/]    — role unassigned\n"
        "  [bold green]=[/]  [green]fit[/]        — already optimal\n\n"
        "[lcars1]Why this exists:[/lcars1]\n"
        "  [bold]models --tune[/bold] uses catalog VRAM × 0.55 × total RAM. That math\n"
        "  passes models that OOM in practice (everything else on your laptop\n"
        "  competes for RAM). [bold]performance[/bold] uses free RAM (read at runtime)\n"
        "  AND the actual benchmark error (with --benchmark) so it catches\n"
        "  real-world failures the catalog can't predict.\n\n"
        "[dim]Source: org_llm/performance.py  |  org-llm source performance[/dim]",
    ),
    (
        "grants",
        "[lcars2]org-llm grant / revoke / grants[/lcars2] — let the LLM read files (carefully)\n\n"
        "Three-tier authorization for the [bold]read_file[/bold], [bold]list_directory[/bold],\n"
        "[bold]request_access[/bold], [bold]open_url[/bold], and [bold]browser_command[/bold] MCP tools:\n\n"
        "  [lcars1]1. ALWAYS denied (deny-list):[/lcars1]\n"
        "     ~/.ssh, ~/.gnupg, ~/.password-store, ~/.aws/credentials,\n"
        "     ~/.kube/config, ~/.netrc, /etc/shadow, /root/, …\n"
        "     [dim]Even with grants. Even with auto-roots. Always.[/dim]\n\n"
        "  [lcars1]2. Direct grants (allow-list):[/lcars1]\n"
        "     [bold]org-llm grant <path>[/bold]    — LLM can read this + descendants\n"
        "     [bold]org-llm revoke <path>[/bold]   — remove\n\n"
        "  [lcars1]3. Auto-grant roots (LLM self-extends):[/lcars1]\n"
        "     [bold]org-llm grant-root ~[/bold]    — LLM may self-grant under ~ (except deny-list)\n"
        "     [bold]org-llm revoke-root <path>[/bold]\n"
        "     The MCP tool [lcars3]request_access(path, reason)[/lcars3] checks if path is\n"
        "     under a trusted root, then auto-grants and proceeds.\n\n"
        "[lcars1]Browser:[/lcars1]\n"
        "  [bold]org-llm grant-browser[/bold]      — enable open_url + browser_command\n"
        "  [bold]org-llm revoke-browser[/bold]\n"
        "  Uses qutebrowser if installed ([bold]doctor --install qutebrowser[/bold]),\n"
        "  else xdg-open. Refuses non-http(s) URLs always.\n\n"
        "[lcars1]Inspect what the LLM sees:[/lcars1]\n"
        "  [bold]org-llm grants[/bold] — show all current grants + auto-roots + browser flag\n\n"
        "[lcars1]Why three tiers?[/lcars1]\n"
        "  Direct grants are auditable. Auto-roots let you say \"trust the LLM\n"
        "  inside this folder\" without preauthorising every file. The deny-list\n"
        "  catches the obvious dangers regardless. The MCP server runs over\n"
        "  stdio with no TTY — it can't pop a permission dialog at request\n"
        "  time, so explicit grants are the contract.\n\n"
        "[dim]Source: org_llm/access.py  |  org-llm source access[/dim]",
    ),
    (
        "knob",
        "[lcars2]org-llm knob[/lcars2] — define your own theme dials\n\n"
        "Built-in theme knobs (each 0..3) shape the make_it_so message pool:\n"
        "  • [bold]trek[/bold]    — Trek references\n"
        "  • [bold]commie[/bold]  — solidarity / labor / property messaging\n"
        "  • [bold]queer[/bold]   — pride / trans messaging\n\n"
        "[lcars1]Levels are weights, not booleans:[/lcars1]\n"
        "  0 = silent | 1 = sparse (½×) | 2 = normal (1×) | 3 = max (2×)\n"
        "  Multi-tagged messages (e.g. trek+commie) use the MIN level. Setting\n"
        "  any tag to 0 silences messages that depend on it.\n\n"
        "[lcars1]Set persistently (writes to SQLite config):[/lcars1]\n"
        "  [bold]org-llm config queer_level 1[/bold]      — half as much pride\n"
        "  [bold]org-llm config commie_level 3[/bold]     — twice as much solidarity\n"
        "  [bold]org-llm config trek_level 0[/bold]       — silence Trek entirely\n\n"
        "[lcars1]Override per command (env wins over config):[/lcars1]\n"
        "  [bold]ORG_LLM_QUEER_LEVEL=0 org-llm report all[/bold]\n\n"
        "[lcars1]Add your own knob:[/lcars1]\n"
        "  [bold]org-llm knob add dinosaur \\[/bold]\n"
        "    [bold]-m '◀ ROAR.|info' \\[/bold]\n"
        "    [bold]-m '◀ Dino-mite work, comrade.|lcars1'[/bold]\n"
        "  Activate:  [bold]org-llm config dinosaur_level 2[/bold]   (or =ORG_LLM_DINOSAUR_LEVEL=2=)\n\n"
        "[lcars1]Inspect:[/lcars1]\n"
        "  [bold]org-llm knob list[/bold]   — built-in + user knobs with active level + source\n"
        "  Active-level column shows [dim](env)[/dim] / [dim](config)[/dim] / [dim](default)[/dim]\n\n"
        "[lcars1]Storage:[/lcars1]\n"
        "  Built-in levels:  Config rows =trek_level=, =commie_level=, =queer_level=\n"
        "  User knobs:       Config row =user_theme_knobs= (JSON list)\n"
        "  Built-in knobs cannot be removed; set their level to 0 to silence.\n\n"
        "[lcars1]Quiet-everything mode:[/lcars1]\n"
        "  [bold]org-llm config trek_level 0; and config commie_level 0; and config queer_level 0[/bold]\n"
        "  → every completion becomes a single neutral \"◀ Done.\"\n\n"
        "[dim]Source: org_llm/ui.py → _enabled_msgs()  |  org-llm source ui[/dim]",
    ),
    (
        "personalize",
        "[lcars2]org-llm personalize[/lcars2] — auto-create theme knobs from your content\n\n"
        "Reads your actual vault + filesystem and proposes theme knobs that\n"
        "match your real interests. Detection is deterministic; only the\n"
        "completion-message generation can call out to the local LLM.\n\n"
        "[lcars1]What it scans:[/lcars1]\n"
        "  • Top non-boring tags in your =Node.tags= (≥3 occurrences each)\n"
        "  • Project names under repos-roots from [bold]org-llm discover[/bold]\n"
        "  • Detected preferred language (from code-shaped file extensions)\n\n"
        "[lcars1]What you get:[/lcars1]\n"
        "  Each proposal becomes a [bold]knob[/bold]: a name, default level (1–2),\n"
        "  and 4–8 messages spread across LCARS / pride colours. The LLM in\n"
        "  opencode also sees these knobs in its system prompt and is asked\n"
        "  to match the energy.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm personalize[/bold]                  — dry-run preview (default)\n"
        "  [bold]org-llm personalize --apply[/bold]          — register the proposed knobs\n"
        "  [bold]org-llm personalize -a --no-llm[/bold]      — apply with template messages only\n"
        "  [bold]org-llm personalize -a --overwrite[/bold]   — replace existing user knobs\n"
        "  [bold]org-llm personalize --max 3[/bold]          — cap proposed count\n\n"
        "[lcars1]Inspect after:[/lcars1]\n"
        "  [bold]org-llm knob list[/bold]                    — see all dials + active levels\n"
        "  [bold]org-llm config <name>_level 0..3[/bold]     — tune individually\n\n"
        "[dim]Source: org_llm/personalize.py  |  org-llm source personalize[/dim]",
    ),
    (
        "doctor-walkthrough",
        "[lcars2]org-llm doctor --walkthrough[/lcars2] — LLM-driven self-test\n\n"
        "Doctor in self-tester mode. Runs a curated set of read-only commands,\n"
        "captures stdout, asks the configured cloud LLM to judge each output,\n"
        "and ends with a top-3 recommendation list ranked by impact:effort.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm doctor -w[/bold]                          — run the walkthrough\n"
        "  [bold]org-llm doctor -wr ~/org/dev-log.org[/bold]       — append a structured report\n"
        "  [bold]org-llm doctor --walkthrough --report-to PATH[/bold]   — long form\n\n"
        "[lcars1]What it probes (13 read-only commands):[/lcars1]\n"
        "  tutor welcome | doctor | cloud --status | cloud --providers |\n"
        "  cloud --cost  | models --discover | performance --quick |\n"
        "  theme show    | knob list | grants | db -q '...' |\n"
        "  report tags   | --help\n\n"
        "[lcars1]Two layers of judgement:[/lcars1]\n"
        "  Mechanical — exit code + expected substrings present?\n"
        "  Qualitative — cloud LLM marks each probe ✓ PASS / ⚠ NIT / ✗ ISSUE\n"
        "                with specific suggestions when it sees friction.\n\n"
        "[lcars1]Why this exists:[/lcars1]\n"
        "  CI-friendly self-test you can wire into a pre-push hook:\n"
        "    [bold]org-llm doctor -wr dev-log/run-$(git rev-parse --short HEAD).org[/bold]\n"
        "  Plus: a second pair of eyes on every change, courtesy of the LLM.\n\n"
        "[lcars1]Requirements:[/lcars1]\n"
        "  Cloud backend configured (the local llama3.3 won't fit on most\n"
        "  laptops). Run [bold]org-llm cloud --quick-start openrouter[/bold] first.\n\n"
        "[dim]Source: org_llm/cli.py → _doctor_walkthrough()  |  org-llm source cli[/dim]",
    ),
    (
        "theme",
        "[lcars2]org-llm theme[/lcars2] — dark (default) or light UI\n\n"
        "Every colour the app emits — Rich text, banners, panels, progress bars,\n"
        "trans/pride stripes, and the [bold]bat[/]/[bold]delta[/]/[bold]starship[/]/[bold]fzf[/] theme files\n"
        "[bold]doctor --install <tool>[/bold] writes — switches with this setting.\n\n"
        "[lcars1]Set persistently:[/lcars1]\n"
        "  [bold]org-llm theme dark[/bold]      — bright LCARS oranges/purples on a dark terminal (default)\n"
        "  [bold]org-llm theme light[/bold]     — darkened palette legible on a white terminal\n"
        "  [bold]org-llm theme toggle[/bold]    — flip whatever is currently set\n"
        "  [bold]org-llm theme show[/bold]      — print the stored mode + active mode\n\n"
        "[lcars1]Override per command:[/lcars1]\n"
        "  [bold]ORG_LLM_THEME=light org-llm doctor[/bold]\n"
        "  [bold]ORG_LLM_THEME=dark  org-llm report all[/bold]\n\n"
        "[lcars1]How it works:[/lcars1]\n"
        "  [bold]ui.py[/bold] defines [lcars3]DARK_PALETTE[/lcars3] and [lcars3]LIGHT_PALETTE[/lcars3] with the same keys.\n"
        "  Resolution order: ORG_LLM_THEME env → 'theme' config row → 'dark'.\n"
        "  Tool theme generators ([bold]models.py[/bold]) read a live proxy onto [bold]ui.PALETTE[/bold]\n"
        "  so re-running [bold]doctor --install <tool>[/bold] after a flip writes new colours.\n\n"
        "[lcars1]What changes between modes:[/lcars1]\n"
        "  • LCARS orange/purple/blue darkened ~40% in light mode\n"
        "  • Pride yellow → mustard (#996600) — pure yellow is invisible on white\n"
        "  • Trans white → gray (#444444) — same reason\n"
        "  • Doom accents swap to one-light analogues\n\n"
        "[dim]Source: ui.py → DARK_PALETTE / LIGHT_PALETTE / _build_theme()[/dim]",
    ),
    (
        "env",
        "[lcars2]Environment variables[/lcars2] — override config without touching the DB\n\n"
        "Resolution order: [bold]env var → SQLite config → built-in default[/bold].\n"
        "Set any of these to override a single command run.\n\n"
        "[lcars1]Storage / paths:[/lcars1]\n"
        "  [bold]ORG_LLM_DB[/bold]            SQLite DB path  (default: ~/.local/share/org-llm/org-llm.db)\n"
        "  [bold]ORG_LLM_ORG_DIR[/bold]       Org-roam directory  (default: org_dir config, fallback ~/org)\n"
        "  [bold]PASSWORD_STORE_DIR[/bold]    `pass` store location  (default: ~/.password-store)\n\n"
        "[lcars1]Models / endpoints:[/lcars1]\n"
        "  [bold]ORG_LLM_OLLAMA_URL[/bold]    Ollama base URL  (default: ollama_url config, fallback http://localhost:11434)\n"
        "  [bold]ANTHROPIC_API_KEY[/bold]     Used by [bold]org-llm claude[/bold]; falls back to pass slug org-llm/anthropic/api-key\n\n"
        "[lcars1]UI / theme:[/lcars1]\n"
        "  [bold]ORG_LLM_THEME[/bold]            dark | light  (default: dark; also: [bold]org-llm theme[/bold])\n"
        "  [bold]ORG_LLM_NERD_FONTS[/bold]       1/yes/true | 0/no/false — force icon mode\n"
        "  [bold]ORG_LLM_TREK_LEVEL[/bold]       0..3 — weight, not on/off (also: [bold]config trek_level N[/bold])\n"
        "  [bold]ORG_LLM_COMMIE_LEVEL[/bold]     0..3 — solidarity weight (also: [bold]config commie_level N[/bold])\n"
        "  [bold]ORG_LLM_QUEER_LEVEL[/bold]      0..3 — pride/trans weight (also: [bold]config queer_level N[/bold])\n"
        "  [bold]ORG_LLM_<KNOB>_LEVEL[/bold]     0..3 — any user-registered knob (see [bold]org-llm knob[/bold])\n"
        "                            Resolution: env → SQLite config row → default.\n"
        "                            Levels behave as weights (0=silent, 1=½×, 2=1×, 3=2×).\n\n"
        "[lcars1]Examples:[/lcars1]\n"
        "  [bold]ORG_LLM_DB=/tmp/test.db org-llm init[/bold]                  — sandbox a throwaway DB\n"
        "  [bold]ORG_LLM_ORG_DIR=~/work-notes org-llm index[/bold]            — index a side-vault\n"
        "  [bold]ORG_LLM_OLLAMA_URL=https://ollama.lan:11434 org-llm ask 'q'[/bold]  — point at remote Ollama\n"
        "  [bold]ORG_LLM_TREK_LEVEL=0 ORG_LLM_COMMIE_LEVEL=0 org-llm doctor[/bold]  — quiet mode\n\n"
        "[dim]Source: cli.py → _engine() / _ollama_url() / _org_dir()  |  ui.py for theme vars[/dim]",
    ),
    (
        "review-emacs",
        "[lcars2]org-llm review-emacs[/lcars2] — LLM-driven Emacs config review\n\n"
        "Auto-detects your Doom (~/.config/doom or ~/.doom.d) or vanilla\n"
        "(~/.config/emacs or ~/.emacs.d) config, reads the canonical files,\n"
        "and asks the [bold]reason_model[/bold] for structured advice.\n\n"
        "[lcars1]Sections in the report:[/lcars1]\n"
        "  Summary  •  Strengths  •  Issues  •  Improvements  •  Optional polish\n"
        "  Each item cites filename:line so you can jump straight to it.\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm review-emacs[/bold]                       — full review\n"
        "  [bold]org-llm review-emacs --focus performance[/bold]   — narrow scope\n"
        "  [bold]org-llm review-emacs --focus theming[/bold]       — visual coherence only\n"
        "  [bold]org-llm review-emacs --diff-only -o p.md[/bold]   — emit patch hunks\n"
        "  [bold]org-llm review-emacs -d ~/dotfiles/doom[/bold]    — explicit path\n\n"
        "[lcars1]Focus areas:[/lcars1]  all | theming | performance | packages | keybindings | cleanup\n\n"
        "[lcars1]How it works:[/lcars1]\n"
        "  1. Detects flavor (doom vs vanilla) by directory name\n"
        "  2. Reads init.el / config.el / packages.el (or lisp/*.el for vanilla)\n"
        "  3. Truncates each file to 8 KB so the prompt stays bounded\n"
        "  4. Logs the review into the [bold]history[/bold] table for later search\n\n"
        "[lcars1]Why reason_model?[/lcars1]\n"
        "  Config review benefits from a deliberation-style model (deepseek-r1).\n"
        "  Override with --model phi4 if you want fast/cheap instead.\n\n"
        "[dim]Source: cli.py → review_emacs()  |  history table holds past reviews[/dim]",
    ),
    (
        "creds",
        "[lcars2]org-llm credentials[/lcars2] — encrypted secrets via [bold]pass[/bold]\n\n"
        "API keys for cloud providers and Claude Code are stored in the standard\n"
        "Unix password manager [bold]pass[/bold] (passwordstore.org), under slugs of the form\n"
        "  [lcars3]org-llm/cloud/<provider>/api-key[/lcars3]\n"
        "  [lcars3]org-llm/anthropic/api-key[/lcars3]\n\n"
        "[lcars1]Why pass?[/lcars1]\n"
        "  • One file per secret, GPG-encrypted at rest under ~/.password-store/\n"
        "  • Standard tooling (browser extensions, mobile, Emacs auth-source)\n"
        "  • Versioned with git if you `pass git init`\n"
        "  • Keys never touch SQLite or any plaintext config\n\n"
        "[lcars1]One-time setup:[/lcars1]\n"
        "  1. [bold]org-llm install-tools[/bold]                  — installs pass + gnupg\n"
        "  2. [bold]gpg --full-generate-key[/bold]          — pick RSA 4096, no expiry, your name+email\n"
        "  3. [bold]gpg --list-secret-keys[/bold]           — copy the long key id\n"
        "  4. [bold]pass init <KEY-ID>[/bold]               — initializes the store\n\n"
        "[lcars1]Storing keys:[/lcars1]\n"
        "  [bold]org-llm cloud --signup runpod[/bold]   — opens browser, then prompts for key\n"
        "  [bold]org-llm cloud --configure[/bold]        — pick provider, paste endpoint + key\n"
        "  [bold]pass insert org-llm/anthropic/api-key[/bold]  — manual entry for claude\n\n"
        "[lcars1]Inspecting + managing:[/lcars1]\n"
        "  [bold]org-llm cloud --creds[/bold]    — list stored slugs, show pass status\n"
        "  [bold]pass ls org-llm[/bold]          — tree view of all org-llm secrets\n"
        "  [bold]pass show <slug>[/bold]         — print one secret\n"
        "  [bold]pass rm <slug>[/bold]           — delete one secret\n\n"
        "[lcars1]Resolution order at runtime:[/lcars1]\n"
        "  Anthropic key:  ANTHROPIC_API_KEY env  →  pass  →  prompt\n"
        "  Cloud GPU key:  pass  →  cloud_api_key in SQLite (legacy)  →  none\n\n"
        "[lcars1]Migrating from SQLite:[/lcars1]\n"
        "  When you run [bold]--configure[/bold] with pass available, an existing SQLite key is\n"
        "  copied into pass and removed from the DB.\n\n"
        "[lcars1]Override store location:[/lcars1]\n"
        "  export PASSWORD_STORE_DIR=~/sync/secrets\n\n"
        "[dim]Source: org_llm/creds.py  |  org-llm source creds[/dim]",
    ),
    (
        "cloud",
        "[lcars2]org-llm cloud[/lcars2] — multi-provider GPU cloud backend\n\n"
        "When local Ollama can't run a model (not enough VRAM/RAM), point the app\n"
        "at any Ollama- or OpenAI-compatible cloud endpoint.\n\n"
        "[lcars1]Supported providers:[/lcars1]\n"
        "  RunPod        Vast.ai      Lambda Labs    TensorDock\n"
        "  Salad Cloud   Paperspace   CoreWeave\n\n"
        "[lcars1]Setup flow:[/lcars1]\n"
        "  1. [bold]org-llm cloud --providers[/bold]        — compare prices and APIs\n"
        "  2. [bold]org-llm cloud --signup <slug>[/bold]    — open signup for chosen provider\n"
        "  3. Deploy an Ollama or OpenAI-compatible inference endpoint\n"
        "  4. [bold]org-llm cloud --configure[/bold]        — pick provider + paste URL + API key\n"
        "  5. [bold]org-llm cloud --test[/bold]              — verify connection\n\n"
        "[lcars1]Assessment + cost:[/lcars1]\n"
        "  [bold]org-llm cloud --assess[/bold]  — which models need cloud (VRAM/RAM check)\n"
        "  [bold]org-llm cloud --cost[/bold]    — side-by-side GPU pricing across providers\n\n"
        "[lcars1]Config keys set by --configure:[/lcars1]\n"
        "  cloud_provider       runpod | vast | lambda | tensordock | salad | …\n"
        "  cloud_endpoint_url   provider-specific URL (see endpoint_hint)\n"
        "  cloud_api_key        bearer token (blank for unauthenticated pods)\n"
        "  cloud_model          model tag on the cloud endpoint\n\n"
        "[lcars1]Doctor integration:[/lcars1]\n"
        "  org-llm doctor shows a Cloud GPU section with provider + connection status.\n\n"
        "[lcars1]Theme levels (env vars):[/lcars1]\n"
        "  ORG_LLM_TREK_LEVEL=0..3    — Trek references intensity (default: 2)\n"
        "  ORG_LLM_COMMIE_LEVEL=0..3  — Solidarity messaging intensity (default: 2)\n\n"
        "[dim]Source: org_llm/cloud.py  |  org-llm source cloud[/dim]",
    ),
    (
        "launch",
        "[lcars2]org-llm launch[/lcars2] — themed opencode workspace, the *other face* of org-llm\n\n"
        "opencode is intended to be as rich as the CLI — not a stripped chat\n"
        "interface. [bold]launch[/bold] sets up a project-local =.opencode/= directory\n"
        "with everything wired in.\n\n"
        "[lcars1]What gets written:[/lcars1]\n"
        "  • [bold].opencode.json[/bold]                       — model, MCP server, instructions, theme ref\n"
        "  • [bold].opencode/themes/org-llm-lcars.json[/bold]  — LCARS palette matching CLI (light + dark)\n"
        "  • [bold].opencode/command/<name>.md[/bold]          — slash-commands (see below)\n\n"
        "[lcars1]Workspaces (--workspace / -w):[/lcars1]\n"
        "  [bold]all[/bold]         — full toolbox (default)\n"
        "  [bold]researcher[/bold]  — read-heavy: search/ask/get_node, no captures\n"
        "  [bold]scribe[/bold]      — capture-heavy: capture_note + skill workflows\n"
        "  [bold]engineer[/bold]    — code-corpus + repo focus, code_search emphasis\n\n"
        "[lcars1]Slash-commands (default):[/lcars1]\n"
        "  [lcars3]/discover[/lcars3]  /recent  /health  /stats  /tags  /tutor  /code\n"
        "  Plus per-workspace: [lcars3]/explore[/lcars3] (researcher), [lcars3]/capture[/lcars3] (scribe), [lcars3]/repo[/lcars3] (engineer)\n\n"
        "[lcars1]System prompt is pre-loaded with:[/lcars1]\n"
        "  vault stats · recent activity · top-10 tags · model assignments ·\n"
        "  free RAM/VRAM · filesystem inventory · active theme dials/knobs\n\n"
        "[lcars1]30 MCP tools available inside opencode:[/lcars1]\n"
        "  Reading:    search_notes ask_notes get_node list_nodes_by_tag\n"
        "              list_recent_nodes recent_files get_vault_stats\n"
        "  Writing:    capture_note run_skill tangle_file index_vault\n"
        "              embed_pending set_config (allow-listed)\n"
        "  Code:       code_search (lang-filterable)\n"
        "  Filesystem: discover_filesystem doctor_health performance_status\n"
        "              list_models read_file list_directory list_grants request_access\n"
        "  Browser:    open_url browser_command (when granted)\n"
        "  Skills:     list_skills list_tutor_steps get_tutor_step\n"
        "  Config:     get_config set_config\n\n"
        "[lcars1]Theme integration:[/lcars1]\n"
        "  The TUI uses LCARS colors matching the CLI. The system prompt also\n"
        "  surfaces your active dials ([bold]trek/commie/queer[/bold]) and any user-\n"
        "  defined [bold]knob[/bold]s, so the in-opencode model matches your CLI vibe.\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]  [lcars2]SPC l o[/lcars2] — opens vterm + launches workspace\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]                       — default (all) workspace\n"
        "  [bold]org-llm launch -w researcher[/bold]         — researcher flavor\n"
        "  [bold]org-llm launch --no-theme[/bold]            — skip writing theme file\n"
        "  [bold]org-llm launch --no-commands[/bold]         — skip slash-commands\n"
        "  [bold]org-llm launch --no-context[/bold]          — minimal prompt\n"
        "  [bold]org-llm launch --dry-run[/bold]             — preview without launching\n"
        "  [bold]org-llm launch --model phi4[/bold]          — override chat model\n"
        "  [bold]org-llm mcp[/bold]                          — run MCP server standalone\n\n"
        "[dim]Source: org_llm/mcp_server.py + cli.py → launch()  |  org-llm source mcp_server[/dim]",
    ),
    (
        "claude",
        "[lcars2]org-llm claude[/lcars2] — Claude Code as interactive org-roam workspace\n\n"
        "Connects the Anthropic Claude CLI to your vault via MCP tools.\n"
        "Unlike opencode (Ollama-backed), Claude Code uses the Anthropic API\n"
        "or a claude.ai Pro subscription — state-of-the-art models, no local GPU needed.\n\n"
        "[lcars1]What it does:[/lcars1]\n"
        "  1. Installs [bold]claude[/bold] CLI via npm if missing\n"
        "  2. Writes [bold]{org_dir}/.claude/settings.json[/bold] — MCP server entry\n"
        "  3. Writes [bold]{org_dir}/.claude/CLAUDE.md[/bold] — vault context instructions\n"
        "  4. exec-replaces itself with [bold]claude[/bold] in org_dir\n\n"
        "[lcars1]Requirements:[/lcars1]\n"
        "  ANTHROPIC_API_KEY  — get one at console.anthropic.com\n"
        "  OR claude.ai Pro subscription (claude.ai/login)\n"
        "  Node.js / npm       — for installing the claude package\n\n"
        "[lcars1]From Doom Emacs:[/lcars1]  [lcars2]SPC l C[/lcars2] — opens vterm + launches Claude Code\n\n"
        "[lcars1]Commands:[/lcars1]\n"
        "  [bold]org-llm claude[/bold]              — full context workspace\n"
        "  [bold]org-llm claude --no-context[/bold] — minimal .claude/CLAUDE.md\n"
        "  [bold]org-llm claude --dry-run[/bold]    — preview config without launching\n\n"
        "[lcars1]vs opencode:[/lcars1]\n"
        "  opencode → Ollama models (local, private, free after hardware)\n"
        "  claude   → Anthropic API (cloud, frontier models, per-token cost)\n\n"
        "[dim]Source: cli.py → claude_frontend()  |  config: .claude/settings.json[/dim]",
    ),
    (
        "done",
        "[bold lcars1]You're ready to explore your second brain.[/bold lcars1]\n\n"
        "[lcars1]Recommended first flight:[/lcars1]\n\n"
        "  1. [bold]org-llm install-tools[/bold]         — Ollama + models + fonts + opencode + claude + pass\n"
        "  2. [bold]org-llm init[/bold]             — create DB + default config\n"
        "  3. [bold]org-llm index[/bold]            — parse all org files into DB\n"
        "  4. [bold]org-llm embed[/bold]            — generate embeddings (takes a while)\n"
        "  5. [bold]org-llm doctor[/bold]           — verify everything is green\n"
        "  6. [bold]org-llm ask 'What did I write about X?'[/bold]\n\n"
        "[lcars1]Explore further:[/lcars1]\n"
        "  [bold]org-llm launch[/bold]              — opencode workspace (SPC l o in Emacs)\n"
        "  [bold]org-llm claude[/bold]              — Claude Code workspace (SPC l C in Emacs)\n"
        "  [bold]org-llm cloud --assess[/bold]      — check which models need a cloud GPU\n"
        "  [bold]org-llm cloud --creds[/bold]       — list stored API keys (in pass)\n"
        "  [bold]org-llm report all[/bold]          — analytics on your vault\n"
        "  [bold]org-llm skill-new my_skill[/bold]  — create your first skill\n"
        "  [bold]org-llm tag[/bold]                 — auto-tag untagged nodes\n"
        "  [bold]org-llm source mcp_server[/bold]   — see all MCP tools\n"
        "  [bold]org-llm tutor --all[/bold]          — read the whole manual\n\n"
        "Engage. ☭ ✊ 🏳️‍🌈 — Queer, collective, free.",
    ),
]


@app.command(rich_help_panel="Maintenance")
def tutor(
    step: Annotated[str, typer.Argument(
        help="Step name to jump to (welcome/init/index/embed/search/ask/capture/"
             "tag/code/config/skills/report/doctor/install/dbt/opencode/source/cloud/launch/emacs/claude/done)"
    )] = "welcome",
    all_steps: Annotated[bool, typer.Option("--all", "-a",
               help="Print all steps at once")] = False,
):
    """Interactive tutorial: learn org-llm features step by step."""
    from rich.panel import Panel
    from rich.markdown import Markdown
    from .ui import trans_stripe, PRIDE_BANNER, solidarity

    step_names = [s for s, _ in _TUTOR_STEPS]

    if all_steps:
        console.print()
        console.print(trans_stripe(52))
        console.print(PRIDE_BANNER)
        for name, body in _TUTOR_STEPS:
            idx = step_names.index(name) + 1
            console.print(Panel(
                body,
                title=f"[lcars1]{idx}/{len(_TUTOR_STEPS)}  {name}[/lcars1]",
                border_style="lcars2",
                padding=(1, 2),
            ))
        console.print(trans_stripe(52))
        return

    # find requested step
    match = next(((n, b) for n, b in _TUTOR_STEPS if n == step), None)
    if not match:
        red_alert(f"Unknown step {step!r}. Valid: {', '.join(step_names)}")
        raise typer.Exit(1)

    name, body = match
    idx = step_names.index(name) + 1
    total = len(_TUTOR_STEPS)

    prev_step = step_names[idx - 2] if idx > 1 else None
    next_step = step_names[idx] if idx < total else None

    console.print()
    console.print(Panel(
        body,
        title=f"[lcars1]{idx}/{total}  {name}[/lcars1]",
        border_style="lcars2",
        padding=(1, 2),
    ))

    nav = []
    if prev_step:
        nav.append(f"← [dim]org-llm tutor {prev_step}[/dim]")
    if next_step:
        nav.append(f"→ [lcars2]org-llm tutor {next_step}[/lcars2]")
    if nav:
        console.print("  " + "    ".join(nav))
    console.print()

    # Personalized tail: read real vault state and recommend the most-useful
    # next tutor step. Only fires on `welcome` to avoid distraction elsewhere.
    if name == "welcome":
        try:
            from .db import Node, File
            from .discover import discover, detect_preferred_language
            engine = _engine()
            with get_session(engine) as session:
                n_files    = session.query(File).count()
                n_nodes    = session.query(Node).count()
                n_embedded = session.query(Node).filter(Node.embedding.isnot(None)).count()
            n_org_on_disk = 0
            try:
                with get_session(engine) as session:
                    org_dir = _org_dir(session)
                if org_dir.exists():
                    n_org_on_disk = sum(1 for _ in org_dir.rglob("*.org"))
            except Exception:
                pass
            lang = ""
            try:
                lang = detect_preferred_language()
            except Exception:
                pass

            # Pick the most-relevant next step for this user's actual state.
            if n_org_on_disk == 0 and n_nodes == 0:
                rec = ("init", f"start with [bold]org-llm init[/bold] — your vault is empty")
            elif n_files == 0 and n_org_on_disk > 0:
                rec = ("index", f"jump to [bold]org-llm tutor index[/bold] — you have {n_org_on_disk} .org files un-indexed")
            elif n_nodes > 0 and n_embedded < n_nodes:
                rec = ("embed", f"jump to [bold]org-llm tutor embed[/bold] — {n_nodes - n_embedded} nodes still need embeddings")
            elif n_nodes > 0 and n_embedded == n_nodes:
                rec = ("ask", "jump to [bold]org-llm tutor ask[/bold] — your vault is fully searchable")
            else:
                rec = ("init", "start with [bold]org-llm tutor init[/bold]")

            on_screen(f"[lcars3]Recommended for you[/lcars3]: {rec[1]}")
            stats_bits = [f"{n_files} files", f"{n_nodes} nodes",
                          f"{n_embedded}/{n_nodes} embedded" if n_nodes else None]
            stats = " · ".join(b for b in stats_bits if b)
            if lang:
                stats += f" · top language: {lang}"
            on_screen(f"[dim]Your vault: {stats}[/dim]")
        except Exception:
            pass
        console.print()


_MODULE_MAP = {
    "cli":        "org_llm.cli",
    "db":         "org_llm.db",
    "indexer":    "org_llm.indexer",
    "llm":        "org_llm.llm",
    "search":     "org_llm.search",
    "skills":     "org_llm.skills",
    "cli_skills": "org_llm.cli_skills",
    "report":     "org_llm.report",
    "ui":         "org_llm.ui",
    "mcp_server": "org_llm.mcp_server",
    "cloud":      "org_llm.cloud",
    "creds":      "org_llm.creds",
    "models":     "org_llm.models",
}


@app.command(rich_help_panel="Maintenance")
def source(
    module:  Annotated[str,  typer.Argument(
             help="Module to show: cli | db | indexer | llm | search | skills | report | ui"
    )],
    explain: Annotated[bool, typer.Option("--explain", "-e",
             help="Use LLM to explain the source")] = False,
    model:   Annotated[str,  typer.Option("--model", "-m",
             help="Override model for --explain")] = "",
):
    """Show org-llm's own source code with syntax highlighting, optionally explained by LLM."""
    import importlib
    import inspect
    from rich.syntax import Syntax
    from rich.panel import Panel

    if module not in _MODULE_MAP:
        red_alert(
            f"Unknown module {module!r}. "
            f"Available: {', '.join(_MODULE_MAP)}"
        )
        raise typer.Exit(1)

    mod = importlib.import_module(_MODULE_MAP[module])
    src = inspect.getsource(mod)
    path = inspect.getfile(mod)

    console.print()
    console.rule(f"[lcars2]{path}[/lcars2]")
    console.print(Syntax(src, "python", theme="monokai", line_numbers=True))
    console.rule(f"[dim]{len(src.splitlines())} lines[/dim]")

    if explain:
        engine = _engine()
        with get_session(engine) as session:
            url      = _ollama_url(session)
            chat_mdl = model or _cfg(session, "chat_model") or MODEL_DEFAULTS["chat_model"]

        from .llm import chat
        system = (
            "You are an expert Python developer and Emacs/org-mode enthusiast. "
            "Explain the following Python module from the org-llm codebase. "
            "Cover: what it does, its key functions/classes, design decisions, "
            "and how it fits into the overall system. Be clear and concrete."
        )
        prompt = f"Module: {module}\n\n```python\n{src[:6000]}\n```"
        with warp(f"Explaining {module} with {chat_mdl}"):
            explanation = chat(prompt, model=chat_mdl, base_url=url, system=system)

        console.print()
        console.rule(f"[lcars1]{chat_mdl} explains {module}[/lcars1]")
        console.print(explanation)
        console.rule()


@app.command(rich_help_panel="Querying")
def capture(
    title:  Annotated[str,  typer.Option("--title",  "-t", help="Note title")] = "",
    body:   Annotated[str,  typer.Option("--body",   "-b", help="Raw content / prompt")] = "",
    file:   Annotated[str,  typer.Option("--file",   "-f", help="Target org file (relative to org_dir)")] = "inbox.org",
    polish: Annotated[bool, typer.Option("--polish/--no-polish", "-p/-P",
            help="Let LLM structure the note (default: on; --no-polish writes raw body)")] = True,
):
    """Capture a new note into your org vault, optionally polished by an LLM."""
    import uuid
    from datetime import datetime

    engine = _engine()
    with get_session(engine) as session:
        org_dir   = _org_dir(session)
        url       = _ollama_url(session)
        model     = _cfg(session, "instruct_model") or "mistral-nemo"

    if not title:
        title = typer.prompt("Note title")
    if not body:
        body = typer.prompt("Content (or prompt for LLM)")

    content = body
    if polish:
        system = (
            "You are an org-mode expert. Structure the following content as a clean org-mode "
            "note body. Use headings (* **), bullet points (- ), and code blocks (#+begin_src) "
            "where appropriate. Do not include the title. Output only the org-mode markup."
        )
        from .llm import chat
        with thinking("Polishing", model=model):
            content = chat(body, model=model, base_url=url, system=system)

    node_id  = str(uuid.uuid4())
    ts       = datetime.now().strftime("%Y%m%d%H%M%S")
    org_file = _safe_org_path(org_dir, file)
    org_file.parent.mkdir(parents=True, exist_ok=True)

    entry = (
        f"\n* {title}\n"
        f":PROPERTIES:\n:ID: {node_id}\n:CREATED: [{ts}]\n:END:\n\n"
        f"{content.strip()}\n"
    )
    with open(org_file, "a") as fh:
        fh.write(entry)

    hail(f"Captured to {org_file}")
    on_screen(f"  Node ID: {node_id}")
    try:
        with get_session(_engine()) as session:
            on_screen(_suggest_note_ask(session, prefix="Try it: "))
    except Exception:
        pass
    make_it_so()


@app.command(rich_help_panel="Indexing")
def tag(
    force:   Annotated[bool, typer.Option("--force", "-f", help="Re-tag already-tagged nodes")] = False,
    limit:   Annotated[int,  typer.Option("--limit", "-n", help="Max nodes to tag")] = 50,
    apply:   Annotated[bool, typer.Option("--apply",   "-a",
             help="Write tags back to org files (default: dry-run preview only)")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-d",
             help="Explicit dry-run flag — same as omitting --apply (kept for clarity)")] = False,
):
    """Auto-tag untagged nodes using the fast_model.

    Default mode is a dry-run preview — pass --apply to actually write tags
    back to the index. --dry-run is accepted as a synonym for "no --apply".
    """
    if dry_run and apply:
        red_alert("--dry-run and --apply are mutually exclusive.")
        raise typer.Exit(1)
    from .db import Node
    from .llm import chat

    engine = _engine()
    with get_session(engine) as session:
        url   = _ollama_url(session)
        model = _cfg(session, "fast_model") or "phi4"

        q = session.query(Node)
        if not force:
            q = q.filter((Node.tags == "") | (Node.tags.is_(None)))
        nodes = q.limit(limit).all()

    if not nodes:
        on_screen("No untagged nodes found.")
        return

    hail(f"Auto-tagging {len(nodes)} nodes with [bold]{model}[/bold]")
    system = (
        "You are an org-mode expert. Given a note title and body, output ONLY a space-separated "
        "list of lowercase org-mode tags (no colons, no explanation). "
        "Max 5 tags. Example: python programming tools"
    )

    tagged = 0
    with get_session(engine) as session:
        with impulse("Tagging", total=len(nodes)) as (prog, task):
            for node in nodes:
                text = f"{node.title}\n{node.body[:500]}"
                try:
                    result = chat(text, model=model, base_url=url, system=system)
                    new_tags = " ".join(
                        t.strip().lower().replace(":", "")
                        for t in result.strip().split()
                        if t.strip()
                    )
                    db_node = session.get(Node, node.id)
                    if db_node:
                        db_node.tags = new_tags
                        session.commit()
                    tagged += 1
                except Exception:
                    session.rollback()
                prog.advance(task)

    hail(f"Tagged {tagged} nodes.")
    if apply:
        on_screen("[warn]--apply (write to org files) not yet implemented.[/warn]")
    if tagged > 0:
        # Show a Try-it weighted toward the freshly-popular tag.
        try:
            with get_session(engine) as session:
                on_screen(_suggest_note_ask(session, prefix="Try it: "))
        except Exception:
            pass
    make_it_so()


def _strip_code_fences(s: str, lang: str = "") -> str:
    """Remove leading ``` fences and trailing ``` from LLM-generated code.

    Small models routinely ignore "no fences" instructions. Strip them post-hoc
    so `--output` produces files that actually parse.
    """
    s = (s or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # Drop the first fence line (``` or ```python or ```elisp)
    lines = lines[1:]
    # Drop trailing closing fence if present
    if lines and lines[-1].rstrip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


@app.command(rich_help_panel="Querying")
def code(
    task:     Annotated[str,  typer.Argument(help="What to generate")],
    lang:     Annotated[str,  typer.Option("--lang", "-l",
              help="Language: python | sh | elisp | sql | rust")] = "python",
    model:    Annotated[str,  typer.Option("--model", "-m",
              help="Override model")] = "",
    context:  Annotated[bool, typer.Option("--context", "-c",
              help="Retrieve relevant org notes as context")] = True,
    output:   Annotated[str,  typer.Option("--output", "-o",
              help="Write generated code to file")] = "",
    cloud_:   Annotated[bool, typer.Option("--cloud", "-C",
              help="Route the chat through the configured cloud backend instead of local Ollama")] = False,
):
    """Generate code for an org/roam task using the code model.

    Pass --cloud to use the configured cloud provider (set up via
    `org-llm cloud --quick-start <slug>`) when the local code_model is too
    big for available RAM.
    """
    from .llm import chat as local_chat, embed
    from .search import vector_search
    from rich.syntax import Syntax

    engine = _engine()
    with get_session(engine) as session:
        url       = _ollama_url(session)
        embed_mdl = _cfg(session, "embed_model") or "nomic-embed-text"
        code_mdl  = model or _cfg(session, "code_model") or "qwen2.5-coder"
        cloud_provider = _cfg(session, "cloud_provider")
        cloud_endpoint = _cfg(session, "cloud_endpoint_url")
        cloud_model    = _cfg(session, "cloud_model")
        db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")

        ctx_text = ""
        if context:
            try:
                with warp("Retrieving org context"):
                    qvec    = embed(task, model=embed_mdl, base_url=url)
                    results = vector_search(session, qvec, limit=4)
                if results:
                    ctx_text = "\n\n".join(
                        f"# {r.title}\n{r.body[:600]}" for r in results
                    )
            except Exception:
                pass

    if cloud_:
        if not cloud_endpoint:
            on_screen("[yellow]--cloud requested but no cloud_endpoint_url configured. "
                      "Falling back to local Ollama.[/yellow]")
            on_screen("[dim]To enable cloud: org-llm cloud --quick-start openrouter[/dim]")
            cloud_ = False
        else:
            from . import creds as creds_mod
            api_key = (creds_mod.read_secret(creds_mod.cloud_slug(cloud_provider))
                       if cloud_provider else None) or db_api_key
            code_mdl = model or cloud_model or code_mdl
    if not cloud_:
        if not _ensure_model_pulled(code_mdl, url):
            red_alert(f"Could not pull local model {code_mdl!r}.")
            on_screen("Either run with --cloud, or: org-llm doctor --fix")
            raise typer.Exit(1)

    # Pull filesystem context: top language + recent code files. The model
    # benefits from knowing the user's actual code corpus when generating
    # snippets — e.g. recommend stdlib idioms that match their existing
    # codebase rather than pulling in random new dependencies.
    fs_context = ""
    try:
        from .discover import detect_preferred_language
        from .db        import File, Node
        pref_lang = detect_preferred_language()
        with get_session(engine) as session:
            recent_code = (
                session.query(File.path)
                .join(Node, Node.file_id == File.id)
                .filter(Node.tags.like("%code%"))
                .order_by(File.mtime.desc())
                .distinct()
                .limit(5).all()
            )
        if pref_lang or recent_code:
            bits = []
            if pref_lang:
                bits.append(f"My most-used language is {pref_lang}.")
            if recent_code:
                names = ", ".join(Path(r[0]).name for r in recent_code)
                bits.append(f"Recently-touched code files: {names}.")
            fs_context = "\n".join(bits)
    except Exception:
        pass

    system = (
        f"You are an expert {lang} programmer who specialises in org-mode and Emacs tooling. "
        f"Output ONLY the {lang} code with no explanation or markdown fences. "
        f"The code should be complete and directly runnable."
    )
    if fs_context:
        system += f"\n\nUser environment hints:\n{fs_context}"
    prompt = task
    if ctx_text:
        prompt = f"Context from my org notes:\n\n{ctx_text}\n\n---\n\nTask: {task}"

    if cloud_:
        with get_session(engine) as session:
            local_fallback = (_cfg(session, "code_model")
                              or _cfg(session, "chat_model")
                              or MODEL_DEFAULTS["chat_model"])
        with thinking("Generating code", model=code_mdl):
            try:
                generated = _cloud_chat_with_local_fallback(
                    prompt, cloud_model=code_mdl,
                    cloud_endpoint=cloud_endpoint, cloud_api_key=api_key,
                    local_model=local_fallback, local_url=url, system=system,
                )
            except Exception as e:
                red_alert(f"Cloud code-gen failed: {e}")
                raise typer.Exit(1)
        label = f"{cloud_provider}:{code_mdl}"
    else:
        with warp(f"{TREK_MSGS['code']} [{code_mdl}]"):
            generated = _local_chat_or_friendly_error(
                prompt, model=code_mdl, base_url=url, system=system,
                cloud_hint=f"org-llm code --cloud {task!r}",
            )
        label = code_mdl

    # Small models often emit fences despite the system prompt — strip them.
    generated = _strip_code_fences(generated, lang)

    console.print()
    console.rule(f"[lcars2]{lang}  ·  {label}[/lcars2]")
    console.print(Syntax(generated, lang, theme="monokai", line_numbers=True))
    console.rule()

    if output:
        Path(output).write_text(generated)
        hail(f"Written to {output}")

    make_it_so()


# ── emacs config review ──────────────────────────────────────────────────────

_EMACS_CONFIG_FILES = {
    "doom": [
        "config.el", "init.el", "packages.el", "custom.el",
    ],
    "vanilla": [
        "init.el", "early-init.el",
    ],
}


def _detect_emacs_config_dir(override: str = "") -> tuple[Path, str] | None:
    """Return (path, flavor) for the user's Emacs config, or None.

    Tries: explicit override → ~/.config/doom → ~/.doom.d → ~/.emacs.d.
    Flavor is "doom" or "vanilla" — used to pick which files to review.
    """
    candidates: list[tuple[Path, str]] = []
    if override:
        candidates.append((Path(override).expanduser(), "doom"))
    candidates += [
        (Path("~/.config/doom").expanduser(), "doom"),
        (Path("~/.doom.d").expanduser(),       "doom"),
        (Path("~/.config/emacs").expanduser(), "vanilla"),
        (Path("~/.emacs.d").expanduser(),      "vanilla"),
    ]
    for path, flavor in candidates:
        if path.exists() and path.is_dir():
            return path, flavor
    return None


def _gather_emacs_config(config_dir: Path, flavor: str,
                         max_bytes_per_file: int = 8000) -> list[tuple[str, str]]:
    """Read the canonical config files for the given flavor.

    Returns [(relative_path, contents), …]. Each file is truncated to
    max_bytes_per_file so the LLM prompt stays bounded.
    """
    out: list[tuple[str, str]] = []
    names = _EMACS_CONFIG_FILES.get(flavor, _EMACS_CONFIG_FILES["vanilla"])
    for name in names:
        p = config_dir / name
        if not p.exists():
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        if len(text) > max_bytes_per_file:
            text = text[:max_bytes_per_file] + f"\n;; … truncated, file is {len(text)} chars total"
        out.append((str(p.relative_to(config_dir)), text))
    # Also pull lisp/*.el for vanilla (custom modules)
    if flavor == "vanilla":
        lisp_dir = config_dir / "lisp"
        if lisp_dir.is_dir():
            for p in sorted(lisp_dir.glob("*.el")):
                try:
                    text = p.read_text(errors="replace")
                except Exception:
                    continue
                if len(text) > max_bytes_per_file:
                    text = text[:max_bytes_per_file] + "\n;; … truncated"
                out.append((str(p.relative_to(config_dir)), text))
    return out


@app.command(name="review-emacs", rich_help_panel="Querying")
def review_emacs(
    config_dir: Annotated[str,  typer.Option("--config-dir", "-d",
                help="Override config directory (default: auto-detect Doom or vanilla)")] = "",
    focus:      Annotated[str,  typer.Option("--focus", "-f",
                help="Specific area: theming | performance | packages | keybindings | cleanup | all")] = "all",
    model:      Annotated[str,  typer.Option("--model", "-m",
                help="Override review model (default: reason_model)")] = "",
    output:     Annotated[str,  typer.Option("--output", "-o",
                help="Write the review to this file (markdown)")] = "",
    diff_only:  Annotated[bool, typer.Option("--diff-only", "-D",
                help="Suggest concrete edits as patches, not prose advice")] = False,
    cloud_:     Annotated[bool, typer.Option("--cloud",     "-C",
                help="Route the review through the configured cloud backend (recommended for low-RAM hosts)")] = False,
):
    """Have an LLM review your Doom/vanilla Emacs config and suggest improvements.

    Reads init.el / config.el / packages.el (Doom) or init.el + lisp/*.el (vanilla),
    feeds them to the configured reasoning model, and prints structured advice on
    cleanup, theming, performance, keybindings, and package hygiene.

    Examples:
      org-llm review-emacs                       # full review of detected config
      org-llm review-emacs --focus performance   # narrow scope
      org-llm review-emacs --diff-only -o /tmp/patch.md
    """
    from rich.panel import Panel

    if config_dir:
        p = Path(config_dir).expanduser()
        if not p.exists():
            red_alert(f"--config-dir {p} does not exist.")
            raise typer.Exit(1)
        if not p.is_dir():
            red_alert(f"--config-dir {p} is not a directory (got a file).")
            raise typer.Exit(1)
    detected = _detect_emacs_config_dir(config_dir)
    if not detected:
        red_alert("Could not find an Emacs config directory.")
        on_screen("Tried: ~/.config/doom, ~/.doom.d, ~/.config/emacs, ~/.emacs.d")
        on_screen("Pass --config-dir <path> to override.")
        raise typer.Exit(1)
    cfg_dir, flavor = detected

    files = _gather_emacs_config(cfg_dir, flavor)
    if not files:
        red_alert(f"No reviewable .el files in {cfg_dir}")
        raise typer.Exit(1)

    hail(f"Reviewing {flavor} config at [bold]{cfg_dir}[/bold]")
    on_screen(f"  files: {', '.join(name for name, _ in files)}")

    engine = _engine()
    with get_session(engine) as session:
        url       = _ollama_url(session)
        chat_mdl  = (model
                     or _cfg(session, "reason_model")
                     or _cfg(session, "chat_model")
                     or MODEL_DEFAULTS["chat_model"])

    bundle = "\n\n".join(
        f";; ── {name} ─────────────────────────────────────────\n{content}"
        for name, content in files
    )

    focus_guidance = {
        "theming":     "concentrate on doom-themes, font config, modeline, palette, and visual coherence",
        "performance": "concentrate on startup time, lazy loading (use-package :defer, :hook), gc-cons-threshold, and native-compilation hints",
        "packages":    "concentrate on package selection — duplicates, unmaintained packages, missing modern alternatives, and Doom modules vs. raw use-package usage",
        "keybindings": "concentrate on keybindings — conflicts, leader-key conventions, missing :map specifiers, ergonomics",
        "cleanup":     "concentrate on cruft, dead code, commented-out blocks, and config that no longer matches the installed packages",
        "all":         "cover theming, performance, packages, keybindings, and cleanup",
    }.get(focus, "cover theming, performance, packages, keybindings, and cleanup")

    if diff_only:
        system = (
            "You are an Emacs Lisp expert who has read every Doom Emacs module. "
            f"Review the user's {flavor} Emacs config and produce ONLY a list of concrete edits as "
            "unified-diff hunks the user can apply with `patch`. Each hunk MUST start with "
            "`--- a/<filename>` and `+++ b/<filename>` lines. No prose between hunks. "
            f"Focus: {focus_guidance}."
        )
    else:
        system = (
            "You are an Emacs Lisp expert who has read every Doom Emacs module and tracks "
            "the modern (post-29) Emacs ecosystem. Review the user's "
            f"{flavor} Emacs config carefully and produce a structured report.\n\n"
            "Report sections (use `## ` markdown headings):\n"
            "  1. Summary — one paragraph on the overall shape of the config\n"
            "  2. Strengths — what's well-done (be specific, cite filename:line)\n"
            "  3. Issues — bugs, deprecated APIs, conflicts, redundancies\n"
            "  4. Improvements — concrete suggestions ordered by impact\n"
            "  5. Optional polish — theming, ergonomics, nice-to-haves\n\n"
            "Always cite specific lines or symbols. Prefer concrete code snippets over prose. "
            f"Focus: {focus_guidance}."
        )

    prompt = (
        f"My Emacs config flavor: {flavor}\n"
        f"Config directory: {cfg_dir}\n\n"
        f"Files (truncated where noted):\n\n{bundle}"
    )

    # Cloud override: skip local pull, use OpenRouter / Groq / etc.
    if cloud_:
        with get_session(engine) as session:
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            cloud_model    = _cfg(session, "cloud_model")
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
        if not cloud_endpoint:
            on_screen("[yellow]--cloud requested but no cloud_endpoint_url configured. "
                      "Falling back to local Ollama.[/yellow]")
            on_screen("[dim]To enable cloud: org-llm cloud --quick-start openrouter[/dim]")
            cloud_ = False
        else:
            from . import creds as creds_mod
            api_key = (creds_mod.read_secret(creds_mod.cloud_slug(cloud_provider))
                       if cloud_provider else None) or db_api_key
            chat_mdl = model or cloud_model or chat_mdl
        from .cloud import cloud_chat
        try:
            with thinking("Reviewing", model=chat_mdl):
                review = cloud_chat(prompt, model=chat_mdl,
                                    endpoint_url=cloud_endpoint,
                                    api_key=api_key, system=system)
        except Exception as exc:
            red_alert(f"Cloud review failed: {exc}")
            raise typer.Exit(1)
        label = f"{cloud_provider}:{chat_mdl}"
    else:
        # Local path: ensure the model is pulled before asking.
        if not _ensure_model_pulled(chat_mdl, url):
            red_alert(f"Could not pull review model {chat_mdl!r}. Run: org-llm doctor --fix")
            on_screen("Or use the cloud backend:  [bold]org-llm review-emacs --cloud[/bold]")
            raise typer.Exit(1)
        from .llm import chat
        try:
            with warp(f"Reviewing {flavor} config with {chat_mdl}"):
                review = chat(prompt, model=chat_mdl, base_url=url, system=system)
        except Exception as exc:
            msg = str(exc).lower()
            if "memory" in msg or "out of memory" in msg or "oom" in msg:
                red_alert(f"{chat_mdl} requires more RAM than is available.")
                on_screen("Try the cloud backend:        org-llm review-emacs --cloud")
                on_screen("Or pick a smaller model:      org-llm config reason_model deepseek-r1:7b")
            elif "connection" in msg or "refused" in msg:
                red_alert("Ollama isn't reachable. Run: org-llm doctor --fix")
            else:
                red_alert(f"Review failed: {exc}")
            raise typer.Exit(1)
        label = chat_mdl

    console.print()
    console.rule(f"[lcars1]Emacs config review  ·  {label}  ·  focus: {focus}[/lcars1]")
    console.print(Panel(review, border_style="lcars2", padding=(1, 2)))
    console.rule()

    if output:
        Path(output).write_text(review)
        hail(f"Written to {output}")

    # Log into history for searchability
    from .db import History
    from datetime import datetime
    with get_session(engine) as session:
        session.add(History(
            timestamp=datetime.now().isoformat(),
            command="review-emacs",
            query=f"{flavor}:{focus}:{cfg_dir}",
            response=review,
        ))
        session.commit()

    # Closing recommendation: ask the same model for the single most-impactful
    # change drawn from its own review. Best-effort — always succeed even if
    # the model refuses or the endpoint is unreliable.
    try:
        close_sys = (
            "Given a review of an Emacs configuration, return ONE sentence: "
            "the single most impactful change the user should make. Be "
            "concrete (cite a symbol, file, or package). 8-22 words. No "
            "preamble, no list, no quotes."
        )
        close_prompt = (f"Review:\n\n{review[:6000]}\n\n"
                        "What's the single most impactful change?")
        if cloud_:
            from .cloud import cloud_chat as _cc
            most_impactful = _cc(close_prompt, model=chat_mdl,
                                  endpoint_url=cloud_endpoint,
                                  api_key=api_key, system=close_sys)
        else:
            from .llm import chat as _lc
            most_impactful = _lc(close_prompt, model=chat_mdl,
                                  base_url=url, system=close_sys)
        line = next((l.strip(" -–—•\"'").strip()
                      for l in (most_impactful or "").splitlines()
                      if 8 <= len(l.strip()) <= 250), "")
        if line:
            console.print()
            on_screen(f"[lcars3]Most impactful change[/lcars3]: {line}")
    except Exception:
        pass

    make_it_so()


# ── opencode workspace helpers ─────────────────────────────────────────────
# These keep `launch()` legible by extracting prompt/theme/slash-command
# generation. opencode reads its config from .opencode.json in the project
# dir, custom themes from .opencode/themes/<name>.json, and custom slash
# commands from .opencode/command/<name>.md.

OPENCODE_WORKSPACES = ("all", "researcher", "scribe", "engineer")


def _render_context_for_opencode() -> str:
    """Pull tangled USER CONTEXT + HISTORICAL CONTEXT into the opencode prompt."""
    parts: list[str] = []
    try:
        from . import context as _ctx
        body = _ctx.read_context_for_prompt(max_chars=2000)
        if body:
            parts.append(f"\n{_ctx.CONTEXT_HEADER}\n{body}\n"
                         "(prefer this over older note content; surface contradictions.)\n")
        hist = _ctx.read_history_for_prompt(max_chars=2000)
        if hist:
            parts.append(f"\n{_ctx.HISTORY_HEADER}\n{hist}\n"
                         "(temporal background; anchor present-tense answers in USER CONTEXT.)\n")
    except Exception:
        pass
    return "".join(parts)


def _opencode_workspace_prompt(workspace: str, n_files: int, n_nodes: int,
                                n_embedded: int, pct_e: int, org_dir: str,
                                skill_str: str, recent_str: str,
                                top_tags_str: str, model_status: str,
                                discover_str: str, knobs_str: str,
                                hardware_str: str,
                                todays_prompt: str = "",
                                projects_str: str = "") -> str:
    """Return the system prompt for a workspace flavor.

    Workspaces:
      all        — full vault + repos + skills + filesystem (default)
      researcher — read-heavy: search/ask/get_node, no captures
      scribe     — capture-heavy: capture_note/tangle/run_skill emphasis
      engineer   — code-corpus + repos focus, code_search emphasis
    """
    common_header = f"""You are the user's interactive org-llm workspace, running inside opencode with full MCP access to their second brain.

VAULT
  Location: {org_dir}
  Files: {n_files}  |  Nodes: {n_nodes}  |  Embedded: {n_embedded}/{n_nodes} ({pct_e}%)
  Skills: {skill_str}

RECENT ACTIVITY (last 7 days)
{recent_str}

TOP TAGS
{top_tags_str}

MODELS
{model_status}

HARDWARE
{hardware_str}

FILESYSTEM
{discover_str}
{('USER PROJECTS' + chr(10) + projects_str + chr(10)) if projects_str else ''}{knobs_str}{('TODAY OPENING PROMPT' + chr(10) + '  ' + todays_prompt + chr(10) + '  (offer this if the user opens with no question)' + chr(10)) if todays_prompt else ''}{_render_context_for_opencode()}"""

    if workspace == "researcher":
        focus = """
ROLE: RESEARCHER
You help the user think through their notes. Read-heavy mode.
  - PREFER: search_notes, ask_notes, get_node, list_nodes_by_tag, list_recent_nodes
  - AVOID:  capture_note unless the user is explicit
  - When citing notes, use their exact titles
  - Surface connections across notes the user may not have noticed"""
    elif workspace == "scribe":
        focus = """
ROLE: SCRIBE
You help the user capture, tag, and refine notes. Write-heavy mode.
  - PREFER: capture_note, run_skill, tangle_file
  - When capturing, propose tags from TOP TAGS above for consistency
  - Confirm file path and resulting node ID after each capture
  - Use list_skills first to see what org-babel workflows are registered"""
    elif workspace == "engineer":
        focus = """
ROLE: ENGINEER
You help with code that lives across the user's repos. Code-corpus mode.
  - PREFER: code_search (lang-filtered), search_notes, read_file, list_directory
  - When the user mentions a repo by name, check FILESYSTEM above first
  - Cite file paths in answers; use read_file before claiming what code does
  - For tasks spanning notes + code, run code_search AND search_notes in parallel"""
    else:  # all
        focus = """
ROLE: GENERALIST
Full access to notes, code, skills, and filesystem discovery.
  - Always search_notes or ask_notes BEFORE answering questions about notes
  - Use code_search for code questions; capture_note when saving ideas
  - Use discover_filesystem when the user asks "what do I have"
  - Use doctor_health if anything seems off; performance_status for tuning
  - Cite note titles AND file paths when you draw from them"""

    behaviour = """

BEHAVIOUR
  - Tools-first: don't speculate; the data is one MCP call away.
  - Theme-aware: the user runs LCARS-themed tooling. Match the energy.
    If the user has knobs configured (above), nod to them when natural.
  - Be concise. The user reads diffs and tool output, not paragraphs.
  - PROACTIVE AUTO-FIX: when in doubt, call `org_llm_run("<command>")`
    with a free-form command string. The CLI runs three layers of
    recovery (shell-quote repair, LLM intent reconstruction, SRE fix)
    before failing — so even a mangled intent ("ask why my notes look
    weird") tends to land on a real result. Don't ask the user to
    re-quote things manually; throw it at org_llm_run and let the
    auto-fix chain handle it."""

    return common_header + focus + behaviour


def _opencode_pre_flight_context(session) -> dict:
    """Gather rich runtime context for the system prompt."""
    from datetime import datetime, timedelta
    from .db    import Node, File, Config
    from .skills import Skill

    org_dir    = _org_dir(session)
    ollama_url = _ollama_url(session)
    n_files    = session.query(File).count()
    n_nodes    = session.query(Node).count()
    n_embedded = session.query(Node).filter(Node.embedding.isnot(None)).count()
    pct_e      = int(n_embedded / n_nodes * 100) if n_nodes else 0
    skills     = [s.name for s in session.query(Skill).all()]
    cfg_rows   = {r.key: r.value for r in session.query(Config).all()}

    since = (datetime.now() - timedelta(days=7)).timestamp()
    recent = (
        session.query(Node)
        .filter(Node.mtime >= since)
        .order_by(Node.mtime.desc())
        .limit(8).all()
    )
    recent_str = "\n".join(
        f"  - {n.title} ({datetime.fromtimestamp(n.mtime).date().isoformat() if n.mtime else '?'})"
        for n in recent
    ) or "  (no recent activity)"

    # Top tags. Tags are stored space-separated; code-index nodes carry
    # `code` plus `code:<lang>` so filter both prefixes.
    from collections import Counter
    tag_counts: Counter = Counter()
    for (tags,) in session.query(Node.tags).filter(Node.tags.isnot(None)).all():
        for t in (tags or "").split():
            t = t.strip().lower()
            if t and t != "code" and not t.startswith("code:"):
                tag_counts[t] += 1
    top_tags = tag_counts.most_common(10)
    top_tags_str = "\n".join(f"  - {t}  ({c})" for t, c in top_tags) \
                   or "  (none — try: org-llm tag --apply)"

    # Models
    model_lines = []
    for k in ("chat_model", "embed_model", "code_model", "tag_model",
              "review_model", "fixer_model"):
        v = cfg_rows.get(k)
        if v:
            model_lines.append(f"  {k}: {v}")
    model_status = "\n".join(model_lines) or "  (no model assignments)"

    # Hardware
    try:
        from .cloud import local_ram_gb, local_vram_gb
        hardware_str = f"  Free RAM: {local_ram_gb():.1f} GB"
        vram = local_vram_gb()
        if vram:
            hardware_str += f"  |  VRAM: {vram:.1f} GB"
    except Exception:
        hardware_str = "  (hardware probe unavailable)"

    # Filesystem discovery
    try:
        from .discover import discover, suggest_code_dirs, detect_preferred_language
        found = discover()
        lang = detect_preferred_language()
        code_roots = suggest_code_dirs(found)
        d_lines = []
        for f in found[:8]:
            d_lines.append(f"  - {f.path}  [{f.kind}]  {f.description}")
        if code_roots:
            d_lines.append(f"  Suggested code roots: "
                           f"{', '.join(str(c) for c in code_roots[:3])}")
        if lang:
            d_lines.append(f"  Preferred language: {lang}")
        discover_str = "\n".join(d_lines) or "  (nothing standard found)"
    except Exception:
        discover_str = "  (discover unavailable)"

    # Knobs
    try:
        knobs = _read_user_knobs()
    except Exception:
        knobs = []
    knob_lines = []
    for k in knobs:
        name  = k.get("name", "?")
        env   = f"ORG_LLM_{name.upper()}_LEVEL"
        level = os.environ.get(env, str(k.get("default_level", 2)))
        knob_lines.append(f"  - {name}  (level: {level})")
    knobs_str = ""
    if knob_lines:
        knobs_str = "\n\nUSER THEME KNOBS\n" + "\n".join(knob_lines)
    # Built-in dials too
    try:
        from .ui import trek_level, commie_level, queer_level
        levels = (("trek",   trek_level()),
                  ("commie", commie_level()),
                  ("queer",  queer_level()))
        active = [f"  - {n}  (level: {l})" for n, l in levels if l > 0]
        if active:
            if not knobs_str:
                knobs_str = "\n\nACTIVE THEME DIALS"
            else:
                knobs_str += "\n\nACTIVE THEME DIALS"
            knobs_str += "\n" + "\n".join(active)
    except Exception:
        pass

    # User projects: skim READMEs of repos under ~/repos/ etc. The LLM gets
    # one-line summaries so it can name projects when the user mentions them.
    projects_str = ""
    try:
        from .discover import discover, suggest_code_dirs
        proj_lines: list[str] = []
        seen_proj: set[str] = set()
        for found in discover():
            if found.kind != "repos-root":
                continue
            try:
                children = sorted(p for p in found.path.iterdir()
                                  if p.is_dir() and not p.name.startswith("."))
            except OSError:
                continue
            for child in children[:8]:
                if child.name in seen_proj:
                    continue
                seen_proj.add(child.name)
                # Look for a README under common names
                readme = next((child / n for n in
                                ("README.md", "README.org", "Readme.md", "README")
                                if (child / n).exists()), None)
                summary = ""
                if readme:
                    try:
                        text = readme.read_text(errors="replace")
                        # First non-blank, non-heading line up to 120 chars
                        for line in text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            if stripped.startswith(("#", "*", "=", "-")):
                                # First H1 from md/org also counts as a fallback
                                stripped = stripped.lstrip("# *=-").strip()
                                if stripped:
                                    summary = stripped[:120]
                                    break
                                continue
                            summary = stripped[:120]
                            break
                    except Exception:
                        pass
                proj_lines.append(f"  - {child.name}"
                                    + (f" — {summary}" if summary else ""))
            if len(proj_lines) >= 12:
                break
        if proj_lines:
            projects_str = "\n".join(proj_lines)
    except Exception:
        pass

    # Today's prompt: a single concrete starter question seeded from the
    # last 7 days of activity. Cheap LLM call (best-effort) so opencode
    # always opens with something actionable instead of a blank cursor.
    todays_prompt = ""
    try:
        if recent and ollama_url:
            from .llm import chat as _chat
            recent_titles = [n.title for n in recent[:5] if n.title]
            top_words = ", ".join(t for t, _ in tag_counts.most_common(5)) \
                        if tag_counts else "(no tags yet)"
            chat_mdl = (cfg_rows.get("fast_model")
                        or cfg_rows.get("chat_model")
                        or "llama3.2")
            sys_msg = (
                "You suggest one short, concrete starter question that helps "
                "a user re-engage with their org-roam knowledge base. Output "
                "ONE question only — no preamble, no quotes, no numbering. "
                "8-18 words."
            )
            user_msg = (
                f"Recent note titles: {recent_titles}\n"
                f"Top tags: {top_words}\n\n"
                "Suggest one question they might want to start their session with."
            )
            try:
                resp = _chat(user_msg, model=chat_mdl,
                             base_url=ollama_url, system=sys_msg)
                # Take first non-empty line, trim quoting/numbering cruft.
                for line in (resp or "").splitlines():
                    line = line.strip(" -–—•\"'").strip()
                    if 8 <= len(line) <= 200:
                        todays_prompt = line
                        break
            except Exception:
                pass
    except Exception:
        pass

    return {
        "org_dir": str(org_dir), "ollama_url": ollama_url,
        "n_files": n_files, "n_nodes": n_nodes, "n_embedded": n_embedded,
        "pct_e": pct_e, "skills": skills,
        "skill_str": ", ".join(skills) if skills else "none — run org-llm skill-index",
        "recent_str": recent_str, "top_tags_str": top_tags_str,
        "model_status": model_status, "hardware_str": hardware_str,
        "discover_str": discover_str, "knobs_str": knobs_str,
        "todays_prompt": todays_prompt,
        "projects_str": projects_str,
    }


def _opencode_lcars_theme() -> dict:
    """LCARS-themed opencode theme using the same palette as the CLI.

    Best-effort: opencode's theme schema is JSON. We provide both light
    and dark variants so opencode can pick based on its own theme mode.
    """
    from .ui import DARK_PALETTE, LIGHT_PALETTE

    def _t(p: dict) -> dict:
        return {
            "primary":   p["lcars1"],   # signature LCARS orange
            "secondary": p["lcars2"],   # purple
            "accent":    p["lcars3"],   # blue
            "info":      p["lcars3"],
            "warning":   p["lcars1"],
            "error":     p.get("pride.red", "#FF4444"),
            "success":   p.get("pride.green", "#44CC44"),
        }

    return {
        "$schema": "https://opencode.ai/theme.json",
        "name":    "org-llm-lcars",
        "description": "LCARS-themed colour scheme matching org-llm CLI",
        "defs": {
            "lcars1_dark":   DARK_PALETTE["lcars1"],
            "lcars2_dark":   DARK_PALETTE["lcars2"],
            "lcars3_dark":   DARK_PALETTE["lcars3"],
            "lcars1_light":  LIGHT_PALETTE["lcars1"],
            "lcars2_light":  LIGHT_PALETTE["lcars2"],
            "lcars3_light":  LIGHT_PALETTE["lcars3"],
        },
        "theme": {
            "primary":   {"dark": DARK_PALETTE["lcars1"], "light": LIGHT_PALETTE["lcars1"]},
            "secondary": {"dark": DARK_PALETTE["lcars2"], "light": LIGHT_PALETTE["lcars2"]},
            "accent":    {"dark": DARK_PALETTE["lcars3"], "light": LIGHT_PALETTE["lcars3"]},
            "info":      {"dark": DARK_PALETTE["lcars3"], "light": LIGHT_PALETTE["lcars3"]},
            "warning":   {"dark": DARK_PALETTE["lcars1"], "light": LIGHT_PALETTE["lcars1"]},
            "error":     {"dark": DARK_PALETTE.get("pride.red",   "#FF4444"),
                           "light": LIGHT_PALETTE.get("pride.red", "#CC2222")},
            "success":   {"dark": DARK_PALETTE.get("pride.green", "#44CC44"),
                           "light": LIGHT_PALETTE.get("pride.green", "#228822")},
        },
        # Flat fallback in case the consuming opencode build expects a
        # mode-less map under the same key.
        "colors": _t(DARK_PALETTE),
    }


def _opencode_slash_commands(workspace: str) -> dict:
    """Return {name: markdown-body} for slash commands written to
    .opencode/command/<name>.md. opencode treats these as stored prompts
    the user can invoke with /<name>."""

    cmds = {
        "discover": (
            "---\n"
            "description: Probe the filesystem and report what org-llm can use\n"
            "---\n"
            "Call the `discover_filesystem` MCP tool and present the inventory\n"
            "to me. Highlight: vault, repo roots with code, and any dotfiles\n"
            "or Emacs configs that could be MCP grant roots. End with one\n"
            "concrete next-step suggestion (code-index, grant-root, or none).\n"
        ),
        "recent": (
            "---\n"
            "description: Show what I've been working on lately\n"
            "---\n"
            "Call `list_recent_nodes` (last 14 days) and `recent_files`\n"
            "(last 7 days). Group by date; for each date list the notes\n"
            "and files modified. End with a one-line summary of themes\n"
            "you notice in the activity.\n"
        ),
        "health": (
            "---\n"
            "description: Run a concise org-llm health check\n"
            "---\n"
            "Call `doctor_health` and `performance_status`. Surface anything\n"
            "concerning (downgrade markers, OOM risk, unreachable Ollama,\n"
            "missing models). If everything is healthy, say so in one line.\n"
        ),
        "stats": (
            "---\n"
            "description: Vault + corpus statistics\n"
            "---\n"
            "Call `get_vault_stats` and `list_models`. Format as two short\n"
            "tables. End with: total nodes, % embedded, top-3 pulled models.\n"
        ),
        "tags": (
            "---\n"
            "description: Show the tag landscape of my vault\n"
            "---\n"
            "Use `search_notes` and `list_nodes_by_tag` to survey my top\n"
            "tags. Show the top 15 tags with counts. Note any clusters\n"
            "or themes. Suggest one tag that could be split or merged.\n"
        ),
        "tutor": (
            "---\n"
            "description: Walk me through an org-llm tutor step\n"
            "---\n"
            "Call `list_tutor_steps`, then ask which step I want.\n"
            "When I name one, call `get_tutor_step` and explain it\n"
            "conversationally — not as a copy-paste of the original.\n"
            "Cite specific commands I should try.\n"
        ),
        "code": (
            "---\n"
            "description: Search my code corpus for a topic\n"
            "---\n"
            "Ask me what I'm looking for, then call `code_search` with\n"
            "the query. If I mention a language, pass it as the `lang`\n"
            "argument. Show top 5 hits with file path and one-line summary\n"
            "drawn from `read_file` of the most relevant match.\n"
        ),
    }

    if workspace == "researcher":
        cmds["explore"] = (
            "---\n"
            "description: Free-form vault exploration\n"
            "---\n"
            "Pick a tag from my top-10, call `list_nodes_by_tag` for it,\n"
            "skim 3 notes via `get_node`, and surface a connection or\n"
            "open question I hadn't named explicitly.\n"
        )
    elif workspace == "scribe":
        cmds["capture"] = (
            "---\n"
            "description: Capture an idea with consistent tagging\n"
            "---\n"
            "Ask me what I want to capture. Propose a title and 2-3 tags\n"
            "drawn from my existing TOP TAGS. After I confirm, call\n"
            "`capture_note` and report the new node ID.\n"
        )
    elif workspace == "engineer":
        cmds["repo"] = (
            "---\n"
            "description: Brief me on one of my repos\n"
            "---\n"
            "Ask which repo (offer suggestions from `discover_filesystem`).\n"
            "Then `code_search` for entry points (cli, main, init), summarise\n"
            "the architecture in 5-7 bullets, and name one thing that looks\n"
            "interesting or out of place.\n"
        )

    return cmds


@app.command(rich_help_panel="Workspaces")
def launch(
    workspace:  Annotated[str,  typer.Option("--workspace",  "-w",
                help="Workspace flavor: all | researcher | scribe | engineer")] = "all",
    model:      Annotated[str,  typer.Option("--model",      "-m",
                help="Override chat model (default: chat_model from config)")] = "",
    no_context: Annotated[bool, typer.Option("--no-context", "-N",
                help="Skip vault context injection into system prompt")] = False,
    no_theme:   Annotated[bool, typer.Option("--no-theme",   "-T",
                help="Skip writing LCARS theme to .opencode/themes/")] = False,
    no_commands:Annotated[bool, typer.Option("--no-commands","-C",
                help="Skip writing slash-commands to .opencode/command/")] = False,
    dry_run:    Annotated[bool, typer.Option("--dry-run",    "-n",
                help="Print opencode config only, do not launch")] = False,
):
    """Launch opencode as an interactive org-roam workspace.

    Sets up a project-local .opencode/ directory with:
      - .opencode.json — model, MCP, instructions, theme reference
      - themes/lcars.json — LCARS palette matching the CLI
      - command/<name>.md — slash-commands (/discover, /recent, /health,
        /stats, /tags, /tutor, /code, plus workspace-specific extras)

    Workspaces shape the system prompt and slash-command set:
      all         — full toolbox (default)
      researcher  — read-heavy: search/ask/get_node, no captures
      scribe      — capture-heavy: capture_note + skills emphasis
      engineer    — code-corpus + repos focus, code_search emphasis
    """
    import json
    import os
    import shutil
    import subprocess
    from rich.panel  import Panel
    from rich.table  import Table
    from rich.syntax import Syntax
    from .ui         import trans_stripe, PRIDE_BANNER, solidarity
    from .db         import Node, File
    from .skills     import Skill

    if workspace not in OPENCODE_WORKSPACES:
        red_alert(f"Unknown workspace {workspace!r}. "
                  f"Choose one of: {', '.join(OPENCODE_WORKSPACES)}")
        raise typer.Exit(1)

    # ── Locate or install opencode ────────────────────────────────────────────
    oc_path = _opencode_bin()
    if not dry_run and oc_path is None:
        hail("opencode not found — installing via curl…")
        oc_path = _install_opencode_bin(Path("~/.local/bin").expanduser())
        if not oc_path:
            raise typer.Exit(1)
    oc_bin = str(oc_path) if oc_path else "opencode"

    # ── Gather rich vault + filesystem + theme context ─────────────────────
    engine = _engine()
    with get_session(engine) as session:
        ctx = _opencode_pre_flight_context(session)
        chat_mdl = model or _cfg(session, "chat_model") or MODEL_DEFAULTS["chat_model"]

    org_dir     = Path(ctx["org_dir"]).expanduser()
    ollama_url  = ctx["ollama_url"]
    org_llm_dir = Path(__file__).parent.parent.resolve()

    # ── Build system prompt ───────────────────────────────────────────────────
    if no_context:
        instructions = (
            "You are an intelligent assistant connected to an org-roam "
            "knowledge base via org-llm MCP tools. Use them to help with "
            "note management, Q&A, code search, and org-babel workflows."
        )
    else:
        instructions = _opencode_workspace_prompt(
            workspace,
            n_files=ctx["n_files"], n_nodes=ctx["n_nodes"],
            n_embedded=ctx["n_embedded"], pct_e=ctx["pct_e"],
            org_dir=ctx["org_dir"], skill_str=ctx["skill_str"],
            recent_str=ctx["recent_str"], top_tags_str=ctx["top_tags_str"],
            model_status=ctx["model_status"],
            discover_str=ctx["discover_str"],
            knobs_str=ctx["knobs_str"],
            hardware_str=ctx["hardware_str"],
            todays_prompt=ctx.get("todays_prompt", ""),
            projects_str=ctx.get("projects_str", ""),
        )

    # ── Build .opencode.json ──────────────────────────────────────────────────
    oc_config: dict = {
        "model": f"ollama/{chat_mdl}",
        "provider": {
            "ollama": {
                "name": "Ollama",
                "options": {"baseURL": f"{ollama_url.rstrip('/')}/v1"},
            }
        },
        "instructions": instructions,
        "mcp": {
            "org-llm": {
                "type": "local",
                "command": [
                    "uv", "--directory", str(org_llm_dir),
                    "run", "org-llm", "mcp",
                ],
                "env": {"ORG_LLM_DB": str(DB_PATH)},
            }
        },
    }
    if not no_theme:
        oc_config["theme"] = "org-llm-lcars"

    config_path  = org_dir / ".opencode.json"
    theme_path   = org_dir / ".opencode" / "themes"  / "org-llm-lcars.json"
    command_dir  = org_dir / ".opencode" / "command"

    slash_cmds = _opencode_slash_commands(workspace) if not no_commands else {}

    if dry_run:
        console.print()
        console.rule("[lcars1]opencode config (dry-run)[/lcars1]")
        console.print(Syntax(json.dumps(oc_config, indent=2), "json", theme="monokai"))
        console.rule(f"[lcars2]Workspace: {workspace}[/lcars2]")
        on_screen(f"Would write config:   {config_path}")
        if not no_theme:
            on_screen(f"Would write theme:    {theme_path}")
        if slash_cmds:
            on_screen(f"Would write commands: {command_dir}/  "
                      f"({', '.join('/' + n for n in slash_cmds)})")
        return

    # ── Write all files ───────────────────────────────────────────────────────
    config_path.write_text(json.dumps(oc_config, indent=2))
    if not no_theme:
        theme_path.parent.mkdir(parents=True, exist_ok=True)
        theme_path.write_text(json.dumps(_opencode_lcars_theme(), indent=2))
    if slash_cmds:
        command_dir.mkdir(parents=True, exist_ok=True)
        for name, body in slash_cmds.items():
            (command_dir / f"{name}.md").write_text(body)

    # ── Launch banner ─────────────────────────────────────────────────────────
    solidarity()
    console.print()

    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=20)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Workspace",   workspace)
    tbl.add_row("Model",       f"{chat_mdl}  (Ollama)")
    tbl.add_row("Vault",       str(org_dir))
    tbl.add_row("Nodes",       f"{ctx['n_nodes']}  ({ctx['pct_e']}% embedded)")
    tbl.add_row("Skills",      f"{len(ctx['skills'])} registered")
    tbl.add_row("MCP server",  "org-llm mcp  (stdio)")
    if not no_theme:
        tbl.add_row("Theme",   "org-llm-lcars (LCARS palette)")
    if slash_cmds:
        tbl.add_row("Commands", "/" + ", /".join(slash_cmds.keys()))
    tbl.add_row("Config",      str(config_path))
    console.print(Panel(
        tbl,
        title=f"[lcars1]org-llm  ×  opencode  workspace[/lcars1]  "
              f"[dim]({workspace})[/dim]",
        border_style="lcars2",
        padding=(1, 2),
    ))
    console.print()
    hail("Engaging opencode… (q to quit, Ctrl-C to abort)")
    console.print()

    # ── Hand off to opencode ──────────────────────────────────────────────────
    os.chdir(org_dir)
    os.execvp(oc_bin, [oc_bin])


@app.command(name="claude", rich_help_panel="Workspaces")
def claude_frontend(
    no_context: Annotated[bool, typer.Option("--no-context", "-N",
                help="Skip vault context in CLAUDE.md instructions")] = False,
    dry_run:    Annotated[bool, typer.Option("--dry-run",    "-n",
                help="Print config only, do not launch")] = False,
):
    """Launch Claude Code as an interactive org-roam workspace with vault context and MCP tools."""
    import json
    import os
    import shutil
    from rich.panel  import Panel
    from rich.syntax import Syntax
    from rich.table  import Table
    from .db    import Node, File
    from .skills import Skill
    from .ui    import solidarity, trans_stripe

    # ── Locate or install claude ──────────────────────────────────────────────
    claude_path = _claude_bin()
    if not dry_run and claude_path is None:
        hail("Claude Code not found — installing via npm…")
        claude_path = _install_claude_bin()
        if not claude_path:
            raise typer.Exit(1)
    claude_bin = str(claude_path) if claude_path else "claude"

    # ── Gather vault context (same as launch) ─────────────────────────────────
    engine = _engine()
    with get_session(engine) as session:
        org_dir     = _org_dir(session)
        n_files     = session.query(File).count()
        n_nodes     = session.query(Node).count()
        n_embedded  = session.query(Node).filter(Node.embedding.isnot(None)).count()
        pct_e       = int(n_embedded / n_nodes * 100) if n_nodes else 0
        skill_names = [s.name for s in session.query(Skill).all()]
        from datetime import datetime, timedelta
        since  = (datetime.now() - timedelta(days=7)).timestamp()
        recent = [
            (n.title, n.mtime)
            for n in session.query(Node)
            .filter(Node.mtime >= since)
            .order_by(Node.mtime.desc())
            .limit(8).all()
        ]

    org_llm_dir = Path(__file__).parent.parent.resolve()

    recent_str = "\n".join(
        f"  - {title} ({datetime.fromtimestamp(mtime).date().isoformat() if mtime else '?'})"
        for title, mtime in recent
    ) or "  (no recent activity)"
    skill_str = ", ".join(skill_names) if skill_names else "none — run org-llm skill-index"

    # ── MCP server config → .claude/settings.json ────────────────────────────
    claude_dir = org_dir / ".claude"
    settings_path = claude_dir / "settings.json"

    # Merge with existing settings if present (preserve user's own config)
    existing_settings: dict = {}
    if settings_path.exists():
        try:
            existing_settings = json.loads(settings_path.read_text())
        except Exception:
            pass

    mcp_entry = {
        "command": "uv",
        "args": ["--directory", str(org_llm_dir), "run", "org-llm", "mcp"],
        "env": {"ORG_LLM_DB": str(DB_PATH)},
    }
    existing_settings.setdefault("mcpServers", {})["org-llm"] = mcp_entry

    # ── Vault instructions → .claude/CLAUDE.md ───────────────────────────────
    if no_context:
        claude_md = (
            "# org-llm Vault Assistant\n\n"
            "You have access to the org-roam knowledge base via MCP tools.\n"
            "Use `search_notes`, `ask_notes`, `capture_note` and other tools to "
            "assist with note management, Q&A, and automation workflows.\n"
        )
    else:
        claude_md = f"""# org-llm Vault Assistant

You are an intelligent personal assistant with full access to the user's
org-roam second brain via org-llm MCP tools.

## Vault Summary
- Location: {org_dir}
- Files: {n_files}  |  Nodes: {n_nodes}  |  Embedded: {n_embedded}/{n_nodes} ({pct_e}%)
- Skills: {skill_str}

## Recent Activity (last 7 days)
{recent_str}

## Available MCP Tools
- `search_notes(query, limit, keyword)` — semantic or keyword search
- `ask_notes(question, top_k)` — RAG Q&A grounded in org notes
- `capture_note(title, body, file)` — add a new note to the vault
- `get_node(title)` — fetch full note content by title
- `list_nodes_by_tag(tag, limit)` — browse notes by tag
- `list_recent_nodes(days)` — see recent activity
- `get_vault_stats()` — vault statistics
- `list_skills()` — available org-babel skill workflows
- `run_skill(name, input)` — execute a skill workflow
- `tangle_file(file_path)` — org-babel-tangle via emacsclient
- `get_config()` — current model/config assignments
- `list_tutor_steps()` / `get_tutor_step(step)` — interactive tutorial

## Behaviour
- Always call `search_notes` or `ask_notes` before answering questions about notes
- When saving something, use `capture_note` and confirm file path
- When discussing automation, check `list_skills` first
- Cite note titles when drawing from the knowledge base
- Use `tangle_file` to materialise org-babel workflows after editing
"""

    if dry_run:
        console.print()
        console.rule("[lcars1].claude/settings.json (dry-run)[/lcars1]")
        console.print(Syntax(json.dumps(existing_settings, indent=2), "json", theme="monokai"))
        console.rule()
        console.print()
        console.rule("[lcars1].claude/CLAUDE.md (dry-run)[/lcars1]")
        console.print(claude_md)
        console.rule()
        on_screen(f"Would write to: {settings_path}")
        on_screen(f"Would write to: {claude_dir / 'CLAUDE.md'}")
        return

    # ── Write config files ────────────────────────────────────────────────────
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing_settings, indent=2))
    (claude_dir / "CLAUDE.md").write_text(claude_md)

    # ── Check for API key (env → pass → prompt) ──────────────────────────────
    from . import creds as _creds
    if not os.environ.get("ANTHROPIC_API_KEY"):
        stored = _creds.read_secret(_creds.anthropic_slug()) if _creds.is_available() else None
        if stored:
            os.environ["ANTHROPIC_API_KEY"] = stored
            hail(f"Loaded ANTHROPIC_API_KEY from pass ({_creds.anthropic_slug()})")
        else:
            console.print()
            console.print("[bold yellow]⚠  ANTHROPIC_API_KEY not set[/bold yellow]")
            console.print(
                "  Claude Code needs an API key (or claude.ai Pro subscription).\n"
                "  Get one at [bold]console.anthropic.com[/bold]\n\n"
                "  Persist it via either:\n"
                "    export ANTHROPIC_API_KEY=sk-ant-...\n"
                f"    pass insert {_creds.anthropic_slug()}\n"
            )

    # ── Launch banner ─────────────────────────────────────────────────────────
    solidarity()
    console.print()

    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=20)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Model",       "claude (Anthropic API / claude.ai Pro)")
    tbl.add_row("Vault",       str(org_dir))
    tbl.add_row("Nodes",       f"{n_nodes}  ({pct_e}% embedded)")
    tbl.add_row("Skills",      f"{len(skill_names)} registered")
    tbl.add_row("MCP server",  "org-llm mcp  (stdio)")
    tbl.add_row("Settings",    str(settings_path))
    console.print(Panel(
        tbl,
        title="[lcars1]org-llm  ×  Claude Code  workspace[/lcars1]",
        border_style="lcars2",
        padding=(1, 2),
    ))
    console.print()
    hail("Engaging Claude Code… (Ctrl-C to abort)")
    console.print()

    # ── Hand off to claude ────────────────────────────────────────────────────
    os.chdir(org_dir)
    os.execvp(claude_bin, [claude_bin])


@app.command(rich_help_panel="Models & Cloud")
def cloud(
    status:    Annotated[bool, typer.Option("--status",    "-s",  help="Show configured provider status")] = False,
    providers: Annotated[bool, typer.Option("--providers", "-p",  help="List all supported cloud providers")] = False,
    signup:    Annotated[str,  typer.Option("--signup",     "-S",  help="Open signup page (provider slug or 'list')")] = "",
    console_:  Annotated[str,  typer.Option("--console",    "-O",  help="Open console for a provider slug")] = "",
    configure: Annotated[bool, typer.Option("--configure",  "-c",  help="Set up provider, endpoint, and API key (stores key in `pass`)")] = False,
    test:      Annotated[bool, typer.Option("--test",       "-t",  help="Ping the configured endpoint")] = False,
    assess:    Annotated[bool, typer.Option("--assess",     "-a",  help="Assess which models need cloud vs local")] = False,
    cost:      Annotated[bool, typer.Option("--cost",       "-x",  help="Show cost table across providers")] = False,
    creds:     Annotated[bool, typer.Option("--creds",      "-r",  help="Show stored cloud credentials in `pass`")] = False,
    quick_start: Annotated[str, typer.Option("--quick-start", "-q",
                 help="Provider slug for one-shot signup flow (e.g. openrouter, groq)")] = "",
    key:         Annotated[str, typer.Option("--key", "-K",
                 help="API key for --quick-start (skips the interactive paste prompt)")] = "",
    recommend_upgrade: Annotated[bool, typer.Option("--recommend-upgrade", "-u",
                 help="Analyse recent cloud usage; suggest a paid tier if free is choking")] = False,
    upgrade:     Annotated[str, typer.Option("--upgrade",      "-U",
                 help="Open a provider's billing/upgrade page (slug; default: configured provider)")] = "",
):
    """Manage cloud GPU backends — RunPod, Vast.ai, Lambda, TensorDock, Salad, and more.

    API keys are stored in the standard Unix password manager `pass`
    (https://www.passwordstore.org) under the slug
    `org-llm/cloud/<provider>/api-key`. If `pass` is not installed or the store
    is not initialized, keys fall back to the SQLite config table — see
    `org-llm tutor creds` for setup instructions.
    """
    from rich.panel import Panel
    from rich.table import Table
    from .cloud import (
        PROVIDERS, PROVIDER_MAP, get_provider,
        assess_local_capability, check_connection, cost_per_1k_tokens,
        local_vram_gb, local_ram_gb, open_url,
    )
    from .ui import TREK_MSGS, stardate, COMRADE_STAR
    from .db import Config
    from . import creds as creds_mod

    engine = _engine()

    # default: show status
    if not any([status, providers, signup, console_, configure, test, assess,
                cost, creds, quick_start, recommend_upgrade, upgrade]):
        status = True

    # ── Recommend a paid upgrade based on recent usage telemetry ─────────────
    if recommend_upgrade:
        from rich.panel import Panel as _P
        from rich.table import Table as _T
        from .cloud import recommend_upgrade as _ru, PROVIDER_MAP as _PM
        # Read top fixer accuracy from cloud_usage if available
        # (we don't keep a structured score, so use last benchmark winner as proxy)
        with get_session(engine) as session:
            best = _cfg(session, "fixer_model")
        rec = _ru()
        console.print()
        console.rule("[lcars1]Upgrade recommendation[/lcars1]")
        m = rec.metrics
        info = _T(box=None, pad_edge=False, show_header=False)
        info.add_column("Key",   style="lcars1", width=22, no_wrap=True)
        info.add_column("Value", style="lcars2", overflow="fold")
        info.add_row("Recent calls (7d)", str(m["events"]))
        info.add_row("✓ successes",       str(m["successes"]))
        info.add_row("⚠ rate-limits",     f"{m['rate_limits']}"
                      + (f"  ([yellow]{m['rate_limits']/m['events']*100:.0f}% of calls[/yellow])"
                         if m['events'] else ""))
        info.add_row("⚠ server errors",   str(m["server_errors"]))
        info.add_row("⚠ timeouts",        str(m["timeouts"]))
        if m["median_latency_ms"]:
            info.add_row("median latency", f"{m['median_latency_ms']:.0f} ms")
        info.add_row("severity score",    f"{m['score']}  ({rec.severity})")
        if best:
            info.add_row("benched fixer",  best)
        console.print(_P(info, title="[lcars1]Cloud usage (last 7d)[/lcars1]",
                          border_style="lcars2"))
        console.print()

        if not rec.should_upgrade:
            on_screen("[bold green]No upgrade signal yet.[/bold green]  "
                      f"Free tier is serving you fine ({m['successes']}/{m['events']} ok).")
            on_screen("[dim]Re-run after a heavy day of asks to surface throttling.[/dim]")
            return

        verdict_style = {"strongly": "red", "recommend": "yellow",
                          "consider": "cyan", "ok": "green"}.get(rec.severity, "yellow")
        console.print(f"[bold {verdict_style}]Verdict: {rec.severity.upper()} an upgrade.[/]")
        console.print()
        for r in rec.reasons:
            on_screen(f"  • {r}")
        console.print()

        prov = _PM.get(rec.suggested_provider)
        if prov:
            on_screen(f"Suggested: stay on [bold]{prov.name}[/bold] but switch to a paid model.")
            on_screen("[dim]Order: FOSS / self-hostable first; closed APIs last.[/dim]")
            if prov.paid_examples:
                # Heuristic: assume the first 4 are the FOSS picks based on our
                # ordering convention in cloud.py
                on_screen("  Open-weights picks (run locally if you want, "
                          "with the same tag in Ollama):")
                for m in prov.paid_examples[:4]:
                    on_screen(f"    • [bold]{m}[/bold]")
                if len(prov.paid_examples) > 4:
                    on_screen("  Closed API (last resort):")
                    for m in prov.paid_examples[4:]:
                        on_screen(f"    • [dim]{m}[/dim]")
            on_screen(f"  Pricing:       [bold]{prov.pricing_url or prov.docs_url}[/bold]")
            on_screen(f"  Open billing:  [bold]org-llm cloud --upgrade "
                      f"{rec.suggested_provider}[/bold]")
        return

    # ── Open a provider's billing / upgrade page ─────────────────────────────
    if upgrade:
        from .cloud import PROVIDER_MAP as _PM
        slug = upgrade.strip().lower()
        if slug in ("", "self", "default"):
            with get_session(engine) as session:
                slug = _cfg(session, "cloud_provider") or "openrouter"
        prov = _PM.get(slug)
        if not prov:
            red_alert(f"Unknown provider: {slug!r}")
            on_screen("List slugs: org-llm cloud --providers")
            raise typer.Exit(1)
        url = prov.pricing_url or prov.console_url or prov.signup_url
        hail(f"Opening {prov.name} pricing/upgrade: {url}")
        from .cloud import open_url as _open
        _open(url)
        if prov.paid_examples:
            console.print()
            on_screen("Once you've upgraded, swap to a paid model:")
            for m in prov.paid_examples[:4]:
                on_screen(f"  [bold]org-llm config cloud_model {m}[/bold]")
        return

    # ── Quick-start: one-shot onboarding for free-tier providers ─────────────
    if quick_start:
        from rich.panel import Panel
        slug = quick_start.lower()
        chosen = get_provider(slug)
        if not chosen:
            red_alert(f"Unknown provider {slug!r}. Try: openrouter, groq, huggingface")
            raise typer.Exit(1)

        # Default model + key-prefix hints per provider for the "test" call
        model_for, key_hint = {
            "openrouter": ("openai/gpt-oss-20b:free",            "sk-or-v1-…"),
            "groq":       ("llama-3.1-8b-instant",                "gsk_…"),
            "huggingface":("meta-llama/Llama-3.1-8B-Instruct",   "hf_…"),
        }.get(slug, (chosen.gpu_costs and list(chosen.gpu_costs)[0] or "", ""))

        console.print()
        console.rule(f"[lcars1]Quick-start: {chosen.name}[/lcars1]")
        console.print(Panel(
            f"[lcars2]{chosen.name}[/lcars2] — {chosen.description}\n\n"
            f"This flow will:\n"
            f"  1. Install [bold]pass[/bold] (if missing)\n"
            f"  2. Bootstrap a passwordless GPG key (if missing)\n"
            f"  3. Open [bold]{chosen.signup_url}[/bold] — you create a free account + API key\n"
            f"  4. You paste the key once — we store it encrypted in pass\n"
            f"  5. We configure org-llm to use it and run a real test call\n\n"
            f"Total manual effort: 1 sign-in (Google/GitHub button) + 1 paste.",
            border_style="lcars2", padding=(1, 2),
        ))
        # Skip confirmation when called with --key (non-interactive mode)
        if not key and not typer.confirm("Proceed?", default=True):
            return

        # Step 1: pass installation
        if not creds_mod.is_installed():
            hail("Installing `pass`…")
            if not creds_mod.install():
                red_alert("Could not install `pass` automatically.")
                console.print(creds_mod.install_help())
                raise typer.Exit(1)
        hail("`pass` is installed.")

        # Step 2: GPG bootstrap (only if no key)
        if not creds_mod.is_initialized():
            keys = creds_mod.list_gpg_keys()
            if not keys:
                # Build a sane default identity from git config or env
                import subprocess as _sp
                try:
                    git_email = _sp.run(["git", "config", "--global", "user.email"],
                                        capture_output=True, text=True, timeout=3).stdout.strip()
                    git_name  = _sp.run(["git", "config", "--global", "user.name"],
                                        capture_output=True, text=True, timeout=3).stdout.strip()
                except Exception:
                    git_email = git_name = ""
                default_email = git_email or os.environ.get("EMAIL", "you@example.invalid")
                default_name  = git_name or "org-llm user"
                console.print()
                hail("No GPG key found — creating a passwordless RSA-4096 key for `pass`.")
                on_screen(f"  identity: [bold]{default_name} <{default_email}>[/bold]")
                if not typer.confirm("Create the key with these defaults?", default=True):
                    on_screen("Run `gpg --full-generate-key` yourself, then `pass init <KEY-ID>`.")
                    raise typer.Exit(1)
                kid = creds_mod.bootstrap_gpg_key(default_name, default_email)
                if not kid:
                    red_alert("GPG key generation failed. Run `gpg --full-generate-key` manually.")
                    raise typer.Exit(1)
                hail(f"Generated GPG key {kid}")
            else:
                kid = keys[0][0]
                hail(f"Using existing GPG key {kid}  ({keys[0][1]})")
            if not creds_mod.init_store(kid):
                red_alert("`pass init` failed. Check `pass` and `gpg` setup.")
                raise typer.Exit(1)
            hail("`pass` store initialized.")

        # Step 3: open browser
        console.print()
        on_screen(f"Opening {chosen.name}…")
        on_screen(f"  signup:  {chosen.signup_url}")
        on_screen(f"  keys:    {chosen.console_url}")
        open_url(chosen.console_url)
        console.print()
        on_screen("Sign in (Google/GitHub button), click [bold]Create Key[/bold], copy it, paste below.")
        on_screen(f"  Key format: [dim]{key_hint}[/dim]")
        console.print()

        # Step 4: paste + store
        api_key = key or typer.prompt(f"{chosen.name} API key", hide_input=True)
        if not api_key.strip():
            red_alert("No key entered — aborting.")
            raise typer.Exit(1)
        slug_path = creds_mod.cloud_slug(slug)
        if not creds_mod.write_secret(slug_path, api_key.strip()):
            red_alert(f"Could not store key in pass at {slug_path}")
            raise typer.Exit(1)
        hail(f"API key stored at {slug_path} (encrypted via GPG)")

        # Step 5: configure org-llm + test
        endpoint = chosen.endpoint_hint   # for these providers it's a literal URL
        with get_session(engine) as session:
            for k, v in [
                ("cloud_provider",     slug),
                ("cloud_endpoint_url", endpoint),
                ("cloud_model",        model_for),
            ]:
                row = session.get(Config, k)
                if row: row.value = v
                else:   session.add(Config(key=k, value=v))
            # Wipe any legacy plaintext copies
            for stale in ("cloud_api_key", "runpod_api_key"):
                row = session.get(Config, stale)
                if row:
                    session.delete(row)
            session.commit()
        hail(f"Configured: provider={slug}  endpoint={endpoint}  model={model_for}")

        # Live ping
        console.print()
        with warp(f"{TREK_MSGS['cloud']}: {endpoint}"):
            cs = check_connection(endpoint, api_key, model_for)
        if cs.reachable and cs.auth_ok:
            hail(f"Endpoint reachable in {cs.latency_ms:.0f}ms ✓")
        elif cs.reachable:
            red_alert("Endpoint responded but rejected the key. Re-check the paste and try again.")
            raise typer.Exit(1)
        else:
            red_alert(f"Could not reach {endpoint}. Check your network.")
            raise typer.Exit(1)

        # Real chat call to confirm end-to-end
        console.print()
        on_screen("Running a test chat call…")
        from .cloud import cloud_chat
        try:
            answer = cloud_chat(
                "Reply with just the words: solidarity confirmed.",
                model=model_for, endpoint_url=endpoint, api_key=api_key,
                system="You are a concise assistant.",
            )
            console.print(Panel(answer.strip(), title=f"[lcars1]{model_for}[/lcars1]",
                                border_style="lcars2", padding=(1, 2)))
            hail("Cloud backend is live.")
        except Exception as e:
            red_alert(f"Test chat failed: {e}")
            raise typer.Exit(1)

        console.print()
        try:
            with get_session(_engine()) as session:
                on_screen(_suggest_note_ask(session, prefix="Try it now:  "))
        except Exception:
            on_screen("Try it now:  [bold]org-llm ask 'what's interesting in my vault?'[/bold]")
        on_screen("View status: [bold]org-llm cloud --status[/bold]")
        make_it_so()
        return

    # ── Stored credentials snapshot ──────────────────────────────────────────
    if creds:
        cs = creds_mod.status()
        console.print()
        console.rule("[lcars1]Cloud Credentials  ·  pass[/lcars1]")
        tbl = Table(box=None, pad_edge=False, show_header=False)
        tbl.add_column("Key", style="lcars1", width=20)
        tbl.add_column("Value", style="lcars2")
        tbl.add_row("pass installed",   "[green]yes[/green]" if cs.installed else "[red]no[/red]")
        tbl.add_row("store initialized", "[green]yes[/green]" if cs.initialized else "[red]no[/red]")
        tbl.add_row("store path",        str(cs.store_path))
        if cs.gpg_id:
            tbl.add_row("gpg key id",    cs.gpg_id)
        console.print(Panel(tbl, title="[lcars1]pass[/lcars1]", border_style="lcars2"))

        if cs.secrets:
            console.print()
            tbl2 = Table(title="Stored secrets (org-llm/*)", box=None, pad_edge=False)
            tbl2.add_column("Slug", style="lcars3")
            for s in cs.secrets:
                tbl2.add_row(s)
            console.print(tbl2)
        elif cs.installed and cs.initialized:
            on_screen("No org-llm secrets stored yet. Use [bold]org-llm cloud --configure[/bold].")
        else:
            console.print()
            console.print(creds_mod.install_help())
            on_screen("Detailed setup: [bold]org-llm tutor creds[/bold]")
        return

    # ── Provider list ─────────────────────────────────────────────────────────
    if providers:
        console.print()
        console.rule("[lcars1]Supported Cloud GPU Providers[/lcars1]")
        # On narrow terminals, drop the description column entirely and emit
        # one-line "<slug>: <description>" rows underneath the table instead.
        # Rich would otherwise wrap the description to 4-char strips.
        narrow = console.width < 110
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("Slug",       style="lcars1",  no_wrap=True, width=14)
        tbl.add_column("Name",       style="lcars2",  no_wrap=True, max_width=22, overflow="ellipsis")
        tbl.add_column("API",        style="dim",     width=8)
        tbl.add_column("Cheapest GPU",                width=22, no_wrap=True, overflow="ellipsis")
        if not narrow:
            tbl.add_column("Description", style="dim", overflow="fold", min_width=24)
        for p in PROVIDERS:
            cheapest_gpu = min(p.gpu_costs, key=p.gpu_costs.get)
            cheapest_cost = p.gpu_costs[cheapest_gpu]
            cheapest_label = (
                f"FREE — {cheapest_gpu}" if cheapest_cost == 0
                else f"${cheapest_cost:.2f}/hr ({cheapest_gpu})"
            )
            row = [p.slug, p.name, p.api_compat, cheapest_label]
            if not narrow:
                row.append(p.description)
            tbl.add_row(*row)
        console.print(tbl)
        if narrow:
            console.print()
            for p in PROVIDERS:
                console.print(f"  [lcars1]{p.slug:<14}[/lcars1] [dim]{p.description}[/dim]")
        console.print()
        on_screen("Sign up: [bold]org-llm cloud --signup <slug>[/bold]")
        on_screen("Configure: [bold]org-llm cloud --configure[/bold]")
        return

    # ── Signup ────────────────────────────────────────────────────────────────
    if signup:
        if not signup.strip():
            red_alert("--signup needs a provider slug. Try: [bold]org-llm cloud --signup list[/bold]")
            raise typer.Exit(1)
        if signup in ("list", "?", "help"):
            on_screen("Available providers:")
            for p in PROVIDERS:
                console.print(f"  [lcars1]{p.slug:<14}[/lcars1] [lcars2]{p.name}[/lcars2]  — {p.description}")
            console.print()
            on_screen("Usage: org-llm cloud --signup <slug>")
            return

        p = get_provider(signup)
        if not p:
            names = [pr.slug for pr in PROVIDERS]
            red_alert(f"Unknown provider: {signup!r}")
            on_screen(f"Available: {', '.join(names)}")
            on_screen("List all:  org-llm cloud --providers")
            raise typer.Exit(1)

        hail(f"Opening {p.name} signup: {p.signup_url}")
        open_url(p.signup_url)
        console.print()
        console.print(f"[lcars2]{p.name}[/lcars2]  —  {p.description}")
        console.print()
        if p.api_compat in ("ollama", "both"):
            on_screen(f"Deploy an Ollama pod/container. Endpoint format: [bold]{p.endpoint_hint}[/bold]")
        else:
            on_screen(f"Endpoint format: [bold]{p.endpoint_hint}[/bold]  (OpenAI-compatible)")
        console.print()

        # Offer to capture the API key right now (after the user has signed up)
        if creds_mod.is_available():
            on_screen("After signing up, paste your API key to store it securely.")
            on_screen(f"  ↪ slot: [dim]{creds_mod.cloud_slug(p.slug)}[/dim]")
            api_key = typer.prompt("API key (blank to skip)", default="", hide_input=True)
            if api_key:
                if creds_mod.write_secret(creds_mod.cloud_slug(p.slug), api_key):
                    hail(f"Stored API key in pass at {creds_mod.cloud_slug(p.slug)}")
                else:
                    red_alert("Failed to store API key in pass — run: org-llm cloud --configure")
        else:
            on_screen("Tip: install `pass` for encrypted credential storage.")
            console.print()
            console.print(creds_mod.install_help())
        on_screen("Then run: [bold]org-llm cloud --configure[/bold] to set the endpoint URL.")
        return

    # ── Console ───────────────────────────────────────────────────────────────
    if console_:
        p = get_provider(console_)
        if not p:
            # fall back to configured provider
            with get_session(engine) as session:
                slug = _cfg(session, "cloud_provider")
            p = get_provider(slug)
        if p:
            hail(f"Opening {p.name} console…")
            open_url(p.console_url)
        else:
            red_alert("No provider configured. Run: org-llm cloud --configure")
        return

    # ── Configure ─────────────────────────────────────────────────────────────
    if configure:
        console.print()
        console.rule("[lcars1]Cloud Configuration[/lcars1]")

        # Pre-flight: pass status + offer to install
        if not creds_mod.is_installed():
            console.print()
            on_screen("`pass` is not installed — your API key would be stored in plain SQLite.")
            if typer.confirm("Install `pass` now?", default=True):
                if creds_mod.install():
                    hail("`pass` installed.")
                else:
                    red_alert("Could not install `pass` automatically.")
                    console.print(creds_mod.install_help())
        if creds_mod.is_installed() and not creds_mod.is_initialized():
            console.print()
            console.print(creds_mod.install_help())
            on_screen("Continuing with SQLite fallback — re-run after `pass init <key>`.")

        # Provider selection
        console.print()
        on_screen("Available providers:")
        for i, p in enumerate(PROVIDERS, 1):
            console.print(f"  [lcars1]{i}.[/lcars1] [lcars2]{p.slug:<14}[/lcars2] {p.name}  — {p.description}")
        console.print()
        choice = typer.prompt("Provider (number or slug)", default="runpod")
        if choice.isdigit() and 1 <= int(choice) <= len(PROVIDERS):
            chosen = PROVIDERS[int(choice) - 1]
        else:
            chosen = get_provider(choice) or PROVIDERS[0]
        hail(f"Selected: {chosen.name}")

        console.print()
        on_screen(f"Endpoint format: [bold]{chosen.endpoint_hint}[/bold]")
        on_screen(f"Docs: {chosen.docs_url}")
        console.print()

        endpoint = typer.prompt(f"{chosen.name} endpoint URL", default="")
        existing_key_in_pass = creds_mod.read_secret(creds_mod.cloud_slug(chosen.slug))
        with get_session(engine) as _s:
            existing_key_in_db = (_cfg(_s, "cloud_api_key")
                                  or _cfg(_s, "runpod_api_key"))

        prompt_default_hint = ""
        if existing_key_in_pass:
            prompt_default_hint = "  [stored in pass; blank = keep]"
        elif existing_key_in_db:
            prompt_default_hint = "  [in SQLite; blank = migrate to pass]"
        api_key = typer.prompt(
            f"API key (blank if public endpoint){prompt_default_hint}",
            default="", hide_input=True,
        )
        model = typer.prompt("Model on cloud endpoint (blank = use chat_model)", default="")

        if endpoint:
            stored_in_pass = False
            slug = creds_mod.cloud_slug(chosen.slug)
            key_to_store = api_key or (existing_key_in_db if not existing_key_in_pass else "")
            if creds_mod.is_available() and key_to_store:
                if creds_mod.write_secret(slug, key_to_store):
                    stored_in_pass = True
                    hail(f"API key stored in pass at {slug}")

            with get_session(engine) as session:
                for key, val in [
                    ("cloud_provider",    chosen.slug),
                    ("cloud_endpoint_url", endpoint.rstrip("/")),
                ]:
                    row = session.get(Config, key)
                    if row: row.value = val
                    else:   session.add(Config(key=key, value=val))

                if stored_in_pass:
                    # Clear any plaintext copies from SQLite (migration)
                    for stale in ("cloud_api_key", "runpod_api_key"):
                        row = session.get(Config, stale)
                        if row:
                            session.delete(row)
                            on_screen(f"Migrated {stale} from SQLite → pass.")
                elif api_key:
                    # Fallback: pass not available, store in SQLite
                    row = session.get(Config, "cloud_api_key")
                    if row: row.value = api_key
                    else:   session.add(Config(key="cloud_api_key", value=api_key))
                    on_screen("API key stored in SQLite (`pass` not available).")

                if model:
                    row = session.get(Config, "cloud_model")
                    if row: row.value = model
                    else:   session.add(Config(key="cloud_model", value=model))
                session.commit()
            hail(f"Cloud endpoint saved: {endpoint}")
            if model:
                hail(f"Cloud model: {model}")

        make_it_so()
        return

    # Load cloud config
    with get_session(engine) as session:
        provider_slug = _cfg(session, "cloud_provider")
        endpoint_url  = _cfg(session, "cloud_endpoint_url")
        cloud_model   = _cfg(session, "cloud_model") or _cfg(session, "chat_model") or MODEL_DEFAULTS["chat_model"]
        all_models    = [_cfg(session, k) for _, k, _ in _TASK_MODEL_KEYS if _cfg(session, k)]
        db_api_key    = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
    api_key = (creds_mod.read_secret(creds_mod.cloud_slug(provider_slug))
               if provider_slug else None) or db_api_key

    provider_info = get_provider(provider_slug)

    # ── Test connection ───────────────────────────────────────────────────────
    if test:
        if not endpoint_url:
            red_alert("No cloud endpoint configured. Run: org-llm cloud --configure")
            raise typer.Exit(1)
        with warp(f"{TREK_MSGS['cloud']}: {endpoint_url}"):
            cs = check_connection(endpoint_url, api_key, cloud_model)
        if cs.reachable and cs.auth_ok:
            hail(f"Cloud endpoint reachable  ({cs.latency_ms:.0f}ms)")
        elif cs.reachable:
            red_alert("Endpoint reachable but authentication failed — check API key.")
        else:
            red_alert(f"Cloud endpoint unreachable: {endpoint_url}")
        return

    # ── Hardware assessment ───────────────────────────────────────────────────
    if assess:
        console.print()
        console.rule(f"[lcars1]{TREK_MSGS['assess']}[/lcars1]")
        vram = local_vram_gb()
        ram  = local_ram_gb()
        hail(f"Hardware: {'%.0f GB VRAM' % vram if vram else 'no GPU detected'}  |  {ram:.0f} GB RAM")
        hail(f"Stardate: {stardate()}")
        console.print()

        results = assess_local_capability(list(set(all_models)))
        tbl = Table(box=None, pad_edge=False)
        tbl.add_column("Model",       style="lcars2")
        tbl.add_column("VRAM needed", style="lcars3", justify="right")
        tbl.add_column("Local?",      justify="center")
        tbl.add_column("Verdict",     style="dim")
        needs_cloud = []
        for r in results:
            sym = "[bold green]✓[/]" if r["can_local"] else "[bold red]→ cloud[/]"
            tbl.add_row(r["model"], f"{r['vram_needed']:.0f} GB", sym, r["reason"])
            if not r["can_local"]:
                needs_cloud.append(r["model"])
        console.print(tbl)

        if needs_cloud:
            console.print()
            if endpoint_url:
                on_screen(f"Cloud endpoint ready for: {', '.join(needs_cloud)}")
            else:
                on_screen(f"[bold]{len(needs_cloud)} model(s) need cloud.[/bold] "
                          "Run: [bold]org-llm cloud --providers[/bold]  then  "
                          "[bold]org-llm cloud --signup <slug>[/bold]")
        return

    # ── Cost comparison across providers ─────────────────────────────────────
    if cost:
        console.print()
        console.rule("[lcars1]Cloud GPU Cost Comparison[/lcars1]")
        console.print()
        for p in PROVIDERS:
            tbl = Table(title=f"[lcars2]{p.name}[/lcars2]  ({p.api_compat})",
                        box=None, pad_edge=False)
            tbl.add_column("GPU",       style="lcars1")
            tbl.add_column("$/hr",      style="lcars3", justify="right")
            tbl.add_column("¢/1k tok",  style="lcars2", justify="right")
            for gpu, hourly in p.gpu_costs.items():
                cpp = cost_per_1k_tokens(gpu, tokens_per_sec=30.0, provider_slug=p.slug)
                tbl.add_row(gpu, f"${hourly:.2f}", f"{cpp*100:.3f}¢")
            console.print(tbl)
            console.print()
        on_screen("Prices are approximate spot rates. Verify at each provider's site.")
        on_screen("Sign up: org-llm cloud --signup <slug>   │   List: org-llm cloud --providers")
        return

    # ── Status panel ─────────────────────────────────────────────────────────
    console.print()
    console.rule(f"[lcars1]Cloud Status  ·  stardate {stardate()}[/lcars1]")

    cs = None
    if endpoint_url:
        with warp(f"{TREK_MSGS['cloud']}: {endpoint_url}"):
            cs = check_connection(endpoint_url, api_key, cloud_model)

    pname = provider_info.name if provider_info else (provider_slug or "not configured")
    if api_key and provider_slug and creds_mod.read_secret(creds_mod.cloud_slug(provider_slug)):
        key_source = "set (pass)"
    elif api_key:
        key_source = "set (SQLite — run --configure to migrate to pass)"
    else:
        key_source = "—"
    tbl = Table(box=None, pad_edge=False, show_header=False)
    tbl.add_column("Key",   style="lcars1", width=22)
    tbl.add_column("Value", style="lcars2")
    tbl.add_row("Provider",    pname)
    tbl.add_row("Endpoint",    endpoint_url or "—")
    tbl.add_row("API key",     key_source)
    tbl.add_row("Cloud model", cloud_model)
    if cs:
        if cs.reachable and cs.auth_ok:
            tbl.add_row("Connection", f"[bold green]✓ reachable ({cs.latency_ms:.0f}ms)[/bold green]")
        elif cs.reachable:
            tbl.add_row("Connection", "[bold yellow]⚠ reachable, auth failed[/bold yellow]")
        else:
            tbl.add_row("Connection", "[bold red]✗ unreachable[/bold red]")

    vram = local_vram_gb()
    ram  = local_ram_gb()
    tbl.add_row("Local GPU", f"{vram:.0f} GB VRAM" if vram else "none detected")
    tbl.add_row("Local RAM", f"{ram:.0f} GB")

    console.print(Panel(
        tbl,
        title=f"[lcars1]{COMRADE_STAR}  Cloud GPU  {COMRADE_STAR}[/lcars1]",
        border_style="lcars2", padding=(1, 2),
    ))

    if not endpoint_url:
        console.print()
        on_screen("No cloud backend configured. Options:")
        on_screen("  [bold]org-llm cloud --providers[/bold]   — compare all providers + pricing")
        on_screen("  [bold]org-llm cloud --signup <slug>[/bold] — open signup for a provider")
        on_screen("  [bold]org-llm cloud --configure[/bold]   — enter endpoint URL + API key")
        on_screen("  [bold]org-llm cloud --assess[/bold]      — see which models need cloud")
    console.print()


@app.command(rich_help_panel="Workspaces")
def mcp():
    """Start the org-llm MCP server over stdio (for opencode and other MCP clients)."""
    from .mcp_server import main as _mcp_main
    _mcp_main()


@app.command(rich_help_panel="LLM Auth (MCP)")
def grant(
    path: Annotated[str, typer.Argument(help="Filesystem path to authorise the LLM to read")],
):
    """Authorise the MCP server (and any LLM connected to it) to read files
    under PATH. Adds an entry to the `mcp_file_allowlist` config row.

    The LLM can then use the `read_file` and `list_directory` MCP tools on
    any descendant. Revoke with `org-llm revoke <path>`. Inspect with
    `org-llm grants`.
    """
    from . import access
    p = Path(path).expanduser().resolve()
    if not p.exists():
        red_alert(f"Path does not exist: {p}")
        on_screen("Grant the path anyway? It must exist when the LLM tries to read it.")
        if not typer.confirm("Add to allow-list anyway?", default=False):
            raise typer.Exit(1)
    if access.grant(str(p)):
        hail(f"Granted: {p}")
        if p.is_dir():
            on_screen("LLM can now read any file under this directory.")
        on_screen("Inspect with: [bold]org-llm grants[/bold]")
        make_it_so()
    else:
        red_alert("Failed to write allow-list. Is the DB initialised?")
        raise typer.Exit(1)


@app.command(rich_help_panel="LLM Auth (MCP)")
def revoke(
    path: Annotated[str, typer.Argument(help="Path to remove from the allow-list")],
):
    """Remove a previously-granted path from the MCP allow-list."""
    from . import access
    p = Path(path).expanduser().resolve()
    if access.revoke(str(p)):
        hail(f"Revoked: {p}")
        make_it_so()
    else:
        red_alert("Failed to update allow-list.")
        raise typer.Exit(1)


@app.command(name="grants", rich_help_panel="LLM Auth (MCP)")
def grants_list():
    """List paths the LLM (via MCP) is currently authorised to read."""
    from rich.table import Table as _T
    from . import access
    grants = access.allowlist()
    roots  = access.auto_grant_roots()
    console.print()
    console.rule("[lcars1]MCP file-access grants[/lcars1]")
    if grants:
        tbl = _T(title="[lcars2]Direct grants[/lcars2]", box=None, pad_edge=False)
        tbl.add_column("Path",   style="lcars2", no_wrap=True)
        tbl.add_column("Exists", style="dim", width=8)
        tbl.add_column("Type",   style="dim", width=8)
        for g in grants:
            exists = "[green]yes[/green]" if g.exists() else "[red]no[/red]"
            kind   = ("dir" if g.is_dir() else
                      "file" if g.is_file() else "—")
            tbl.add_row(str(g), exists, kind)
        console.print(tbl)
    else:
        on_screen("[dim]No direct grants. The LLM cannot read any file (yet).[/dim]")
    console.print()
    if roots:
        rtbl = _T(title="[lcars3]Auto-grant roots (LLM may self-extend under these)[/lcars3]",
                  box=None, pad_edge=False)
        rtbl.add_column("Root", style="lcars3", no_wrap=True)
        rtbl.add_column("Exists", style="dim", width=8)
        for r in roots:
            rtbl.add_row(str(r),
                          "[green]yes[/green]" if r.exists() else "[red]no[/red]")
        console.print(rtbl)
    else:
        on_screen("[dim]No auto-grant roots. LLM cannot self-grant; "
                  "every path needs an explicit `org-llm grant`.[/dim]")
        # Proactive: offer concrete candidates discovered on disk.
        try:
            from .discover import suggest_grant_roots
            cands = suggest_grant_roots()
        except Exception:
            cands = []
        if cands:
            on_screen("[dim]Suggested roots based on what's on disk:[/dim]")
            for c in cands:
                on_screen(f"  → [bold]org-llm grant-root {c}[/bold]")
    console.print()
    on_screen(f"Browser access: "
              f"{'[green]enabled[/green]' if access.browser_enabled() else '[dim]disabled[/dim]'}")
    console.print()
    on_screen("Add a path:        [bold]org-llm grant <path>[/bold]")
    on_screen("Remove a path:     [bold]org-llm revoke <path>[/bold]")
    on_screen("Trust a root:      [bold]org-llm grant-root <path>[/bold]")
    on_screen("Untrust a root:    [bold]org-llm revoke-root <path>[/bold]")
    on_screen("Enable browser:    [bold]org-llm grant-browser[/bold]")
    on_screen("Disable browser:   [bold]org-llm revoke-browser[/bold]")
    console.print()
    on_screen("[dim]Sensitive paths (SSH/GPG/cloud creds) are ALWAYS denied, "
              "even with grants.[/dim]")


# ── User-defined theme knobs ────────────────────────────────────────────────

knob_app = typer.Typer(help="Define custom theme knobs (dinosaur, coffee, …) to "
                            "extend the trek/commie/queer dials.",
                       cls=PrefixGroup)
app.add_typer(knob_app, name="knob", rich_help_panel="Themes")


# ── context (LLM-readable current-truth file) ─────────────────────────────

context_app = typer.Typer(
    help="Manage org-llm's user-context file — facts that override "
         "stale info in your vault. Tangled to plain-text and "
         "prepended to every system prompt.",
    cls=PrefixGroup,
)
app.add_typer(context_app, name="context", rich_help_panel="Context & Vault")


@context_app.command("show")
def context_show():
    """Print the current tangled context (what the LLM actually sees)."""
    from . import context as _ctx
    body = _ctx.read_context_for_prompt(max_chars=20000)
    if not body:
        on_screen("[dim]No context registered yet.[/dim]")
        on_screen("Add one:  [bold]org-llm context add 'I work at Idexx now'[/bold]")
        on_screen("Or:       [bold]org-llm context from-prompt 'natural language'[/bold]")
        return
    console.print()
    console.rule(f"[lcars1]{_ctx.CONTEXT_HEADER}[/lcars1]")
    console.print(body)
    console.rule(f"[dim]source: {_ctx.context_org_path()} · "
                 f"tangled: {_ctx.context_tangle_path()}[/dim]")


@context_app.command("add")
def context_add(
    fact: Annotated[str, typer.Argument(help="A short crisp fact to record")],
    topic: Annotated[str, typer.Option("--topic", "-t",
            help="Topic tag (employment, address, project-status, …)")] = "",
    sweep: Annotated[bool, typer.Option("--sweep/--no-sweep",
            help="After adding, scan vault for related stale notes")] = True,
    apply: Annotated[bool, typer.Option("--apply", "-a",
            help="Auto-apply stale tags to detected matches (skips confirm)")] = False,
):
    """Append a fact to the active context.

    The fact is written to the context org file's `active-facts` block,
    a one-line history entry is logged, and the file is re-tangled so
    the LLM sees the new fact immediately. With --sweep (default), the
    vault is then scanned for nodes whose content references words in
    the new fact — those are proposed for `:stale:` tagging.
    """
    from . import context as _ctx
    p = _ctx.add_fact(fact, source="cli")
    hail(f"Added to {p}")
    on_screen(f"  fact: {fact.strip()}")

    if not sweep:
        make_it_so()
        return

    # Quick keyword sweep (deterministic) — pull capitalised tokens.
    import re as _re
    keywords = list(set(_re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", fact)))
    if not keywords:
        on_screen("[dim]No proper-noun keywords in fact — skipping sweep.[/dim]")
        make_it_so()
        return

    engine = _engine()
    with get_session(engine) as session:
        candidates = _ctx.find_stale_candidates(session, keywords)
    if not candidates:
        on_screen(f"[dim]No notes match keywords {keywords}.[/dim]")
        make_it_so()
        return

    on_screen(f"[yellow]Found {len(candidates)} note(s) referencing "
              f"{', '.join(keywords)}:[/yellow]")
    for n, kw in candidates[:10]:
        on_screen(f"  [{kw}] {n.title or '(untitled)'}")
    if len(candidates) > 10:
        on_screen(f"  … and {len(candidates) - 10} more")

    if not apply:
        if not typer.confirm(f"Tag these {len(candidates)} node(s) as :stale:?",
                              default=False):
            on_screen("Skipped. Re-run with --apply to tag automatically.")
            return

    with get_session(engine) as session:
        # Re-fetch under the new session for safe writes
        kws = keywords
        cands = _ctx.find_stale_candidates(session, kws)
        n_updated = _ctx.apply_stale_tags(session, cands, topic=topic)
    hail(f"Tagged {n_updated} node(s) as :stale: "
         + (f"+ :re:{topic}:" if topic else ""))
    make_it_so()


@context_app.command("from-prompt")
def context_from_prompt(
    prompt: Annotated[str, typer.Argument(help="Freeform statement (LLM parses to a fact)")],
    apply:  Annotated[bool, typer.Option("--apply", "-a",
            help="Skip confirmation; apply stale tags directly")] = False,
):
    """Parse a freeform statement into a structured fact via the LLM.

    Example:
      org-llm context from-prompt "I no longer work at Unum, now at Idexx as of April"
        → fact:    "Works at Idexx as of 2026-04 (formerly Unum, 2018-2026)."
        → keywords: ["Unum"]
        → topic:   "employment"
    The vault is then scanned for nodes mentioning the keywords and
    proposed for stale-tagging.
    """
    from . import context as _ctx
    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)
        mdl = (_cfg(session, "fast_model")
               or _cfg(session, "chat_model")
               or "llama3.2")
        # Prefer smallest fitting chat-capable model for parse
        try:
            from .llm import list_models as _lm
            pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                       for m in _lm(url) or []}
            for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                if any(p == cand or p.startswith(cand + ":") for p in pulled):
                    mdl = cand
                    break
        except Exception:
            pass
    plan = _ctx.parse_user_request(prompt, model=mdl, base_url=url)
    if not plan or not plan.get("fact"):
        red_alert("LLM couldn't extract a fact from that statement.")
        on_screen(f"  Try: [bold]org-llm context add '{prompt[:50]}…'[/bold]")
        raise typer.Exit(1)
    fact     = plan["fact"]
    keywords = plan.get("supersedes_keywords") or []
    topic    = plan.get("topic") or ""
    on_screen(f"[lcars3]Parsed:[/lcars3] {fact}")
    if keywords:
        on_screen(f"  [dim]Supersedes keywords:[/dim] {', '.join(keywords)}")
    if topic:
        on_screen(f"  [dim]Topic:[/dim] {topic}")
    p = _ctx.add_fact(fact, source="from-prompt")
    hail(f"Added to {p}")

    if not keywords:
        make_it_so()
        return

    with get_session(engine) as session:
        candidates = _ctx.find_stale_candidates(session, keywords)
    if not candidates:
        on_screen(f"[dim]No notes match keywords.[/dim]")
        make_it_so()
        return
    on_screen(f"[yellow]Found {len(candidates)} note(s) likely affected:[/yellow]")
    for n, kw in candidates[:10]:
        on_screen(f"  [{kw}] {n.title or '(untitled)'}")
    if not apply:
        if not typer.confirm(f"Tag these as :stale: + :re:{topic}: ?", default=False):
            on_screen("Skipped. Re-run with --apply to tag automatically.")
            return
    with get_session(engine) as session:
        cands = _ctx.find_stale_candidates(session, keywords)
        n_updated = _ctx.apply_stale_tags(session, cands, topic=topic)
    hail(f"Tagged {n_updated} node(s) as :stale:"
         + (f" + :re:{topic}:" if topic else ""))
    make_it_so()


@context_app.command("build")
def context_build(
    apply:       Annotated[bool, typer.Option("--apply", "-a",
                 help="Append inferred facts to the context file")] = False,
    interactive: Annotated[bool, typer.Option("--interactive", "-i",
                 help="Review each inferred fact before adding")] = False,
    max_facts:   Annotated[int,  typer.Option("--max", "-n",
                 help="Cap on number of inferred facts")] = 5,
):
    """LLM reads your notes + projects, infers durable context facts.

    Use this to bootstrap your context file from real content (run by
    `org-llm setup` automatically, available standalone for re-building
    later — e.g. after a major life change reshapes your vault).

    Default mode (no flags) prints the inferred facts as a preview.
    Pass --apply to register them. Pass --interactive to confirm each
    one individually.
    """
    from . import context as _ctx
    _auto_init_db_if_needed()
    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)
        mdl = (_cfg(session, "chat_model")
               or _cfg(session, "fast_model") or "llama3.2")
        try:
            from .llm import list_models as _lm
            pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                       for m in _lm(url) or []}
            for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                if any(p == cand or p.startswith(cand + ":") for p in pulled):
                    mdl = cand; break
        except Exception:
            pass
        facts = _ctx.infer_initial_facts(session, model=mdl, base_url=url,
                                           max_facts=max_facts)
    if not facts:
        red_alert("LLM didn't surface any confident durable facts.")
        on_screen("[dim]Try after indexing more content, or use:[/dim]")
        on_screen("[bold]org-llm context add 'fact'[/bold]")
        raise typer.Exit(1)
    on_screen("[lcars3]Inferred facts:[/lcars3]")
    for f in facts:
        on_screen(f"  - {f}")
    if not apply and not interactive:
        on_screen(f"\nDry-run only. Apply with: "
                  "[bold]org-llm context build --apply[/bold]")
        return
    added = 0
    for f in facts:
        if interactive and not typer.confirm(f"Add: {f!r} ?", default=True):
            continue
        _ctx.add_fact(f, source="context-build")
        added += 1
    hail(f"Added {added} fact(s).")
    make_it_so()


@context_app.command("tangle")
def context_tangle():
    """Re-tangle the context org file → plain-text targets the LLM reads."""
    from . import context as _ctx
    org = _ctx.context_org_path()
    if not org.exists():
        on_screen(f"[dim]No context file yet at {org}.[/dim]")
        on_screen("Create one with: [bold]org-llm context add '<fact>'[/bold]")
        return
    written = _ctx.tangle()
    if not written:
        on_screen("[dim]Nothing to tangle (no :tangle blocks and no "
                  ":LLM_CONTEXT: sections).[/dim]")
        return
    for tgt, body in written.items():
        hail(f"Tangled → {tgt}  ({len(body)} chars)")
    make_it_so()


@context_app.command("edit")
def context_edit():
    """Open the context file in $EDITOR."""
    from . import context as _ctx
    p = _ctx.ensure_context_file_exists()
    editor = os.environ.get("EDITOR", "")
    if not editor:
        red_alert("$EDITOR not set; printing path instead.")
        on_screen(f"  {p}")
        return
    import subprocess
    subprocess.call([editor, str(p)])
    # Re-tangle on close in case the user edited tangle blocks.
    _ctx.tangle()


@context_app.command("clear")
def context_clear(
    yes: Annotated[bool, typer.Option("--yes", "-y",
          help="Skip confirmation")] = False,
):
    """Wipe the context file (with confirmation)."""
    from . import context as _ctx
    p = _ctx.context_org_path()
    if not p.exists():
        on_screen("[dim]No context file to clear.[/dim]")
        return
    if not yes and not typer.confirm(f"Delete {p}?", default=False):
        return
    p.unlink()
    tangle_p = _ctx.context_tangle_path()
    if tangle_p.exists():
        tangle_p.unlink()
    hail("Context cleared.")


@context_app.command("stale")
def context_stale(
    apply:    Annotated[bool, typer.Option("--apply", "-a",
              help="Auto-apply suggested tags without confirmation")] = False,
    limit:    Annotated[int,  typer.Option("--limit", "-n",
              help="Max nodes to judge per sweep")] = 20,
    since_days: Annotated[int, typer.Option("--since-days", "-d",
                help="Only consider nodes older than N days (0 = all)")] = 0,
):
    """Alias for `org-llm stale` — kept under context for discoverability.

    The staleness sweep belongs conceptually to the context layer (it
    applies USER CONTEXT to the vault by tagging contradicted notes).
    Both commands invoke the same machinery; use whichever feels more
    natural in your shell history.
    """
    # Direct call to the top-level stale() function — same defaults.
    stale(apply=apply, limit=limit, since_days=since_days)


history_app = typer.Typer(
    help="Build and manage the LLM-generated history narrative — a "
         "tangled summary of older/stale notes prepended to every "
         "system prompt as background context.",
    cls=PrefixGroup,
)
app.add_typer(history_app, name="history", rich_help_panel="Context & Vault")


@history_app.command("build")
def history_build(
    sample_limit: Annotated[int, typer.Option("--limit", "-n",
                  help="Max notes to feed the model")] = 60,
    age_days:     Annotated[int, typer.Option("--age-days", "-d",
                  help="Notes older than N days qualify (in addition to :stale: tagged)")] = 180,
    interactive:  Annotated[bool, typer.Option("--interactive", "-i",
                  help="Short interactive interview before generation")] = False,
):
    """Scan stale + old notes; LLM writes a narrative summary to llm-history.org.

    The narrative is tangled to a plain-text file org-llm prepends to
    every system prompt as HISTORICAL CONTEXT — so even after notes get
    tagged :stale: their gist still informs answers.

    Pass --interactive for a 3-question interview (emphasise / skip /
    tone) that shapes the narrative.
    """
    from . import context as _ctx
    _auto_init_db_if_needed()

    user_guidance = ""
    if interactive:
        console.print()
        console.rule("[lcars1]History narrative — interview[/lcars1]")
        on_screen("[dim]Three short questions. Press Enter to skip any.[/dim]")
        emphasise = typer.prompt(
            "Topics to emphasise (e.g. employment, projects, places)",
            default="", show_default=False,
        ).strip()
        skip_topics = typer.prompt(
            "Topics to skip (e.g. work, todos, daily)",
            default="", show_default=False,
        ).strip()
        tone = typer.prompt(
            "Tone — 'factual', 'impressionistic', 'terse'",
            default="factual", show_default=True,
        ).strip()
        bits = []
        if emphasise:
            bits.append(f"  Emphasise topics: {emphasise}")
        if skip_topics:
            bits.append(f"  Skip topics: {skip_topics}")
        if tone and tone != "factual":
            bits.append(f"  Tone: {tone}")
        if bits:
            user_guidance = "\n\nUser guidance:\n" + "\n".join(bits)
            on_screen(f"[dim]Guidance captured. Generating…[/dim]")
        console.print()

    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)
        mdl = (_cfg(session, "fast_model")
               or _cfg(session, "chat_model")
               or "llama3.2")
        try:
            from .llm import list_models as _lm
            pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                       for m in _lm(url) or []}
            for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                if any(p == cand or p.startswith(cand + ":") for p in pulled):
                    mdl = cand; break
        except Exception:
            pass
    with get_session(engine) as session:
        narrative = _ctx.build_history(session, model=mdl, base_url=url,
                                         sample_limit=sample_limit,
                                         age_days=age_days,
                                         user_guidance=user_guidance)
    if not narrative:
        red_alert("Couldn't build history. Either no qualifying notes "
                  "(stale-tagged or >180 days), or the LLM didn't reply.")
        on_screen("[dim]Tag some notes stale first or lower --age-days.[/dim]")
        raise typer.Exit(1)
    p = _ctx.history_org_path()
    hail(f"History written to {p}")
    on_screen(f"  [dim]Tangled to: {_ctx.history_tangle_path()}[/dim]")
    console.print()
    on_screen("[lcars3]Preview:[/lcars3]")
    for line in narrative.splitlines()[:20]:
        console.print(f"  {line}")
    if len(narrative.splitlines()) > 20:
        on_screen(f"  [dim]…and {len(narrative.splitlines()) - 20} more lines[/dim]")
    make_it_so()


@history_app.command("show")
def history_show():
    """Print the current tangled history narrative."""
    from . import context as _ctx
    body = _ctx.read_history_for_prompt(max_chars=20000)
    if not body:
        on_screen("[dim]No history narrative yet.[/dim]")
        on_screen("Build one: [bold]org-llm history build[/bold]")
        return
    console.print()
    console.rule(f"[lcars1]{_ctx.HISTORY_HEADER}[/lcars1]")
    console.print(body)
    console.rule(f"[dim]source: {_ctx.history_org_path()} · "
                 f"tangled: {_ctx.history_tangle_path()}[/dim]")


@history_app.command("tangle")
def history_tangle():
    """Re-tangle llm-history.org → plain-text tangle target."""
    from . import context as _ctx
    p = _ctx.history_org_path()
    if not p.exists():
        on_screen(f"[dim]No history file at {p}.[/dim]")
        on_screen("Build one: [bold]org-llm history build[/bold]")
        return
    written = _ctx.tangle(p, _ctx.history_tangle_path())
    for tgt, body in written.items():
        hail(f"Tangled → {tgt}  ({len(body)} chars)")
    make_it_so()


@app.command(rich_help_panel="Context & Vault")
def stale(
    apply:    Annotated[bool, typer.Option("--apply", "-a",
              help="Auto-apply suggested tags without confirmation")] = False,
    limit:    Annotated[int,  typer.Option("--limit", "-n",
              help="Max nodes to judge per sweep")] = 20,
    since_days: Annotated[int, typer.Option("--since-days", "-d",
                help="Only consider nodes older than N days (0 = all)")] = 0,
):
    """LLM-driven stale-content sweep.

    Samples nodes the LLM hasn't yet flagged, gives them to the model
    along with your active context file, asks for verdicts. Nodes that
    contradict your current truth are tagged :stale: (or :re:<topic>:);
    nodes that merely drift get a softer :drift: tag.

    Run periodically — after a `context add`, after a job change, or
    via `doctor` (it nudges you when there's unswept context).
    """
    from . import context as _ctx
    engine = _engine()
    with get_session(engine) as session:
        url = _ollama_url(session)
        mdl = (_cfg(session, "fast_model")
               or _cfg(session, "chat_model")
               or "llama3.2")
        try:
            from .llm import list_models as _lm
            pulled = {(m.get("name") if isinstance(m, dict) else m.name)
                       for m in _lm(url) or []}
            for cand in ("llama3.2:1b", "llama3.2:3b", "llama3.2"):
                if any(p == cand or p.startswith(cand + ":") for p in pulled):
                    mdl = cand; break
        except Exception:
            pass

    if not _ctx.read_context_for_prompt():
        red_alert("No context registered — staleness is judged against "
                  "current truth.")
        on_screen("Start with: [bold]org-llm context add '<fact>'[/bold]")
        on_screen("Or:         [bold]org-llm context from-prompt '<sentence>'[/bold]")
        raise typer.Exit(1)

    with get_session(engine) as session:
        verdicts = _ctx.llm_stale_sweep(session, model=mdl, base_url=url,
                                          limit=limit, since_days=since_days)
    if not verdicts:
        on_screen("[green]No stale candidates surfaced this sweep.[/green]")
        on_screen("[dim]LLM either found everything fresh, or didn't return "
                  "valid JSON. Try `--limit 30` or rephrase your context.[/dim]")
        return

    on_screen(f"[yellow]LLM proposes {len(verdicts)} action(s):[/yellow]")
    for v in verdicts[:25]:
        on_screen(f"  [{v['verdict']}/{v['tag']}] {v['node_id'][:20]} "
                  f"— {v['reason']}")

    if not apply:
        if not typer.confirm(f"Apply tags to {len(verdicts)} node(s)?",
                              default=False):
            on_screen("Skipped. Re-run with --apply to tag automatically.")
            return

    from .db import Node
    n_updated = 0
    with get_session(engine) as session:
        for v in verdicts:
            db_n = (session.query(Node)
                    .filter_by(node_id=v["node_id"]).first())
            if not db_n:
                continue
            existing = (db_n.tags or "").split()
            tag = v["tag"]
            if tag in existing:
                continue
            db_n.tags = " ".join(existing + [tag])
            n_updated += 1
        session.commit()
    hail(f"Tagged {n_updated} node(s).")
    make_it_so()


# context + stale are added to _LLM_FIXABLE_VERBS / _INTENT_RECOVERABLE_VERBS
# at their definition sites (much later in the file).


def _read_user_knobs() -> list[dict]:
    """Load user-registered knobs from the config DB. Resilient to missing DB."""
    import json as _json
    from .db import Config as _Cfg
    engine = _engine()
    try:
        with get_session(engine) as session:
            row = session.get(_Cfg, "user_theme_knobs")
            if row and row.value:
                data = _json.loads(row.value)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def _write_user_knobs(knobs: list[dict]) -> None:
    import json as _json
    from .db import Config as _Cfg
    engine = _engine()
    with get_session(engine) as session:
        row = session.get(_Cfg, "user_theme_knobs")
        payload = _json.dumps(knobs)
        if row:
            row.value = payload
        else:
            session.add(_Cfg(key="user_theme_knobs", value=payload))
        session.commit()


@knob_app.command("add")
def knob_add(
    name:    Annotated[str,  typer.Argument(help="Knob name (e.g. 'dinosaur'). Lowercased; ASCII letters/digits/_- only.")],
    message: Annotated[list[str], typer.Option("--message", "-m",
             help="Add a done-message. Repeatable. Format: 'text|style' or just 'text' (style defaults to lcars1).")] = None,
    default_level: Annotated[int, typer.Option("--default-level", "-l",
                   help="Level 0..3 used when ORG_LLM_<NAME>_LEVEL is unset.")] = 2,
):
    """Register a new theme knob.

    A knob is a named bundle of completion messages controlled by the
    matching ORG_LLM_<NAME>_LEVEL env var (0=off, 1+=on). After registering,
    the messages roll into `make_it_so`'s pool whenever the level is ≥ 1.

    Example:
      org-llm knob add dinosaur \\
        -m '◀ ROAR.|info' \\
        -m '◀ Dino-mite work, comrade.|lcars1' \\
        -m '◀ Extinction is for capitalism, not progress.|pride.green'

    Then:
      ORG_LLM_DINOSAUR_LEVEL=2 org-llm doctor
    """
    import re
    norm = name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", norm):
        red_alert(f"Invalid knob name {name!r}. Use lowercase letters, digits, _ or -.")
        raise typer.Exit(1)
    if norm in ("trek", "commie", "queer"):
        red_alert(f"{norm!r} is built-in; you can already control it via "
                  f"ORG_LLM_{norm.upper()}_LEVEL.")
        raise typer.Exit(1)

    msgs: list[list[str]] = []
    for m in (message or []):
        if "|" in m:
            text, style = m.split("|", 1)
            msgs.append([text.strip(), style.strip() or "lcars1"])
        else:
            msgs.append([m.strip(), "lcars1"])
    if not msgs:
        on_screen("[yellow]No --message entries given. Add at least one with -m later via `knob edit`,[/yellow]")
        on_screen("[yellow]or pass several -m flags now to seed the knob.[/yellow]")
        if not typer.confirm("Register an empty knob anyway?", default=False):
            raise typer.Exit(1)

    knobs = _read_user_knobs()
    knobs = [k for k in knobs if k.get("name") != norm]
    knobs.append({
        "name": norm,
        "messages": msgs,
        "default_level": int(default_level),
    })
    _write_user_knobs(knobs)
    hail(f"Registered theme knob [bold]{norm}[/bold] with {len(msgs)} message(s).")
    on_screen(f"Activate:  [bold]ORG_LLM_{norm.upper()}_LEVEL=2 org-llm doctor[/bold]")
    on_screen(f"Inspect:   [bold]org-llm knob list[/bold]")
    make_it_so()


@knob_app.command("remove")
def knob_remove(
    name: Annotated[str, typer.Argument(help="Knob name to remove")],
):
    """Remove a user-registered theme knob."""
    norm = name.strip().lower()
    knobs = _read_user_knobs()
    kept = [k for k in knobs if k.get("name") != norm]
    if len(kept) == len(knobs):
        red_alert(f"No user knob named {norm!r}. Built-in knobs (trek/commie/queer) "
                  "are not removable.")
        raise typer.Exit(1)
    _write_user_knobs(kept)
    hail(f"Removed knob: {norm}")
    make_it_so()


@knob_app.command("list")
def knob_list():
    """Show all theme knobs (built-in + user-defined) and their current levels."""
    from rich.table import Table as _T
    knobs = _read_user_knobs()
    builtins = [
        {"name": "trek",   "messages": "(built-in Trek refs)",  "default_level": 2},
        {"name": "commie", "messages": "(built-in solidarity)", "default_level": 2},
        {"name": "queer",  "messages": "(built-in pride)",      "default_level": 2},
    ]
    tbl = _T(box=None, pad_edge=False)
    tbl.add_column("Knob",       style="lcars1", no_wrap=True)
    tbl.add_column("Type",       style="dim",    width=8)
    tbl.add_column("Default",    style="lcars3", justify="right", width=8)
    tbl.add_column("Active level", style="lcars2", justify="right", width=12)
    tbl.add_column("Messages",   style="dim")

    def _active(name: str, default: int) -> str:
        from . import ui as _ui
        env = os.environ.get(f"ORG_LLM_{name.upper()}_LEVEL", "").strip()
        if env.isdigit():
            return f"{env} (env)"
        # Consult the SQLite config row (e.g. queer_level = 1)
        cfg_val = _ui._theme_level_from_config(f"{name}_level")
        if cfg_val is not None:
            return f"{cfg_val} (config)"
        return f"{default} (default)"

    for k in builtins:
        tbl.add_row(k["name"], "built-in", str(k["default_level"]),
                    _active(k["name"], k["default_level"]),
                    k["messages"])
    for k in knobs:
        msgs = k.get("messages", []) or []
        sample = msgs[0][0] if msgs else "(no messages)"
        tbl.add_row(k["name"], "user",
                     str(k.get("default_level", 2)),
                     _active(k["name"], k.get("default_level", 2)),
                     f"{len(msgs)} msg(s) — e.g. {sample[:50]}")
    console.print()
    console.rule("[lcars1]Theme knobs[/lcars1]")
    console.print(tbl)
    console.print()
    on_screen("Add a knob:    [bold]org-llm knob add <name> -m 'msg|style' …[/bold]")
    on_screen("Remove a knob: [bold]org-llm knob remove <name>[/bold]")
    on_screen("Override:      [bold]ORG_LLM_<NAME>_LEVEL=0..3[/bold]")
    on_screen("Auto-create:   [bold]org-llm personalize --apply[/bold]")


@app.command(rich_help_panel="Themes")
def personalize(
    apply:    Annotated[bool, typer.Option("--apply",    "-a",
              help="Write proposed knobs to the config DB")] = False,
    no_llm:   Annotated[bool, typer.Option("--no-llm",   "-L",
              help="Use deterministic templates only — skip LLM message generation")] = False,
    max_themes: Annotated[int, typer.Option("--max",     "-n",
              help="Cap on number of proposed knobs")] = 5,
    overwrite: Annotated[bool, typer.Option("--overwrite",
              help="Replace existing user knobs instead of merging")] = False,
    show:     Annotated[bool, typer.Option("--show",
              help="Show currently-registered user theme knobs and exit")] = False,
    clear:    Annotated[bool, typer.Option("--clear",
              help="Delete all user-registered theme knobs (with confirm)")] = False,
):
    """Auto-create theme knobs from your vault + filesystem content.

    Reads your top non-boring tags, recent note titles, project names
    under your code roots, and detected preferred language. Each surfaces
    as a proposed [bold]knob[/bold] — a named bundle of make_it_so
    completion messages controlled by ORG_LLM_<NAME>_LEVEL.

    Default mode is dry-run (preview only). Pass --apply to actually
    register the knobs in the config DB.

    Examples:
      org-llm personalize                  — preview proposals (dry-run)
      org-llm personalize --apply          — register all proposals
      org-llm personalize -a --no-llm      — apply with template messages only
      org-llm personalize -a --overwrite   — replace existing user knobs
      org-llm personalize --show           — print currently-registered knobs
      org-llm personalize --clear          — delete all user knobs

    The personalisation pipeline is read-only by default and *always*
    deterministic in detection. Only message generation can call out to
    the local LLM (chat_model / fast_model). Use --no-llm to keep the
    whole flow offline.
    """
    # --show: just print existing knobs and bail.
    if show:
        knobs = _read_user_knobs()
        if not knobs:
            on_screen("[dim]No user knobs registered yet.[/dim]")
            on_screen("Generate some: [bold]org-llm personalize --apply[/bold]")
            return
        from rich.table import Table as _T
        tbl = _T(box=None, pad_edge=False)
        tbl.add_column("Knob",     style="lcars1", no_wrap=True)
        tbl.add_column("Source",   style="dim",    width=14)
        tbl.add_column("Level",    style="lcars3", justify="right", width=6)
        tbl.add_column("Sample message", style="lcars2")
        for k in knobs:
            msgs = k.get("messages", [])
            sample = msgs[0][0] if msgs else "(none)"
            tbl.add_row(k.get("name", "?"),
                         k.get("_source", "manual"),
                         str(k.get("default_level", 2)),
                         sample[:60])
        console.print()
        console.rule("[lcars1]Registered theme knobs[/lcars1]")
        console.print(tbl)
        return

    # --clear: wipe with confirm.
    if clear:
        knobs = _read_user_knobs()
        if not knobs:
            on_screen("[dim]No user knobs to clear.[/dim]")
            return
        if not typer.confirm(f"Delete all {len(knobs)} user-registered "
                              f"knob(s)?", default=False):
            return
        _write_user_knobs([])
        hail("Cleared all user-registered knobs.")
        return

    from rich.table import Table as _T
    from . import personalize as _p

    _auto_init_db_if_needed()
    engine = _engine()
    with get_session(engine) as session:
        url       = _ollama_url(session)
        # Prefer the smallest pulled chat-capable model for synthesis: this
        # is bg copy-writing, not a primary chat. llama3.2:1b is fastest;
        # then llama3.2; only then fall back to chat_model / fast_model.
        try:
            from .llm import list_models as _lm
            pulled = {m.get("name","") if isinstance(m, dict)
                       else getattr(m, "name", "") for m in _lm(url) or []}
        except Exception:
            pulled = set()
        preferred_for_synthesis = [
            "llama3.2:1b", "llama3.2:3b", "llama3.2",
            "phi3.5", "phi3.5:mini",
        ]
        chat_mdl = ""
        for cand in preferred_for_synthesis:
            if any(p == cand or p.startswith(cand + ":") for p in pulled):
                chat_mdl = cand
                break
        if not chat_mdl:
            chat_mdl = (_cfg(session, "chat_model")
                         or _cfg(session, "fast_model")
                         or "llama3.2")
        proposals = _p.detect_themes(session,
                                       model=chat_mdl, base_url=url,
                                       use_llm=not no_llm,
                                       max_themes=max_themes)

    if not proposals:
        on_screen("No themes detected yet. Index more notes first:")
        on_screen("  [bold]org-llm index[/bold]   then re-run [bold]org-llm personalize[/bold]")
        return

    use_llm = not no_llm
    with warp(f"Generating messages for {len(proposals)} theme(s)"
              + (f" via {chat_mdl}" if use_llm else " (templates only)")):
        knobs = _p.proposals_to_knobs(proposals, model=chat_mdl,
                                        base_url=url, use_llm=use_llm)

    tbl = _T(box=None, pad_edge=False)
    tbl.add_column("Knob",     style="lcars1", no_wrap=True)
    tbl.add_column("Source",   style="dim",    width=14)
    tbl.add_column("Level",    style="lcars3", justify="right", width=6)
    tbl.add_column("Sample message", style="lcars2")
    for p, k in zip(proposals, knobs):
        msgs = k.get("messages", [])
        sample = msgs[0][0] if msgs else "(none)"
        tbl.add_row(k["name"], p.source, str(k["default_level"]),
                     sample[:60])
    console.print()
    console.rule("[lcars1]Personalised theme proposals[/lcars1]")
    console.print(tbl)
    console.print()

    if not apply:
        on_screen(f"Dry-run only. Apply with: [bold]org-llm personalize --apply[/bold]")
        return

    # Merge / overwrite into user_theme_knobs
    existing = _read_user_knobs() if not overwrite else []
    by_name = {k["name"]: k for k in existing}
    for k in knobs:
        # strip provenance key before persisting
        cleaned = {kk: vv for kk, vv in k.items() if not kk.startswith("_")}
        by_name[k["name"]] = cleaned
    _write_user_knobs(list(by_name.values()))

    hail(f"Registered {len(knobs)} knob(s).")
    on_screen("Inspect:  [bold]org-llm knob list[/bold]")
    on_screen("Adjust:   [bold]ORG_LLM_<NAME>_LEVEL=0..3[/bold]")
    make_it_so()


@app.command(name="grant-root", rich_help_panel="LLM Auth (MCP)")
def grant_root(
    path: Annotated[str, typer.Argument(help="Trusted prefix the LLM may self-grant within")],
):
    """Trust a directory as a self-grant root for the LLM.

    Once trusted, the LLM can call its `request_access` MCP tool with any
    path under this root and it gets auto-granted (added to the regular
    allow-list). Sensitive paths (~/.ssh, ~/.gnupg, ~/.password-store,
    cloud creds, /etc/shadow, etc.) remain denied even with a trusted root.

    Example:
      org-llm grant-root ~/repos    # LLM can self-grant any file under ~/repos
      org-llm grant-root ~/org      # ditto for the vault
    """
    from . import access
    p = Path(path).expanduser().resolve()
    if not p.exists():
        red_alert(f"Path does not exist: {p}")
        raise typer.Exit(1)
    if not p.is_dir():
        red_alert(f"--root must be a directory: {p}")
        raise typer.Exit(1)
    if access.add_auto_root(str(p)):
        hail(f"Trusted as auto-grant root: {p}")
        on_screen("LLM can now self-grant any non-sensitive path under this dir.")
        on_screen("Inspect: [bold]org-llm grants[/bold]")
        make_it_so()
    else:
        red_alert("Failed to write auto-grant roots.")
        raise typer.Exit(1)


@app.command(name="revoke-root", rich_help_panel="LLM Auth (MCP)")
def revoke_root(
    path: Annotated[str, typer.Argument(help="Auto-grant root to remove")],
):
    """Remove an auto-grant root. Existing direct grants under it remain."""
    from . import access
    p = Path(path).expanduser().resolve()
    if access.remove_auto_root(str(p)):
        hail(f"Untrusted: {p}")
        make_it_so()
    else:
        red_alert("Failed to update auto-grant roots.")
        raise typer.Exit(1)


@app.command(name="grant-browser", rich_help_panel="LLM Auth (MCP)")
def grant_browser():
    """Allow the LLM to open URLs and drive qutebrowser via MCP."""
    from . import access
    access.set_browser_enabled(True)
    hail("Browser access enabled.")
    on_screen("LLM tools available:  [bold]open_url[/bold], [bold]browser_command[/bold]")
    if not access._qute_bin():
        on_screen("[yellow]qutebrowser not on PATH — open_url falls back to xdg-open.[/yellow]")
    make_it_so()


@app.command(name="revoke-browser", rich_help_panel="LLM Auth (MCP)")
def revoke_browser():
    """Disallow LLM browser access."""
    from . import access
    access.set_browser_enabled(False)
    hail("Browser access disabled.")
    make_it_so()


@app.command(rich_help_panel="Themes")
def theme(
    mode: Annotated[str, typer.Argument(help="dark | light | toggle | show")] = "show",
):
    """Set the UI color mode. Default is dark; light mode darkens every color
    so it's legible on a white terminal background. Override per-command via
    `ORG_LLM_THEME=light org-llm …` instead.
    """
    from .db import Config as Cfg
    from . import ui as ui_mod
    valid = {"dark", "light", "toggle", "show"}
    if mode not in valid:
        red_alert(f"Unknown mode {mode!r}. Use: {', '.join(sorted(valid))}")
        raise typer.Exit(1)

    engine = _engine()
    with get_session(engine) as session:
        current_row = session.get(Cfg, "theme")
        current = (current_row.value if current_row else "dark").strip().lower()

        if mode == "show":
            env = os.environ.get("ORG_LLM_THEME", "")
            valid_modes = ("dark", "light")
            on_screen(f"Stored theme: [bold]{current}[/bold]"
                      + ("" if current in valid_modes
                         else f"  [yellow](invalid value — falls back to dark)[/yellow]"))
            if env:
                env_norm = env.strip().lower()
                if env_norm in ("dark", "night"):
                    on_screen(f"ORG_LLM_THEME env override: [bold]dark[/bold] (this session)")
                elif env_norm in ("light", "day"):
                    on_screen(f"ORG_LLM_THEME env override: [bold]light[/bold] (this session)")
                else:
                    on_screen(f"ORG_LLM_THEME env: [bold]{env}[/bold] [yellow](unrecognised — ignored)[/yellow]")
            on_screen(f"Active right now: [bold]{ui_mod.THEME_MODE}[/bold]"
                      + (" (next command will pick up changes)" if current != ui_mod.THEME_MODE else ""))
            return

        new = mode if mode != "toggle" else ("light" if current == "dark" else "dark")
        if current_row:
            current_row.value = new
        else:
            session.add(Cfg(key="theme", value=new))
        session.commit()

    hail(f"Theme set to [bold]{new}[/bold]. Restart the command (or unset ORG_LLM_THEME) to see it apply.")
    if os.environ.get("ORG_LLM_THEME"):
        on_screen("Note: ORG_LLM_THEME env var is set and overrides this config for the current shell.")
    make_it_so()


@app.command(rich_help_panel="Maintenance")
def completion(
    shell:   Annotated[str,  typer.Argument(help="Shell: fish | bash | zsh | powershell | pwsh")] = "fish",
    install: Annotated[bool, typer.Option("--install", "-i",
             help="Write the completion to the standard location for that shell")] = False,
):
    """Print or install shell completions. Always works regardless of $SHELL.

    Default locations on --install:
      fish        ~/.config/fish/completions/org-llm.fish
      bash        ~/.local/share/bash-completion/completions/org-llm
      zsh         ~/.zfunc/_org-llm  (add ~/.zfunc to fpath in .zshrc)
      powershell  $PROFILE  (appended)
    """
    from typer.completion import get_completion_script

    valid = {"fish", "bash", "zsh", "powershell", "pwsh"}
    if shell not in valid:
        red_alert(f"Unknown shell {shell!r}. Use one of: {', '.join(sorted(valid))}")
        raise typer.Exit(1)

    script = get_completion_script(
        prog_name="org-llm",
        complete_var="_ORG_LLM_COMPLETE",
        shell=shell,
    )

    if not install:
        print(script)
        return

    targets = {
        "fish":       Path("~/.config/fish/completions/org-llm.fish").expanduser(),
        "bash":       Path("~/.local/share/bash-completion/completions/org-llm").expanduser(),
        "zsh":        Path("~/.zfunc/_org-llm").expanduser(),
        "powershell": Path("~/.config/powershell/Microsoft.PowerShell_profile.ps1").expanduser(),
        "pwsh":       Path("~/.config/powershell/Microsoft.PowerShell_profile.ps1").expanduser(),
    }
    target = targets[shell]
    target.parent.mkdir(parents=True, exist_ok=True)

    if shell in ("powershell", "pwsh"):
        # Append rather than overwrite the profile
        existing = target.read_text() if target.exists() else ""
        if "_ORG_LLM_COMPLETE" not in existing:
            with open(target, "a") as f:
                f.write("\n\n# org-llm completion\n" + script + "\n")
            hail(f"Appended completion to {target}")
        else:
            hail(f"Completion already present in {target}")
    else:
        target.write_text(script)
        hail(f"Wrote {shell} completion → {target}")
        if shell == "zsh":
            on_screen("Add to ~/.zshrc:  fpath+=~/.zfunc; autoload -Uz compinit && compinit")
        elif shell == "fish":
            on_screen("Reload:  source ~/.config/fish/completions/org-llm.fish")
    make_it_so()


# Register skill commands at import time so they appear in --help
from . import cli_skills as _cs
_cs.register(app)


_SINGLE_QUERY_VERBS = {
    "ask", "code", "search", "tutor", "source",
}


# Verbs whose intent the LLM may reconstruct from a mangled argv. Distinct
# from _LLM_FIXABLE_VERBS (which is for SRE-style infrastructure repairs and
# specifically excludes user-facing verbs to avoid burning tokens). This list
# is what the LLM is allowed to ARGUE its way back into when shell quoting
# breaks the original invocation.
_INTENT_RECOVERABLE_VERBS = {
    "ask", "code", "capture", "search", "tutor", "source",
    "models", "doctor", "report", "discover", "personalize",
    "code-index", "review-emacs", "tag", "config", "knob",
    "context", "stale",
}


def _llm_intent_repair(broken_argv: list[str], error: str) -> list[str] | None:
    """Ask the LLM to reconstruct the user's intent from a mangled argv.

    Used as the last-resort recovery for Typer parse errors (typically shell
    quoting issues). Different from _llm_assisted_fix: that one is for SRE
    fixes (config, models, doctor); this one re-creates a user-facing
    invocation in the same verb the user typed.

    Returns the proposed argv (without leading "org-llm") on success, or
    None if no safe recovery was found.
    """
    if not broken_argv:
        return None
    verb = next((a for a in broken_argv if not a.startswith("-")), "")
    if verb not in _INTENT_RECOVERABLE_VERBS:
        return None

    try:
        engine = _engine()
        with get_session(engine) as session:
            cloud_provider = _cfg(session, "cloud_provider")
            cloud_endpoint = _cfg(session, "cloud_endpoint_url")
            cloud_model    = (_cfg(session, "fixer_model")
                              or _cfg(session, "cloud_model")
                              or "openai/gpt-oss-20b:free")
            db_api_key     = _cfg(session, "cloud_api_key") or _cfg(session, "runpod_api_key")
    except Exception:
        return None
    if not cloud_endpoint:
        return None

    from . import creds as _creds
    api_key = (_creds.read_secret(_creds.cloud_slug(cloud_provider))
               if cloud_provider else None) or db_api_key

    sys_prompt = (
        "You repair shell-quoting errors for a CLI tool called `org-llm`.\n"
        "The user typed a command. The shell mangled it (typically because "
        "of nested quotes), so Typer rejected the argv with 'Got unexpected "
        "extra arguments'. Your job is to reconstruct what the user meant.\n\n"
        "Reply with STRICT JSON only. No prose, no markdown:\n"
        '  {"argv": ["ask", "connect Literate Programming Approach to anything else in my vault"], "reason": "shell unquoted the inner double quotes"}\n'
        'OR {"argv": null, "reason": "cannot reconstruct intent"}\n\n'
        f"argv[0] MUST be one of: {', '.join(sorted(_INTENT_RECOVERABLE_VERBS))}.\n"
        "Glue all stray positional tokens back into the single string the\n"
        "user clearly intended. Preserve any --flag and its value as separate\n"
        "argv entries. Do NOT add new flags. Do NOT change the verb."
    )
    user_prompt = (
        f"Verb: {verb}\n"
        f"Broken argv: {broken_argv}\n"
        f"Typer error: {error[:600]}\n\n"
        "Reconstruct the intended argv."
    )

    try:
        from .cloud import cloud_chat
        reply = cloud_chat(user_prompt, model=cloud_model,
                           endpoint_url=cloud_endpoint,
                           api_key=api_key, system=sys_prompt)
    except Exception:
        return None

    cleaned = _strip_code_fences(reply, "json").strip()
    import json as _json
    try:
        plan = _json.loads(cleaned)
    except Exception:
        return None
    proposed = plan.get("argv")
    if not isinstance(proposed, list) or not proposed:
        return None
    if str(proposed[0]) != verb:
        # Don't let the LLM silently change verbs.
        return None
    if any(not isinstance(x, str) for x in proposed):
        return None
    if plan.get("reason"):
        on_screen(f"  [dim]LLM intent: {plan['reason']}[/dim]")
    return proposed


def _shell_quote_repair(argv: list[str]) -> list[str] | None:
    """If argv looks like a single-string command got split by bad shell
    quoting, glue everything from arg index 1 onward back into one string.

    Example: `ask connect "Foo Bar" lately` becomes `ask 'connect Foo Bar lately'`
    when the user originally meant a single quoted query but the shell
    eat/recombined the quotes.

    Only applies to commands in _SINGLE_QUERY_VERBS — those take exactly
    one positional string argument. Returns the repaired argv or None when
    the heuristic doesn't apply.
    """
    if len(argv) < 3:
        return None
    # Strip leading global options (none today, but guard for future flags).
    cmd_idx = 0
    while cmd_idx < len(argv) and argv[cmd_idx].startswith("-"):
        cmd_idx += 1
    if cmd_idx >= len(argv):
        return None
    verb = argv[cmd_idx]
    if verb not in _SINGLE_QUERY_VERBS:
        return None
    # Repaired tail: re-join any non-flag positional tokens. Keep flags as-is.
    head = argv[: cmd_idx + 1]
    tail = argv[cmd_idx + 1:]
    flags: list[str] = []
    words: list[str] = []
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok.startswith("-"):
            flags.append(tok)
            # Pull the next token as flag's value if it doesn't itself look like a flag
            if i + 1 < len(tail) and not tail[i + 1].startswith("-"):
                # Conservative: only treat as a value if it's short (avoid
                # eating real query words). Real --model values are model tags
                # like "qwen2.5:3b" — short and contain digits/colons.
                nxt = tail[i + 1]
                if len(nxt) <= 32 and any(c in nxt for c in ":/_") or nxt.isdigit():
                    flags.append(nxt); i += 1
            i += 1
            continue
        words.append(tok)
        i += 1
    if len(words) <= 1:
        return None
    glued = " ".join(words)
    return head + flags + [glued]


def main():
    """Top-level entry. Three-layer recovery chain for argv parse errors:

      1. Deterministic shell-quote repair — fastest, no LLM call.
      2. LLM intent repair — reconstruct the user's intended argv from
         the mangled one, then EXECUTE it (no confirmation).
      3. SRE-style LLM fix — only if the first two can't recover, falls
         through to allow-listed infra fixes (config, doctor, models).

    Be proactive: at each layer that produces a runnable argv, try to run
    it. The user typed a command; our job is to deliver on that intent,
    not to interrupt them with a quiz about quoting.
    """
    import sys
    from click.exceptions import UsageError as _ClickUsageError

    def _invoke(argv: list[str] | None = None):
        """Invoke the Typer app with standalone_mode=False so UsageErrors
        propagate up here instead of being caught + printed by Click."""
        click_cmd = typer.main.get_command(app)
        return click_cmd.main(args=argv, prog_name="org-llm",
                                standalone_mode=False)

    def _abort_on_ctrl_c():
        """Print a clean one-liner and exit 130 (128 + SIGINT)."""
        try:
            from .ui import on_screen as _on_int
            console.print()
            _on_int("[yellow]Interrupted (Ctrl-C). "
                    "Partial work is preserved — re-run when ready.[/yellow]")
        except Exception:
            print("\nInterrupted (Ctrl-C).", file=sys.stderr)
        sys.exit(130)

    try:
        _invoke()
        return
    except KeyboardInterrupt:
        _abort_on_ctrl_c()
    except _ClickUsageError as exc:
        original_error = str(exc)
        # Be proactive: try recovery for ALL parse errors, not just "extra
        # arguments". Bad subcommand, bad flag, missing required arg —
        # the LLM can guess intent for any of them.

    argv = list(sys.argv[1:])
    from .ui import on_screen as _on

    # ── Layer 0: deterministic fuzzy-match for unknown subcommand ─────────
    if "no such command" in original_error.lower() and argv:
        import difflib as _dl
        first = argv[0]
        try:
            known = sorted(typer.main.get_command(app).commands.keys())
        except Exception:
            known = []
        guess = _dl.get_close_matches(first, known, n=1, cutoff=0.6)
        if guess:
            _on(f"[yellow]Unknown command [bold]{first!r}[/bold] — "
                f"did you mean [bold]{guess[0]}[/bold]? Retrying.[/yellow]")
            argv = [guess[0]] + argv[1:]
            try:
                _invoke(argv)
                return
            except KeyboardInterrupt:
                _abort_on_ctrl_c()
            except _ClickUsageError as exc:
                original_error = str(exc)

    # ── Layer 1: deterministic shell-quote repair ─────────────────────────
    repaired = _shell_quote_repair(argv)
    if repaired and repaired != argv:
        _on(f"[yellow]Auto-repairing shell quoting and retrying:[/yellow]")
        _on(f"  [dim]→[/dim] [bold]org-llm {' '.join(repaired)}[/bold]")
        try:
            _invoke(repaired)
            return
        except KeyboardInterrupt:
            _abort_on_ctrl_c()
        except _ClickUsageError as exc:
            original_error = str(exc)
            argv = repaired

    # ── Layer 2: LLM intent reconstruction → execute proactively ──────────
    intent_argv = _llm_intent_repair(argv, original_error)
    if intent_argv:
        _on(f"[yellow]LLM reconstructed intent — executing:[/yellow]")
        _on(f"  [dim]→[/dim] [bold]org-llm {' '.join(intent_argv)}[/bold]")
        try:
            _invoke(intent_argv)
            return
        except KeyboardInterrupt:
            _abort_on_ctrl_c()
        except _ClickUsageError:
            pass

    # ── Layer 3: SRE-style fix (config / doctor / models repairs) ─────────
    try:
        attempted = "org-llm " + " ".join(argv)
        if _llm_assisted_fix(
            error=f"Typer parse error: {' '.join(argv)!r} — {original_error}",
            attempted_command=attempted,
            context=("Shell quoting likely broke a single-string argument "
                     "into multiple positional tokens, OR the user typed an "
                     "unknown subcommand or flag. Pick the closest safe fix."),
        ):
            try:
                _invoke(argv)
                return
            except KeyboardInterrupt:
                _abort_on_ctrl_c()
            except _ClickUsageError:
                pass
    except KeyboardInterrupt:
        _abort_on_ctrl_c()
    except Exception:
        pass

    # All layers exhausted — print the original Typer error and tip.
    _on(f"[red]Could not auto-recover: {original_error}[/red]")
    _on("[dim]Tip: wrap the query in single quotes — "
        "[bold]org-llm ask 'your full question here'[/bold][/dim]")
    sys.exit(2)
# cli.py:1 ends here
